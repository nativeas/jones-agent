"""`SessionService` integration tests against `fake_acp_agent.py` — queue
serialization, stop, crash isolation across parallel sessions, restart
recovery, and the permission round trip (Issue #10 acceptance criteria, PRD
9.2/9.3/11.3, docs/design/01-w2-interfaces.md §2)."""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from jones_daemon.context import DaemonContext, NullConfigResolver, NullProviderResolver
from jones_daemon.context import ProviderResolver as ProviderResolverProtocol
from jones_daemon.projects.bootstrap import bootstrap_projects_and_agents
from jones_daemon.sessions import queries
from jones_daemon.sessions.service import DEFAULT_AGENT_ID, DEFAULT_PROJECT_ID, SessionService
from jones_daemon.store import apply_pending, connect, run_in_db_thread

_FAKE_AGENT = str(Path(__file__).parent / "fake_acp_agent.py")


class _StubProviderResolver(ProviderResolverProtocol):
    """A `ProviderResolver` that always succeeds — the default `_make_service()`
    injects, standing in for B/#7's real `DaemonProviderResolver` (which needs an
    actual configured vendor Key) so every test below that isn't specifically
    about the provider pre-flight check (02-w3-interfaces.md §2, `_run_turn`'s new
    `ctx.providers.resolve()` call) keeps exercising the normal turn-running path
    it did before that check existed. `NullProviderResolver` (always raises) is
    still injected explicitly by the one test that wants the provider_error path.
    """

    def resolve(self, model_pref: dict[str, Any] | None) -> Any:
        return {"provider": "anthropic", "model": "claude-test", "env": {}, "hermes_config": {}}

    def list_models(self, provider: str | None) -> list[dict[str, Any]]:
        return []


class FakeServer:
    """Stands in for `RpcServer` — `SessionService` only ever calls
    `broadcast()` on `ctx.server`, so a plain recorder is enough to assert on
    notification traffic without standing up a real socket (the real
    `RpcServer.broadcast`/subscribe wiring itself is covered by
    `test_rpc.py`)."""

    def __init__(self) -> None:
        self.broadcasts: list[tuple[str, str, Any]] = []
        self.broadcast_alls: list[tuple[str, Any]] = []

    async def broadcast(self, session_id: str, method: str, params: Any) -> None:
        self.broadcasts.append((session_id, method, params))

    async def broadcast_all(self, method: str, params: Any) -> None:
        self.broadcast_alls.append((method, params))

    def events(self, method: str) -> list[tuple[str, Any]]:
        return [(sid, p) for sid, m, p in self.broadcasts if m == method]


async def _make_service(
    tmp_path, monkeypatch, *, providers: ProviderResolverProtocol | None = None
) -> SessionService:
    monkeypatch.setenv("JONES_HOME", str(tmp_path))
    monkeypatch.setenv("FAKE_ACP_MODE", "normal")

    def _open() -> Any:
        conn = connect(tmp_path / "jones.db")
        apply_pending(conn)
        # Real daemon startup runs this before the RPC server accepts connections
        # (see __main__.py) — it's what fixes `proj_default.path` from 002/004's
        # environment-independent placeholder to a real, per-machine directory
        # (projects/service.py::ensure_default_project). `_cwd_for_project`
        # (02-w3-interfaces.md §2 集成收口 #1) now reads that real path via
        # `ProjectService.get()` instead of hardcoding `Path.home()` itself, so
        # tests need this same bootstrap step to see the same real home directory
        # `_cwd_for_project` used to return unconditionally.
        bootstrap_projects_and_agents(conn)
        return conn

    conn = await run_in_db_thread(_open)
    from jones_daemon import paths

    ctx = DaemonContext(
        db=conn,
        paths=paths,
        server=FakeServer(),
        providers=providers if providers is not None else _StubProviderResolver(),
        config=NullConfigResolver(),
    )
    service = SessionService(ctx, worker_cmd=[sys.executable, _FAKE_AGENT])
    return service


async def _new_session(service: SessionService, *, title: str) -> str:
    row = await service.create(
        project_id=DEFAULT_PROJECT_ID, agent_id=DEFAULT_AGENT_ID, mode="task", title=title
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


async def test_send_when_idle_runs_immediately_and_completes(tmp_path, monkeypatch):
    service = await _make_service(tmp_path, monkeypatch)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        result = await service.send(session_id, "hello there")
        assert result["queued"] is False
        await _wait_until(lambda: service.worker_manager.get(session_id) is not None)
        await _wait_until(
            lambda: any(
                m == "run.terminated" or m == "message.completed"
                for _sid, m, _p in service.ctx.server.broadcasts
            )
        )
        messages = await service.turn_messages(session_id, limit=10)
        texts = [m["content"]["text"] for m in messages if m["role"] == "assistant"]
        assert "Hello" in texts
    finally:
        await service.shutdown()


async def test_unexpected_exception_in_run_turn_still_terminates_the_run(tmp_path, monkeypatch):
    """Round-1 review fix: `_run_turn` used to only catch `WorkerStartupError` and
    `(AcpError, AcpProtocolError)` — any other exception (a DB failure,
    `_cwd_for_project`'s `RpcError` for a non-default project, a filesystem error
    from worker setup surfacing as something other than `WorkerStartupError`, ...)
    escaped the coroutine entirely (nobody awaits a task started via
    `asyncio.create_task` in `_start_turn`), leaving the Run stuck 'running'
    forever with no `run.terminated` and nothing surfaced anywhere (contract §7
    诚实失败)."""
    service = await _make_service(tmp_path, monkeypatch)
    await service.worker_manager.start()

    def _boom(_self, _project_id):
        raise RuntimeError("simulated unexpected failure")

    monkeypatch.setattr(SessionService, "_cwd_for_project", _boom)
    try:
        session_id = await _new_session(service, title="s1")
        await service.send(session_id, "hello there")
        await _wait_until(
            lambda: any(m == "run.terminated" for _sid, m, _p in service.ctx.server.broadcasts),
            timeout=5,
        )
        terminated = [p for _sid, m, p in service.ctx.server.broadcasts if m == "run.terminated"]
        assert terminated[0]["kind"] == "error"
        assert "simulated unexpected failure" in terminated[0]["reason"]
    finally:
        await service.shutdown()


async def test_tool_call_is_recorded_as_a_step_and_broadcast(tmp_path, monkeypatch):
    service = await _make_service(tmp_path, monkeypatch)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        result = await service.send(session_id, "please USE_TOOL now")
        await _wait_until(lambda: len(service.ctx.server.events("step.completed")) >= 1)

        started = service.ctx.server.events("step.started")
        completed = service.ctx.server.events("step.completed")
        assert started and started[0][1]["tool"] == "demo_tool"
        assert completed[0][1]["status"] == "completed"

        run_id = (await service.get(session_id))["latest_turn"]["run_id"]
        # `send()`'s own response already carries this Turn's run — fall back to
        # it if the DB read raced ahead of `create_run` (it shouldn't, but keep
        # the assertion from being flaky over a stronger ordering guarantee).
        if run_id is None:
            run_id = completed[0][1]["run_id"]
        steps = await service.run_steps(run_id)
        assert any(s["tool"] == "demo_tool" and s["status"] == "completed" for s in steps)
        assert result["queued"] is False
    finally:
        await service.shutdown()


async def test_send_while_running_is_queued_then_auto_advances(tmp_path, monkeypatch):
    service = await _make_service(tmp_path, monkeypatch)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        first = await service.send(session_id, "SLEEP_MS:300 first turn")
        assert first["queued"] is False
        second = await service.send(session_id, "second turn")
        assert second["queued"] is True

        queue_broadcasts = service.ctx.server.events("queue.changed")
        assert any(sid == session_id for sid, _p in queue_broadcasts)

        # Both Turns eventually complete, second run after first (queue serial
        # execution, PRD 9.2), never in parallel on the same session.
        await _wait_until(
            lambda: len(
                [
                    p
                    for _sid, m, p in service.ctx.server.broadcasts
                    if m == "queue.changed" and p["items"] == []
                ]
            )
            >= 1,
            timeout=5,
        )
        messages = await service.turn_messages(session_id, limit=20)
        assert messages[0]["content"]["text"] == "SLEEP_MS:300 first turn"
        user_texts = [m["content"]["text"] for m in messages if m["role"] == "user"]
        assert user_texts == ["SLEEP_MS:300 first turn", "second turn"]
    finally:
        await service.shutdown()


async def test_queue_remove_and_reorder(tmp_path, monkeypatch):
    service = await _make_service(tmp_path, monkeypatch)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        await service.send(session_id, "SLEEP_MS:400 first turn")
        await service.send(session_id, "queued A")
        await service.send(session_id, "queued B")
        items = await service.queue(session_id)
        assert [i["text"] for i in items] == ["queued A", "queued B"]

        reordered = await service.queue_reorder(session_id, [items[1]["id"], items[0]["id"]])
        assert [i["text"] for i in reordered] == ["queued B", "queued A"]

        removed = await service.queue_remove(session_id, reordered[0]["id"])
        assert [i["text"] for i in removed] == ["queued A"]

        # Let the still-running first Turn finish before tearing the worker
        # down, so shutdown() doesn't abandon its task mid-flight.
        await _wait_until(
            lambda: any(m == "message.completed" for _sid, m, _p in service.ctx.server.broadcasts)
        )
    finally:
        await service.shutdown()


async def test_stop_cancels_the_running_turn(tmp_path, monkeypatch):
    service = await _make_service(tmp_path, monkeypatch)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        await service.send(session_id, "SLEEP_MS:500 long turn")
        await _wait_until(lambda: service.worker_manager.get(session_id) is not None)
        result = await service.stop(session_id)
        assert result["stopped"] is True

        await _wait_until(
            lambda: any(
                m == "run.terminated" for _sid, m, _p in service.ctx.server.broadcasts
            ),
            timeout=5,
        )
        terminated = [p for _sid, m, p in service.ctx.server.broadcasts if m == "run.terminated"]
        assert terminated[0]["kind"] == "user"
    finally:
        await service.shutdown()


async def test_permission_request_and_decide_round_trip(tmp_path, monkeypatch):
    service = await _make_service(tmp_path, monkeypatch)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        await service.send(session_id, "NEEDS_PERMISSION do the risky thing")
        await _wait_until(lambda: len(service.ctx.server.events("permission.requested")) >= 1)
        pending = await service.permission_pending(session_id)
        assert len(pending) == 1
        request_id = pending[0]["request_id"]

        decided = await service.permission_decide(request_id, "allow")
        assert decided["decision"] == "allow"
        assert decided["decided_by"] == "user"

        # A granted permission lets the Turn run to a normal `end_turn` — the
        # only notification for that (design §4.2 has no "run succeeded" event,
        # only `run.terminated` for the user/error/budget cases) is
        # `message.completed`.
        await _wait_until(
            lambda: any(m == "message.completed" for _sid, m, _p in service.ctx.server.broadcasts),
            timeout=5,
        )
        run_row = await service.run_get(
            (await service.get(session_id))["latest_turn"]["run_id"]
        )
        assert run_row["status"] == "completed"
    finally:
        await service.shutdown()


async def test_turn_started_is_broadcast_when_a_turn_begins_running(tmp_path, monkeypatch):
    """RPC v0 §4.2 `turn.started {session_id, turn_id, run_id}` — round-1 review
    fix: this notification was never sent at all (every other §4.2 notification
    was implemented). It's the only signal the front end has that a Turn started
    running, in particular for the queue-auto-advance path, which never calls
    `send()` and so has nothing else to watch."""
    service = await _make_service(tmp_path, monkeypatch)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        result = await service.send(session_id, "hello there")
        await _wait_until(lambda: len(service.ctx.server.events("turn.started")) >= 1)
        started = service.ctx.server.events("turn.started")[0][1]
        assert started == {
            "session_id": session_id,
            "turn_id": result["turn_id"],
            "run_id": (await service.get(session_id))["latest_turn"]["run_id"],
        }
    finally:
        await service.shutdown()


async def test_turn_started_is_broadcast_for_a_turn_the_queue_auto_advances_to(
    tmp_path, monkeypatch
):
    """Same notification, but for the queue-auto-advance path (PRD 9.2) — the one
    case where nothing calls `send()` for the Turn that starts."""
    service = await _make_service(tmp_path, monkeypatch)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        first = await service.send(session_id, "SLEEP_MS:200 first turn")
        second = await service.send(session_id, "second turn")
        assert second["queued"] is True

        await _wait_until(
            lambda: any(
                p.get("turn_id") == second["turn_id"]
                for _sid, p in service.ctx.server.events("turn.started")
            ),
            timeout=5,
        )
        turn_ids = {p["turn_id"] for _sid, p in service.ctx.server.events("turn.started")}
        assert turn_ids == {first["turn_id"], second["turn_id"]}
    finally:
        await service.shutdown()


async def test_stop_while_a_permission_is_pending_still_terminates_the_turn(
    tmp_path, monkeypatch
):
    """Round-1 review fix: `stop()` alone only sends `session/cancel` — it can't
    unblock a worker that's itself stuck waiting on our answer to a
    `session/request_permission` it already sent (the fake agent's prompt
    handler thread blocks on exactly that, same as a real Hermes would). Without
    resolving that pending permission, `run.terminated` never arrives and the
    Turn is stuck 'running' forever."""
    service = await _make_service(tmp_path, monkeypatch)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        await service.send(session_id, "NEEDS_PERMISSION do the risky thing")
        await _wait_until(lambda: len(service.ctx.server.events("permission.requested")) >= 1)

        result = await service.stop(session_id)
        assert result["stopped"] is True

        await _wait_until(
            lambda: any(m == "run.terminated" for _sid, m, _p in service.ctx.server.broadcasts),
            timeout=5,
        )
        terminated = [p for _sid, m, p in service.ctx.server.broadcasts if m == "run.terminated"]
        assert terminated[0]["kind"] == "user"
    finally:
        await service.shutdown()


async def test_worker_crash_while_a_permission_is_pending_still_terminates_the_turn(
    tmp_path, monkeypatch
):
    """Round-1 review fix companion case: killing the worker outright (not
    `stop()`) while a permission is pending must also still reach
    `run.terminated` — this used to hang too, because `AcpClient`'s read loop was
    blocked awaiting the pending-permission handler and never got back to
    `readline()` to observe the closed pipe."""
    service = await _make_service(tmp_path, monkeypatch)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        await service.send(session_id, "NEEDS_PERMISSION do the risky thing")
        await _wait_until(lambda: len(service.ctx.server.events("permission.requested")) >= 1)

        worker = service.worker_manager.get(session_id)
        worker.process.kill()

        await _wait_until(
            lambda: any(m == "run.terminated" for _sid, m, _p in service.ctx.server.broadcasts),
            timeout=5,
        )
        terminated = [p for _sid, m, p in service.ctx.server.broadcasts if m == "run.terminated"]
        assert terminated[0]["kind"] == "error"
    finally:
        await service.shutdown()


async def test_streamed_assistant_text_is_kept_when_the_worker_errors_mid_turn(
    tmp_path, monkeypatch
):
    """Round-1 review fix: text already streamed via `message.delta` must not be
    lost when the Turn ends in error — `insert_assistant_message` writes an
    empty placeholder and only `_finalize_streamed_messages`
    (`queries.finalize_message`) fills in the real text, so the error path must
    call it too, not just the success/stop paths."""
    service = await _make_service(tmp_path, monkeypatch)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        # "normal" mode streams "Hel"+"lo" *before* handling a SLEEP_MS marker
        # (see fake_acp_agent.py's `_handle_normal_prompt`) — the sleep keeps the
        # prompt() call in flight long enough to reliably kill the worker after
        # the delta lands but before it would otherwise finish on its own.
        await service.send(session_id, "SLEEP_MS:500 hello there")
        await _wait_until(lambda: len(service.ctx.server.events("message.delta")) >= 1)

        worker = service.worker_manager.get(session_id)
        worker.process.kill()

        await _wait_until(
            lambda: any(m == "run.terminated" for _sid, m, _p in service.ctx.server.broadcasts),
            timeout=5,
        )
        terminated = [p for _sid, m, p in service.ctx.server.broadcasts if m == "run.terminated"]
        assert terminated[0]["kind"] == "error"

        completed = service.ctx.server.events("message.completed")
        assert completed, "the partially-streamed assistant message was never finalized"
        assert completed[0][1]["content"]["text"] == "Hello"

        messages = await service.turn_messages(session_id, limit=10)
        assistant_texts = [m["content"]["text"] for m in messages if m["role"] == "assistant"]
        assert "Hello" in assistant_texts
    finally:
        await service.shutdown()


async def test_send_racing_the_end_of_a_turn_never_strands_a_queued_message(
    tmp_path, monkeypatch
):
    """Round-1 review fix: `_advance_queue` used to run without holding the
    session lock, so a `send()` racing in right as a Turn finished could decide
    "running" from stale state (the finishing task isn't `.done()` yet), submit
    its own enqueue write *after* `_advance_queue`'s `pop_next_queue_item` had
    already run and found nothing — that message then sits 'pending' forever
    with no Turn ever started for it, since nothing calls `_advance_queue` again.

    Pins down that exact interleaving with real synchronization (a
    `threading.Event` gate inside a patched `pop_next_queue_item`, since the DB
    call runs on `run_in_db_thread`'s own worker thread) instead of hoping wall-
    clock sleeps land in the right order:
      1. let the first Turn's `_advance_queue()` call reach `pop_next_queue_item`
         and block there (proving, if unlocked, `_turn_tasks[session_id]` is
         still "not done" at this exact instant);
      2. only then start the second `send()` (concurrently, not awaited yet) and
         give it a moment to make its own "is this session running" decision and
         queue its DB write behind the still-blocked pop;
      3. only then unblock the pop — reproducing precisely "pop already
         committed to 'nothing to advance to', second message's write lands
         after" for the *unlocked* code. The fix (holding `self._lock` across
         that whole decision) makes `send()` block until `_advance_queue` is
         fully done deciding either way, so nothing is ever stranded regardless
         of which branch `send()` ends up taking."""
    service = await _make_service(tmp_path, monkeypatch)

    real_pop = queries.pop_next_queue_item
    pop_started = threading.Event()
    release_pop = threading.Event()

    def _gated_pop(conn, session_id):
        pop_started.set()
        release_pop.wait(timeout=5.0)
        return real_pop(conn, session_id)

    monkeypatch.setattr(queries, "pop_next_queue_item", _gated_pop)

    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        first = await service.send(session_id, "first turn")
        assert first["queued"] is False

        # Block here (off-loop, so the event loop keeps running everything else)
        # until `_advance_queue`'s `pop_next_queue_item` call has actually
        # started and is itself now blocked on `release_pop`.
        await asyncio.get_running_loop().run_in_executor(None, pop_started.wait, 5.0)
        assert pop_started.is_set(), "_advance_queue() never reached pop_next_queue_item"

        second_task = asyncio.create_task(service.send(session_id, "second turn"))
        # Give `send()` a moment to make its own running/not-running decision
        # (and, on the pre-fix code, submit its enqueue write behind the still-
        # blocked pop on the same single-worker DB thread) before we let the
        # pop proceed.
        await asyncio.sleep(0.05)
        release_pop.set()

        second = await second_task

        # If the race were still open, this message would stay 'pending' forever
        # with no Turn ever started for it, and this never reaches 2.
        await _wait_until(
            lambda: len(service.ctx.server.events("message.completed")) >= 2,
            timeout=5,
        )
        items = await service.queue(session_id)
        assert items == [], f"stranded in queue: {items!r} (queued={second['queued']!r})"
        user_texts = [
            m["content"]["text"]
            for m in await service.turn_messages(session_id, limit=20)
            if m["role"] == "user"
        ]
        assert user_texts == ["first turn", "second turn"]
    finally:
        await service.shutdown()


async def test_four_parallel_sessions_survive_one_worker_crash(tmp_path, monkeypatch):
    """Issue #10 acceptance: "4 个 Session 并行，kill 其一 worker 其余不受影响"."""
    service = await _make_service(tmp_path, monkeypatch)
    await service.worker_manager.start()
    try:
        session_ids = [await _new_session(service, title=f"s{i}") for i in range(4)]
        for sid in session_ids:
            await service.send(sid, f"SLEEP_MS:400 hello from {sid}")
        for sid in session_ids:
            await _wait_until(lambda sid=sid: service.worker_manager.get(sid) is not None)

        victim = session_ids[0]
        survivors = session_ids[1:]
        victim_worker = service.worker_manager.get(victim)
        victim_worker.process.kill()

        await _wait_until(lambda: service.worker_manager.get(victim) is None, timeout=5)

        # The other three workers are untouched — still the exact same worker
        # objects (no restart), still running.
        for sid in survivors:
            assert service.worker_manager.get(sid) is not None

        for sid in session_ids:
            await _wait_until(
                lambda sid=sid: any(
                    s == sid and m in ("run.terminated", "message.completed")
                    for s, m, _p in service.ctx.server.broadcasts
                ),
                timeout=5,
            )

        # The crashed session's Turn ended in error, not silently hung.
        victim_terminated = [
            p for sid, m, p in service.ctx.server.broadcasts
            if sid == victim and m == "run.terminated"
        ]
        assert victim_terminated and victim_terminated[0]["kind"] == "error"
    finally:
        await service.shutdown()


async def test_restart_marks_stale_runs_terminated_and_never_auto_resends_the_queue(
    tmp_path, monkeypatch
):
    """PRD 11.3 崩溃恢复 + PRD 9.2 "重启后队列恢复为「待发送」且 UI 提醒" — a Run/Turn
    left 'running' by a killed-mid-flight daemon must not still claim to be
    running after the next startup, and a pending queue item must stay pending
    (not silently re-sent) until a human resends it."""
    service = await _make_service(tmp_path, monkeypatch)

    # Simulate the previous process's state directly (no live worker involved):
    # one Run/Turn stuck 'running', one queue item still 'pending'.
    session_id = await service.ensure_main_session()

    def _seed(conn):
        turn_id, message_id, run_id = "turn-stale", "msg-stale", "run-stale"
        queries.create_turn_and_user_message(
            conn, turn_id=turn_id, message_id=message_id, session_id=session_id,
            text="orphaned by a crash", queued=False,
        )
        queries.create_run(conn, run_id=run_id, turn_id=turn_id, session_id=session_id)
        # `queue_items.turn_id` is a real FK (002 migration) — every queued item
        # has its own Turn row already created up front, same as `send()` does
        # (see 002's header note on why).
        queued_turn_id, queued_message_id = "turn-queued", "msg-queued"
        queries.create_turn_and_user_message(
            conn, turn_id=queued_turn_id, message_id=queued_message_id, session_id=session_id,
            text="still waiting", queued=True,
        )
        queries.enqueue(
            conn, session_id=session_id, turn_id=queued_turn_id, text="still waiting",
            attachments=None,
        )
        return run_id, turn_id

    run_id, turn_id = await run_in_db_thread(_seed, service.ctx.db)

    await service.startup()
    try:
        run_row = await service.run_get(run_id)
        assert run_row["status"] == "terminated"
        assert run_row["terminated_kind"] == "error"

        items = await service.queue(session_id)
        assert [i["state"] for i in items] == ["pending"]
        # No Turn was auto-started for the queued item — restart never resends
        # (PRD 9.2/G10/N04): the worker registry stays empty until a real send.
        assert service.worker_manager.get(session_id) is None
    finally:
        await service.shutdown()


async def test_provider_error_terminates_the_run_before_spawning_a_worker(tmp_path, monkeypatch):
    """02-w3-interfaces.md §2 集成收口 #4 (`pnpm e2e`'s daemon-side half): a
    Session whose Agent has no usable provider/Key terminates immediately with a
    `provider_error`-flavored `run.terminated{kind:"error"}` card — and never
    reaches `WorkerManager.ensure_started()` at all (asserted via `worker_cmd`
    pointing at a script that always fails to spawn cleanly, so if the pre-flight
    check didn't short-circuit first, this test would hang/fail for a different,
    confusing reason instead of cleanly proving the check ran)."""
    service = await _make_service(tmp_path, monkeypatch, providers=NullProviderResolver())
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="no-provider")
        await service.send(session_id, "hello")
        await _wait_until(lambda: len(service.ctx.server.events("run.terminated")) >= 1)

        terminated = service.ctx.server.events("run.terminated")
        assert terminated[0][1]["kind"] == "error"
        assert "provider_error" in terminated[0][1]["reason"]
        # The worker must never have been spawned for this Session.
        assert service.worker_manager.get(session_id) is None
    finally:
        await service.shutdown()


async def test_run_turn_survives_a_broken_config_resolver_and_reports_it(tmp_path, monkeypatch):
    """Review round-2 finding #4 / controller ruling R-H4: `ctx.config.
    mcp_servers(project_id)` is now resolved by `_run_turn` itself, off the
    event loop (`run_in_db_thread`) — this used to be `WorkerManager`'s job,
    called directly on the loop, which reproducibly raised `sqlite3.
    ProgrammingError` against any REAL `ConfigResolver` (not a test double)
    and was silently caught as a mere warning, so FR13's MCP wiring never
    actually took effect on the production path. This test exercises the
    degrade-gracefully contract that moved here (DEV.md 工程原则 #4: 诚实失败
    — a broken `mcp.json` must not block the worker from starting, but must
    not be silent either, R-H4 "不允许只 warning")."""

    class _BrokenConfig:
        def settings(self, project_id):
            return {}

        def permissions(self, project_id):
            return {}

        def mcp_servers(self, project_id):
            raise ValueError("simulated malformed mcp.json")

    service = await _make_service(tmp_path, monkeypatch)
    service.ctx.config = _BrokenConfig()
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="broken-mcp-config")
        await service.send(session_id, "hello")
        await _wait_until(
            lambda: any(
                m == "run.terminated" or m == "message.completed"
                for _sid, m, _p in service.ctx.server.broadcasts
            )
        )

        # The worker still started (a broken mcp.json degrades to zero MCP
        # servers, it never blocks the Turn) — no `run.terminated{kind:
        # "error"}` for this session.
        assert not [
            p for sid, m, p in service.ctx.server.broadcasts
            if m == "run.terminated" and sid == session_id
        ]
        assert service.worker_manager.get(session_id) is not None

        # But it must not be silent (R-H4): a real `daemon.error` reports it.
        down = [
            p for m, p in service.ctx.server.broadcast_alls
            if m == "daemon.error" and p["code"] == 1008
        ]
        assert down
        assert down[0]["detail"]["session_id"] == session_id
    finally:
        await service.shutdown()


async def test_tool_call_full_payload_is_written_and_fetchable_via_run_payload(
    tmp_path, monkeypatch
):
    """FR06 回放 full-fidelity payload (02-w3-interfaces.md §2): the truncated
    `result_summary` isn't the only copy of a Step's output — `payload_ref` points
    at a file under `runs/<run_id>/` holding the same `rawOutput`, fetchable via
    `run_payload()` (the RPC method a real replay UI calls)."""
    service = await _make_service(tmp_path, monkeypatch)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="payload")
        await service.send(session_id, "please USE_TOOL now")
        await _wait_until(lambda: len(service.ctx.server.events("step.completed")) >= 1)

        run_id = service.ctx.server.events("step.completed")[0][1]["run_id"]

        # The payload write is a background task (see `_handle_tool_call_update`'s
        # comment on why it must not be awaited inline) — poll for it rather than
        # assuming it's already landed by the time step.completed broadcast fired.
        async def _demo_step() -> dict[str, Any] | None:
            steps = await service.run_steps(run_id)
            return next((s for s in steps if s["tool"] == "demo_tool"), None)

        demo_step = None
        for _ in range(250):
            demo_step = await _demo_step()
            if demo_step and demo_step.get("payload_ref"):
                break
            await asyncio.sleep(0.02)
        assert demo_step is not None and demo_step.get("payload_ref"), "payload_ref never written"
        ref = demo_step["payload_ref"]
        assert ref.startswith(f"{run_id}/")

        payload = await service.run_payload(ref)
        assert payload["eof"] is True
        decoded = json.loads(base64.b64decode(payload["data_base64"]))
        assert decoded == {"ok": True}
    finally:
        await service.shutdown()


async def test_run_list_returns_a_sessions_runs_newest_first(tmp_path, monkeypatch):
    """`run.list` (02-w3-interfaces.md §2 契约新增 — 回放视图"选一个 Run"的数据
    源，00-foundation.md §4.1 原表没有任何列出历史 Run 的方法)."""
    service = await _make_service(tmp_path, monkeypatch)

    def _seed(conn):
        session_id = "sess-runlist"
        queries.create_session(
            conn, session_id=session_id, project_id=DEFAULT_PROJECT_ID,
            agent_id=DEFAULT_AGENT_ID, parent_id=None, is_main=False, mode="task",
            title="runlist",
        )
        for i in range(1, 4):
            turn_id, message_id, run_id = f"turn-rl{i}", f"msg-rl{i}", f"run-rl{i}"
            queries.create_turn_and_user_message(
                conn, turn_id=turn_id, message_id=message_id, session_id=session_id,
                text="x", queued=False,
            )
            queries.create_run(conn, run_id=run_id, turn_id=turn_id, session_id=session_id)
            # Three inserts in one tight loop can land in the same millisecond
            # (`iso_now()`'s resolution) — force distinct, strictly increasing
            # `created_at` so "newest first" below isn't racing a timestamp tie.
            conn.execute(
                "UPDATE runs SET created_at = ? WHERE id = ?",
                (f"2026-01-01T00:00:0{i}.000Z", run_id),
            )
        conn.commit()
        return session_id

    session_id = await run_in_db_thread(_seed, service.ctx.db)

    runs = await service.run_list(session_id)
    assert [r["id"] for r in runs] == ["run-rl3", "run-rl2", "run-rl1"]  # newest first
    assert all(r["session_id"] == session_id for r in runs)

    limited = await service.run_list(session_id, limit=1)
    assert [r["id"] for r in limited] == ["run-rl3"]


async def test_run_steps_pagination(tmp_path, monkeypatch):
    """02-w3-interfaces.md §2 集成收口: `run.steps` 分页 — `after_seq`/`limit`
    page forward through a Run's Steps in `seq` order."""
    service = await _make_service(tmp_path, monkeypatch)

    def _seed(conn):
        session_id = "sess-paginate"
        queries.create_session(
            conn, session_id=session_id, project_id=DEFAULT_PROJECT_ID,
            agent_id=DEFAULT_AGENT_ID, parent_id=None, is_main=False, mode="task",
            title="paginate",
        )
        turn_id, message_id, run_id = "turn-p", "msg-p", "run-p"
        queries.create_turn_and_user_message(
            conn, turn_id=turn_id, message_id=message_id, session_id=session_id,
            text="x", queued=False,
        )
        queries.create_run(conn, run_id=run_id, turn_id=turn_id, session_id=session_id)
        for i in range(1, 6):
            queries.insert_step(
                conn, step_id=f"step-{i}", run_id=run_id, seq=i, tool=f"tool{i}",
                args={}, status="completed",
            )
        return run_id

    run_id = await run_in_db_thread(_seed, service.ctx.db)

    first_page = await service.run_steps(run_id, limit=2)
    assert [s["seq"] for s in first_page] == [1, 2]

    second_page = await service.run_steps(run_id, after_seq=2, limit=2)
    assert [s["seq"] for s in second_page] == [3, 4]

    rest = await service.run_steps(run_id, after_seq=4)
    assert [s["seq"] for s in rest] == [5]

    everything = await service.run_steps(run_id)
    assert [s["seq"] for s in everything] == [1, 2, 3, 4, 5]


async def test_terminated_run_records_the_step_seq_it_died_at(tmp_path, monkeypatch):
    """FR06 回放"终止记录" (02-w3-interfaces.md §2): `_terminate_run` writes
    `terminated_step_seq` from `ctx_turn.step_seq` — the seq of the last Step
    that had started on this Run when it ended (0/None if none had). Calls
    `_terminate_run` directly against a real `_TurnContext` (rather than trying
    to race a fake worker's timing to catch a Step mid-flight, which every other
    "worker dies mid-turn" test in this file avoids too, e.g. by killing the
    process outright once a known state is reached) — `mark_run_terminated`'s
    SQL and the `run_get` round-trip are exactly the same code path either way."""
    from jones_daemon.sessions.service import _TurnContext  # noqa: PLC0415 - test-only import

    service = await _make_service(tmp_path, monkeypatch)
    session_id = await service.ensure_main_session()

    def _seed(conn):
        turn_id, message_id, run_id = "turn-dies", "msg-dies", "run-dies"
        queries.create_turn_and_user_message(
            conn, turn_id=turn_id, message_id=message_id, session_id=session_id,
            text="x", queued=False,
        )
        queries.create_run(conn, run_id=run_id, turn_id=turn_id, session_id=session_id)
        return run_id, turn_id

    run_id, turn_id = await run_in_db_thread(_seed, service.ctx.db)
    ctx_turn = _TurnContext(turn_id=turn_id, run_id=run_id, session_id=session_id)
    ctx_turn.step_seq = 3  # as if 3 Steps had started before this Run died

    await service._terminate_run(ctx_turn, kind="error", reason="simulated worker crash")

    run_row = await service.run_get(run_id)
    assert run_row["terminated_kind"] == "error"
    assert run_row["terminated_step_seq"] == 3


async def test_terminated_run_with_no_steps_started_records_no_step_seq(tmp_path, monkeypatch):
    """The same path, but for a Run that never started any Step before dying
    (e.g. the provider pre-flight check, or a worker that never even makes it to
    a first tool call) — `terminated_step_seq` must be NULL, not a fabricated 0
    that would misleadingly look like "Step #0 was in flight" (DEV.md 工程原则
    #4 诚实失败)."""
    from jones_daemon.sessions.service import _TurnContext  # noqa: PLC0415 - test-only import

    service = await _make_service(tmp_path, monkeypatch)
    session_id = await service.ensure_main_session()

    def _seed(conn):
        turn_id, message_id, run_id = "turn-dies2", "msg-dies2", "run-dies2"
        queries.create_turn_and_user_message(
            conn, turn_id=turn_id, message_id=message_id, session_id=session_id,
            text="x", queued=False,
        )
        queries.create_run(conn, run_id=run_id, turn_id=turn_id, session_id=session_id)
        return run_id, turn_id

    run_id, turn_id = await run_in_db_thread(_seed, service.ctx.db)
    ctx_turn = _TurnContext(turn_id=turn_id, run_id=run_id, session_id=session_id)

    await service._terminate_run(ctx_turn, kind="error", reason="never got anywhere")

    run_row = await service.run_get(run_id)
    assert run_row["terminated_step_seq"] is None


@pytest.mark.parametrize("_run", range(3))
async def test_send_to_first_message_delta_latency_measurement(tmp_path, monkeypatch, _run):
    """Records the real send() -> first `message.delta` broadcast latency
    against the fake agent for the PR report (01-w2-interfaces.md §7). This is
    the daemon-side path only (SessionService + WorkerManager + AcpClient), not
    a real LLM's time-to-first-token — see the report for that caveat."""
    import time

    service = await _make_service(tmp_path, monkeypatch)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title=f"lat-{_run}")
        t0 = time.monotonic()
        await service.send(session_id, "hello there")
        await _wait_until(lambda: len(service.ctx.server.events("message.delta")) >= 1, timeout=5)
        elapsed = time.monotonic() - t0
        assert elapsed < 5.0
    finally:
        await service.shutdown()


# -- 第 1 轮评审修复的新增测试 (docs/design/02-w3-interfaces.md §2, PR 报告"第 1 轮
# 修复记录") -----------------------------------------------------------------


async def test_run_steps_includes_the_steps_permission_decision(tmp_path, monkeypatch):
    """G07 回放"审批结果" (PRD 12.1 / 02-w3-interfaces.md §2): `run.steps` used to
    leave a caller with only `permission_id` — the `permission_decisions` row's
    *primary key*, not the actual allow/deny outcome — and there was no RPC path
    back to that row at all otherwise. `queries.list_run_steps` now LEFT JOINs
    `permission_decisions` and surfaces `permission_decision`/
    `permission_decided_by` directly on each Step row (ReplayView.tsx renders
    those, not `permission_id`)."""

    def _seed(conn):
        session_id = "sess-g07"
        queries.create_session(
            conn, session_id=session_id, project_id=DEFAULT_PROJECT_ID,
            agent_id=DEFAULT_AGENT_ID, parent_id=None, is_main=False, mode="task", title="g07",
        )
        queries.create_turn_and_user_message(
            conn, turn_id="turn-g07", message_id="msg-g07", session_id=session_id,
            text="x", queued=False,
        )
        queries.create_run(conn, run_id="run-g07", turn_id="turn-g07", session_id=session_id)
        # Step 1: triggered a permission gate that was decided.
        queries.insert_step(
            conn, step_id="step-g07-decided", run_id="run-g07", seq=1, tool="risky_tool",
            args={}, status="completed",
        )
        queries.insert_permission_decision(
            conn, decision_id="dec-g07", step_id="step-g07-decided", gate="user",
            risk="unclassified", request={"toolCall": {"toolCallId": "tc"}},
        )
        queries.decide_permission(conn, "dec-g07", decision="deny", decided_by="user")
        # Step 2: never touched a permission gate at all — must not fabricate a
        # decision for it (DEV.md 工程原则 #4 诚实失败).
        queries.insert_step(
            conn, step_id="step-g07-none", run_id="run-g07", seq=2, tool="quiet_tool",
            args={}, status="completed",
        )
        return "run-g07"

    service = await _make_service(tmp_path, monkeypatch)
    run_id = await run_in_db_thread(_seed, service.ctx.db)

    steps = await service.run_steps(run_id)
    assert [s["id"] for s in steps] == ["step-g07-decided", "step-g07-none"]

    decided, none = steps
    assert decided["permission_id"] == "dec-g07"
    assert decided["permission_decision"] == "deny"
    assert decided["permission_decided_by"] == "user"

    assert none["permission_id"] is None
    assert none["permission_decision"] is None
    assert none["permission_decided_by"] is None


async def test_tool_call_update_serializes_raw_output_only_once(tmp_path, monkeypatch):
    """性能修复 (02-w3-interfaces.md §3 "回放 payload 写入异步、不阻塞 ACP 读循环")：
    `_handle_tool_call_update` used to `json.dumps(raw_output, ...)` once for the
    truncated `result_summary`, then `_write_step_payload` dumped the very same
    object a *second*, independent time — on the event loop thread, before ever
    reaching `asyncio.to_thread` (CPython's C json encoder holds the GIL
    regardless), roughly doubling the CPU the ACP read loop pays per
    `tool_call_update` for a large payload. Counts only `json.dumps` calls whose
    first argument *is* (identity, not equality) this test's own `raw_output`
    object, so the assertion stays correct even with unrelated `json.dumps`
    calls happening concurrently on other DB-thread writes."""
    service = await _make_service(tmp_path, monkeypatch)
    session_id = await service.ensure_main_session()

    def _seed(conn):
        queries.create_turn_and_user_message(
            conn, turn_id="turn-ser", message_id="msg-ser", session_id=session_id,
            text="x", queued=False,
        )
        queries.create_run(conn, run_id="run-ser", turn_id="turn-ser", session_id=session_id)

    await run_in_db_thread(_seed, service.ctx.db)

    from jones_daemon.sessions.service import _TurnContext  # noqa: PLC0415 - test-only import

    ctx_turn = _TurnContext(turn_id="turn-ser", run_id="run-ser", session_id=session_id)
    tool_call_id = "tc-ser"
    await service._handle_tool_call_start(
        ctx_turn,
        {"toolCallId": tool_call_id, "title": "demo", "status": "pending", "rawInput": {}},
    )

    raw_output = {"big": "x" * 100}
    real_dumps = json.dumps
    calls = 0

    def counting_dumps(obj, *args, **kwargs):
        nonlocal calls
        if obj is raw_output:
            calls += 1
        return real_dumps(obj, *args, **kwargs)

    monkeypatch.setattr(json, "dumps", counting_dumps)

    await service._handle_tool_call_update(
        ctx_turn, {"toolCallId": tool_call_id, "status": "completed", "rawOutput": raw_output}
    )
    # The payload write is scheduled via `asyncio.create_task`, not awaited
    # inline (see `_handle_tool_call_update`'s own comment) — wait for it.
    tasks = list(service._background_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    assert calls == 1, f"json.dumps(raw_output, ...) ran {calls} times, expected exactly 1"

    expected = real_dumps(raw_output, default=str, ensure_ascii=False)
    step = await run_in_db_thread(
        queries.get_step, service.ctx.db, ctx_turn.tool_call_steps[tool_call_id]
    )
    assert step["result_summary"] == expected[:4000]
    assert step["payload_ref"] is not None
    payload = await service.run_payload(step["payload_ref"])
    assert base64.b64decode(payload["data_base64"]).decode("utf-8") == expected


async def test_cwd_for_project_uses_the_real_projects_own_path(tmp_path, monkeypatch):
    """02-w3-interfaces.md §2 集成收口 #1: the only existing test that touches
    `_cwd_for_project` (`test_unexpected_exception_in_run_turn_still_terminates_
    the_run`) monkeypatches it to raise — it exercises the failure-handling
    path but never asserts what a *real*, non-default Project's own directory
    resolves to, which is the entire point of this collapse item (replacing a
    hardcoded home-directory special case with `ProjectService.get()`)."""
    from jones_daemon.projects.service import ProjectService  # noqa: PLC0415

    service = await _make_service(tmp_path, monkeypatch)
    other_dir = tmp_path / "other-project"
    other_dir.mkdir()
    project = await run_in_db_thread(ProjectService(service.ctx.db).create, str(other_dir))
    assert project["id"] != DEFAULT_PROJECT_ID

    cwd = await service._cwd_for_project(project["id"])
    assert cwd == str(other_dir.resolve())

    default_cwd = await service._cwd_for_project(DEFAULT_PROJECT_ID)
    assert default_cwd != cwd


async def test_run_turn_writes_prompt_snapshot_ref_into_the_runs_row(tmp_path, monkeypatch):
    """FR06 回放 (02-w3-interfaces.md §2): `test_replay_store.py` only unit-tests
    `write_prompt_snapshot` writing a file — nothing asserted that `_run_turn`
    actually persists the resulting ref onto `runs.prompt_snapshot_ref` (a
    best-effort write that only logs on failure, exactly the kind of code whose
    most likely failure mode is silently never running at all, not raising)."""
    service = await _make_service(tmp_path, monkeypatch)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="snap")
        await service.send(session_id, "hello there")
        await _wait_until(
            lambda: any(
                m in ("run.terminated", "message.completed")
                for _sid, m, _p in service.ctx.server.broadcasts
            )
        )
        run_id = (await service.get(session_id))["latest_turn"]["run_id"]
        run_row = await service.run_get(run_id)
        assert run_row["prompt_snapshot_ref"] is not None
        assert run_row["prompt_snapshot_ref"].startswith(f"{run_id}/")

        snapshot = await service.run_payload(run_row["prompt_snapshot_ref"])
        decoded = json.loads(base64.b64decode(snapshot["data_base64"]))
        assert decoded["run_id"] == run_id
        assert decoded["user_message"] == "hello there"
    finally:
        await service.shutdown()


async def test_daemon_status_reports_real_active_sessions_and_worker_counts(tmp_path, monkeypatch):
    """02-w3-interfaces.md §2 集成收口 #2: `test_rpc.py`'s `daemon.status` test
    only ever exercises `register_builtin_methods`'s honest-zero placeholder (it
    never calls `register_daemon_status`) — nothing asserted that the live
    `sessions_active`/`workers` counts `register_daemon_status` wires up actually
    move off zero for a real running Turn/worker, only that the placeholder
    stays zero forever."""
    from jones_daemon.rpc.methods import register_daemon_status  # noqa: PLC0415
    from jones_daemon.rpc.server import RpcServer  # noqa: PLC0415

    service = await _make_service(tmp_path, monkeypatch)
    await service.worker_manager.start()
    server = RpcServer(None)
    register_daemon_status(server, service)
    handler = server._methods["daemon.status"]
    try:
        idle = await handler({}, None)
        assert idle["sessions_active"] == 0
        assert idle["workers"] == 0

        session_id = await _new_session(service, title="status")
        await service.send(session_id, "SLEEP_MS:300 hello there")
        # A Session is "active" from the moment its Turn starts, but its worker
        # process is spawned *inside* that Turn (`_run_turn` → `ensure_started`)
        # — so "active session, zero workers" is a real transient state, not a
        # bug. Wait for both before asserting the busy snapshot.
        await _wait_until(
            lambda: service.active_turn_session_ids() != []
            and service.worker_manager.worker_count() >= 1
        )

        busy = await handler({}, None)
        assert busy["sessions_active"] == 1
        assert busy["workers"] >= 1

        await _wait_until(
            lambda: any(m == "run.terminated" or m == "message.completed"
                        for _sid, m, _p in service.ctx.server.broadcasts),
            timeout=5,
        )
        await _wait_until(lambda: service.active_turn_session_ids() == [])
        after = await handler({}, None)
        assert after["sessions_active"] == 0
    finally:
        await service.shutdown()
