"""Stdlib-only fake ACP agent (docs/design/01-w2-interfaces.md §2: "用一个假的 ACP
agent ... 覆盖 client、WorkerManager 生命周期、队列串行、stop、崩溃恢复").

Run as a subprocess (`python fake_acp_agent.py`), speaking the same
newline-delimited JSON-RPC 2.0 over stdio that a real Hermes `acp_adapter`
worker would (`kernel/acp_client.py`'s docstring lists the exact method
names this replays: `initialize`, `session/new`, `session/prompt`,
`session/cancel` incoming; `session/update` notifications and
`session/request_permission` requests outgoing).

No dependency on `jones_daemon` or `hermes-agent` — this script must run with
nothing but the stdlib so it stays a fast, deterministic double, not a copy of
the real thing. `PROBE_TOOL_NAME` is duplicated by hand from
`workers/manager.py` (same pattern `kernel/plugin/jones_gate/__init__.py`
already uses, see that file's module docstring) rather than imported.

Behavior is selected via the `FAKE_ACP_MODE` env var (default "normal"):

- "normal": blocks the startup-probe tool call (as a correctly-loaded
  jones_gate would), and for any other prompt streams two `agent_message_chunk`
  deltas; a prompt containing the marker "USE_TOOL" also emits a
  `tool_call`/`tool_call_update` pair for a fake `demo_tool`; a prompt
  containing "NEEDS_PERMISSION" issues a real `session/request_permission`
  request and waits for the daemon's answer before finishing; a prompt
  containing `CUSTOM_PERMISSION_JSON:<json>` does the same with an
  arbitrary caller-supplied `toolCall`/`options` shape (Issue #11 — see
  `_handle_custom_permission_prompt`'s docstring). Finishes with
  `stopReason: "cancelled"` if a `session/cancel` notification arrived for
  that session while the prompt was in flight, else `"end_turn"`.
- "probe_completes": reports the probe tool call as *completed* instead of
  blocked — simulates `jones_gate` having silently failed to load
  (`HERMES_SAFE_MODE`, see 00-foundation.md §7/§8.1) so the self-check must
  refuse to deliver this worker.
- "probe_fails_unverified": reports the probe tool call as *failed*, same as
  "normal", but WITHOUT jones_gate's own block-message marker in `rawOutput` —
  simulates the plugin never having loaded at all (so Hermes fails the call as
  an unknown tool, a `status: failed` event that looks identical to a real
  block unless the daemon checks *why* it failed, round-1 review fix; see
  `workers/manager.py::_probe_event_verdict`'s docstring). The self-check must
  refuse to deliver this worker too — "failed" alone is not proof jones_gate
  did the blocking.
- "no_probe_call": never emits any tool_call event at all for the probe —
  simulates the self-check prompt itself going unanswered/ignored.
- "hang_init": never responds to `initialize` — exercises the daemon's
  handshake timeout.
- "crash_on_prompt": behaves like "normal" through the startup self-check
  (so the worker is accepted), then hard-exits without responding to the
  first *non-probe* `session/prompt` — simulates a worker crashing mid-Turn.
- "exit_immediately": exits before reading anything at all — simulates a
  worker whose stdout closes out from under an in-flight request the instant
  it's sent, for `AcpClient`'s own "worker stdout closed" honesty check.
- "call_unsupported_method": right after answering `session/new`, itself
  issues an `fs/read_text_file` request back to the daemon (one of the
  capabilities `AcpClient.initialize()` declares unsupported) and writes the
  daemon's answer to stderr as one JSON line — exercises the daemon's honest
  "not supported" reply instead of a hang/crash (00-foundation.md §8.3).
- "session_new_non_default_mode": pure addition, changes no existing
  behavior (same pattern as "normal"'s `CUSTOM_PERMISSION_JSON` marker) —
  `session/new`'s response carries `"modes": {"currentModeId":
  "accept_edits"}`, simulating a worker whose ACP session mode isn't
  `"default"` — exercises `AcpClient.new_session()`'s startup self-check
  (Issue #11 round-1 review finding #6, docs/design/02-w3-interfaces.md
  §1.2).
- "SPAWN_REAL_SUBPROCESS_TERMINAL" marker (pure addition, "normal" mode
  only, same pattern as "USE_TOOL"/`CUSTOM_PERMISSION_JSON`): controller
  ruling R-I2 (round 3, 2026-09-19, Issue #14/G09) — this fake agent starts
  a REAL OS child process (`sleep 30`) for a `terminal` tool_call, exactly
  like real Hermes's own `tools/environments/base.py` would for an actual
  shell command, then BLOCKS until a `session/cancel` notification arrives
  for this session — simulating the daemon-side half of G09
  (`test_cap_terminal_stop_cancel.py`) actually reaching a worker that owns
  a live subprocess. On cancel, it kills the child (SIGTERM then, if still
  alive after a grace period, SIGKILL — the same two-stage shape real
  Hermes's `_kill_process_group_posix` uses) and reports the pid it killed
  in the `tool_call_update`'s `rawOutput` (`{"killed_pid": ..., "reaped":
  true}`) — the daemon-side test asserts that pid no longer exists
  (`os.kill(pid, 0)` -> `ProcessLookupError`), which is what a fake agent
  with no real subprocess (this file's other modes) could never prove.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time

PROBE_TOOL_NAME = "jones.__probe__"
MODE = os.environ.get("FAKE_ACP_MODE", "normal")
# Kept in sync by hand with jones_daemon.workers.manager._GATE_BLOCK_MARKER —
# see that module's comment for why it's duplicated rather than imported (this
# file must stay dependency-free of jones_daemon).
_GATE_BLOCK_MARKER = "jones_gate startup self-check: this tool is reserved and never runs."

_stdout_lock = threading.Lock()
_cancelled_sessions: set[str] = set()
# Controller ruling R-I2 (round 3): one `threading.Event` per session
# currently blocked inside `_handle_spawn_subprocess_terminal`, waiting for
# THIS session's `session/cancel` — set from the dispatch loop's reader
# thread (below), waited on from the per-prompt handler thread.
_cancel_events: dict[str, threading.Event] = {}
_next_id = [1000]  # outgoing (agent-initiated) request ids, boxed for the closure below


def _send(obj: dict) -> None:
    line = json.dumps(obj) + "\n"
    with _stdout_lock:
        sys.stdout.write(line)
        sys.stdout.flush()


def _notify(method: str, params: dict) -> None:
    _send({"jsonrpc": "2.0", "method": method, "params": params})


def _respond(req_id, result=None, error=None) -> None:
    msg = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result if result is not None else {}
    _send(msg)


def _send_update(session_id: str, update: dict) -> None:
    _notify("session/update", {"sessionId": session_id, "update": update})


def _handle_probe_prompt(session_id: str) -> None:
    tool_call_id = "probe-1"
    if MODE == "no_probe_call":
        return
    _send_update(
        session_id,
        {"sessionUpdate": "tool_call", "toolCallId": tool_call_id, "title": PROBE_TOOL_NAME,
         "status": "pending", "rawInput": {}},
    )
    final_status = "completed" if MODE == "probe_completes" else "failed"
    if final_status == "failed":
        raw_output = (
            {"error": "unknown tool: jones.__probe__"}
            if MODE == "probe_fails_unverified"
            else {"error": _GATE_BLOCK_MARKER}
        )
    else:
        raw_output = {"blocked": False}
    _send_update(
        session_id,
        # Real ACP `tool_call_update` events carry the tool's identifying fields
        # again (they're a full snapshot, not a diff against the initial
        # `tool_call` event) — `title` here is what lets a listener resolve
        # "this update is about the probe tool" from the update alone.
        {"sessionUpdate": "tool_call_update", "toolCallId": tool_call_id, "title": PROBE_TOOL_NAME,
         "status": final_status, "rawOutput": raw_output},
    )


_SLEEP_MARKER = re.compile(r"SLEEP_MS:(\d+)")

# Issue #11 (W3 permission gates): lets a test drive an ARBITRARY
# `session/request_permission` `toolCall.rawInput` shape through a real
# SessionService round trip, instead of the fixed `{"toolCallId": "perm-1",
# "title": "risky_tool"}` (no rawInput at all) "NEEDS_PERMISSION" sends —
# `sessions/service.py::_on_request_permission`'s review-gate classification
# needs a real tool name/args to test its auto-allow (low risk + auto mode)
# vs. user-gate branches, and its two different `rawInput` shapes (the
# generic `jones_gate` escalation encoding vs. the `edit_approval.py` shape —
# see that function's docstring). Usage: embed
# `CUSTOM_PERMISSION_JSON:<json>` in the prompt text, where `<json>` is an
# object with optional `toolCall` (default: `{"toolCallId": "custom-1",
# "title": "custom_tool"}`), `options` (default: the same four allow/deny
# options "NEEDS_PERMISSION" offers), and `wait_timeout` (seconds, default
# 30 — long enough that this fake agent is never what actually causes a
# timeout the daemon-side `approval_timeout_minutes` test is measuring).
_CUSTOM_PERMISSION_MARKER = "CUSTOM_PERMISSION_JSON:"

_DEFAULT_PERMISSION_OPTIONS = [
    {"optionId": "opt-allow-once", "kind": "allow_once"},
    {"optionId": "opt-allow-always", "kind": "allow_always"},
    {"optionId": "opt-reject-once", "kind": "reject_once"},
    {"optionId": "opt-reject-always", "kind": "reject_always"},
]


def _handle_custom_permission_prompt(session_id: str, text: str) -> None:
    payload_text = text.split(_CUSTOM_PERMISSION_MARKER, 1)[1]
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        payload = {}
    tool_call = payload.get("toolCall") or {"toolCallId": "custom-1", "title": "custom_tool"}
    tool_call_id = tool_call.get("toolCallId", "custom-1")
    options = payload.get("options") or _DEFAULT_PERMISSION_OPTIONS
    _next_id[0] += 1
    req_id = _next_id[0]
    _send(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "session/request_permission",
            "params": {"sessionId": session_id, "toolCall": tool_call, "options": options},
        }
    )
    answer = _wait_for_response(req_id, timeout=float(payload.get("wait_timeout", 30.0)))
    _send_update(
        session_id,
        {"sessionUpdate": "tool_call_update", "toolCallId": tool_call_id,
         "status": "completed" if (answer or {}).get("outcome", {}).get("outcome") == "selected"
         else "failed", "rawOutput": answer},
    )


_SPAWN_SUBPROCESS_MARKER = "SPAWN_REAL_SUBPROCESS_TERMINAL"


def _handle_spawn_subprocess_terminal(session_id: str) -> None:
    """Controller ruling R-I2 (round 3, 2026-09-19, Issue #14/G09): spawn a
    REAL OS child (`sleep 30`), report it as an in-flight `terminal`
    tool_call, then block until `session/cancel` arrives for THIS session —
    on cancel, kill the child and report the pid killed. See this module's
    docstring for the full contract; `test_cap_terminal_stop_cancel.py` is
    the only caller."""
    tool_call_id = "term-subproc-1"
    proc = subprocess.Popen(["sleep", "30"])
    _send_update(
        session_id,
        {"sessionUpdate": "tool_call", "toolCallId": tool_call_id, "title": "terminal",
         "status": "pending", "rawInput": {"command": "sleep 30"}},
    )
    ev = threading.Event()
    _cancel_events[session_id] = ev
    got_cancel = ev.wait(10.0)
    _cancel_events.pop(session_id, None)
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    _send_update(
        session_id,
        {"sessionUpdate": "tool_call_update", "toolCallId": tool_call_id, "title": "terminal",
         "status": "failed",
         "rawOutput": {"killed_pid": proc.pid, "reaped": True, "cancelled": got_cancel}},
    )


def _handle_normal_prompt(session_id: str, text: str) -> None:
    _send_update(
        session_id,
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Hel"}},
    )
    _send_update(
        session_id,
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "lo"}},
    )
    sleep_match = _SLEEP_MARKER.search(text)
    if sleep_match:
        # Lets a test hold a Turn "running" long enough to exercise queueing,
        # stop()/cancel, and parallel-session behavior deterministically instead
        # of racing real wall-clock timing against an instant fake response.
        time.sleep(int(sleep_match.group(1)) / 1000)
    if "USE_TOOL" in text:
        tool_call_id = "demo-1"
        _send_update(
            session_id,
            {"sessionUpdate": "tool_call", "toolCallId": tool_call_id, "title": "demo_tool",
             "status": "pending", "rawInput": {"arg": 1}},
        )
        _send_update(
            session_id,
            {"sessionUpdate": "tool_call_update", "toolCallId": tool_call_id, "title": "demo_tool",
             "status": "completed", "rawOutput": {"ok": True}},
        )
    if "NEEDS_PERMISSION" in text:
        _next_id[0] += 1
        req_id = _next_id[0]
        _send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "session/request_permission",
                "params": {
                    "sessionId": session_id,
                    "toolCall": {"toolCallId": "perm-1", "title": "risky_tool"},
                    "options": [
                        {"optionId": "opt-allow-once", "kind": "allow_once"},
                        {"optionId": "opt-allow-always", "kind": "allow_always"},
                        {"optionId": "opt-reject-once", "kind": "reject_once"},
                        {"optionId": "opt-reject-always", "kind": "reject_always"},
                    ],
                },
            }
        )
        # Block this handler thread (each prompt is handled on its own thread,
        # see `_dispatch_loop` below) until the daemon answers on stdin — a real
        # agent genuinely blocks a worker thread waiting for human approval here.
        answer = _wait_for_response(req_id)
        _send_update(
            session_id,
            {"sessionUpdate": "tool_call_update", "toolCallId": "perm-1",
             "status": "completed" if (answer or {}).get("outcome", {}).get("outcome") == "selected"
             else "failed", "rawOutput": answer},
        )
    if _CUSTOM_PERMISSION_MARKER in text:
        _handle_custom_permission_prompt(session_id, text)
    if _SPAWN_SUBPROCESS_MARKER in text:
        _handle_spawn_subprocess_terminal(session_id)


_pending_responses: dict[int, dict] = {}
_response_events: dict[int, threading.Event] = {}
_response_lock = threading.Lock()


def _wait_for_response(req_id: int, timeout: float = 10.0) -> dict | None:
    ev = threading.Event()
    with _response_lock:
        _response_events[req_id] = ev
    if not ev.wait(timeout):
        return None
    with _response_lock:
        return _pending_responses.pop(req_id, None)


def _handle_prompt_request(req_id, params: dict) -> None:
    session_id = params.get("sessionId")
    prompt_blocks = params.get("prompt") or []
    text = "".join(b.get("text", "") for b in prompt_blocks if isinstance(b, dict))
    if PROBE_TOOL_NAME in text:
        _handle_probe_prompt(session_id)
    elif MODE == "crash_on_prompt":
        os._exit(7)
    else:
        _handle_normal_prompt(session_id, text)
    stop_reason = "cancelled" if session_id in _cancelled_sessions else "end_turn"
    _cancelled_sessions.discard(session_id)
    _respond(req_id, {"stopReason": stop_reason})


def _call_unsupported_method() -> None:
    _next_id[0] += 1
    req_id = _next_id[0]
    _send(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "fs/read_text_file",
            "params": {"path": "/tmp/x"},
        }
    )
    result_or_error = _wait_for_response(req_id, timeout=5.0)
    sys.stderr.write(json.dumps({"fs_read_text_file_response": result_or_error}) + "\n")
    sys.stderr.flush()


def _dispatch_loop() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        if "method" in msg:
            method = msg["method"]
            params = msg.get("params") or {}
            req_id = msg.get("id")
            if method == "initialize":
                if MODE == "hang_init":
                    continue  # never respond — exercises the daemon's handshake timeout
                _respond(req_id, {"protocolVersion": 1, "agentCapabilities": {}})
            elif method == "session/new":
                result = {"sessionId": "fake-session-1"}
                if MODE == "session_new_non_default_mode":
                    result["modes"] = {"currentModeId": "accept_edits"}
                _respond(req_id, result)
                if MODE == "call_unsupported_method":
                    threading.Thread(target=_call_unsupported_method, daemon=True).start()
            elif method == "session/prompt":
                # Each prompt runs on its own thread so a "NEEDS_PERMISSION" prompt
                # can block waiting for the daemon's answer while this loop keeps
                # reading stdin (in particular, keeps reading the response to the
                # request_permission call it just sent, and any session/cancel
                # notification that arrives while the prompt is in flight).
                threading.Thread(
                    target=_handle_prompt_request, args=(req_id, params), daemon=True
                ).start()
            elif method == "session/cancel":
                session_id = params.get("sessionId")
                if session_id:
                    _cancelled_sessions.add(session_id)
                    ev = _cancel_events.get(session_id)
                    if ev is not None:
                        ev.set()
            # Unknown incoming methods are ignored — this double only needs to
            # answer what kernel/acp_client.py actually sends.
            continue

        # A response to one of our own outgoing requests (session/request_permission).
        resp_id = msg.get("id")
        with _response_lock:
            _pending_responses[resp_id] = msg.get("result") if "result" in msg else msg.get("error")
            ev = _response_events.get(resp_id)
        if ev is not None:
            ev.set()


if __name__ == "__main__":
    if MODE == "exit_immediately":
        os._exit(3)
    _dispatch_loop()
