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

`jones_tools.json` (Issue #38 ruling 1 — the daemon's PRIMARY, fail-closed
self-check criterion, `workers/manager.py::_wait_for_tools_snapshot`): every
mode below WRITES this file into `$HERMES_HOME` the moment this agent handles
its FIRST `session/prompt` request for a session (before dispatching to any
mode-specific handler) — standing in for the real `jones_gate` plugin's own
`on_session_start` hook (`kernel/plugin/jones_gate/_tools_snapshot.py`).
Deliberately NOT written at `session/new` time: source-verified against the
installed hermes-agent checkout, the real hook only fires from `agent/
conversation_loop.py` as part of building the FIRST TURN's system prompt —
i.e. strictly after a `session/prompt` is sent, never before (see `workers/
manager.py::_startup_self_check`'s docstring for the full story and why this
means the daemon's own probe prompt is a REQUIRED trigger, not merely a nice-
to-have second layer). Getting this fake agent's timing right matters: an
earlier version of this file wrote the snapshot at `session/new` instead,
which made every fake-agent test pass while the equivalent real-Hermes E2E
run hung forever — see the PR report.
EXCEPT "no_tools_snapshot", which deliberately never writes it, simulating a
`HERMES_SAFE_MODE`-style "plugin skipped loading entirely" (see that mode's
own entry below).

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
- "no_tools_snapshot": behaves exactly like "normal" (including blocking the
  probe correctly) EXCEPT it never writes `jones_tools.json` — simulates the
  PRIMARY self-check criterion's own file never appearing (e.g. a
  `HERMES_SAFE_MODE`-style skip that, unrealistically for this one mode, still
  left `pre_tool_call` blocking intact) so a test can isolate "primary layer
  alone must fail-closed" from whatever the optional probe layer shows (Issue
  #38 ruling 1/3 — the primary criterion must reject even when the secondary
  probe would otherwise look fine).
- "probe_completes": reports the probe tool call as *completed* instead of
  blocked, AND never writes `jones_tools.json` — simulates `jones_gate` having
  silently failed to load entirely (`HERMES_SAFE_MODE`, see
  00-foundation.md §7/§8.1): neither its `pre_tool_call` block nor its
  `on_session_start` snapshot write would have run. Issue #38 ruling 3: the
  PRIMARY (tools-snapshot) layer must still refuse this worker — the
  *optional* second layer's "completed" verdict is exercised elsewhere
  (`no_tools_snapshot` above supplies a snapshot so that path can be tested
  as diagnostic-only, non-blocking).
- "probe_fails_unverified": reports the probe tool call as *failed*, same as
  "normal", but WITHOUT jones_gate's own block-message marker in `rawOutput`,
  AND never writes `jones_tools.json` — simulates the plugin never having
  loaded at all (so Hermes fails the call as an unknown tool, a
  `status: failed` event that looks identical to a real block unless the
  daemon checks *why* it failed, round-1 review fix; see
  `workers/manager.py::_probe_event_verdict`'s docstring). Same as
  "probe_completes": the PRIMARY layer must refuse this worker regardless of
  what the optional probe shows.
- "no_probe_call": never emits any tool_call event at all for the probe, but
  DOES write `jones_tools.json` (jones_gate loaded fine; the model just never
  called the probe tool) — Issue #38 ruling 2's central scenario: the
  self-check must still deliver this worker, only logging that the optional
  second layer got no cooperation.
- "gate_not_enforcing" (round-1 review findings #2/#5): writes `jones_tools.
  json` normally (jones_gate genuinely loaded, unlike "probe_completes"
  above) but STILL reports the probe tool call as *completed* instead of
  blocked — simulates the plugin having loaded but its `pre_tool_call` block
  hook not actually enforcing. Positive, vendor-agnostic evidence
  `_probe_second_layer` must still fail-closed reject on, even though the
  primary tools-snapshot layer already passed.
- "dies_after_snapshot" (round-1 review findings #3/#7): writes `jones_tools.
  json` on the first prompt (the probe prompt) exactly like "normal", then
  immediately hard-exits (`os._exit(1)`) without responding to it at all or
  emitting any tool_call event — simulates a worker process dying during the
  self-check window, after having already proven the plugin loaded.
- "slow_probe_response" (round-1 review findings #1/#6): writes `jones_tools.
  json` immediately, then holds the probe prompt's response until either a
  `session/cancel` notification arrives for this session or a long
  safety-net timeout elapses (whichever first), never emitting any probe
  tool_call event — simulates a real model that's simply slow to finish its
  Turn, exercising `_probe_second_layer`'s own bounded budget and the
  `session/cancel` it must send once that budget expires.
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
- "TOOL_EXCEPTION" prompt marker (Issue #22, G08's "工具抛异常（ACP tool_call
  返回 error）" fault injection — pure addition, same pattern as "USE_TOOL"/
  "NEEDS_PERMISSION" above, no existing behavior changed): emits a
  `demo_tool` `tool_call`/`tool_call_update(status="failed")` pair, then
  responds to the `session/prompt` call itself with a JSON-RPC **error**
  object instead of a normal `{"stopReason": ...}` result — the shape real
  Hermes uses when a tool's exception is unrecoverable enough to end the
  Turn (`kernel/acp_client.py`'s `AcpError`), as opposed to "USE_TOOL"'s
  always-succeeds `demo_tool`, which the agent just continues past.
- `_new_session_calls.json` (pure addition, no existing behavior changed):
  every mode writes the running count of `session/new` requests handled so
  far to this file under `$HERMES_HOME` (same best-effort/atomic-replace
  spirit as `jones_tools.json`) — lets a fake-agent-level test assert
  `WorkerManager.ensure_started()` actually requests a SECOND, fresh session
  after the startup self-check finishes (Issue #40 investigation finding:
  see `workers/manager.py::_spawn_and_check`'s comment on why the production
  session must never be the same one the self-check's probe ran on).
- "SPAWN_ORPHAN_ON_SHUTDOWN" prompt marker (pure addition, "normal" mode
  only, same pattern as "USE_TOOL"/`SPAWN_REAL_SUBPROCESS_TERMINAL`) — Issue
  #40 investigation finding, G09's OTHER half: starts a real `sleep 30` with
  a plain `subprocess.Popen` (deliberately NOT its own process group, and
  deliberately never cleaned up by THIS agent on `session/cancel` or
  anything else — simulates a worker that leaves a background child running
  and is then torn down from the OUTSIDE, e.g. `WorkerManager.stop_worker`/
  idle-reap/daemon shutdown, not a cooperative in-Turn cancel), writes the
  child's pid to `$HERMES_HOME/_orphan_child.pid`, then finishes the prompt
  normally. Proves `WorkerManager._terminate`'s process-GROUP kill (this
  agent process itself has no chance to reap its own child before dying) —
  `SPAWN_REAL_SUBPROCESS_TERMINAL` above instead proves the AGENT's own
  cancel-driven cleanup, a different half of G09.
- "SPAWN_ORPHAN_INDEPENDENT_SESSION" prompt marker (pure addition, "normal"
  mode only, same pattern as "USE_TOOL"/`SPAWN_ORPHAN_ON_SHUTDOWN`) — Issue
  #41's exact repro shape, which `SPAWN_ORPHAN_ON_SHUTDOWN` above does NOT
  cover: a real child OS process started with its OWN new session/pgid
  (real Hermes's `tools/environments/local.py` does exactly this for every
  shell command, precisely so a cancelled command's cleanup can target it
  independently — source-verified, see `workers/manager.py::_signal_worker_
  group`'s docstring) — so `killpg` on the WORKER's own pgid (W9's fix,
  `_terminate`/`_reap_process_group` in that file) structurally cannot reach
  it, only a real ppid-tree walk can (`WorkerManager.snapshot_worker_
  descendants`, Issue #41's fallback). Stays a completely ordinary, directly
  attached child of THIS agent process throughout (`ppid` never changes) —
  unlike `SPAWN_ORPHAN_ON_SHUTDOWN`, this agent process itself is NOT torn
  down here, standing in for the exact production shape `SessionService.
  stop()` faces: the WORKER is still alive and answers `session/cancel`
  completely normally (same as "normal" mode always does), but this one
  specific child is deliberately never cleaned up by this agent on cancel or
  anything else — simulating a Hermes whose own cancel-driven cleanup thread
  missed it (Issue #41's own ~17% real-model repro rate), which is precisely
  the gap `WorkerManager.reap_stop_orphans` exists to cover. Writes the
  child's pid to `$HERMES_HOME/_orphan_independent_session.pid`, then
  finishes the prompt normally (`stopReason: "end_turn"`).
- "SPAWN_ORPHAN_IGNORING_SIGTERM" prompt marker (Issue #41 round-2 review
  findings #3/#6, pure addition, "normal" mode only, same pattern as
  "SPAWN_ORPHAN_INDEPENDENT_SESSION" above) — identical shape EXCEPT the
  child ignores `SIGTERM` (only `SIGKILL` ends it), to force a test all the
  way through `WorkerManager.reap_stop_orphans`'s TERM-grace-then-KILL
  escalation instead of a plain `sleep` dying the instant `SIGTERM` arrives.
  Writes the child's pid to `$HERMES_HOME/_orphan_ignoring_sigterm.pid`.
- "MANY_TOOL_CALLS:<n>" prompt marker (R-N5, controller ruling 2026-09-20,
  04-w5-interfaces.md §4.3 — Issue #22's Step-count budget test: "假 ACP
  agent 发 201 个 tool_call") — pure addition, same pattern as "USE_TOOL"/
  "TOOL_EXCEPTION": emits `n` `tool_call`/`tool_call_update(status=
  "completed")` pairs back to back for a fake `demo_tool`, then finishes the
  prompt normally (`stopReason: "end_turn"`, same as any other "normal"-mode
  prompt) — unlike "USE_TOOL" (always exactly one pair), `n` lets a test
  drive the daemon's `sessions/service.py::_handle_tool_call_start` Step
  budget past its limit with a single real ACP round trip. The daemon is
  expected to `task.cancel()` the Turn partway through once its own
  Step-count check trips (see that method's docstring) — this agent has no
  idea that happens and just keeps writing `session/update` lines to stdout
  until it's done sending all `n` (a closed pipe on the daemon side, if the
  Turn's worker gets torn down first, surfaces as a normal `BrokenPipeError`
  from `_send`, not a hang).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

PROBE_TOOL_NAME = "jones.__probe__"
MODE = os.environ.get("FAKE_ACP_MODE", "normal")
# Kept in sync by hand with jones_daemon.workers.manager._GATE_BLOCK_MARKER —
# see that module's comment for why it's duplicated rather than imported (this
# file must stay dependency-free of jones_daemon).
_GATE_BLOCK_MARKER = "jones_gate startup self-check: this tool is reserved and never runs."

# Kept in sync by hand with jones_daemon.workers.manager._TOOLS_SNAPSHOT_FILE_NAME
# / kernel/plugin/jones_gate/_tools_snapshot.py's `_FILE_NAME` — see module
# docstring's "jones_tools.json" section.
_TOOLS_SNAPSHOT_FILE_NAME = "jones_tools.json"
_TOOLS_SNAPSHOT_SKIP_MODES = {"probe_completes", "probe_fails_unverified", "no_tools_snapshot"}
# Sessions this agent has already written a snapshot for — real Hermes only
# fires `on_session_start` once, on the first Turn (see module docstring);
# writing again on every later prompt would be harmless (atomic replace) but
# wouldn't match reality, so this mirrors "once per session" instead.
_snapshot_written: set[str] = set()


def _maybe_write_tools_snapshot(session_id: str) -> None:
    if MODE in _TOOLS_SNAPSHOT_SKIP_MODES or session_id in _snapshot_written:
        return
    _snapshot_written.add(session_id)
    _write_tools_snapshot(session_id)


def _write_tools_snapshot(session_id: str) -> None:
    """Stands in for `jones_gate`'s real `on_session_start` hook — see module
    docstring's "jones_tools.json" section. Best-effort, same spirit as the
    real hook: `HERMES_HOME` should always be set (the daemon always sets it,
    `workers/manager.py::_worker_env`) but this must never crash the fake
    agent if it somehow isn't."""
    home = os.environ.get("HERMES_HOME")
    if not home:
        return
    payload = {
        "session_id": session_id,
        "tools": ["hermes-acp"],
        "mcp_servers": [],
        "mcp_discovery_complete": True,
        "written_at": time.time(),
    }
    target = Path(home) / _TOOLS_SNAPSHOT_FILE_NAME
    tmp = Path(home) / f"{_TOOLS_SNAPSHOT_FILE_NAME}.tmp"
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(target)

_NEW_SESSION_CALLS_FILE_NAME = "_new_session_calls.json"
_new_session_call_count = [0]  # boxed for the closure below, same pattern as `_next_id`


def _record_new_session_call() -> int:
    """See module docstring's `_new_session_calls.json` entry. Best-effort, same
    spirit as `_write_tools_snapshot` — must never crash this agent. Returns the
    running count so the caller can mint a session id that actually varies call
    to call (round-2 review finding #3: a constant `"fake-session-1"` for every
    call made `test_ensure_started_gets_a_fresh_session_for_production_use`
    unable to tell "got a new session" apart from "got handed the self-check's
    OWN session back" — the call-count assertion still passed with the
    production-session hand-off line deleted entirely)."""
    _new_session_call_count[0] += 1
    count = _new_session_call_count[0]
    home = os.environ.get("HERMES_HOME")
    if not home:
        return count
    target = Path(home) / _NEW_SESSION_CALLS_FILE_NAME
    tmp = Path(home) / f"{_NEW_SESSION_CALLS_FILE_NAME}.tmp"
    tmp.write_text(json.dumps({"count": count}), encoding="utf-8")
    tmp.replace(target)
    return count


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


_SLOW_PROBE_HOLD_S = 10.0  # safety net only — cut short by session/cancel in practice


def _handle_slow_probe_prompt(session_id: str) -> None:
    """`"slow_probe_response"` mode — see module docstring. Blocks this prompt's
    own thread (same pattern as `_handle_spawn_subprocess_terminal`) until
    `session/cancel` arrives for THIS session, or `_SLOW_PROBE_HOLD_S` elapses
    as a safety net so a daemon bug that never sends cancel can't hang this
    fake agent forever. Never emits a probe tool_call event either way —
    indistinguishable, from the daemon's side, from "no probe call" once cut
    off."""
    ev = threading.Event()
    _cancel_events[session_id] = ev
    ev.wait(_SLOW_PROBE_HOLD_S)
    _cancel_events.pop(session_id, None)


def _handle_probe_prompt(session_id: str) -> None:
    tool_call_id = "probe-1"
    if MODE == "no_probe_call":
        return
    _send_update(
        session_id,
        {"sessionUpdate": "tool_call", "toolCallId": tool_call_id, "title": PROBE_TOOL_NAME,
         "status": "pending", "rawInput": {}},
    )
    final_status = "completed" if MODE in ("probe_completes", "gate_not_enforcing") else "failed"
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


_SPAWN_ORPHAN_MARKER = "SPAWN_ORPHAN_ON_SHUTDOWN"
_ORPHAN_CHILD_PID_FILE_NAME = "_orphan_child.pid"


def _handle_spawn_orphan_on_shutdown() -> None:
    """See module docstring's `SPAWN_ORPHAN_ON_SHUTDOWN` entry. Deliberately a
    plain `Popen` (no `start_new_session`) so the child inherits THIS agent's
    own process group — whatever `WorkerManager` spawned this agent process
    into — and deliberately never reaped by this agent itself."""
    proc = subprocess.Popen(["sleep", "30"])
    home = os.environ.get("HERMES_HOME")
    if home:
        Path(home, _ORPHAN_CHILD_PID_FILE_NAME).write_text(str(proc.pid), encoding="utf-8")


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


_SPAWN_ORPHAN_INDEPENDENT_SESSION_MARKER = "SPAWN_ORPHAN_INDEPENDENT_SESSION"
_ORPHAN_INDEPENDENT_SESSION_PID_FILE_NAME = "_orphan_independent_session.pid"


def _handle_spawn_orphan_independent_session() -> None:
    """See module docstring's `SPAWN_ORPHAN_INDEPENDENT_SESSION` entry. A
    plain, direct child of THIS agent process (`ppid` stays this agent's pid
    for its whole life — nothing here ever detaches it), just started with
    `start_new_session=True` so it's the leader of its OWN new session/pgid,
    not this agent's — the one property that actually defeats `WorkerManager
    ._terminate`'s `killpg`-based reap (W9) and is what Issue #41's fallback
    (a real ppid-tree walk, not a pgid signal) exists to still catch."""
    proc = subprocess.Popen(
        ["sleep", "300"], start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    home = os.environ.get("HERMES_HOME")
    if home:
        Path(home, _ORPHAN_INDEPENDENT_SESSION_PID_FILE_NAME).write_text(
            str(proc.pid), encoding="utf-8"
        )
    # Hold this prompt "in flight" a moment after the orphan exists —
    # without this, `_respond` a few lines below (this handler returns right
    # back into `_handle_prompt_request`) can finish the Turn before a test
    # ever gets a chance to observe the pid file and call `stop()` while the
    # Turn is still active (`SessionService.stop()` is a no-op once its own
    # `_turn_tasks` entry is already `done()`). Short — this only needs to
    # outlast one daemon-side stop() call, not simulate a real, long shell
    # command finishing.
    time.sleep(1.0)


_SPAWN_ORPHAN_IGNORING_SIGTERM_MARKER = "SPAWN_ORPHAN_IGNORING_SIGTERM"
_ORPHAN_IGNORING_SIGTERM_PID_FILE_NAME = "_orphan_ignoring_sigterm.pid"


def _handle_spawn_orphan_ignoring_sigterm() -> None:
    """Issue #41 round-2 review (findings #3/#6): same shape as `_handle_
    spawn_orphan_independent_session` above (a real child in its OWN new
    session/pgid, never cleaned up by this agent on cancel or anything
    else) EXCEPT this child installs its own `SIGTERM` handler that does
    nothing — only `SIGKILL` can end it. A plain `sleep` (the other
    handler's child) dies the instant `SIGTERM` arrives, which never
    exercises `WorkerManager.reap_stop_orphans`'s TERM-grace-then-KILL
    escalation at all; this one forces a test through that full path, to
    prove `SessionService.shutdown()`'s own wait is bounded to cover it
    (round-2 review findings #3/#6 — a flat 5.0s bound was shorter than that
    escalation's own worst case)."""
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)"],
        start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    home = os.environ.get("HERMES_HOME")
    if home:
        Path(home, _ORPHAN_IGNORING_SIGTERM_PID_FILE_NAME).write_text(
            str(proc.pid), encoding="utf-8"
        )
    # Same reasoning as `_handle_spawn_orphan_independent_session` above:
    # hold this prompt "in flight" long enough for a test to observe the pid
    # file and call stop() while the Turn is still active.
    time.sleep(1.0)


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
    if _SPAWN_ORPHAN_MARKER in text:
        _handle_spawn_orphan_on_shutdown()
    if _SPAWN_ORPHAN_INDEPENDENT_SESSION_MARKER in text:
        _handle_spawn_orphan_independent_session()
    if _SPAWN_ORPHAN_IGNORING_SIGTERM_MARKER in text:
        _handle_spawn_orphan_ignoring_sigterm()
    many_match = _MANY_TOOL_CALLS_MARKER.search(text)
    if many_match:
        _handle_many_tool_calls_prompt(session_id, int(many_match.group(1)))


_MANY_TOOL_CALLS_MARKER = re.compile(r"MANY_TOOL_CALLS:(\d+)")


def _handle_many_tool_calls_prompt(session_id: str, count: int) -> None:
    """R-N5 (controller ruling, 2026-09-20) — see this module's docstring."""
    for i in range(count):
        tool_call_id = f"many-{i}"
        _send_update(
            session_id,
            {"sessionUpdate": "tool_call", "toolCallId": tool_call_id, "title": "demo_tool",
             "status": "pending", "rawInput": {"i": i}},
        )
        _send_update(
            session_id,
            {"sessionUpdate": "tool_call_update", "toolCallId": tool_call_id, "title": "demo_tool",
             "status": "completed", "rawOutput": {"ok": True}},
        )


_TOOL_EXCEPTION_MARKER = "TOOL_EXCEPTION"


def _handle_tool_exception_prompt(session_id: str, req_id) -> None:
    tool_call_id = "boom-1"
    _send_update(
        session_id,
        {"sessionUpdate": "tool_call", "toolCallId": tool_call_id, "title": "demo_tool",
         "status": "pending", "rawInput": {"arg": 1}},
    )
    _send_update(
        session_id,
        {"sessionUpdate": "tool_call_update", "toolCallId": tool_call_id, "title": "demo_tool",
         "status": "failed", "rawOutput": {"error": "boom: tool raised an exception"}},
    )
    # A JSON-RPC error response (not a normal `{"stopReason": ...}` result) —
    # `kernel/acp_client.py`'s `prompt()` turns this into `AcpError`, ending the
    # Turn the same way a real unrecoverable tool exception would.
    _respond(req_id, error={"code": -32000, "message": "tool execution failed: boom"})


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
    # Real Hermes writes `jones_tools.json` as a side effect of processing the
    # FIRST Turn's prompt (see module docstring's "jones_tools.json" section)
    # — done before dispatching to any mode-specific handler below, same
    # relative ordering (early in Turn processing, before anything else this
    # agent does for the prompt).
    _maybe_write_tools_snapshot(session_id)
    if MODE == "dies_after_snapshot":
        # round-1 review findings #3/#7 — see module docstring: dies right
        # after proving the plugin loaded, before responding to anything,
        # unconditionally (this is always the FIRST prompt, i.e. the probe).
        os._exit(1)
    prompt_blocks = params.get("prompt") or []
    text = "".join(b.get("text", "") for b in prompt_blocks if isinstance(b, dict))
    if PROBE_TOOL_NAME in text:
        if MODE == "slow_probe_response":
            _handle_slow_probe_prompt(session_id)
        else:
            _handle_probe_prompt(session_id)
    elif MODE == "crash_on_prompt":
        os._exit(7)
    elif _TOOL_EXCEPTION_MARKER in text:
        _handle_tool_exception_prompt(session_id, req_id)
        return
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
                call_count = _record_new_session_call()
                # A per-call-incrementing id (not a constant) so a test can
                # actually distinguish "the self-check's own session" from
                # "the fresh session `ensure_started()` hands to a caller" —
                # see `_record_new_session_call`'s docstring.
                result = {"sessionId": f"fake-session-{call_count}"}
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
