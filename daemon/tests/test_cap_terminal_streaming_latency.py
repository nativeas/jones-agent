"""FR08's "流式输出延迟 < 200 ms" (Issue #14, PRD 12.3) — measured exactly the
way docs/design/03-w4-interfaces.md §3 names it for branch I: "测 daemon 内从
收到 update 到 broadcast 的时延", i.e. `SessionService`'s own processing cost
turning one already-received ACP `session/update` notification into a
`ctx.server.broadcast()` call — NOT round-trip wall clock against a real
model (that number is dominated by ACP/subprocess scheduling and the model
itself, see docs/design/02-w3-interfaces.md §1.5's own "性能实测" table for
the equivalent measurement on the permission-request path: ~21ms end to end,
almost entirely IPC/process-scheduling noise, not application code).

`_handle_tool_call_start`/`_handle_tool_call_update` (the two `_on_session_
update` branches a terminal command's streamed output flows through:
`tool_call` for the start, `tool_call_update` for each subsequent chunk/
completion) are timed directly — real DB writes via `run_in_db_thread` (a
real thread-pool round trip, not mocked away), a real `_FakeServer.broadcast`
call, nothing stubbed out except the ACP wire itself (which this
measurement deliberately excludes, per the contract wording above).
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from jones_daemon.context import DaemonContext
from jones_daemon.context import ProviderResolver as ProviderResolverProtocol
from jones_daemon.projects.bootstrap import bootstrap_projects_and_agents
from jones_daemon.sessions.service import DEFAULT_AGENT_ID, DEFAULT_PROJECT_ID, SessionService
from jones_daemon.store import apply_pending, connect, run_in_db_thread

_FAKE_AGENT = str(Path(__file__).parent / "fake_acp_agent.py")
_LATENCY_BUDGET_S = 0.2  # PRD 12.3 FR08: "流式输出延迟 < 200 ms"


class _FakeServer:
    def __init__(self) -> None:
        self.broadcasts: list[tuple[str, str, Any]] = []

    async def broadcast(self, session_id: str, method: str, params: Any) -> None:
        self.broadcasts.append((session_id, method, params))


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


async def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.02) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


async def _open_turn(service: SessionService):
    row = await service.create(
        project_id=DEFAULT_PROJECT_ID, agent_id=DEFAULT_AGENT_ID, mode="auto", title="latency"
    )
    session_id = row["id"]
    await service.send(session_id, "SLEEP_MS:2000 hold this turn open")
    await _wait_until(lambda: session_id in service._active_turns)
    return service._active_turns[session_id]


async def test_tool_call_start_update_to_broadcast_latency_is_well_under_200ms(
    tmp_path, monkeypatch
):
    service = await _make_service(tmp_path, monkeypatch)
    try:
        ctx_turn = await _open_turn(service)
        samples: list[float] = []
        for i in range(20):
            t0 = time.monotonic()
            await service._handle_tool_call_start(
                ctx_turn,
                {
                    "toolCallId": f"terminal-{i}", "title": "terminal", "status": "pending",
                    "rawInput": {"command": "echo hi"},
                },
            )
            samples.append(time.monotonic() - t0)
        p50, p_max = statistics.median(samples), max(samples)
        assert p_max < _LATENCY_BUDGET_S, f"p_max={p_max * 1000:.2f}ms >= 200ms budget: {samples}"
        # Recorded for the PR report's "性能实测" section, not asserted beyond
        # the budget above — printed via -s if a reviewer wants to see it.
        print(
            f"\n[latency] tool_call start->broadcast: "
            f"p50={p50 * 1000:.3f}ms max={p_max * 1000:.3f}ms"
        )
    finally:
        await service.shutdown()


async def test_tool_call_update_to_broadcast_latency_is_well_under_200ms(tmp_path, monkeypatch):
    service = await _make_service(tmp_path, monkeypatch)
    try:
        ctx_turn = await _open_turn(service)
        samples: list[float] = []
        for i in range(20):
            tool_call_id = f"terminal-{i}"
            await service._handle_tool_call_start(
                ctx_turn,
                {
                    "toolCallId": tool_call_id, "title": "terminal", "status": "pending",
                    "rawInput": {"command": "echo hi"},
                },
            )
            t0 = time.monotonic()
            await service._handle_tool_call_update(
                ctx_turn,
                {
                    "toolCallId": tool_call_id, "status": "completed",
                    "rawOutput": {"output": f"hi\n (call {i})"},
                },
            )
            samples.append(time.monotonic() - t0)
        p50, p_max = statistics.median(samples), max(samples)
        assert p_max < _LATENCY_BUDGET_S, f"p_max={p_max * 1000:.2f}ms >= 200ms budget: {samples}"
        print(
            f"\n[latency] tool_call_update -> broadcast: "
            f"p50={p50 * 1000:.3f}ms max={p_max * 1000:.3f}ms"
        )
    finally:
        await service.shutdown()
