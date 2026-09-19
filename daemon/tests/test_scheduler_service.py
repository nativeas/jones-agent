"""`CronService` tests (Issue #20, docs/design/04-w5-interfaces.md §2).

Most tests dispatch against `StubSessionService` — a fake honoring only the public
surface this branch is allowed to call (`ensure_main_session`/`create`/`send`/`get`/
`run_get`, per §1's ownership table) — so they're fast, deterministic, and don't
need a real ACP worker subprocess. `test_dispatch_against_real_session_service_*`
is the one exception: it exercises the real `SessionService` to concretely
reproduce the cross-branch mode conflict documented in `scheduler/service.py`'s
module docstring and this branch's PR report.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time as time_mod
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from jones_daemon.context import DaemonContext, NullConfigResolver
from jones_daemon.projects.bootstrap import bootstrap_projects_and_agents
from jones_daemon.rpc.errors import INVALID_STATE, NOT_FOUND, RpcError
from jones_daemon.scheduler import queries
from jones_daemon.scheduler import service as cron_service_module
from jones_daemon.scheduler.cron_expr import parse
from jones_daemon.scheduler.service import CronService, _next_after_local
from jones_daemon.sessions.service import DEFAULT_AGENT_ID, DEFAULT_PROJECT_ID, SessionService
from jones_daemon.store import apply_pending, connect, run_in_db_thread

# -- shared fakes -----------------------------------------------------------------


class FakeServer:
    def __init__(self) -> None:
        self.broadcasts: list[tuple[str, str, Any]] = []

    async def broadcast(self, session_id: str, method: str, params: Any) -> None:
        self.broadcasts.append((session_id, method, params))

    def events(self, method: str) -> list[tuple[str, Any]]:
        return [(sid, p) for sid, m, p in self.broadcasts if m == method]


class _StubProviders:
    def resolve(self, model_pref: dict[str, Any] | None) -> Any:
        return {"provider": "anthropic", "model": "claude-test", "env": {}, "hermes_config": {}}

    def list_models(self, provider: str | None) -> list[dict[str, Any]]:
        return []


class ManualClock:
    """A `Clock` driven entirely by the test — `now()` only ever changes via
    `advance()`, and `wait()` never times out on its own (real time never passes
    during a test); the test unblocks a suspended `_loop` by calling
    `service._wake.set()` after advancing the clock, exactly like a real config
    change or timer firing would. `wait_calls` records every `timeout` `_loop`
    asked for, in order — the evidence `test_idle_loop_waits_once...` and
    `test_single_wait_covers_the_nearest_cron` check against."""

    def __init__(self, start: datetime) -> None:
        self.value = start
        self.wait_calls: list[float | None] = []

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)

    async def wait(self, event: asyncio.Event, *, timeout: float | None) -> None:
        self.wait_calls.append(timeout)
        await event.wait()


class StubSessionService:
    """Implements exactly the methods `CronService` is allowed to call
    (`ensure_main_session`/`create`/`send`/`get`/`run_get`) — nothing from
    `sessions/service.py` is imported or subclassed, so this can't accidentally
    depend on internals this branch isn't supposed to touch.

    `next_status`/`next_terminated` control what a subsequent `send()` makes the
    freshly-created child session immediately report via `get()` — every stub
    session "completes" synchronously (no real Turn execution), so
    `_await_completion`'s poll loop always resolves on its first check, keeping
    tests fast with no real sleeping (`CronService.poll_interval_seconds` is set
    to a small value in `_make_cron_service` as a second layer of protection, in
    case a future test intentionally leaves a session "running").
    """

    def __init__(self, conn) -> None:
        self._conn = conn  # only used to satisfy `sessions` FK rows, see `create()`
        self.main_session_id = "main-fake"
        self.sessions: dict[str, dict[str, Any]] = {}
        self.create_calls: list[dict[str, Any]] = []
        self.sent: list[tuple[str, str]] = []
        self._runs: dict[str, dict[str, Any]] = {}
        self._next_seq = 0
        self.create_error: RpcError | None = None
        self.send_error: RpcError | None = None
        self.next_status = "completed"
        self.next_terminated: tuple[str, str] | None = None
        self._next_turn_seq = 0

    async def ensure_main_session(self) -> str:
        return self.main_session_id

    async def create(
        self, *, project_id: str, agent_id: str, parent_id: str | None, mode: str,
        title: str | None, system_dispatch: bool = False,
    ) -> dict[str, Any]:
        self.create_calls.append(
            {"project_id": project_id, "agent_id": agent_id, "parent_id": parent_id,
             "mode": mode, "title": title, "system_dispatch": system_dispatch}
        )
        if self.create_error is not None:
            raise self.create_error
        self._next_seq += 1
        session_id = f"child-{self._next_seq}"
        await run_in_db_thread(_insert_fake_session, self._conn, session_id, mode=mode)
        self.sessions[session_id] = {"id": session_id, "latest_turn": None}
        return {"id": session_id}

    async def send(self, session_id: str, text: str, attachments: Any = None) -> dict[str, Any]:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((session_id, text))
        # Round-1 fix (review #7): a real, distinct turn id per call (was a
        # hardcoded "t1" every time, and `latest_turn` carried no "id" at all) —
        # `CronService._poll_until_done` now compares `send()`'s returned
        # `turn_id` against `get()`'s `latest_turn["id"]`, so the stub has to
        # actually model that both refer to the same Turn for that check (and
        # `test_mismatched_latest_turn_is_reported_as_undetermined`'s deliberate
        # mismatch) to mean anything.
        self._next_turn_seq += 1
        turn_id = f"turn-{self._next_turn_seq}"
        run_id = f"run-{self._next_turn_seq}"
        run: dict[str, Any] = {"id": run_id}
        if self.next_status != "completed" and self.next_terminated is not None:
            run["terminated_kind"], run["terminated_reason"] = self.next_terminated
        self._runs[run_id] = run
        self.sessions[session_id]["latest_turn"] = {
            "id": turn_id, "status": self.next_status, "run_id": run_id,
        }
        return {"turn_id": turn_id, "queued": False}

    async def get(self, session_id: str) -> dict[str, Any]:
        return self.sessions[session_id]

    async def run_get(self, run_id: str) -> dict[str, Any]:
        row = self._runs.get(run_id)
        if row is None:
            raise RpcError(NOT_FOUND, "run not found", {"run_id": run_id})
        return row

    def finish(
        self, session_id: str, *, status: str, terminated: tuple[str, str] | None = None
    ) -> None:
        """Test hook: flip an already-dispatched (still "running") session's
        latest Turn to a terminal status, for tests that need to control exactly
        when `_await_completion`'s poll notices completion (the overlap test)."""
        run_id = self.sessions[session_id]["latest_turn"]["run_id"]
        run = self._runs[run_id]
        if terminated is not None:
            run["terminated_kind"], run["terminated_reason"] = terminated
        self.sessions[session_id]["latest_turn"]["status"] = status


def _insert_fake_session(
    conn, session_id: str, *, is_main: bool = False, mode: str = "task"
) -> None:
    """`messages.session_id`/`tasks.session_id` are real foreign keys
    (`PRAGMA foreign_keys=ON`, store/db.py) — `StubSessionService` stands in for
    `SessionService` itself, so it (and this fixture's fake main session) must
    still leave a real `sessions` row behind for anything this branch's own
    `scheduler/queries.py` writes against that id to satisfy those constraints."""
    conn.execute(
        "INSERT INTO sessions (id, project_id, agent_id, parent_id, is_main, mode, title, "
        "status, created_at, updated_at) VALUES (?, ?, ?, NULL, ?, ?, NULL, 'active', "
        "'2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z')",
        (session_id, DEFAULT_PROJECT_ID, DEFAULT_AGENT_ID, int(is_main), mode),
    )
    conn.commit()


async def _open_ctx(tmp_path, monkeypatch) -> DaemonContext:
    monkeypatch.setenv("JONES_HOME", str(tmp_path))

    def _open() -> Any:
        conn = connect(tmp_path / "jones.db")
        apply_pending(conn)
        bootstrap_projects_and_agents(conn)
        # Round-1 fix: real `ensure_main_session()` always builds `mode="task"`
        # (`sessions/service.py`) — this fixture used to say `mode="auto"`,
        # which isn't a config the real system ever produces, and quietly let
        # every `mode="auto"` cron test below dispatch through `StubSessionService`
        # (which never enforces PRD 9.6's gate) without ever exercising anything
        # resembling the real default. Matching the real default here doesn't
        # change these tests' behavior (the stub still doesn't gate), but it's
        # honest about what production actually looks like — see review #1's
        # aside and `test_dispatch_against_real_session_service_hits_the_mode_
        # conflict` for the one test that exercises the real gate.
        _insert_fake_session(conn, "main-fake", is_main=True, mode="task")
        return conn

    conn = await run_in_db_thread(_open)
    from jones_daemon import paths

    return DaemonContext(
        db=conn, paths=paths, server=FakeServer(),
        providers=_StubProviders(), config=NullConfigResolver(),
    )


def _make_cron_service(ctx: DaemonContext, session_service: Any, clock: ManualClock) -> CronService:
    return CronService(ctx, session_service, clock=clock, poll_interval_seconds=0.001)


async def _settle(n: int = 20) -> None:
    """Give the event loop time to run a background task through at least one
    real `run_in_db_thread` round trip (a genuine OS thread-pool hop, not just an
    in-process coroutine switch — a bare `await asyncio.sleep(0)` isn't reliably
    enough for that to have landed yet)."""
    for _ in range(n):
        await asyncio.sleep(0.005)


async def _drain_background(service: CronService) -> None:
    """Await every currently-tracked background task (watch-and-report coroutines
    `_dispatch` schedules but doesn't itself await) — tests need the outcome
    (success/failure recording + broadcast) to have actually landed before
    asserting on it."""
    tasks = list(service._background_tasks)
    if tasks:
        await asyncio.gather(*tasks)


async def _upsert_every_minute_cron(
    service: CronService, *, name: str = "test-cron"
) -> dict[str, Any]:
    return await service.upsert(
        project_id=DEFAULT_PROJECT_ID, agent_id=DEFAULT_AGENT_ID, name=name,
        expr="* * * * *", prompt="do the thing", mode="auto",
    )


# -- CRUD ---------------------------------------------------------------------------


async def test_upsert_create_then_update_recomputes_next_run_at(tmp_path, monkeypatch):
    ctx = await _open_ctx(tmp_path, monkeypatch)
    clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    service = _make_cron_service(ctx, StubSessionService(ctx.db), clock)
    row = await _upsert_every_minute_cron(service)
    assert row["next_run_at"] == "2026-01-01T10:01:00.000Z"
    assert row["fail_count"] == 0
    assert row["enabled"] == 1

    updated = await service.upsert(
        id=row["id"], project_id=DEFAULT_PROJECT_ID, agent_id=DEFAULT_AGENT_ID,
        name="renamed", expr="0 * * * *", prompt="do the thing", mode="task", enabled=True,
    )
    assert updated["id"] == row["id"]
    assert updated["name"] == "renamed"
    assert updated["mode"] == "task"
    assert updated["next_run_at"] == "2026-01-01T11:00:00.000Z"


async def test_upsert_invalid_expr_raises_invalid_params(tmp_path, monkeypatch):
    ctx = await _open_ctx(tmp_path, monkeypatch)
    clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    service = _make_cron_service(ctx, StubSessionService(ctx.db), clock)
    with pytest.raises(RpcError):
        await service.upsert(
            project_id=DEFAULT_PROJECT_ID, agent_id=DEFAULT_AGENT_ID, name="bad",
            expr="not a cron expr", prompt="x", mode="auto",
        )


async def test_upsert_disabled_cron_has_no_next_run_at(tmp_path, monkeypatch):
    ctx = await _open_ctx(tmp_path, monkeypatch)
    clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    service = _make_cron_service(ctx, StubSessionService(ctx.db), clock)
    row = await service.upsert(
        project_id=DEFAULT_PROJECT_ID, agent_id=DEFAULT_AGENT_ID, name="off",
        expr="* * * * *", prompt="x", mode="auto", enabled=False,
    )
    assert row["next_run_at"] is None
    assert row["enabled"] == 0


async def test_delete_unknown_cron_raises_not_found(tmp_path, monkeypatch):
    ctx = await _open_ctx(tmp_path, monkeypatch)
    clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    service = _make_cron_service(ctx, StubSessionService(ctx.db), clock)
    with pytest.raises(RpcError):
        await service.delete("does-not-exist")


async def test_delete_removes_the_row(tmp_path, monkeypatch):
    ctx = await _open_ctx(tmp_path, monkeypatch)
    clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    service = _make_cron_service(ctx, StubSessionService(ctx.db), clock)
    row = await _upsert_every_minute_cron(service)
    await service.delete(row["id"])
    assert await run_in_db_thread(queries.get_cron, ctx.db, row["id"]) is None


# -- dispatch: happy path -----------------------------------------------------------


async def test_run_now_dispatches_immediately_and_reports_success(tmp_path, monkeypatch):
    ctx = await _open_ctx(tmp_path, monkeypatch)
    clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    stub = StubSessionService(ctx.db)
    service = _make_cron_service(ctx, stub, clock)
    row = await _upsert_every_minute_cron(service, name="my cron")

    await service.run_now(row["id"])
    await _drain_background(service)

    assert len(stub.create_calls) == 1
    call = stub.create_calls[0]
    assert call["mode"] == "auto"
    assert call["parent_id"] == stub.main_session_id
    assert call["title"] == "my cron"
    # Issue #20 跨分支裁定: cron dispatch always identifies itself as a system
    # dispatch, so `SessionService.create()`'s N13 mode-narrowing gate treats it
    # as pre-authorized rather than agent self-escalation.
    assert call["system_dispatch"] is True
    assert stub.sent == [("child-1", "do the thing")]

    completed = ctx.server.events("message.completed")
    assert len(completed) == 1
    session_id, message = completed[0]
    assert session_id == stub.main_session_id
    content = message["content"]
    assert content["kind"] == "cron_result"
    assert content["meta"]["kind"] == "cron_completed"
    assert "已完成" in content["text"]

    after = await run_in_db_thread(queries.get_cron, ctx.db, row["id"])
    assert after["fail_count"] == 0
    assert after["enabled"] == 1
    # `run_now` walks the same dispatch path as a natural tick (04-w5-interfaces.md
    # §2 "run_now 走同一路径"), so it also advances last_run_at/next_run_at.
    assert after["last_run_at"] is not None
    assert after["next_run_at"] > row["next_run_at"] or after["next_run_at"] == row["next_run_at"]


async def test_dispatched_task_row_is_created_with_source_cron(tmp_path, monkeypatch):
    ctx = await _open_ctx(tmp_path, monkeypatch)
    clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    stub = StubSessionService(ctx.db)
    service = _make_cron_service(ctx, stub, clock)
    row = await _upsert_every_minute_cron(service)
    await service.run_now(row["id"])
    await _drain_background(service)

    task = await run_in_db_thread(
        lambda conn: conn.execute("SELECT * FROM tasks WHERE cron_id = ?", (row["id"],)).fetchone(),
        ctx.db,
    )
    assert task is not None
    assert task["source"] == "cron"
    assert task["session_id"] == "child-1"


# -- dispatch: failure + 3-strikes auto-disable --------------------------------------


async def test_failed_run_increments_fail_count_and_posts_a_card(tmp_path, monkeypatch):
    ctx = await _open_ctx(tmp_path, monkeypatch)
    clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    stub = StubSessionService(ctx.db)
    stub.next_status = "terminated"
    stub.next_terminated = ("provider_error", "no API key configured")
    service = _make_cron_service(ctx, stub, clock)
    row = await _upsert_every_minute_cron(service)

    await service.run_now(row["id"])
    await _drain_background(service)

    after = await run_in_db_thread(queries.get_cron, ctx.db, row["id"])
    assert after["fail_count"] == 1
    assert after["enabled"] == 1  # not yet at the threshold

    completed = ctx.server.events("message.completed")
    assert len(completed) == 1
    content = completed[0][1]["content"]
    assert content["meta"]["kind"] == "cron_failed"
    assert content["meta"]["card"] == {"kind": "provider_error", "message": "no API key configured"}


async def test_three_consecutive_failures_auto_disable_and_notify(tmp_path, monkeypatch):
    ctx = await _open_ctx(tmp_path, monkeypatch)
    clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    stub = StubSessionService(ctx.db)
    stub.next_status = "terminated"
    stub.next_terminated = ("tool_exception", "boom")
    service = _make_cron_service(ctx, stub, clock)
    row = await _upsert_every_minute_cron(service)

    for _ in range(3):
        await service.run_now(row["id"])
        await _drain_background(service)

    after = await run_in_db_thread(queries.get_cron, ctx.db, row["id"])
    assert after["fail_count"] == 3
    assert after["enabled"] == 0

    completed = ctx.server.events("message.completed")
    # 3 failure cards + 1 "auto-disabled" notice.
    assert len(completed) == 4
    kinds = [p["content"]["meta"]["kind"] for _sid, p in completed]
    assert kinds == ["cron_failed", "cron_failed", "cron_failed", "cron_disabled"]
    assert "连续失败 3 次" in completed[-1][1]["content"]["text"]
    assert "已自动停用" in completed[-1][1]["content"]["text"]


async def test_success_after_failures_resets_fail_count(tmp_path, monkeypatch):
    ctx = await _open_ctx(tmp_path, monkeypatch)
    clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    stub = StubSessionService(ctx.db)
    stub.next_status = "terminated"
    stub.next_terminated = ("network", "dns failure")
    service = _make_cron_service(ctx, stub, clock)
    row = await _upsert_every_minute_cron(service)

    await service.run_now(row["id"])
    await _drain_background(service)
    await service.run_now(row["id"])
    await _drain_background(service)
    mid = await run_in_db_thread(queries.get_cron, ctx.db, row["id"])
    assert mid["fail_count"] == 2

    stub.next_status = "completed"
    await service.run_now(row["id"])
    await _drain_background(service)

    after = await run_in_db_thread(queries.get_cron, ctx.db, row["id"])
    assert after["fail_count"] == 0
    assert after["enabled"] == 1


async def test_session_create_rpc_error_is_recorded_as_a_dispatch_failure(tmp_path, monkeypatch):
    """`SessionService.create()` itself can reject a dispatch (bad project/agent id,
    or the PRD 9.6 mode-escalation guard — see
    `test_dispatch_against_real_session_service_hits_the_mode_conflict` below for a
    concrete repro of the latter) — this must land as an honest recorded failure,
    not an unhandled exception that kills the dispatch task silently."""
    ctx = await _open_ctx(tmp_path, monkeypatch)
    clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    stub = StubSessionService(ctx.db)
    service = _make_cron_service(ctx, stub, clock)
    row = await _upsert_every_minute_cron(service)

    stub.create_error = RpcError(INVALID_STATE, "child session cannot be mode=auto", {})
    await service.run_now(row["id"])
    await _drain_background(service)

    after = await run_in_db_thread(queries.get_cron, ctx.db, row["id"])
    assert after["fail_count"] == 1
    completed = ctx.server.events("message.completed")
    assert len(completed) == 1
    assert completed[0][1]["content"]["meta"]["kind"] == "cron_dispatch_error"
    assert "mode=auto" in completed[0][1]["content"]["text"]


# -- overlap skip ---------------------------------------------------------------------


async def test_overlapping_trigger_is_skipped_while_previous_run_is_active(tmp_path, monkeypatch):
    ctx = await _open_ctx(tmp_path, monkeypatch)
    clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    stub = StubSessionService(ctx.db)
    stub.next_status = "running"  # stays "running" until the test finishes it
    service = _make_cron_service(ctx, stub, clock)
    row = await _upsert_every_minute_cron(service)

    # First trigger: dispatches and leaves a Run "in flight" (stub session stays
    # "running", so `_await_completion`'s watch task never resolves on its own).
    await service._dispatch(row)
    assert len(stub.create_calls) == 1
    assert row["id"] in service._in_flight

    # Second trigger (a minute later) while the first is still active: must
    # skip, not dispatch a second child session — 04-w5-interfaces.md §2 "若已有
    # 该 cron 的 Run 在跑则跳过并记 skipped_overlap".
    clock.advance(60)
    await service._dispatch(row)
    assert len(stub.create_calls) == 1  # unchanged

    # The schedule must still have advanced for the skipped trigger (recomputed
    # from the *second* dispatch's clock reading) — else it would refire on
    # every future `_loop` tick against the same stale `next_run_at`.
    after_skip = await run_in_db_thread(queries.get_cron, ctx.db, row["id"])
    assert after_skip["next_run_at"] == "2026-01-01T10:02:00.000Z"

    # Finishing the in-flight Run clears the overlap guard for the next trigger.
    stub.finish("child-1", status="completed")
    await _drain_background(service)
    assert row["id"] not in service._in_flight
    await service._dispatch(row)
    assert len(stub.create_calls) == 2


# -- the timer loop: single wait, no polling -------------------------------------------


async def test_idle_loop_waits_once_with_no_timer_armed(tmp_path, monkeypatch):
    ctx = await _open_ctx(tmp_path, monkeypatch)
    clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    service = _make_cron_service(ctx, StubSessionService(ctx.db), clock)
    await service.start()  # no crons at all
    try:
        await _settle()
        # Exactly one suspend, with no deadline (nothing scheduled) — PRD 11.2
        # "空闲不轮询、用系统定时器唤醒", the pure idle case: no timer at all.
        assert clock.wait_calls == [None]
        assert service._loop_task is not None and not service._loop_task.done()
    finally:
        await service.stop()


async def test_single_wait_covers_the_nearest_cron_not_a_poll_loop(tmp_path, monkeypatch):
    ctx = await _open_ctx(tmp_path, monkeypatch)
    clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    stub = StubSessionService(ctx.db)
    service = _make_cron_service(ctx, stub, clock)
    await _upsert_every_minute_cron(service)  # due at 10:01:00

    await service.start()
    try:
        await _settle()
        # A single wait for exactly the delay until the nearest cron — not a
        # sequence of short polls.
        assert clock.wait_calls == [pytest.approx(60.0)]
        assert stub.create_calls == []  # not due yet

        clock.advance(60)
        service._wake.set()
        await _settle()

        assert len(stub.create_calls) == 1
    finally:
        await service.stop()


async def test_upsert_wakes_an_idle_loop_immediately(tmp_path, monkeypatch):
    ctx = await _open_ctx(tmp_path, monkeypatch)
    clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    stub = StubSessionService(ctx.db)
    service = _make_cron_service(ctx, stub, clock)
    await service.start()  # idle: waiting on `_wake` with timeout=None
    try:
        await _settle()
        assert clock.wait_calls == [None]

        row = await _upsert_every_minute_cron(service)
        await _settle()
        # The loop woke up and recomputed a real deadline instead of staying
        # parked on its earlier `timeout=None` wait.
        assert clock.wait_calls[-1] == pytest.approx(60.0)
        assert row["next_run_at"] == "2026-01-01T10:01:00.000Z"
    finally:
        await service.stop()


# -- startup: missed triggers are not caught up ------------------------------------


async def test_startup_reschedules_a_missed_trigger_without_dispatching(tmp_path, monkeypatch):
    ctx = await _open_ctx(tmp_path, monkeypatch)
    clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    stub = StubSessionService(ctx.db)

    def _seed(conn):
        return queries.create_cron(
            conn, project_id=DEFAULT_PROJECT_ID, agent_id=DEFAULT_AGENT_ID, name="missed",
            expr="* * * * *", prompt="x", mode="auto", enabled=True,
            next_run_at="2026-01-01T09:00:00.000Z",  # an hour in the past
        )

    seeded = await run_in_db_thread(_seed, ctx.db)
    service = _make_cron_service(ctx, stub, clock)

    await service.start()
    try:
        assert stub.create_calls == []  # never dispatched retroactively

        after = await run_in_db_thread(queries.get_cron, ctx.db, seeded["id"])
        assert after["next_run_at"] == "2026-01-01T10:01:00.000Z"

        rows = await run_in_db_thread(
            lambda conn: conn.execute(
                "SELECT content_json FROM messages WHERE session_id = ?", (stub.main_session_id,)
            ).fetchall(),
            ctx.db,
        )
        contents = [json.loads(r["content_json"]) for r in rows]
        missed = [c for c in contents if c["meta"].get("kind") == "cron_missed"]
        assert len(missed) == 1
        assert "错过" in missed[0]["text"]
    finally:
        await service.stop()


async def test_startup_computes_next_run_at_for_a_never_scheduled_cron(tmp_path, monkeypatch):
    ctx = await _open_ctx(tmp_path, monkeypatch)
    clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    stub = StubSessionService(ctx.db)

    def _seed(conn):
        return queries.create_cron(
            conn, project_id=DEFAULT_PROJECT_ID, agent_id=DEFAULT_AGENT_ID, name="fresh",
            expr="0 * * * *", prompt="x", mode="auto", enabled=True, next_run_at=None,
        )

    seeded = await run_in_db_thread(_seed, ctx.db)
    service = _make_cron_service(ctx, stub, clock)
    await service.start()
    try:
        after = await run_in_db_thread(queries.get_cron, ctx.db, seeded["id"])
        assert after["next_run_at"] == "2026-01-01T11:00:00.000Z"
    finally:
        await service.stop()


# -- real SessionService: PRD 9.1 / 9.6 mode conflict + its Issue #20 fix -------------


async def test_dispatch_of_an_auto_cron_under_a_task_parent_now_succeeds_system_dispatch(
    tmp_path, monkeypatch
):
    """Issue #20 跨分支裁定 (2026-09-19): this used to be a repro for the PRD
    9.1/9.6 conflict documented in `scheduler/service.py`'s module docstring —
    `ensure_main_session()` always creates the main session as `mode="task"`,
    and `SessionService.create()`'s PRD 9.6 guard used to reject a `mode="auto"`
    child of a `mode="task"` parent, which is exactly what an explicit
    `mode="auto"` Cron dispatches as its parent-child pair (and what PRD 9.1
    says a Cron-triggered Run should default to). The controller ruling
    resolved this: N13's mode-narrowing gate is for agent self-escalation, not
    for a system dispatch carrying the user's own cron-configured
    authorization, so `_dispatch_body_claimed` now calls
    `SessionService.create(..., system_dispatch=True)` and this dispatch
    succeeds against the real, unmodified `SessionService` — no
    `cron_dispatch_error`, no fail_count bump."""
    monkeypatch.setenv("JONES_HOME", str(tmp_path))

    def _open() -> Any:
        conn = connect(tmp_path / "jones.db")
        apply_pending(conn)
        bootstrap_projects_and_agents(conn)
        return conn

    conn = await run_in_db_thread(_open)
    from jones_daemon import paths

    ctx = DaemonContext(
        db=conn, paths=paths, server=FakeServer(),
        providers=_StubProviders(), config=NullConfigResolver(),
    )
    session_service = SessionService(ctx)
    await session_service.startup()
    try:
        main_id = await session_service.ensure_main_session()
        main = await session_service.get(main_id)
        assert main["mode"] == "task"  # the real, unmodified default

        clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
        service = CronService(ctx, session_service, clock=clock, poll_interval_seconds=0.001)
        row = await service.upsert(
            project_id=DEFAULT_PROJECT_ID, agent_id=DEFAULT_AGENT_ID, name="auto cron",
            expr="* * * * *", prompt="x", mode="auto",
        )

        await service.run_now(row["id"])

        after = await run_in_db_thread(queries.get_cron, ctx.db, row["id"])
        assert after["fail_count"] == 0  # no more cron_dispatch_error

        completed = ctx.server.events("message.completed")
        assert not any(
            e[1]["content"].get("meta", {}).get("kind") == "cron_dispatch_error"
            for e in completed
        )

        children = await run_in_db_thread(
            lambda: [
                dict(r)
                for r in ctx.db.execute(
                    "SELECT * FROM sessions WHERE parent_id = ?", (main_id,)
                ).fetchall()
            ]
        )
        assert len(children) == 1
        assert children[0]["mode"] == "auto"
    finally:
        await session_service.shutdown()


async def test_default_mode_is_auto_and_dispatches_against_real_session_service(
    tmp_path, monkeypatch
):
    """Round-3 (controller 跨分支裁定): companion to the test above, for the
    *default* (no explicit `mode=`) path — `cron.upsert`'s default is PRD 9.1's
    `"auto"` again (round-1 had temporarily changed it to `"task"` only because
    the 9.6 gate rejected every default dispatch; round-3's `system_dispatch`
    exemption removed that blocker, so the stopgap default is gone too). Pins
    that a bare `cron.upsert` — no `mode=` at all — both stores `mode="auto"`
    and dispatches successfully against the real, unmodified-elsewhere
    `SessionService`."""
    monkeypatch.setenv("JONES_HOME", str(tmp_path))

    def _open() -> Any:
        conn = connect(tmp_path / "jones.db")
        apply_pending(conn)
        bootstrap_projects_and_agents(conn)
        return conn

    conn = await run_in_db_thread(_open)
    from jones_daemon import paths

    ctx = DaemonContext(
        db=conn, paths=paths, server=FakeServer(),
        providers=_StubProviders(), config=NullConfigResolver(),
    )
    session_service = SessionService(ctx)
    await session_service.startup()
    try:
        main_id = await session_service.ensure_main_session()
        main = await session_service.get(main_id)
        assert main["mode"] == "task"  # the real, unmodified default

        clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
        service = CronService(ctx, session_service, clock=clock, poll_interval_seconds=0.001)
        row = await service.upsert(
            project_id=DEFAULT_PROJECT_ID, agent_id=DEFAULT_AGENT_ID, name="default mode",
            expr="* * * * *", prompt="x",  # no `mode=` — exercises the PRD 9.1 default
        )
        assert row["mode"] == "auto"

        await service.run_now(row["id"])

        after = await run_in_db_thread(queries.get_cron, ctx.db, row["id"])
        # No PRD 9.6 rejection: `system_dispatch=True` cleared the gate, so this
        # is not the `cron_dispatch_error` a plain `mode="auto"` dispatch used
        # to record before round-3.
        assert after["fail_count"] == 0
    finally:
        await session_service.shutdown()


# -- round-1 review fixes ---------------------------------------------------------


async def test_next_after_local_interprets_the_expression_in_the_local_timezone(monkeypatch):
    """Round-1 fix (review #4): `_next_after_local` is the one place that
    converts UTC<->local around `cron_expr.next_after` — this pins that a cron
    expression means the *local* wall clock, not UTC, regardless of what
    timezone the machine actually running the test happens to be in."""
    monkeypatch.setenv("TZ", "Asia/Shanghai")  # UTC+8, a whole-hour offset
    time_mod.tzset()
    try:
        schedule = parse("0 9 * * *")  # local 09:00 daily
        now_utc = datetime(2026, 1, 1, 0, 30, tzinfo=UTC)  # 08:30 local on Jan 1
        result = _next_after_local(schedule, now_utc)
        # 09:00 local on Jan 1 == 01:00 UTC on Jan 1.
        assert result == datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time_mod.tzset()


async def test_runtime_snapshot_written_on_start_and_kept_current(tmp_path, monkeypatch):
    """Round-1 fix (review #2): PRD 10.1 lists "Cron 下次触发时间" under
    `runtime/`; this pins that `runtime/cron_schedule.json` actually gets written
    (at `start()`, and again after `upsert()` changes the schedule)."""
    ctx = await _open_ctx(tmp_path, monkeypatch)
    clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    stub = StubSessionService(ctx.db)
    service = _make_cron_service(ctx, stub, clock)
    snapshot_path = ctx.paths.runtime_dir() / "cron_schedule.json"

    await service.start()
    try:
        assert snapshot_path.exists()
        empty = json.loads(snapshot_path.read_text())
        assert empty["crons"] == []

        row = await _upsert_every_minute_cron(service, name="snapshot-me")
        current = json.loads(snapshot_path.read_text())
        assert current["crons"] == [
            {"id": row["id"], "name": "snapshot-me", "next_run_at": row["next_run_at"]}
        ]
    finally:
        await service.stop()


def _insert_pending_user_permission(conn, *, session_id: str, decision_id: str, risk: str) -> None:
    """Test-only fixture: a `gate="user"`/`decision="pending"` permission request
    against `session_id`, wired through real `steps`/`runs` rows the same way
    `sessions/service.py::_on_request_permission` would leave behind — needed
    because `scheduler/queries.py::list_pending_user_permissions` joins through
    those tables (see its docstring)."""
    now = "2026-01-01T00:00:00.000Z"
    run_id = f"run-for-{decision_id}"
    step_id = f"step-for-{decision_id}"
    conn.execute(
        "INSERT INTO runs (id, task_id, turn_id, session_id, status, started_at, ended_at, "
        "terminated_kind, terminated_reason, prompt_snapshot_ref, created_at, updated_at) "
        "VALUES (?, NULL, NULL, ?, 'running', ?, NULL, NULL, NULL, NULL, ?, ?)",
        (run_id, session_id, now, now, now),
    )
    conn.execute(
        "INSERT INTO steps (id, run_id, seq, tool, args_json, result_summary, payload_ref, "
        "duration_ms, permission_id, status, created_at, updated_at) "
        "VALUES (?, ?, 1, 'terminal', '{}', NULL, NULL, NULL, ?, 'pending', ?, ?)",
        (step_id, run_id, decision_id, now, now),
    )
    conn.execute(
        "INSERT INTO permission_decisions (id, step_id, gate, risk, decision, decided_by, "
        "request_json, decided_at, created_at, updated_at) "
        "VALUES (?, ?, 'user', ?, 'pending', NULL, '{}', NULL, ?, ?)",
        (decision_id, step_id, risk, now, now),
    )
    conn.commit()


async def test_pending_permission_on_child_session_is_relayed_to_main_session(
    tmp_path, monkeypatch
):
    """Round-1 fix (review #3): PRD 9.4 "待审动作：推回主会话提醒" — a pending
    `gate="user"` permission request on the cron-dispatched child session must
    surface as a reminder in the main session, since nobody is subscribed to the
    child session's own `permission.requested` broadcast."""
    ctx = await _open_ctx(tmp_path, monkeypatch)
    clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    stub = StubSessionService(ctx.db)
    stub.next_status = "running"  # stays running until the test finishes it
    service = _make_cron_service(ctx, stub, clock)
    row = await _upsert_every_minute_cron(service)

    await service._dispatch(row)
    assert stub.create_calls  # dispatched
    child_id = "child-1"
    await run_in_db_thread(
        _insert_pending_user_permission, ctx.db,
        session_id=child_id, decision_id="perm-1", risk="high",
    )

    await _settle()  # let the watch task's poll loop notice it

    pending_events = [
        e for e in ctx.server.events("message.completed")
        if e[1]["content"]["meta"].get("kind") == "cron_permission_pending"
    ]
    assert len(pending_events) == 1
    session_id, message = pending_events[0]
    assert session_id == stub.main_session_id
    assert message["content"]["meta"]["decision_id"] == "perm-1"
    assert message["content"]["meta"]["risk"] == "high"

    # Settling again must not re-relay the same still-pending request.
    await _settle()
    pending_events_again = [
        e for e in ctx.server.events("message.completed")
        if e[1]["content"]["meta"].get("kind") == "cron_permission_pending"
    ]
    assert len(pending_events_again) == 1

    stub.finish(child_id, status="completed")
    await _drain_background(service)


async def test_successful_dispatch_marks_the_task_row_completed(tmp_path, monkeypatch):
    """Round-1 fix (review #5): the `tasks` row a cron dispatch creates
    (`source='cron'`, `status='running'`) used to never be updated again."""
    ctx = await _open_ctx(tmp_path, monkeypatch)
    clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    stub = StubSessionService(ctx.db)
    service = _make_cron_service(ctx, stub, clock)
    row = await _upsert_every_minute_cron(service)

    await service.run_now(row["id"])
    await _drain_background(service)

    task = await run_in_db_thread(
        lambda conn: conn.execute(
            "SELECT * FROM tasks WHERE cron_id = ?", (row["id"],)
        ).fetchone(),
        ctx.db,
    )
    assert task["status"] == "completed"


async def test_failed_dispatch_marks_the_task_row_failed(tmp_path, monkeypatch):
    """Round-1 fix (review #5), the failure half."""
    ctx = await _open_ctx(tmp_path, monkeypatch)
    clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    stub = StubSessionService(ctx.db)
    stub.next_status = "terminated"
    stub.next_terminated = ("network", "dns failure")
    service = _make_cron_service(ctx, stub, clock)
    row = await _upsert_every_minute_cron(service)

    await service.run_now(row["id"])
    await _drain_background(service)

    task = await run_in_db_thread(
        lambda conn: conn.execute(
            "SELECT * FROM tasks WHERE cron_id = ?", (row["id"],)
        ).fetchone(),
        ctx.db,
    )
    assert task["status"] == "failed"


async def test_unexpected_exception_during_dispatch_is_not_swallowed(tmp_path, monkeypatch):
    """Round-1 fix (review #6): an exception other than `RpcError` (a DB write
    failing after `create()` already succeeded, say) used to propagate out of the
    `asyncio.create_task`-spawned coroutine and just vanish — no log anyone could
    retrieve, no failure recorded, `_in_flight` left claimed forever."""
    ctx = await _open_ctx(tmp_path, monkeypatch)
    clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    stub = StubSessionService(ctx.db)
    service = _make_cron_service(ctx, stub, clock)
    row = await _upsert_every_minute_cron(service)

    def _boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(queries, "create_cron_task", _boom)

    await service._dispatch(row)
    await _settle()

    assert row["id"] not in service._in_flight  # the guard was released, not wedged
    completed = ctx.server.events("message.completed")
    assert len(completed) == 1
    assert completed[0][1]["content"]["meta"]["kind"] == "cron_dispatch_error"


async def test_non_rpc_error_from_get_during_watch_is_caught(tmp_path, monkeypatch):
    """Round-1 fix (review #6), the watch-task half: an unexpected exception from
    `session_service.get()` while polling (not the `RpcError`-for-a-deleted-
    session case `_poll_until_done` already handles) must not just end the watch
    task by exception."""
    ctx = await _open_ctx(tmp_path, monkeypatch)
    clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    stub = StubSessionService(ctx.db)
    stub.next_status = "running"
    service = _make_cron_service(ctx, stub, clock)
    row = await _upsert_every_minute_cron(service)

    await service._dispatch(row)
    assert stub.create_calls

    real_get = stub.get

    async def _flaky_get(session_id: str) -> dict[str, Any]:
        stub.get = real_get  # only fail once
        raise ValueError("boom")

    stub.get = _flaky_get

    await _drain_background(service)

    assert row["id"] not in service._in_flight
    completed = ctx.server.events("message.completed")
    assert len(completed) == 1
    assert completed[0][1]["content"]["meta"]["kind"] == "cron_watch_error"


async def test_mismatched_latest_turn_is_reported_as_undetermined(tmp_path, monkeypatch):
    """Round-1 fix (review #7): if the child session's `latest_turn` isn't the one
    this cron dispatched (a user typed their own message into the cron's child
    session, becoming its newest Turn), that must not be graded as this cron's
    outcome."""
    ctx = await _open_ctx(tmp_path, monkeypatch)
    clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    stub = StubSessionService(ctx.db)
    service = _make_cron_service(ctx, stub, clock)
    row = await _upsert_every_minute_cron(service)

    await service.run_now(row["id"])
    # The watch task has been created but not yet scheduled to run (no `await`
    # has yielded control back to the loop since `asyncio.create_task` — see the
    # comment in `_dispatch_body_claimed`) — safe to rewrite the "foreign" Turn
    # the poll will see on its very first check.
    stub.sessions["child-1"]["latest_turn"] = {
        "id": "someone-elses-turn", "status": "completed", "run_id": "someone-elses-run",
    }

    await _drain_background(service)

    completed = ctx.server.events("message.completed")
    assert len(completed) == 1
    content = completed[0][1]["content"]
    assert content["meta"]["kind"] == "cron_failed"
    assert content["meta"]["card"]["kind"] == "mismatched_turn"
    after = await run_in_db_thread(queries.get_cron, ctx.db, row["id"])
    assert after["fail_count"] == 1  # an honest failure, not a false "success"


async def test_concurrent_run_now_for_the_same_cron_dispatches_only_once(tmp_path, monkeypatch):
    """Round-1 fix (review #9): the overlap check-then-set used to straddle two
    `await` points (`ensure_main_session()`, `create()`), so two concurrent
    triggers for the same cron could both pass the check before either set the
    guard — each spawning its own real Run. The claim now happens synchronously,
    before this coroutine's first `await`."""
    ctx = await _open_ctx(tmp_path, monkeypatch)
    clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    stub = StubSessionService(ctx.db)
    stub.next_status = "running"  # stays in flight — isolates the race from the
    # ordinary "second trigger after the first already finished" case
    service = _make_cron_service(ctx, stub, clock)
    row = await _upsert_every_minute_cron(service)

    await asyncio.gather(service._dispatch(row), service._dispatch(row))

    assert len(stub.create_calls) == 1
    assert len(stub.sent) == 1


async def test_loop_tick_error_is_logged_and_retried_not_fatal(tmp_path, monkeypatch):
    """Round-1 fix (review #10): an unexpected exception during a tick (a
    `sqlite3.OperationalError` from `list_enabled_crons`, say) used to end
    `_loop_task` for good — every cron would simply stop firing, forever, with
    nothing surfacing that. It must instead log, back off, and keep going."""
    ctx = await _open_ctx(tmp_path, monkeypatch)
    clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    stub = StubSessionService(ctx.db)
    service = _make_cron_service(ctx, stub, clock)

    real_list = queries.list_enabled_crons
    calls = {"n": 0}

    def _flaky_list(conn):
        calls["n"] += 1
        # Calls #1/#2 happen inside `start()` itself (`_reconcile_on_startup`,
        # then `_write_runtime_snapshot`) — let those through so `start()`
        # succeeds; call #3 is `_loop_tick`'s first real tick, the one this test
        # is actually about.
        if calls["n"] == 3:
            raise RuntimeError("simulated DB hiccup")
        return real_list(conn)

    monkeypatch.setattr(queries, "list_enabled_crons", _flaky_list)
    monkeypatch.setattr(cron_service_module, "_LOOP_ERROR_BACKOFF_SECONDS", 0.001)

    await service.start()
    try:
        await _settle()
        # The tick that raised backed off rather than dying — the loop task is
        # still alive and has already retried at least once past the failure.
        assert service._loop_task is not None and not service._loop_task.done()
        assert calls["n"] >= 4
    finally:
        monkeypatch.setattr(queries, "list_enabled_crons", real_list)
        await service.stop()


async def test_stuck_run_times_out_instead_of_polling_forever(tmp_path, monkeypatch):
    """Round-1 fix (review #11): a Run stuck `running` past `max_run_wait_seconds`
    must be reported as an honest failure (and free `_in_flight`) rather than
    being watched forever — which would also permanently wedge this cron's
    overlap guard shut, since nothing would ever clear it."""
    ctx = await _open_ctx(tmp_path, monkeypatch)
    clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    stub = StubSessionService(ctx.db)
    stub.next_status = "running"  # never resolves on its own
    service = CronService(
        ctx, stub, clock=clock, poll_interval_seconds=0.001, max_run_wait_seconds=0.02,
    )
    row = await _upsert_every_minute_cron(service)

    await service._dispatch(row)
    await _drain_background(service)

    assert row["id"] not in service._in_flight
    completed = ctx.server.events("message.completed")
    assert len(completed) == 1
    content = completed[0][1]["content"]
    assert content["meta"]["kind"] == "cron_failed"
    assert content["meta"]["card"]["kind"] == "timeout"
    after = await run_in_db_thread(queries.get_cron, ctx.db, row["id"])
    assert after["fail_count"] == 1


async def test_pending_approval_does_not_time_out_or_release_the_overlap_guard(
    tmp_path, monkeypatch
):
    """Round-2 fix (review #1): a Run genuinely parked at `gate="user"` waiting
    on a pending approval must not be judged dead by the same wall-clock ceiling
    that catches a hung worker (round-1 review #11) — PRD 9.4's "挂起等待用户"
    is long-lived by design, and `list_pending_user_permissions` proves that
    state, not silence. `max_run_wait_seconds` is set far shorter than the real
    time this test lets pass, so without the fix this would already have fired
    a false "timeout" failure, disabled-count included, and released
    `_in_flight` — reopening the overlap-guard hole round-1 review #9 closed."""
    ctx = await _open_ctx(tmp_path, monkeypatch)
    clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    stub = StubSessionService(ctx.db)
    stub.next_status = "running"  # stays running until the test finishes it
    service = CronService(
        ctx, stub, clock=clock, poll_interval_seconds=0.005, max_run_wait_seconds=0.02,
    )
    row = await _upsert_every_minute_cron(service)

    await service._dispatch(row)
    assert stub.create_calls
    child_id = "child-1"
    await run_in_db_thread(
        _insert_pending_user_permission, ctx.db,
        session_id=child_id, decision_id="perm-1", risk="high",
    )

    # Real wall-clock time well past `max_run_wait_seconds` — the ceiling would
    # already have fired here if pending approvals didn't push it back out.
    await asyncio.sleep(0.1)

    assert row["id"] in service._in_flight  # guard still held — the Run is alive
    failed = [
        e for e in ctx.server.events("message.completed")
        if e[1]["content"]["meta"].get("kind") == "cron_failed"
    ]
    assert failed == []  # no false timeout
    still_enabled = await run_in_db_thread(queries.get_cron, ctx.db, row["id"])
    assert still_enabled["fail_count"] == 0  # no false strike toward auto-disable

    # The user finally decides — the poll notices the Turn finishing normally.
    stub.finish(child_id, status="completed")
    await _drain_background(service)

    assert row["id"] not in service._in_flight
    completed = [
        e for e in ctx.server.events("message.completed")
        if e[1]["content"]["meta"].get("kind") == "cron_completed"
    ]
    assert len(completed) == 1
    after = await run_in_db_thread(queries.get_cron, ctx.db, row["id"])
    assert after["fail_count"] == 0


async def test_stop_cancels_a_background_task_still_running_past_the_wait_timeout(
    tmp_path, monkeypatch
):
    """Round-1 fix (review #12): `stop()`'s 5s `asyncio.wait` used to neither
    cancel nor report on a task still running past it — leaving it free to keep
    calling into a `SessionService`/DB connection `__main__.py` may have already
    torn down. This pins that nothing is left in `_background_tasks` once
    `stop()` returns, using a hand-rolled never-finishing task rather than
    waiting out the real 5s timeout in the test."""
    ctx = await _open_ctx(tmp_path, monkeypatch)
    clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    stub = StubSessionService(ctx.db)
    service = _make_cron_service(ctx, stub, clock)
    await service.start()

    async def _never_finishes() -> None:
        await asyncio.sleep(3600)

    stuck = asyncio.create_task(_never_finishes())
    service._background_tasks.add(stuck)
    stuck.add_done_callback(service._background_tasks.discard)

    # Shrink `stop()`'s "give background tasks this long before cancelling"
    # window instead of actually waiting out the real 5s default.
    monkeypatch.setattr(cron_service_module, "_STOP_BACKGROUND_WAIT_SECONDS", 0.01)
    await service.stop()

    assert stuck.cancelled() or stuck.done()
    assert stuck not in service._background_tasks


async def test_cron_delete_after_a_dispatch_does_not_hit_the_fk_constraint(tmp_path, monkeypatch):
    """Round-1 fix (review #8): `tasks.cron_id REFERENCES crons(id)` with
    `PRAGMA foreign_keys=ON` — deleting a cron that has ever dispatched (leaving
    a `tasks` row behind) used to raise `sqlite3.IntegrityError` from a bare
    `DELETE FROM crons`."""
    ctx = await _open_ctx(tmp_path, monkeypatch)
    clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    stub = StubSessionService(ctx.db)
    service = _make_cron_service(ctx, stub, clock)
    row = await _upsert_every_minute_cron(service)

    await service.run_now(row["id"])
    await _drain_background(service)

    # Must not raise `sqlite3.IntegrityError` (previously did, 100% of the time
    # once a cron had fired at least once).
    deleted = await service.delete(row["id"])
    assert deleted["id"] == row["id"]

    still_there = await run_in_db_thread(
        lambda conn: conn.execute(
            "SELECT cron_id FROM tasks WHERE session_id = 'child-1'"
        ).fetchone(),
        ctx.db,
    )
    assert still_there["cron_id"] is None  # detached, not deleted
