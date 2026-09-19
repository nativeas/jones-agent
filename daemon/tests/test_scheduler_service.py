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
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from jones_daemon.context import DaemonContext, NullConfigResolver
from jones_daemon.projects.bootstrap import bootstrap_projects_and_agents
from jones_daemon.rpc.errors import INVALID_STATE, NOT_FOUND, RpcError
from jones_daemon.scheduler import queries
from jones_daemon.scheduler.service import CronService
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

    async def ensure_main_session(self) -> str:
        return self.main_session_id

    async def create(
        self, *, project_id: str, agent_id: str, parent_id: str | None, mode: str, title: str | None
    ) -> dict[str, Any]:
        self.create_calls.append(
            {"project_id": project_id, "agent_id": agent_id, "parent_id": parent_id,
             "mode": mode, "title": title}
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
        run_id = f"run-{session_id}"
        run: dict[str, Any] = {"id": run_id}
        if self.next_status != "completed" and self.next_terminated is not None:
            run["terminated_kind"], run["terminated_reason"] = self.next_terminated
        self._runs[run_id] = run
        self.sessions[session_id]["latest_turn"] = {"status": self.next_status, "run_id": run_id}
        return {"turn_id": "t1", "queued": False}

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
        _insert_fake_session(conn, "main-fake", is_main=True, mode="auto")
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


# -- real SessionService: documents the PRD 9.1 / 9.6 mode conflict --------------------


async def test_dispatch_against_real_session_service_hits_the_mode_conflict(tmp_path, monkeypatch):
    """Concrete repro for the conflict documented in `scheduler/service.py`'s
    module docstring: `ensure_main_session()` always creates the main session as
    `mode="task"` (sessions/service.py, out of this branch's reach), and
    `SessionService.create()`'s existing PRD 9.6 guard rejects a `mode="auto"`
    child of a `mode="task"` parent — exactly what a default (mode="auto") Cron
    dispatches as its parent-child pair. This is not this branch's bug to fix (it
    can't touch `sessions/service.py`), but the failure must still come back as an
    honest, recorded dispatch failure — not a crash — which is what this test
    actually asserts."""
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
        clock = ManualClock(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
        service = CronService(ctx, session_service, clock=clock, poll_interval_seconds=0.001)
        row = await service.upsert(
            project_id=DEFAULT_PROJECT_ID, agent_id=DEFAULT_AGENT_ID, name="conflict repro",
            expr="* * * * *", prompt="x", mode="auto",
        )

        await service.run_now(row["id"])

        after = await run_in_db_thread(queries.get_cron, ctx.db, row["id"])
        assert after["fail_count"] == 1  # an honest failure, not a silent no-op

        completed = ctx.server.events("message.completed")
        assert len(completed) == 1
        content = completed[0][1]["content"]
        assert content["meta"]["kind"] == "cron_dispatch_error"
        assert "mode=auto" in content["text"]
        assert "mode=task" in content["text"]
    finally:
        await session_service.shutdown()
