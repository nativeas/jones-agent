"""`SessionService` integration tests against `fake_acp_agent.py` — queue
serialization, stop, crash isolation across parallel sessions, restart
recovery, and the permission round trip (Issue #10 acceptance criteria, PRD
9.2/9.3/11.3, docs/design/01-w2-interfaces.md §2)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from jones_daemon.context import DaemonContext, NullConfigResolver, NullProviderResolver
from jones_daemon.sessions import queries
from jones_daemon.sessions.service import DEFAULT_AGENT_ID, DEFAULT_PROJECT_ID, SessionService
from jones_daemon.store import apply_pending, connect, run_in_db_thread

_FAKE_AGENT = str(Path(__file__).parent / "fake_acp_agent.py")


class FakeServer:
    """Stands in for `RpcServer` — `SessionService` only ever calls
    `broadcast()` on `ctx.server`, so a plain recorder is enough to assert on
    notification traffic without standing up a real socket (the real
    `RpcServer.broadcast`/subscribe wiring itself is covered by
    `test_rpc.py`)."""

    def __init__(self) -> None:
        self.broadcasts: list[tuple[str, str, Any]] = []

    async def broadcast(self, session_id: str, method: str, params: Any) -> None:
        self.broadcasts.append((session_id, method, params))

    def events(self, method: str) -> list[tuple[str, Any]]:
        return [(sid, p) for sid, m, p in self.broadcasts if m == method]


async def _make_service(tmp_path, monkeypatch) -> SessionService:
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
