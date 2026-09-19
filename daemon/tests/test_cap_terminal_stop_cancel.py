"""FR08's "中途中断后子进程被回收，无孤儿进程" / G09 (Issue #14).

Two layers, matching this branch's actual scope:

- This file: the DAEMON's own half of G09 — `SessionService.stop()` reaches
  a terminal-class call sitting on an in-flight `session/request_permission`
  (the state a real `terminal` call is in while nobody has approved it yet)
  and unblocks it via `_resolve_pending_permissions`, AND sends the ACP
  `session/cancel` notification to the worker — `AcpClient.cancel()` is
  exactly what real Hermes's own subprocess-reaping poll loop watches
  (`tools/environments/base.py::_wait_for_process`'s `is_interrupted()`
  check, source-verified — see the PR report's "契约变更"/evidence section
  for the full call chain: `_begin_tool_execution`'s `tool.started` ->
  `_wait_for_process`'s adaptive 5ms-200ms poll -> `_kill_process_group_
  posix` (SIGTERM then SIGKILL, real process-group kill, not just the
  immediate child) once cancellation is observed). `fake_acp_agent.py`
  doesn't spawn a real OS subprocess for a `terminal` call, so it cannot
  prove the KILL half of G09 by itself — that's what the JONES_E2E test in
  `tests/integration/test_real_hermes_e2e_files_terminal.py` is for.
- `tests/integration/test_real_hermes_e2e_files_terminal.py`
  (`JONES_E2E`-gated): a REAL Hermes subprocess actually running a real
  terminal command, stopped mid-flight, with the child PID verified gone
  afterward — the actual G09 proof, not reachable in this sandbox (no
  `ANTHROPIC_API_KEY`; see this PR's report for why one wasn't sourced from
  `~/.hermes/.env`).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from jones_daemon.context import DaemonContext
from jones_daemon.context import ProviderResolver as ProviderResolverProtocol
from jones_daemon.kernel.plugin.jones_gate import _review_payload
from jones_daemon.projects.bootstrap import bootstrap_projects_and_agents
from jones_daemon.sessions.service import DEFAULT_AGENT_ID, DEFAULT_PROJECT_ID, SessionService
from jones_daemon.store import apply_pending, connect, run_in_db_thread

_FAKE_AGENT = str(Path(__file__).parent / "fake_acp_agent.py")


class _FakeServer:
    def __init__(self) -> None:
        self.broadcasts: list[tuple[str, str, Any]] = []

    async def broadcast(self, session_id: str, method: str, params: Any) -> None:
        self.broadcasts.append((session_id, method, params))

    def events(self, method: str) -> list[tuple[str, Any]]:
        return [(sid, p) for sid, m, p in self.broadcasts if m == method]


class _StubProviderResolver(ProviderResolverProtocol):
    def resolve(self, model_pref: dict[str, Any] | None) -> Any:
        return {"provider": "anthropic", "model": "claude-test", "env": {}, "hermes_config": {}}

    def list_models(self, provider: str | None) -> list[dict[str, Any]]:
        return []


class _NullConfigResolver:
    def settings(self, project_id: str | None) -> dict[str, Any]:
        return {}

    def permissions(self, project_id: str | None) -> dict[str, Any]:
        return {}

    def mcp_servers(self, project_id: str | None) -> list[dict[str, Any]]:
        return []


async def _make_service(tmp_path, monkeypatch) -> SessionService:
    monkeypatch.setenv("JONES_HOME", str(tmp_path))
    monkeypatch.setenv("FAKE_ACP_MODE", "normal")

    def _open() -> Any:
        conn = connect(tmp_path / "jones.db")
        apply_pending(conn)
        bootstrap_projects_and_agents(conn)
        return conn

    conn = await run_in_db_thread(_open)
    from jones_daemon import paths

    ctx = DaemonContext(
        db=conn, paths=paths, server=_FakeServer(),
        providers=_StubProviderResolver(), config=_NullConfigResolver(),
    )
    service = SessionService(ctx, worker_cmd=[sys.executable, _FAKE_AGENT])
    await service.worker_manager.start()
    return service


async def _new_session(service: SessionService, *, mode: str = "task") -> str:
    row = await service.create(
        project_id=DEFAULT_PROJECT_ID, agent_id=DEFAULT_AGENT_ID, mode=mode, title="g09"
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


def _terminal_permission_prompt(command: str, *, mode: str) -> str:
    encoded = _review_payload.encode("terminal", {"command": command}, mode=mode)
    payload = {
        "toolCall": {
            "toolCallId": "term-1", "title": "terminal",
            "rawInput": {"command": "<terminal> (plugin approval rule)", "description": encoded},
        }
    }
    return f"CUSTOM_PERMISSION_JSON:{json.dumps(payload)}"


async def test_stop_sends_acp_cancel_while_a_terminal_call_is_pending_approval(
    tmp_path, monkeypatch
):
    service = await _make_service(tmp_path, monkeypatch)
    try:
        session_id = await _new_session(service, mode="task")
        # `sleep 30` (never in a permissions.json allow rule, task mode) ->
        # `_decide_terminal_like_permission` always falls through to the
        # user gate for this — a real, unresolved `permission_decisions
        # (pending)` row exists exactly like a real in-flight `terminal`
        # call sitting on approval.
        prompt = _terminal_permission_prompt("sleep 30", mode="task")
        await service.send(session_id, prompt)
        await _wait_until(lambda: service.ctx.server.events("permission.requested"))

        worker = service.worker_manager.get(session_id)
        assert worker is not None and worker.client is not None
        cancel_calls: list[str] = []
        real_cancel = worker.client.cancel

        async def _spy_cancel(acp_session_id: str) -> None:
            cancel_calls.append(acp_session_id)
            await real_cancel(acp_session_id)

        monkeypatch.setattr(worker.client, "cancel", _spy_cancel)

        result = await service.stop(session_id)
        assert result["stopped"] is True
        # G09's daemon-side half: the ACP cancel notification reached the
        # worker for THIS session (real Hermes's own subprocess-reaping
        # poll loop watches this — see module docstring).
        assert cancel_calls == [worker.acp_session_id]
        # The other daemon-side half: stop() while nobody has answered the
        # pending approval must not leave the Run "looks alive, is actually
        # dead" (N07) — `_resolve_pending_permissions` answers it as denied
        # so the worker (real Hermes) is unblocked rather than left waiting
        # on an approval that will never come.
        await _wait_until(lambda: service.ctx.server.events("run.terminated"))
        terminated = service.ctx.server.events("run.terminated")[0][1]
        assert terminated["kind"] == "user"
    finally:
        await service.shutdown()


async def test_cancel_reaps_a_real_subprocess_spawned_by_the_worker_and_reports_the_pid(
    tmp_path, monkeypatch
):
    """Controller ruling R-I2 (round 3, 2026-09-19): splits G09's remaining
    open item into its two independently provable halves. The test above
    proves the daemon actually SENDS `session/cancel` when it should — this
    one proves that once an ACP agent that owns a real OS child process
    receives that cancel, the child is actually gone afterward — the "reap"
    half real Hermes's own subprocess-killing code (`tools/environments/
    base.py::_kill_process_group_posix`, SIGTERM then SIGKILL) is
    responsible for, which this module's other test can't exercise because
    `fake_acp_agent.py`'s ordinary modes never spawn a real subprocess at
    all. `fake_acp_agent.py`'s `SPAWN_REAL_SUBPROCESS_TERMINAL` marker
    starts a real `sleep 30`, reports it as an in-flight `terminal`
    tool_call, blocks until `session/cancel` arrives, kills the child, and
    reports the pid it killed in the `tool_call_update`'s `rawOutput` —
    still not a real Hermes process (that remains `JONES_E2E`-gated, see
    module docstring), but a real OS-level kill/reap this sandbox CAN prove
    without an `ANTHROPIC_API_KEY`."""
    service = await _make_service(tmp_path, monkeypatch)
    try:
        session_id = await _new_session(service, mode="task")
        await service.send(session_id, "SPAWN_REAL_SUBPROCESS_TERMINAL")
        await _wait_until(lambda: service.ctx.server.events("step.started"))

        result = await service.stop(session_id)
        assert result["stopped"] is True

        await _wait_until(lambda: service.ctx.server.events("step.completed"))
        completed = service.ctx.server.events("step.completed")[-1][1]
        summary = json.loads(completed["result_summary"])
        # `cancelled` is False only if the fake agent's own 10s wait for
        # `session/cancel` timed out — would mean the daemon's cancel never
        # reached it, a real bug this assertion is here to catch, not a
        # value this test should treat as an acceptable alternative.
        assert summary["cancelled"] is True
        pid = summary["killed_pid"]

        # The pid `fake_acp_agent.py` reports killing must actually be gone
        # — the G09 "no orphan process" proof this module's other test
        # can't give on its own (it never owns a real subprocess to check).
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        await service.shutdown()
