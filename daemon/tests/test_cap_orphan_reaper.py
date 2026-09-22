"""Issue #41 — `SessionService.stop()`'s own G09 fallback.

Issue #40's own follow-up investigation found real Hermes's cancel-driven
subprocess cleanup can (rarely, ~17% in that issue's real-model repro) leave
a genuine orphan behind: a descendant with its OWN new session/pgid (so
`WorkerManager._terminate`'s `killpg` fix, W9, can never reach it) already
reparented to init (`ppid=1`) by the time anyone looks. Controller ruling
R1/R3 (#41): Jones must guarantee G09 "停止后无孤儿进程" itself, from the
user-visible `SessionService.stop()` path, not depend on Hermes's own
cleanup ever finishing. `WorkerManager.reap_stop_orphans`/`snapshot_worker_
descendants` (`workers/manager.py`) are that fallback; this file is its
"can actually fail" proof (R4):

- `test_stop_reaps_an_orphaned_independent_session_child`: `fake_acp_agent.
  py`'s `SPAWN_ORPHAN_INDEPENDENT_SESSION` marker deterministically
  reproduces Issue #41's exact repro shape (a child in its OWN new
  session/pgid — not the worker's, so W9's `killpg`-based fix structurally
  can't reach it — while the worker process itself stays alive and answers
  `session/cancel` completely normally, standing in for a Hermes whose own
  cleanup thread missed this one). Asserts `stop()` leaves it dead.
- `test_stop_does_not_touch_a_process_outside_the_worker_tree`: the #41
  review's explicit "绝不能误伤" requirement — a real OS process that is NOT
  a descendant of the worker must survive a `stop()` untouched.

Both revert-tested (per R4): with `workers/manager.py`'s `snapshot_worker_
descendants`/`reap_stop_orphans` and `sessions/service.py::stop()`'s call
site reverted to this branch's pre-#41 code, the first test fails red (the
orphan is still alive 5s later) — see this PR's report for the actual
captured output — while the second continues to pass either way (it isn't
asserting anything the fix changes, only that the fix doesn't overreach).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from jones_daemon.context import DaemonContext
from jones_daemon.context import ProviderResolver as ProviderResolverProtocol
from jones_daemon.projects.bootstrap import bootstrap_projects_and_agents
from jones_daemon.sessions.service import DEFAULT_AGENT_ID, DEFAULT_PROJECT_ID, SessionService
from jones_daemon.store import apply_pending, connect, run_in_db_thread
from jones_daemon.workers import manager as manager_module

_FAKE_AGENT = str(Path(__file__).parent / "fake_acp_agent.py")
_ORPHAN_PID_FILE_NAME = "_orphan_independent_session.pid"


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
    # Speed only — same shape `test_workers_manager.py` already uses to
    # shrink `_IDLE_SCAN_INTERVAL_S` for its own idle-reaper tests. The
    # production default (3s) is chosen against real Hermes's own cleanup
    # latency (see that constant's docstring), irrelevant to what THIS test
    # is proving (that the fallback fires and reaps correctly at all).
    monkeypatch.setattr(manager_module, "_ORPHAN_CANCEL_GRACE_S", 0.2)

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
        project_id=DEFAULT_PROJECT_ID, agent_id=DEFAULT_AGENT_ID, mode=mode, title="issue-41"
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


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


async def test_stop_reaps_an_orphaned_independent_session_child(tmp_path, monkeypatch):
    service = await _make_service(tmp_path, monkeypatch)
    orphan_pid: int | None = None
    try:
        session_id = await _new_session(service, mode="task")

        async def _get_worker():
            while service.worker_manager.get(session_id) is None:
                await asyncio.sleep(0.02)
            return service.worker_manager.get(session_id)

        await service.send(session_id, "SPAWN_ORPHAN_INDEPENDENT_SESSION")
        worker = await _get_worker()
        pid_file = worker.hermes_home / _ORPHAN_PID_FILE_NAME

        await _wait_until(lambda: pid_file.exists(), timeout=5.0)
        orphan_pid = int(pid_file.read_text().strip())

        # Confirm it's actually alive and genuinely reproduces Issue #41's
        # shape (its own session/pgid, not the worker's) — a false "no
        # orphan" pass over a process that was never really started, or
        # shares the worker's pgid (already covered by W9's `killpg` fix,
        # not what this test is proving), would prove nothing.
        assert _alive(orphan_pid), "orphan child never started"
        worker_pgid = os.getpgid(worker.process.pid)
        assert os.getpgid(orphan_pid) != worker_pgid, (
            f"test setup bug: orphan pid {orphan_pid} shares the worker's own pgid "
            f"({worker_pgid}) — W9's existing killpg fix would already reap this, not "
            "exercising Issue #41's fallback at all"
        )

        # The Turn is still "in flight" here (fake_acp_agent.py's own
        # `_ORPHAN_INDEPENDENT_SESSION` handler holds the prompt open for a
        # beat after spawning it — see its docstring) — `stop()` must be
        # called while `_turn_tasks[session_id]` isn't done yet, or it's a
        # no-op (`{"stopped": False}`) that proves nothing about the
        # fallback.
        result = await service.stop(session_id)
        assert result["stopped"] is True

        # `stop()`'s own reap is backgrounded (see its docstring) —
        # `_ORPHAN_CANCEL_GRACE_S` (monkeypatched to 0.2s above) plus a
        # short SIGTERM grace is all it should take; generous margin below.
        deadline = time.monotonic() + 5.0
        gone = False
        while time.monotonic() < deadline:
            if not _alive(orphan_pid):
                gone = True
                break
            await asyncio.sleep(0.05)
        assert gone, (
            f"orphan pid {orphan_pid} (ppid=1, independent session) is still alive 5s "
            "after stop() — Issue #41's fallback did not reap it"
        )
    finally:
        if orphan_pid is not None and _alive(orphan_pid):
            # Best-effort: don't leak a `sleep 300` past a failed assertion.
            import contextlib

            with contextlib.suppress(ProcessLookupError):
                os.kill(orphan_pid, 9)
        await service.shutdown()


async def test_stop_does_not_touch_a_process_outside_the_worker_tree(tmp_path, monkeypatch):
    """#41 review's hard "绝不能误伤" requirement: a real process that is NOT
    a descendant of the worker (here: a sibling the TEST itself spawns,
    completely outside the worker's process tree) must still be alive after
    a `stop()` that has real reaping work to do elsewhere in that same call.
    """
    service = await _make_service(tmp_path, monkeypatch)
    outside = subprocess.Popen(["sleep", "30"])
    try:
        session_id = await _new_session(service, mode="task")
        # An ordinary, no-orphan-marker Turn, held open long enough for
        # `stop()` to reach a live worker — same `SLEEP_MS` pattern this
        # branch's other stop()-timing tests already use.
        await service.send(session_id, "SLEEP_MS:1500 plain turn, nothing spawned")
        await _wait_until(lambda: service.worker_manager.get(session_id) is not None)

        result = await service.stop(session_id)
        assert result["stopped"] is True

        # Give the fallback's own grace/kill windows time to run (there's
        # nothing in THIS session's snapshot for it to act on, but the
        # assertion is about the outside process, not this session's own
        # cleanup timing) before checking the outside process survived.
        await asyncio.sleep(1.0)
        assert outside.poll() is None, (
            "a process outside the worker's descendant tree was killed by stop()'s "
            "orphan fallback — Issue #41's 'must never误伤' requirement violated"
        )
    finally:
        outside.terminate()
        try:
            outside.wait(timeout=5)
        except subprocess.TimeoutExpired:
            outside.kill()
            outside.wait(timeout=5)
        await service.shutdown()
