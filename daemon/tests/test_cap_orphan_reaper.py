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

Round-2 review (第 1 轮修复记录, see this PR's report) added four more, one
per surviving finding:

- `test_stop_does_not_touch_a_descendant_that_predates_this_turn` (findings
  #1/#4, critical): the review's own real-world repro — a worker's OTHER
  long-lived descendants (a stdio MCP server, a persistent `code_kernel`, a
  `terminal(background=true)` process) are structurally indistinguishable
  from a genuine leaked orphan, so an earlier version of this fallback
  reaped BOTH. Turn 1 spawns an independent-session child and finishes
  normally (never stopped); Turn 2 (an ordinary, unrelated Turn on the SAME
  worker) is stopped mid-flight. Asserts Turn 1's child is still alive well
  past both grace periods.
- `test_reap_stop_orphans_continues_past_a_permission_error_on_one_entry`
  (finding #5): a `PermissionError` signalling ONE entry in a batch must
  not abort the REST of that same batch — a real, ordinary orphan in the
  same batch as a (simulated) permission-denied one must still be reaped.
- `test_shutdown_waits_long_enough_for_reap_stop_orphans_own_worst_case`
  (findings #3/#6): with the two grace periods tuned so their sum exceeds
  the OLD flat 5.0s bound `SessionService.shutdown()` used to wait on its
  background tasks with, asserts the orphan (which ignores `SIGTERM`, so
  only the full TERM-grace-then-KILL escalation can end it) is genuinely
  dead by the time `shutdown()` returns — not merely that `shutdown()`
  didn't raise.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psutil

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


def _actually_running(pid: int) -> bool:
    """Unlike `_alive()`/bare `kill(pid, 0)`, tells a genuinely still-
    running process apart from one that was already signalled and is now a
    zombie awaiting reap: `kill(pid, 0)` succeeds for BOTH (the pid still
    exists in the process table either way), so `_alive()` alone can't tell
    "never touched" from "killed but nothing has called `waitpid` on it
    yet" — a real distinction here, since the fake-agent WORKER process
    that's the orphan's actual OS parent never explicitly waits on it, and
    whether it a coincidentally reaps one via CPython's own `Popen.__del__`
    (`_internal_poll()`, fired when its local `proc` variable happens to go
    out of scope) depends on exactly when that happens relative to when
    the process actually died — not something a test should rely on. Used
    where a test needs to assert a process was NOT touched (round-2 review
    findings #1/#4): a zombie unambiguously means something DID signal it."""
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
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


def _bare_manager(tmp_path) -> manager_module.WorkerManager:
    """A `WorkerManager` with no worker ever started — enough for tests that
    exercise `reap_stop_orphans` directly against hand-built snapshot
    entries / real standalone subprocesses, without needing a full
    `SessionService`/fake-agent worker."""

    async def _noop_update(_session_id: str, _params: Any) -> None:
        return None

    async def _noop_permission(_session_id: str, _params: Any) -> dict[str, Any]:
        return {"outcome": {"outcome": "cancelled"}}

    async def _noop_crash(_session_id: str, _exit_code: int | None) -> None:
        return None

    return manager_module.WorkerManager(
        user_root=tmp_path,
        on_session_update=_noop_update,
        on_request_permission=_noop_permission,
        on_worker_crash=_noop_crash,
        worker_cmd=[sys.executable, _FAKE_AGENT],
    )


async def test_stop_does_not_touch_a_descendant_that_predates_this_turn(tmp_path, monkeypatch):
    """Round-2 review findings #1/#4 (critical) — the review's own real-world
    repro: a worker's OTHER long-lived descendants (a stdio MCP server, a
    persistent `code_kernel`, a `terminal(background=true)` process) are
    meant to survive across Turns on the SAME worker, and are structurally
    indistinguishable from a genuine leaked orphan by process-tree shape
    alone (same `ppid`/pgid/independent-session pattern) — an earlier
    version of this fallback reaped BOTH.

    Turn 1 spawns an independent-session child (`SPAWN_ORPHAN_INDEPENDENT_
    SESSION`) and is allowed to finish NORMALLY (never stopped) — standing
    in for a persistent descendant that legitimately came into being at
    some point in this worker's life. Turn 2, an entirely ordinary,
    unrelated Turn on the SAME (reused) worker, is stopped mid-flight.
    Asserts Turn 1's child is still alive well past both of `reap_stop_
    orphans`'s grace periods — `stop()` must never touch a descendant that
    predates the Turn it's stopping."""
    service = await _make_service(tmp_path, monkeypatch)
    baseline_pid: int | None = None
    try:
        session_id = await _new_session(service, mode="task")

        async def _get_worker():
            while service.worker_manager.get(session_id) is None:
                await asyncio.sleep(0.02)
            return service.worker_manager.get(session_id)

        # Turn 1: spawn the independent-session child, let the Turn finish
        # normally (no stop() here) — this is exactly what makes it end up
        # in Turn 2's baseline instead of getting caught by Turn 2's own
        # `stop()`.
        await service.send(session_id, "SPAWN_ORPHAN_INDEPENDENT_SESSION")
        worker = await _get_worker()
        pid_file = worker.hermes_home / _ORPHAN_PID_FILE_NAME
        await _wait_until(lambda: pid_file.exists(), timeout=5.0)
        baseline_pid = int(pid_file.read_text().strip())
        assert _alive(baseline_pid), "baseline (turn-1) child never started"
        await _wait_until(
            lambda: session_id not in service._turn_tasks
            or service._turn_tasks[session_id].done(),
            timeout=5.0,
        )
        assert _alive(baseline_pid), "baseline child died on its own — test setup bug"

        # Turn 2: an ordinary Turn on the SAME worker (reused — `ensure_
        # started()` returns the existing one), stopped while in flight.
        await service.send(session_id, "SLEEP_MS:1500 second, unrelated turn")
        await _wait_until(
            lambda: session_id in service._turn_tasks
            and not service._turn_tasks[session_id].done(),
            timeout=5.0,
        )
        result = await service.stop(session_id)
        assert result["stopped"] is True

        # Past both grace periods (cancel grace monkeypatched to 0.2s by
        # `_make_service`; term grace stays the real 5.0s default) — the
        # baseline child must still be alive the whole time, not just at
        # the deadline (a fallback that killed it immediately and this
        # check simply running late would look identical to "never
        # touched" if only checked once at the very end). `_actually_running`,
        # not `_alive()`: the baseline child's real OS parent is the fake-
        # agent WORKER process, which never explicitly `wait()`s on it — a
        # killed-but-unreaped child is a ZOMBIE, and bare `kill(pid, 0)`
        # can't tell that apart from "never touched" (both answer it the
        # same way) — see `_actually_running`'s own docstring.
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            assert _actually_running(baseline_pid), (
                f"baseline (turn-1) descendant pid {baseline_pid} was killed by turn-2's "
                "stop() — Issue #41 round-2 review findings #1/#4"
            )
            await asyncio.sleep(0.2)
    finally:
        if baseline_pid is not None and _alive(baseline_pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(baseline_pid, 9)
        await service.shutdown()


async def test_reap_stop_orphans_continues_past_a_permission_error_on_one_entry(
    tmp_path, monkeypatch
):
    """Round-2 review finding #5 — `_signal_pid`'s old bare `except
    ProcessLookupError` let an uncaught `PermissionError` (real on macOS for
    a `sudo`/setuid descendant, or a pid-reuse race — see this issue's own
    round-2 report) escape mid-loop and abort every REMAINING entry in the
    same `reap_stop_orphans` batch, so the surviving real orphans in that
    batch were never reaped at all.

    An unprivileged test process can't reliably manufacture a REAL
    `PermissionError` against its own child, so this patches `os.kill`
    itself (as seen through `manager_module.os` — the SAME module object,
    `import os` doesn't create a private copy) to raise one for ONE
    specific pid while delegating to the real `os.kill` for everything
    else — real `_signal_pid`/`reap_stop_orphans` code runs unmodified, so
    this actually exercises whether THEY catch it, not a re-implementation
    of that contract. (Reverted to this branch's pre-round-2 `manager.py`,
    this test fails red: the exception escapes `reap_stop_orphans` entirely
    and the "real" orphan is never even attempted — see this PR's report.)"""
    manager = _bare_manager(tmp_path)
    monkeypatch.setattr(manager_module, "_ORPHAN_CANCEL_GRACE_S", 0.05)
    monkeypatch.setattr(manager_module, "_ORPHAN_TERM_GRACE_S", 0.5)

    denied = subprocess.Popen(["sleep", "300"])
    real_orphan = subprocess.Popen(["sleep", "300"])
    try:
        denied_entry = manager_module.OrphanSnapshotEntry(
            pid=denied.pid,
            create_time=psutil.Process(denied.pid).create_time(),
            cmdline="sleep 300 (denied)",
        )
        real_entry = manager_module.OrphanSnapshotEntry(
            pid=real_orphan.pid,
            create_time=psutil.Process(real_orphan.pid).create_time(),
            cmdline="sleep 300 (real)",
        )

        real_os_kill = manager_module.os.kill

        def _fake_kill(pid: int, sig: int) -> None:
            if pid == denied.pid:
                raise PermissionError(1, "Operation not permitted")
            real_os_kill(pid, sig)

        monkeypatch.setattr(manager_module.os, "kill", _fake_kill)

        reaped = await manager.reap_stop_orphans(
            [denied_entry, real_entry], session_id="s", worker_pid=os.getpid()
        )

        reaped_pids = {r["pid"] for r in reaped}
        assert real_orphan.pid in reaped_pids, (
            "a real orphan in the same batch as a permission-denied one was never reaped "
            "— Issue #41 round-2 review finding #5"
        )
        # `.poll()` (not `_alive()`/`kill(pid, 0)`): THIS test process is the
        # direct parent of both children (unlike the fake-agent-based tests
        # elsewhere in this file, where the ORPHAN's parent is a separate
        # worker subprocess) — a killed child of the calling process becomes
        # a zombie that `kill(pid, 0)` keeps reporting as "alive" until
        # something actually reaps it; `Popen.poll()` is that reap.
        assert real_orphan.poll() is not None, (
            "a real orphan in the same batch as a permission-denied one was never reaped "
            "— Issue #41 round-2 review finding #5"
        )
        assert denied.poll() is None, "denied pid unexpectedly killed — test setup bug"
    finally:
        monkeypatch.undo()
        for proc in (denied, real_orphan):
            with contextlib.suppress(ProcessLookupError):
                os.kill(proc.pid, 9)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)


async def test_shutdown_waits_long_enough_for_reap_stop_orphans_own_worst_case(
    tmp_path, monkeypatch
):
    """Round-2 review findings #3/#6 — `reap_stop_orphans`'s own worst case
    (`_ORPHAN_CANCEL_GRACE_S` + `_ORPHAN_TERM_GRACE_S`) can exceed the flat
    5.0s `SessionService.shutdown()` used to bound its `_background_tasks`
    wait with. "User clicks stop, then immediately quits the app" is the
    single most common way to reach `shutdown()` with a reap task still in
    flight — the old bound could cut it off mid-way, before its SIGKILL
    escalation ever ran, letting the orphan survive past daemon exit.

    Grace periods are tuned so their sum (5.5s) exceeds the OLD flat bound
    (5.0s) but fits inside the NEW one; `SPAWN_ORPHAN_IGNORING_SIGTERM`
    (not `SPAWN_ORPHAN_INDEPENDENT_SESSION` — a plain `sleep` would die the
    instant SIGTERM arrives and never actually exercise the TERM-grace-
    then-KILL escalation this is testing) forces the full escalation path.
    Asserts the orphan is genuinely dead by the time `shutdown()` returns —
    not merely that `shutdown()` didn't raise."""
    service = await _make_service(tmp_path, monkeypatch)
    monkeypatch.setattr(manager_module, "_ORPHAN_CANCEL_GRACE_S", 0.2)
    monkeypatch.setattr(manager_module, "_ORPHAN_TERM_GRACE_S", 5.3)
    orphan_pid: int | None = None
    try:
        session_id = await _new_session(service, mode="task")

        async def _get_worker():
            while service.worker_manager.get(session_id) is None:
                await asyncio.sleep(0.02)
            return service.worker_manager.get(session_id)

        await service.send(session_id, "SPAWN_ORPHAN_IGNORING_SIGTERM")
        worker = await _get_worker()
        pid_file = worker.hermes_home / "_orphan_ignoring_sigterm.pid"
        await _wait_until(lambda: pid_file.exists(), timeout=5.0)
        orphan_pid = int(pid_file.read_text().strip())
        assert _alive(orphan_pid), "orphan child never started"

        result = await service.stop(session_id)
        assert result["stopped"] is True
        reap_tasks = set(service._background_tasks)
        assert reap_tasks, "stop() did not schedule a reap_stop_orphans background task"

        # Immediately shut down — same as a user quitting the app right
        # after clicking "stop" — while the reap task is still mid-grace.
        await service.shutdown()

        # The reap task itself must have actually RUN TO COMPLETION (its own
        # SIGKILL escalation included) by the time `shutdown()` returns — the
        # direct, non-racy signal that shutdown()'s wait was long enough.
        # Checking OS-visible process state right here instead would race a
        # separate, unrelated timing detail this fix doesn't control: once
        # the worker process itself exits (`worker_manager.stop()`, earlier
        # in `shutdown()`), the now-parentless orphan gets reparented to
        # init/launchd, which reaps it — but not necessarily in the same
        # instant `shutdown()` returns.
        for task in reap_tasks:
            assert task.done(), (
                "reap_stop_orphans task was still pending when shutdown() returned — "
                "shutdown()'s wait was shorter than that task's own worst case "
                "(Issue #41 round-2 review findings #3/#6)"
            )
            reaped = task.result()
            assert any(r["pid"] == orphan_pid and r["sigkill"] for r in reaped), (
                f"reap_stop_orphans finished but never escalated to SIGKILL for the "
                f"SIGTERM-ignoring orphan (pid {orphan_pid}): {reaped}"
            )

        # Secondary, OS-level confirmation — bounded, not a single racy
        # immediate check (see comment above): the orphan should disappear
        # shortly after `shutdown()` returns, once launchd reaps it.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and _alive(orphan_pid):
            await asyncio.sleep(0.05)
        assert not _alive(orphan_pid), (
            "orphan pid still alive 2s after shutdown() returned, despite the reap task "
            "itself reporting it SIGKILL'd — Issue #41 round-2 review findings #3/#6"
        )
    finally:
        if orphan_pid is not None and _alive(orphan_pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(orphan_pid, 9)
