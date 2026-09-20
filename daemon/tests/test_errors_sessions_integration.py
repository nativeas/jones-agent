"""`SessionService` integration tests for Issue #22 (FR14 错误面板, 04-w5-
interfaces.md §4): `_terminate_run`'s ErrorCard classification, `_on_worker_
crash`'s N07 5s watchdog, and the `session.retry` RPC (retry/model_override/
abandon — `switch_model` as a card action was withdrawn round-2, see
`errors/classify.py::_ACTIONS`'s comment; `model_override` is still a valid
`session.retry` RPC parameter, just not offered via any card's `actions`
today). Follows `test_sessions_service.py`'s fixture pattern (own local copy,
not a cross-file import — see that file's module docstring for the underlying
`fake_acp_agent.py` this also drives).

G08 (four fault injections, all offline): 断网/Key 失效 are simulated with a stub
`ProviderResolver` (`_run_turn`'s `ctx.providers.resolve()` pre-flight — no ACP
round trip needed at all for these two); 工具抛异常/kill worker are simulated
with `fake_acp_agent.py`'s `TOOL_EXCEPTION` prompt marker and a real
`process.kill()`, matching how every other crash-flavored test in
`test_sessions_service.py` does it."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

from jones_daemon.context import DaemonContext, NullConfigResolver
from jones_daemon.context import ProviderResolver as ProviderResolverProtocol
from jones_daemon.errors.classify import ErrorKind
from jones_daemon.projects.bootstrap import bootstrap_projects_and_agents
from jones_daemon.providers.resolver import ProviderNotConfiguredError
from jones_daemon.sessions import service as service_module
from jones_daemon.sessions.service import DEFAULT_AGENT_ID, DEFAULT_PROJECT_ID, SessionService
from jones_daemon.store import apply_pending, connect, run_in_db_thread

_FAKE_AGENT = str(Path(__file__).parent / "fake_acp_agent.py")


class _StubProviderResolver(ProviderResolverProtocol):
    """Always succeeds — same role as `test_sessions_service.py`'s own stub,
    duplicated locally rather than imported (see this file's module docstring)."""

    def resolve(self, model_pref: dict[str, Any] | None) -> Any:
        return {"provider": "anthropic", "model": "claude-test", "env": {}, "hermes_config": {}}

    def list_models(self, provider: str | None) -> list[dict[str, Any]]:
        return []


class _RaisingProviderResolver(ProviderResolverProtocol):
    """G08 "断网"/"Key 失效" fault injection: a `ProviderResolver` whose
    `resolve()` always raises with the given message — `_run_turn`'s existing
    (untouched by this branch) `except (RpcError, ProviderNotConfiguredError)`
    pre-flight check turns this into `_terminate_run(kind="error",
    reason=f"provider_error: {message}")` before any worker is even spawned, so
    these two fault injections need no ACP round trip / subprocess at all."""

    def __init__(self, message: str) -> None:
        self._message = message

    def resolve(self, model_pref: dict[str, Any] | None) -> Any:
        raise ProviderNotConfiguredError(self._message)

    def list_models(self, provider: str | None) -> list[dict[str, Any]]:
        return []


class _SlowRaisingProviderResolver(_RaisingProviderResolver):
    """R-N4's budget-suspend test needs a real "this Session is running"
    window a second `send()` can reliably observe — `_RaisingProviderResolver`
    alone fails so fast (a single `run_in_db_thread` round trip, no ACP
    round trip at all) that it's often already terminated again before a
    poll loop can ever catch it as "running". `resolve()` here runs on
    `run_in_db_thread`'s own executor thread, not the event loop, so blocking
    it briefly costs nothing the test needs and doesn't stall anything else."""

    def __init__(self, message: str, *, delay_s: float = 0.15) -> None:
        super().__init__(message)
        self._delay_s = delay_s

    def resolve(self, model_pref: dict[str, Any] | None) -> Any:
        time.sleep(self._delay_s)
        return super().resolve(model_pref)


class FakeServer:
    def __init__(self) -> None:
        self.broadcasts: list[tuple[str, str, Any]] = []

    async def broadcast(self, session_id: str, method: str, params: Any) -> None:
        self.broadcasts.append((session_id, method, params))

    def events(self, method: str) -> list[tuple[str, Any]]:
        return [(sid, p) for sid, m, p in self.broadcasts if m == method]


async def _make_service(
    tmp_path,
    monkeypatch,
    *,
    providers: ProviderResolverProtocol | None = None,
    clock: Any = None,
) -> SessionService:
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
        db=conn,
        paths=paths,
        server=FakeServer(),
        providers=providers if providers is not None else _StubProviderResolver(),
        config=NullConfigResolver(),
    )
    kwargs: dict[str, Any] = {}
    if clock is not None:
        # R-N8 (controller ruling, round-6, 2026-09-20): lets a duration-cap
        # test drive `_check_run_durations` deterministically — see
        # `SessionService.__init__`'s own comment on `self._clock`.
        kwargs["clock"] = clock
    service = SessionService(ctx, worker_cmd=[sys.executable, _FAKE_AGENT], **kwargs)
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




def _terminated(service: SessionService) -> list[dict[str, Any]]:
    return [p for _sid, m, p in service.ctx.server.broadcasts if m == "run.terminated"]


def _queue_changed_events(service: SessionService) -> list[dict[str, Any]]:
    return [p for _sid, m, p in service.ctx.server.broadcasts if m == "queue.changed"]


async def _queue_items(service: SessionService, session_id: str) -> list[dict[str, Any]]:
    """R-N9 (controller ruling, round-6, 2026-09-20): `SessionService.queue()`
    now returns `{items, suspended, reason}` (see that method's own
    docstring), not a bare list — most of this file's existing assertions
    only ever cared about the items, so this is the one place that unwraps
    it rather than touching every call site's own assertion shape."""
    return (await service.queue(session_id))["items"]


async def _wait_for_queue_suspended(service: SessionService, *, suspended: bool) -> None:
    """R-N4: `_advance_queue`'s `queue.changed` broadcast (the one carrying
    `suspended`/`reason`) is a few `await`s AFTER `run.terminated` fires (it
    happens in the just-terminated Turn's own `finally` block, released from
    the lock, then the DB read, then the broadcast) — waiting only on
    `run.terminated` and then immediately reading `_queue_changed_events`
    synchronously is a real, observed-in-practice race, not a hypothetical
    one. Poll for the specific broadcast this test actually needs instead."""
    await _wait_until(
        lambda: any(
            e.get("suspended") is suspended for e in _queue_changed_events(service)
        ),
        timeout=5,
    )


# ---------------------------------------------------------------------------
# G08 fault injection #1: 断网 (provider resolution raises a network error)
# ---------------------------------------------------------------------------


async def test_g08_network_fault_produces_a_network_card_and_no_crash(tmp_path, monkeypatch):
    service = await _make_service(
        tmp_path, monkeypatch, providers=_RaisingProviderResolver("Connection refused")
    )
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        result = await service.send(session_id, "hello")
        assert result["queued"] is False
        await _wait_until(lambda: len(_terminated(service)) >= 1)

        card = _terminated(service)[0]
        assert card["kind"] == "error"  # network is NOT a budget-flavored kind
        assert "turn_id" in card and card["turn_id"]
        assert card["card"]["kind"] == ErrorKind.NETWORK.value
        # Round-2 review fix (#1): `switch_model` is withdrawn repo-wide (see
        # `errors/classify.py::_ACTIONS`'s comment) — it never reaches the
        # spawned worker, so offering it would be a no-op dressed up as a fix.
        assert set(card["card"]["actions"]) == {"retry", "abandon"}
        assert card["card"]["retryable"] is True
        # No worker was ever even spawned for this fault — UI never showed
        # "running" against anything real to begin with (N07's stronger claim is
        # exercised separately below).
        assert service.worker_manager.get(session_id) is None
    finally:
        await service.shutdown()


# ---------------------------------------------------------------------------
# G08 fault injection #2: Key 失效 (provider resolution raises an auth error)
# ---------------------------------------------------------------------------


async def test_g08_key_invalid_fault_produces_a_provider_auth_card(tmp_path, monkeypatch):
    resolver = _RaisingProviderResolver("401 Unauthorized: invalid_api_key")
    service = await _make_service(tmp_path, monkeypatch, providers=resolver)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        await service.send(session_id, "hello")
        await _wait_until(lambda: len(_terminated(service)) >= 1)

        card = _terminated(service)[0]
        assert card["kind"] == "error"
        assert card["card"]["kind"] == ErrorKind.PROVIDER_AUTH.value
        # Retrying unchanged can't succeed — a bare "retry" would be dishonest.
        # Round-2 review fix (#1): nor is `switch_model` offered any more (it
        # never reaches the spawned worker) — this card's only action is
        # "abandon", with an honest hint in `message` telling the user what to
        # actually do (go change the Key/model in settings).
        assert card["card"]["actions"] == ["abandon"]
        assert card["card"]["retryable"] is False
        assert "去设置页换 Key / 换默认模型后重发" in card["card"]["message"]
        # G03/N02: the raw excerpt must never carry a real key verbatim even
        # though this stub's message happens not to include one — the
        # `raw_excerpt` field itself must always be present and bounded.
        assert len(card["card"]["raw_excerpt"]) <= 2048
    finally:
        await service.shutdown()


async def test_terminate_run_redacts_the_reason_persisted_and_broadcast_too(
    tmp_path, monkeypatch
):
    """Round-1 review fix (#6): `card.message`/`card.raw_excerpt` were already
    built from a redacted copy of `reason`, but `_terminate_run` used to pass
    the *raw* `reason` on to both `mark_run_terminated` (persisted verbatim
    into `runs.terminated_reason`, later included whole in `session.export`)
    and the `run.terminated` broadcast's own `reason` field — leaking a real
    key sitting right next to the (correctly redacted) card in the very same
    payload."""
    resolver = _RaisingProviderResolver(
        "401 Unauthorized: key=sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    )
    service = await _make_service(tmp_path, monkeypatch, providers=resolver)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        await service.send(session_id, "hello")
        await _wait_until(lambda: len(_terminated(service)) >= 1)

        event = _terminated(service)[0]
        assert "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in event["reason"]
        assert "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in event["card"]["message"]

        run_row = await run_in_db_thread(
            service_module.queries.get_run, service.ctx.db, event["run_id"]
        )
        assert (
            "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
            not in run_row["terminated_reason"]
        )
    finally:
        await service.shutdown()


# ---------------------------------------------------------------------------
# G08 fault injection #3: 工具抛异常 (ACP tool_call returns an error)
# ---------------------------------------------------------------------------


async def test_g08_tool_exception_produces_a_tool_exception_card(tmp_path, monkeypatch):
    service = await _make_service(tmp_path, monkeypatch)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        await service.send(session_id, "TOOL_EXCEPTION please")
        await _wait_until(lambda: len(_terminated(service)) >= 1, timeout=5)

        card = _terminated(service)[0]
        assert card["kind"] == "error"
        assert card["card"]["kind"] == ErrorKind.TOOL_EXCEPTION.value
        assert card["card"]["step_seq"] == 1  # the one (failed) Step this Run ran
        assert "retry" in card["card"]["actions"]
        # The daemon process (this test process, via the service) never raised —
        # the whole point of G08 is "the daemon doesn't crash".
    finally:
        await service.shutdown()


# ---------------------------------------------------------------------------
# G08 fault injection #4: kill worker — N07 "≤5s 被发现"
# ---------------------------------------------------------------------------


async def test_g08_kill_worker_terminates_within_5s_via_the_normal_acp_path(tmp_path, monkeypatch):
    """The common case (AcpClient's read loop notices the closed pipe on its
    own, see `_on_worker_crash`'s docstring) — already covered structurally by
    `test_sessions_service.py`'s crash tests; asserted again here with the
    ErrorCard shape this branch actually adds."""
    service = await _make_service(tmp_path, monkeypatch)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        await service.send(session_id, "SLEEP_MS:400 hello")
        await _wait_until(lambda: service.worker_manager.get(session_id) is not None)

        worker = service.worker_manager.get(session_id)
        worker.process.kill()

        await _wait_until(lambda: len(_terminated(service)) >= 1, timeout=5)
        card = _terminated(service)[0]
        assert card["kind"] == "error"
        assert card["card"]["kind"] == ErrorKind.WORKER_CRASH.value
        assert "retry" in card["card"]["actions"]
    finally:
        await service.shutdown()


async def test_worker_crash_watchdog_force_terminates_when_the_prompt_call_never_reports_it(
    tmp_path, monkeypatch
):
    """N07's actual backstop path: manufactures the exact race
    `_on_worker_crash`'s docstring describes (the in-flight `prompt()` call
    never notices) by registering an active turn whose "worker" is a task that
    just hangs forever, then calling `_on_worker_crash` directly — the grace/
    poll constants are monkeypatched down so this test doesn't take 5 real
    seconds."""
    monkeypatch.setattr(service_module, "_WORKER_CRASH_GRACE_S", 0.2)
    monkeypatch.setattr(service_module, "_WORKER_CRASH_POLL_INTERVAL_S", 0.02)
    service = await _make_service(tmp_path, monkeypatch)
    try:
        session_id = await _new_session(service, title="s1")
        run_id = "run_test01"
        turn_id = "turn_test01"
        await run_in_db_thread(
            service_module.queries.create_turn_and_user_message,
            service.ctx.db,
            turn_id=turn_id,
            message_id="msg_test01",
            session_id=session_id,
            text="hello",
            queued=False,
        )
        await run_in_db_thread(
            service_module.queries.create_run,
            service.ctx.db,
            run_id=run_id,
            turn_id=turn_id,
            session_id=session_id,
        )
        ctx_turn = service_module._TurnContext(
            turn_id=turn_id, run_id=run_id, session_id=session_id
        )
        service._active_turns[session_id] = ctx_turn

        hung = asyncio.get_running_loop().create_future()
        stuck_task = asyncio.create_task(asyncio.wait_for(hung, timeout=None))
        service._turn_tasks[session_id] = stuck_task

        await service._on_worker_crash(session_id, 137)
        # `task.cancel()` (called last, R-N10 — after `_finalize_streamed_
        # messages`/`_terminate_run`, see `_on_worker_crash`'s own comment)
        # only *schedules* delivery of `CancelledError`, it doesn't
        # synchronously run it — this extra yield gives the event loop one
        # more chance to actually mark the task cancelled before the
        # assertion below reads it.
        await asyncio.sleep(0)

        assert len(_terminated(service)) == 1
        card = _terminated(service)[0]
        assert card["kind"] == "error"
        assert card["card"]["kind"] == ErrorKind.WORKER_CRASH.value
        assert card["turn_id"] == turn_id
        assert stuck_task.cancelled() or stuck_task.done()
    finally:
        hung.cancel()
        await service.shutdown()


async def test_worker_crash_watchdog_terminates_before_cancelling_the_stuck_task(
    tmp_path, monkeypatch
):
    """R-N10 (controller ruling, round-6, 2026-09-20) supersedes round-N2's
    (R-N1) "cancel before terminate" ordering, which this test used to lock in
    (`order == ["cancel", "terminate"]`, and before that, round-2's own
    "terminate before cancel" the round-N2 fix had reverted). R-N1's write/
    broadcast idempotency split (`_terminate_run`, see `test_terminate_run_
    rebroadcasts_after_a_racing_write_gets_cancelled` below) already made
    winning the "which coroutine's DB write lands first" race through call
    ordering unnecessary — a second, losing write just no-ops or rebroadcasts
    — so round-N2's own reason for putting `cancel` first no longer applies.
    What cancel-first left open instead (`_advance_queue`'s own comment, R-N4):
    a just-cancelled `_run_turn` task could reach `_advance_queue` and read
    `ctx_turn.terminated_kind` as still `None` before this watchdog got back
    around to `_terminate_run` (which sets it synchronously) — silently
    auto-advancing a worker_crash termination instead of suspending the queue
    for it (PRD 9.3). R-N10 reorders to terminate-then-cancel (mirroring R-N7's
    `_terminate_run_for_exceeded_budget`), which this test now locks in
    instead: `terminated_kind` is guaranteed set before this function ever
    reaches `task.cancel()`. The TASK CAPTURE (`self._turn_tasks.get(session_
    id)`, still immediately after the `current_run` status read, no `await` in
    between) is unchanged — round-N2's identity fix for THAT is still exactly
    what's needed and isn't what moved this round."""
    monkeypatch.setattr(service_module, "_WORKER_CRASH_GRACE_S", 0.2)
    monkeypatch.setattr(service_module, "_WORKER_CRASH_POLL_INTERVAL_S", 0.02)
    service = await _make_service(tmp_path, monkeypatch)
    order: list[str] = []
    try:
        session_id = await _new_session(service, title="s1")
        run_id = "run_order01"
        turn_id = "turn_order01"
        await run_in_db_thread(
            service_module.queries.create_turn_and_user_message,
            service.ctx.db,
            turn_id=turn_id,
            message_id="msg_order01",
            session_id=session_id,
            text="hello",
            queued=False,
        )
        await run_in_db_thread(
            service_module.queries.create_run,
            service.ctx.db,
            run_id=run_id,
            turn_id=turn_id,
            session_id=session_id,
        )
        ctx_turn = service_module._TurnContext(
            turn_id=turn_id, run_id=run_id, session_id=session_id
        )
        service._active_turns[session_id] = ctx_turn

        hung = asyncio.get_running_loop().create_future()
        stuck_task = asyncio.create_task(asyncio.wait_for(hung, timeout=None))

        # `asyncio.Task` is a C (`_asyncio`) type — its `cancel` can't be
        # monkeypatched, per-instance or on the class. `_on_worker_crash` only
        # ever calls `.done()`/`.cancel()` on whatever it finds in
        # `self._turn_tasks[session_id]` (it never `isinstance`-checks that
        # it's a real `Task`, and this direct-call test style — like the other
        # watchdog tests above — sets that dict entry itself rather than going
        # through `_start_turn`), so a thin duck-typed proxy recording call
        # order is enough, without needing a real Task to be patchable.
        class _OrderTrackingTaskProxy:
            def done(self) -> bool:
                return stuck_task.done()

            def cancel(self, *args: Any, **kwargs: Any) -> bool:
                order.append("cancel")
                return stuck_task.cancel(*args, **kwargs)

        service._turn_tasks[session_id] = _OrderTrackingTaskProxy()  # type: ignore[assignment]

        original_terminate = service._terminate_run

        async def _spy_terminate(*args: Any, **kwargs: Any) -> None:
            await original_terminate(*args, **kwargs)
            order.append("terminate")

        monkeypatch.setattr(service, "_terminate_run", _spy_terminate)

        await service._on_worker_crash(session_id, 137)

        assert order == ["terminate", "cancel"]
    finally:
        hung.cancel()
        stuck_task.cancel()
        # `shutdown()` awaits every task still in `_turn_tasks` — replace the
        # proxy with the real (already-cancelled) task before that, or it
        # blows up on the proxy missing `add_done_callback`.
        service._turn_tasks[session_id] = stuck_task
        await service.shutdown()


async def test_terminate_run_rebroadcasts_after_a_racing_write_gets_cancelled(
    tmp_path, monkeypatch
):
    """R-N1 (controller ruling, round-N2): reproduces the exact window the old
    watchdog-race comment wrongly claimed couldn't exist — a call to
    `_terminate_run` gets cancelled *after* its `mark_run_terminated` DB write
    has already committed on `store/db.py`'s single DB thread but *before* it
    reaches its own `run.terminated` broadcast. Before this fix, a second call
    to `_terminate_run` for the same Run (standing in for the N07 watchdog's
    own call right after cancelling the stuck task) would see `runs.status !=
    'running'` and return unconditionally — net result, `run.terminated`
    broadcasts zero times and the UI is stuck on "运行中" forever.

    The delay is injected directly into `queries.mark_run_terminated` itself —
    the real UPDATE runs synchronously first, *then* the delay — so the
    asyncio-level cancel genuinely cannot stop the write (matching
    `store/db.py`'s `ThreadPoolExecutor` semantics exactly, not simulating
    them): once a job is actually running on that single worker thread,
    cancelling the awaiting asyncio Task can't cancel the underlying
    `concurrent.futures.Future`, so the coroutine gets `CancelledError`
    delivered only once that future resolves — discarding its real result, not
    stopping the write that already happened."""
    service = await _make_service(tmp_path, monkeypatch)
    try:
        session_id = await _new_session(service, title="s1")
        run_id = "run_delay01"
        turn_id = "turn_delay01"
        await run_in_db_thread(
            service_module.queries.create_turn_and_user_message,
            service.ctx.db,
            turn_id=turn_id,
            message_id="msg_delay01",
            session_id=session_id,
            text="hello",
            queued=False,
        )
        await run_in_db_thread(
            service_module.queries.create_run,
            service.ctx.db,
            run_id=run_id,
            turn_id=turn_id,
            session_id=session_id,
        )
        ctx_turn = service_module._TurnContext(
            turn_id=turn_id, run_id=run_id, session_id=session_id
        )

        write_committed = threading.Event()
        real_mark_run_terminated = service_module.queries.mark_run_terminated

        def _slow_mark_run_terminated(
            conn: Any,
            run_id_: str,
            turn_id_: str,
            *,
            kind: str,
            reason: str,
            terminated_step_seq: int | None = None,
        ) -> None:
            # The real write happens (and commits) FIRST — everything after
            # this point is purely simulating "still inside the awaited
            # executor future", the exact window `task.cancel()` can't reach.
            real_mark_run_terminated(
                conn,
                run_id_,
                turn_id_,
                kind=kind,
                reason=reason,
                terminated_step_seq=terminated_step_seq,
            )
            write_committed.set()
            time.sleep(0.2)

        monkeypatch.setattr(
            service_module.queries, "mark_run_terminated", _slow_mark_run_terminated
        )

        racing_call = asyncio.create_task(
            service._terminate_run(ctx_turn, kind="error", reason="ACP prompt failed: boom")
        )
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, write_committed.wait, 2.0)
        assert write_committed.is_set(), "the DB write never started — test setup is broken"
        # The write has committed; now interrupt the coroutine before it gets
        # to its own broadcast — this is the exact scenario the deleted
        # comment claimed couldn't happen.
        racing_call.cancel()
        try:
            await racing_call
        except asyncio.CancelledError:
            pass

        # Confirms the cancel really did land before the broadcast — this is
        # the failure mode being reproduced, not (yet) the fix.
        assert _terminated(service) == []

        # A second call (standing in for the watchdog's own `_terminate_run`
        # right after cancelling the stuck task) must not silently no-op just
        # because `runs.status` is already 'terminated' — it has to notice the
        # write landed without a broadcast and rebroadcast from the persisted
        # row.
        await service._terminate_run(ctx_turn, kind="error", reason="ACP prompt failed: boom")

        events = _terminated(service)
        assert len(events) == 1
        assert events[0]["run_id"] == run_id
        assert events[0]["turn_id"] == turn_id
        assert events[0]["card"]["kind"] == ErrorKind.PROVIDER_ERROR.value
    finally:
        await service.shutdown()


async def test_terminate_run_does_not_fake_a_termination_for_a_run_that_completed(
    tmp_path, monkeypatch
):
    """Round-N2 review fix (#3, "新问题"): R-N1's rebroadcast guard above must
    NOT trigger for `runs.status == 'completed'` — only `mark_run_completed`
    writes that value (a normal, successful end of Turn), and it's never added
    to `_terminated_broadcast_run_ids` (a successful completion never
    broadcasts `run.terminated` at all). Before this fix, a bare `status !=
    'running'` check treated 'completed' exactly like an unbroadcast
    'terminated' row and rebuilt a card from `terminated_kind`/
    `terminated_reason`, both NULL on a completed row — `classify.classify
    (kind_hint=None, reason="")` falls through to `ErrorKind.INTERNAL`,
    faking a "internal error" `run.terminated` (with `kind=None` in the
    payload, violating the `user|error|budget` contract) onto a Run that
    actually finished fine.

    The real call site this reproduces is `_handle_permission_request`'s
    approval-timeout branch (best-effort `_terminate_run` call guarded only by
    `self._active_turns.get(session_id) is ctx_turn`, never by run status) —
    `_active_turns` isn't popped until `_advance_queue` runs, which is after
    `_finalize_turn_success` already committed `mark_run_completed`, so that
    window is real. This test drives `_terminate_run` directly rather than
    threading a real approval race through `_handle_permission_request`,
    since the guard being tested lives entirely in `_terminate_run` itself and
    doesn't care which caller reached it."""
    service = await _make_service(tmp_path, monkeypatch)
    try:
        session_id = await _new_session(service, title="s1")
        run_id = "run_done01"
        turn_id = "turn_done01"
        await run_in_db_thread(
            service_module.queries.create_turn_and_user_message,
            service.ctx.db,
            turn_id=turn_id,
            message_id="msg_done01",
            session_id=session_id,
            text="hello",
            queued=False,
        )
        await run_in_db_thread(
            service_module.queries.create_run,
            service.ctx.db,
            run_id=run_id,
            turn_id=turn_id,
            session_id=session_id,
        )
        # Stand-in for `_finalize_turn_success` already having committed the
        # Run as successfully completed before this (late) call arrives.
        await run_in_db_thread(
            service_module.queries.mark_run_completed, service.ctx.db, run_id, turn_id
        )
        ctx_turn = service_module._TurnContext(
            turn_id=turn_id, run_id=run_id, session_id=session_id
        )

        await service._terminate_run(ctx_turn, kind="error", reason="approval timed out")

        # No fake `run.terminated` for a Run that actually completed.
        assert _terminated(service) == []
        # And the DB row itself must stay untouched — 'completed', not
        # overwritten with 'terminated'/NULL-kind garbage.
        row = await run_in_db_thread(service_module.queries.get_run, service.ctx.db, run_id)
        assert row["status"] == "completed"
        assert row["terminated_kind"] is None
    finally:
        await service.shutdown()


async def test_worker_crash_watchdog_backs_off_when_the_prompt_call_already_reported_it(
    tmp_path, monkeypatch
):
    """The other half of the race: if the Run is no longer this exact
    `ctx_turn` by the time the grace period elapses (the normal path already
    terminated it, and possibly a NEW Turn is already running), the watchdog
    must not force a second, spurious termination."""
    monkeypatch.setattr(service_module, "_WORKER_CRASH_GRACE_S", 0.2)
    monkeypatch.setattr(service_module, "_WORKER_CRASH_POLL_INTERVAL_S", 0.02)
    service = await _make_service(tmp_path, monkeypatch)
    try:
        session_id = await _new_session(service, title="s1")
        ctx_turn = service_module._TurnContext(
            turn_id="turn_ghost", run_id="run_ghost", session_id=session_id
        )
        service._active_turns[session_id] = ctx_turn

        async def _resolve_shortly() -> None:
            await asyncio.sleep(0.05)
            service._active_turns.pop(session_id, None)

        asyncio.create_task(_resolve_shortly())
        await service._on_worker_crash(session_id, 137)

        assert _terminated(service) == []
    finally:
        await service.shutdown()


async def test_worker_crash_watchdog_does_not_cancel_a_turn_already_terminated_in_the_db(
    tmp_path, monkeypatch
):
    """Round-1 review fix (#3/#7): the narrower half of the same race as the
    test above, that one didn't cover — `_active_turns[session_id]` can still
    be *this exact* `ctx_turn` at the grace deadline even though the in-flight
    `prompt()` path already ran `_terminate_run` to completion (DB write done)
    and is now inside `finally: await self._advance_queue(...)`, not yet past
    the `self._lock(session_id)` that would pop `_active_turns`. Manufactured
    here by writing `runs.status='terminated'` directly (standing in for "the
    normal path's `_terminate_run` already committed it") while leaving
    `_active_turns`/`_turn_tasks` exactly as `_on_worker_crash` would find them
    mid-race. Before the fix, the watchdog would `task.cancel()` the stuck
    task unconditionally here — if that task were actually still inside
    `_advance_queue`, the cancel could land between `pop_next_queue_item` and
    `_start_turn`, dropping a queued item with no way to recover it."""
    monkeypatch.setattr(service_module, "_WORKER_CRASH_GRACE_S", 0.2)
    monkeypatch.setattr(service_module, "_WORKER_CRASH_POLL_INTERVAL_S", 0.02)
    service = await _make_service(tmp_path, monkeypatch)
    try:
        session_id = await _new_session(service, title="s1")
        run_id = "run_race01"
        turn_id = "turn_race01"
        await run_in_db_thread(
            service_module.queries.create_turn_and_user_message,
            service.ctx.db,
            turn_id=turn_id,
            message_id="msg_race01",
            session_id=session_id,
            text="hello",
            queued=False,
        )
        await run_in_db_thread(
            service_module.queries.create_run,
            service.ctx.db,
            run_id=run_id,
            turn_id=turn_id,
            session_id=session_id,
        )
        # Stand-in for "the in-flight prompt() path's own _terminate_run
        # already wrote this" — `_active_turns` is deliberately left
        # populated, matching the real race window.
        await run_in_db_thread(
            service_module.queries.mark_run_terminated,
            service.ctx.db,
            run_id,
            turn_id,
            kind="error",
            reason="ACP prompt failed: connection closed",
            terminated_step_seq=None,
        )
        ctx_turn = service_module._TurnContext(
            turn_id=turn_id, run_id=run_id, session_id=session_id
        )
        service._active_turns[session_id] = ctx_turn

        hung = asyncio.get_running_loop().create_future()
        stuck_task = asyncio.create_task(asyncio.wait_for(hung, timeout=None))
        service._turn_tasks[session_id] = stuck_task

        await service._on_worker_crash(session_id, 137)

        # The watchdog must back off entirely: no cancel (the queued-item-
        # dropping window this reproduces), and no second `run.terminated`
        # (that would be the "swallowed broadcast" half of review #7 — the
        # in-flight path's own broadcast, not simulated by this direct DB
        # write, is what's supposed to be the only one).
        assert not stuck_task.cancelled()
        assert not stuck_task.done()
        assert _terminated(service) == []
    finally:
        hung.cancel()
        stuck_task.cancel()
        await service.shutdown()


async def test_worker_crash_watchdog_finalizes_streamed_text_before_force_terminating(
    tmp_path, monkeypatch
):
    """Round-1 review fix (#7): every other termination path finalizes
    streamed `message.delta` text (`_finalize_streamed_messages`) before
    `_terminate_run` — the watchdog's own force-termination path used to skip
    straight to `_terminate_run`, so `CancelledError` on the stuck task (a
    `BaseException`, never observed by `_run_turn`'s `except Exception`) meant
    any assistant text the user had already watched stream past was silently
    dropped from the `messages` table forever (FR06 replay)."""
    monkeypatch.setattr(service_module, "_WORKER_CRASH_GRACE_S", 0.2)
    monkeypatch.setattr(service_module, "_WORKER_CRASH_POLL_INTERVAL_S", 0.02)
    service = await _make_service(tmp_path, monkeypatch)
    try:
        session_id = await _new_session(service, title="s1")
        run_id = "run_stream01"
        turn_id = "turn_stream01"
        await run_in_db_thread(
            service_module.queries.create_turn_and_user_message,
            service.ctx.db,
            turn_id=turn_id,
            message_id="msg_stream01",
            session_id=session_id,
            text="hello",
            queued=False,
        )
        await run_in_db_thread(
            service_module.queries.create_run,
            service.ctx.db,
            run_id=run_id,
            turn_id=turn_id,
            session_id=session_id,
        )
        streamed_message_id = "msg_stream01_assistant"
        await run_in_db_thread(
            service_module.queries.insert_assistant_message,
            service.ctx.db,
            message_id=streamed_message_id,
            session_id=session_id,
            turn_id=turn_id,
            kind="text",
        )
        ctx_turn = service_module._TurnContext(
            turn_id=turn_id,
            run_id=run_id,
            session_id=session_id,
            assistant_message_id=streamed_message_id,
            assistant_text="用户已经看到这段文字流过去了",
        )
        service._active_turns[session_id] = ctx_turn

        hung = asyncio.get_running_loop().create_future()
        stuck_task = asyncio.create_task(asyncio.wait_for(hung, timeout=None))
        service._turn_tasks[session_id] = stuck_task

        await service._on_worker_crash(session_id, 137)

        assert len(_terminated(service)) == 1
        completed = [p for _sid, m, p in service.ctx.server.broadcasts if m == "message.completed"]
        assert any(p["id"] == streamed_message_id for p in completed)
        row = await run_in_db_thread(
            service_module.queries.get_session, service.ctx.db, session_id
        )
        assert row is not None  # sanity: DB still readable after the finalize write
        persisted = await run_in_db_thread(
            lambda conn: conn.execute(
                "SELECT content_json FROM messages WHERE id = ?", (streamed_message_id,)
            ).fetchone(),
            service.ctx.db,
        )
        assert json.loads(persisted[0])["text"] == "用户已经看到这段文字流过去了"
        # `_finalize_streamed_messages` resets this so a later, unrelated call
        # can never re-finalize/re-broadcast the same message.
        assert ctx_turn.assistant_message_id is None
    finally:
        hung.cancel()
        await service.shutdown()


# ---------------------------------------------------------------------------
# session.retry — 重试 / 换模型 / 放弃
# ---------------------------------------------------------------------------


async def test_retry_creates_a_new_turn_reusing_the_original_user_message(tmp_path, monkeypatch):
    service = await _make_service(
        tmp_path, monkeypatch, providers=_RaisingProviderResolver("Connection refused")
    )
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        await service.send(session_id, "please retry me")
        await _wait_until(lambda: len(_terminated(service)) >= 1)
        original_turn_id = _terminated(service)[0]["turn_id"]

        result = await service.retry(session_id, original_turn_id, action="retry")
        assert result["action"] == "retry"
        assert result["retried_turn_id"] == original_turn_id
        new_turn_id = result["turn_id"]
        assert new_turn_id != original_turn_id

        await _wait_until(lambda: len(_terminated(service)) >= 2)
        messages = await service.turn_messages(session_id, limit=10)
        user_texts = [m["content"]["text"] for m in messages if m["role"] == "user"]
        assert user_texts == ["please retry me", "please retry me"]
    finally:
        await service.shutdown()


async def test_retry_with_model_override_is_consumed_by_the_new_turn(tmp_path, monkeypatch):
    """First `resolve()` call fails (producing a retryable, terminated Turn);
    the second — driven by `retry()`'s `model_override` — must see exactly the
    override dict, not the Agent's normal (absent, in this test) `model_pref`.

    Round-2 review (#1): this only proves `_run_turn`'s resolver PRE-CHECK
    receives the override — it is NOT evidence that "换模型" actually changes
    what the worker runs (it doesn't; see the companion test right below and
    `errors/classify.py::_ACTIONS`'s comment for why `switch_model` was
    withdrawn from every card's `actions` rather than left looking like it
    works)."""

    class _FirstFailsThenRecordsResolver(ProviderResolverProtocol):
        def __init__(self) -> None:
            self.calls: list[Any] = []
            self._first = True

        def resolve(self, model_pref: dict[str, Any] | None) -> Any:
            self.calls.append(model_pref)
            if self._first:
                self._first = False
                raise ProviderNotConfiguredError("Connection refused")
            return {"provider": "openai", "model": "gpt-test", "env": {}, "hermes_config": {}}

        def list_models(self, provider: str | None) -> list[dict[str, Any]]:
            return []

    resolver = _FirstFailsThenRecordsResolver()
    service = await _make_service(tmp_path, monkeypatch, providers=resolver)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        await service.send(session_id, "hi")
        await _wait_until(lambda: len(_terminated(service)) >= 1)
        failed_turn_id = _terminated(service)[0]["turn_id"]
        assert resolver.calls[0] != {"provider": "openai", "model": "gpt-override"}

        result = await service.retry(
            session_id,
            failed_turn_id,
            action="retry",
            model_override={"provider": "openai", "model": "gpt-override"},
        )
        assert result["queued"] is False
        await _wait_until(lambda: len(resolver.calls) >= 2)
        assert resolver.calls[-1] == {"provider": "openai", "model": "gpt-override"}
    finally:
        await service.shutdown()


async def test_model_override_does_not_restart_or_change_the_actual_worker_process(
    tmp_path, monkeypatch
):
    """Round-2 review (#1, critical): the concrete, process-level proof behind
    `errors/classify.py::_ACTIONS`'s decision to withdraw `switch_model` — a
    session with an already-running worker keeps that EXACT worker process
    (same pid) across a `retry(model_override=...)` call, because
    `WorkerManager.ensure_started` returns the existing worker for a session
    that has one (never respawns), and `_spawn_and_check`'s `_worker_env` call
    is never even reached a second time here. Whatever provider/model that
    worker was originally started with is what still runs the retried Turn —
    "换模型" changes nothing at the process level, no matter what
    `ctx.providers.resolve()` (the companion test above) says."""
    service = await _make_service(tmp_path, monkeypatch)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        await service.send(session_id, "TOOL_EXCEPTION please")
        await _wait_until(lambda: len(_terminated(service)) >= 1, timeout=5)
        failed_turn_id = _terminated(service)[0]["turn_id"]

        worker_before = service.worker_manager.get(session_id)
        assert worker_before is not None
        pid_before = worker_before.process.pid

        result = await service.retry(
            session_id,
            failed_turn_id,
            action="retry",
            model_override={"provider": "openai", "model": "gpt-override"},
        )
        assert result["queued"] is False
        await _wait_until(lambda: len(_terminated(service)) >= 2, timeout=5)

        worker_after = service.worker_manager.get(session_id)
        assert worker_after is not None
        assert worker_after.process.pid == pid_before
        assert worker_after is worker_before
    finally:
        await service.shutdown()


async def test_abandon_clears_the_pending_queue_and_marks_turns_cancelled(tmp_path, monkeypatch):
    service = await _make_service(
        tmp_path, monkeypatch, providers=_RaisingProviderResolver("Connection refused")
    )
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        first = await service.send(session_id, "first (fails immediately)")
        assert first["queued"] is False
        await _wait_until(lambda: len(_terminated(service)) >= 1)
        failed_turn_id = _terminated(service)[0]["turn_id"]

        # First Turn already terminated (not "running" per SessionService's own
        # bookkeeping), so this actually queues behind it the same way any
        # `send()` on a session with an in-flight Turn would — except nothing is
        # in flight here, so instead assert against a manufactured pending
        # queue item directly (the ownership boundary doesn't grant this branch
        # a way to force "second send lands in queue" deterministically via
        # `send()` alone without racing the fault-injected first Turn).
        # `queue_items.turn_id` FKs to `turns(id)` (002_seed_defaults_and_queue_
        # turn.sql) — the turn+message row must exist before `enqueue()`, same
        # order `send()`'s own "running" branch already uses.
        await run_in_db_thread(
            service_module.queries.create_turn_and_user_message,
            service.ctx.db,
            turn_id="turn_queued_1",
            message_id="msg_queued_1",
            session_id=session_id,
            text="queued behind the failure",
            queued=True,
        )
        await run_in_db_thread(
            service_module.queries.enqueue,
            service.ctx.db,
            session_id=session_id,
            turn_id="turn_queued_1",
            text="queued behind the failure",
            attachments=None,
        )

        result = await service.retry(session_id, failed_turn_id, action="abandon")
        assert result["action"] == "abandon"
        assert result["cleared_queue_items"] == 1

        items = await _queue_items(service, session_id)
        assert items == []

        failed_turn = await run_in_db_thread(
            service_module._fetch_turn, service.ctx.db, failed_turn_id
        )
        queued_turn = await run_in_db_thread(
            service_module._fetch_turn, service.ctx.db, "turn_queued_1"
        )
        assert failed_turn["status"] == "cancelled"
        assert queued_turn["status"] == "cancelled"
    finally:
        await service.shutdown()


async def test_retry_on_a_still_running_turn_is_rejected(tmp_path, monkeypatch):
    service = await _make_service(tmp_path, monkeypatch)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        await service.send(session_id, "SLEEP_MS:300 hello")
        await _wait_until(lambda: service.worker_manager.get(session_id) is not None)
        turn = (await service.get(session_id))["latest_turn"]
        try:
            await service.retry(session_id, turn["id"], action="retry")
            raise AssertionError("expected RpcError for a non-terminated turn")
        except Exception as exc:  # noqa: BLE001 - asserting on RpcError below
            assert "not in a retryable state" in str(exc)
    finally:
        await service.shutdown()


async def test_retry_unknown_turn_id_is_not_found(tmp_path, monkeypatch):
    service = await _make_service(tmp_path, monkeypatch)
    try:
        session_id = await _new_session(service, title="s1")
        try:
            await service.retry(session_id, "turn_does_not_exist", action="retry")
            raise AssertionError("expected RpcError for an unknown turn")
        except Exception as exc:  # noqa: BLE001
            assert "not found" in str(exc)
    finally:
        await service.shutdown()


# ---------------------------------------------------------------------------
# R-N4 (controller ruling, 2026-09-20; 04-w5-interfaces.md §4.3, PRD 9.3):
# "终止不清空 Session 队列；队列中的后续指令挂起，等用户决定继续或清空" — applies
# to all three outer termination kinds (user/error/budget), not just
# error/budget. `_advance_queue` no longer auto-pops the next queued item for
# ANY of them; `session.queue_resume` (explicit) and `session.send` a new
# message (implicit) are the two ways to un-suspend.
# ---------------------------------------------------------------------------


async def test_user_stop_suspends_the_queue_instead_of_auto_advancing(tmp_path, monkeypatch):
    service = await _make_service(tmp_path, monkeypatch)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        first = await service.send(session_id, "SLEEP_MS:500 hello")
        assert first["queued"] is False
        await _wait_until(lambda: session_id in service._active_turns)
        second = await service.send(session_id, "queued behind the stop")
        assert second["queued"] is True

        stop_result = await service.stop(session_id)
        assert stop_result["stopped"] is True
        await _wait_until(lambda: len(_terminated(service)) >= 1, timeout=5)
        card = _terminated(service)[0]
        assert card["kind"] == "user"
        await _wait_for_queue_suspended(service, suspended=True)

        # R-N4: the queued item must NOT have been popped/started.
        items = await _queue_items(service, session_id)
        assert len(items) == 1
        assert items[0]["text"] == "queued behind the stop"
        assert session_id not in service._active_turns
        assert len(service.ctx.server.events("turn.started")) == 1

        suspend_events = [e for e in _queue_changed_events(service) if e.get("suspended")]
        assert suspend_events[-1]["reason"] == "user"
    finally:
        await service.shutdown()


async def test_error_termination_suspends_the_queue_instead_of_auto_advancing(
    tmp_path, monkeypatch
):
    service = await _make_service(tmp_path, monkeypatch)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        first = await service.send(session_id, "TOOL_EXCEPTION please")
        assert first["queued"] is False
        await _wait_until(lambda: session_id in service._active_turns)
        second = await service.send(session_id, "queued behind the failure")
        assert second["queued"] is True

        await _wait_until(lambda: len(_terminated(service)) >= 1, timeout=5)
        card = _terminated(service)[0]
        assert card["kind"] == "error"
        await _wait_for_queue_suspended(service, suspended=True)

        items = await _queue_items(service, session_id)
        assert len(items) == 1
        assert session_id not in service._active_turns
        assert len(service.ctx.server.events("turn.started")) == 1

        suspend_events = [e for e in _queue_changed_events(service) if e.get("suspended")]
        assert suspend_events[-1]["reason"] == "error"
    finally:
        await service.shutdown()


async def test_budget_termination_suspends_the_queue_instead_of_auto_advancing(
    tmp_path, monkeypatch
):
    """Unlike the "user"/"error" siblings above, the provider-resolve-only
    fault injection this needs for a `kind="budget"` termination (no real ACP
    round trip — see `_RaisingProviderResolver`'s own docstring) resolves so
    fast that a plain `_wait_until(lambda: session_id in service.
    _active_turns)` can miss the window entirely (observed flaky in practice
    while writing this test). `_SlowRaisingProviderResolver` (this file, right
    above) adds a small real delay on the executor thread `run_in_db_thread`
    runs `resolve()` on — doesn't touch the event loop, just gives the second
    `send()` below a reliable window to land while the first Turn is still
    genuinely "running"."""
    service = await _make_service(
        tmp_path,
        monkeypatch,
        providers=_SlowRaisingProviderResolver("insufficient_quota: monthly cap reached"),
    )
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        first = await service.send(session_id, "first (fails immediately, quota)")
        assert first["queued"] is False
        await _wait_until(lambda: session_id in service._active_turns)
        second = await service.send(session_id, "queued behind the budget termination")
        assert second["queued"] is True

        await _wait_until(lambda: len(_terminated(service)) >= 1, timeout=5)
        card = _terminated(service)[0]
        assert card["kind"] == "budget"
        await _wait_for_queue_suspended(service, suspended=True)

        items = await _queue_items(service, session_id)
        assert len(items) == 1
        assert session_id not in service._active_turns
        assert len(service.ctx.server.events("turn.started")) == 1

        suspend_events = [e for e in _queue_changed_events(service) if e.get("suspended")]
        assert suspend_events[-1]["reason"] == "budget"
    finally:
        await service.shutdown()


async def test_queue_resume_starts_the_next_pending_item(tmp_path, monkeypatch):
    service = await _make_service(tmp_path, monkeypatch)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        await service.send(session_id, "TOOL_EXCEPTION please")
        await _wait_until(lambda: session_id in service._active_turns)
        await service.send(session_id, "resume me")
        await _wait_until(lambda: len(_terminated(service)) >= 1, timeout=5)
        # `_advance_queue` (this Turn's own `finally`) has to have actually
        # cleared `_active_turns`/suspended the queue before `queue_resume`
        # below can see "not running" — `run.terminated` alone (just waited
        # for) fires strictly earlier, inside `_terminate_run`, before
        # `_run_turn`'s `finally: await self._advance_queue(...)` even starts.
        await _wait_for_queue_suspended(service, suspended=True)
        assert len(await _queue_items(service, session_id)) == 1

        result = await service.queue_resume(session_id)
        assert result["resumed"] is True
        # `queue_resume`'s own broadcast is fully awaited before it returns —
        # no extra wait needed for this one, unlike the racier sibling below.
        assert _queue_changed_events(service)[-1]["suspended"] is False
        assert (await _queue_items(service, session_id)) == []

        # The resumed Turn runs the fake agent's plain "normal" path (no
        # marker in "resume me") and completes on its own.
        await _wait_until(
            lambda: len(service.ctx.server.events("turn.started")) >= 2, timeout=5
        )
    finally:
        await service.shutdown()


async def test_queue_resume_rejects_when_nothing_is_pending(tmp_path, monkeypatch):
    service = await _make_service(tmp_path, monkeypatch)
    try:
        session_id = await _new_session(service, title="s1")
        try:
            await service.queue_resume(session_id)
            raise AssertionError("expected RpcError for an empty queue")
        except Exception as exc:  # noqa: BLE001
            assert "no pending queue items" in str(exc)
    finally:
        await service.shutdown()


async def test_queue_resume_rejects_while_a_turn_is_running(tmp_path, monkeypatch):
    service = await _make_service(tmp_path, monkeypatch)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        await service.send(session_id, "SLEEP_MS:500 hello")
        await _wait_until(lambda: session_id in service._active_turns)
        try:
            await service.queue_resume(session_id)
            raise AssertionError("expected RpcError while a Turn is running")
        except Exception as exc:  # noqa: BLE001
            assert "already running" in str(exc)
    finally:
        await service.shutdown()


async def test_send_a_new_message_implicitly_resumes_a_suspended_queue(tmp_path, monkeypatch):
    """The second of R-N4's two resume paths — `session.send` itself is
    unmodified (out of this round's authorized touch set): with nothing
    "running" once the queue is suspended, `send()`'s existing "not running
    -> start immediately" branch already fires for a brand new message, and
    THAT Turn's own normal completion (via `_advance_queue`'s non-suspended
    branch) is what actually pops the still-pending older item behind it —
    see `_advance_queue`'s own comment for why this needs no changes to
    `send()` at all."""
    service = await _make_service(tmp_path, monkeypatch)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        await service.send(session_id, "TOOL_EXCEPTION please")
        await _wait_until(lambda: session_id in service._active_turns)
        await service.send(session_id, "still queued behind the failure")
        await _wait_until(lambda: len(_terminated(service)) >= 1, timeout=5)
        # `send()` below decides immediate-run-vs-queue from `_active_turns`/
        # `_turn_tasks`, which only `_advance_queue` (this failed Turn's own
        # `finally`) clears — `run.terminated` alone fires strictly earlier
        # (see `_wait_for_queue_suspended`'s own docstring).
        await _wait_for_queue_suspended(service, suspended=True)
        assert len(await _queue_items(service, session_id)) == 1

        new_msg = await service.send(session_id, "brand new message, jumps the queue")
        assert new_msg["queued"] is False  # nothing "running" -> starts immediately

        # Once this brand-new Turn completes normally, the still-pending old
        # item resumes on its own via `_advance_queue`'s non-suspended
        # branch — wait for THAT specific broadcast rather than racing it
        # against the resumed item's own `turn.started` (a different
        # broadcast, fired from a different, concurrently-scheduled task —
        # the two have no guaranteed order relative to each other).
        await _wait_for_queue_suspended(service, suspended=False)
        items = await _queue_items(service, session_id)
        assert items == []
        assert len(service.ctx.server.events("turn.started")) == 3
    finally:
        await service.shutdown()


# ---------------------------------------------------------------------------
# R-N9 (controller ruling, round-6, 2026-09-20; 04-w5-interfaces.md §4.3, PRD
# 9.3): "挂起状态必须可查询、可重建，不能只活在 renderer 内存里" — R-N4's
# `suspended`/`reason` were ephemeral (a `queue.changed` broadcast, fired
# once, never replayed); `sessions.queue_suspended_reason` persists the same
# fact so `session.get`/`session.queue` can reconstruct it after a restart or
# a renderer reload, not just a live subscription.
# ---------------------------------------------------------------------------


async def test_session_get_and_queue_report_the_persisted_suspended_reason(
    tmp_path, monkeypatch
):
    """The daemon-side half of "错误终止 → 切走切回 → 仍显示「已暂停」与「继续」
    按钮" (R-N9's own acceptance test) — a renderer reload is just a fresh
    `session.get`/`session.queue` round trip; this asserts what those two
    RPCs actually return after a real error termination with a real pending
    queue item, without needing an Electron renderer to observe it."""
    service = await _make_service(tmp_path, monkeypatch)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        assert (await service.get(session_id))["queue_suspended_reason"] is None

        first = await service.send(session_id, "TOOL_EXCEPTION please")
        assert first["queued"] is False
        await _wait_until(lambda: session_id in service._active_turns)
        second = await service.send(session_id, "queued behind the failure")
        assert second["queued"] is True

        await _wait_until(lambda: len(_terminated(service)) >= 1, timeout=5)
        await _wait_for_queue_suspended(service, suspended=True)

        # `session.get`'s row (`queries.get_session`'s plain `SELECT *`)
        # carries the persisted column for free — no `SessionService.get()`
        # code change was needed for this half.
        session_row = await service.get(session_id)
        assert session_row["queue_suspended_reason"] == "error"

        # `session.queue` (this round's response-shape change) carries the
        # same fact explicitly, alongside the items themselves.
        queue_response = await service.queue(session_id)
        assert queue_response["suspended"] is True
        assert queue_response["reason"] == "error"
        assert len(queue_response["items"]) == 1

        # `queue_resume()` clears the persisted value, not just the live
        # broadcast — simulating "切走切回" is exactly re-reading `session.get`
        # after this, which a real renderer's `bindSession()` already does.
        await service.queue_resume(session_id)
        assert (await service.get(session_id))["queue_suspended_reason"] is None
        cleared_queue = await service.queue(session_id)
        assert cleared_queue["suspended"] is False
        assert cleared_queue["reason"] is None
    finally:
        await service.shutdown()


async def test_sending_a_new_message_clears_the_persisted_suspended_reason(
    tmp_path, monkeypatch
):
    """The implicit-resume half of R-N9's persistence — `_run_turn` (not
    `send()` itself, out of this round's authorized touch set) clears
    `queue_suspended_reason` at the start of every Turn it runs, which is
    where `send()`'s own "not running -> immediate execute" branch ends up."""
    service = await _make_service(tmp_path, monkeypatch)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        await service.send(session_id, "TOOL_EXCEPTION please")
        await _wait_until(lambda: session_id in service._active_turns)
        await service.send(session_id, "still queued behind the failure")
        await _wait_until(lambda: len(_terminated(service)) >= 1, timeout=5)
        await _wait_for_queue_suspended(service, suspended=True)
        assert (await service.get(session_id))["queue_suspended_reason"] == "error"

        await service.send(session_id, "brand new message, jumps the queue")
        # This brand-new message has no marker (no SLEEP_MS/TOOL_EXCEPTION),
        # so its Turn can complete near-instantly — polling `_active_turns`
        # directly would race losing the window entirely. `_run_turn`'s own
        # clear (see that method's comment) commits strictly before this
        # Turn's `turn.started` broadcast, which strictly precedes its own
        # eventual `_advance_queue` -> `queue.changed{suspended:false}` —
        # waiting on that broadcast (same as this file's sibling send()-
        # resume test above) is the non-racy signal.
        await _wait_for_queue_suspended(service, suspended=False)
        assert (await service.get(session_id))["queue_suspended_reason"] is None
    finally:
        await service.shutdown()


async def test_abandon_clears_the_persisted_suspended_reason(tmp_path, monkeypatch):
    """R-N9's third clear path — `retry(action="abandon")` already broadcasts
    `suspended:false`; this asserts the persisted value follows it too."""
    service = await _make_service(tmp_path, monkeypatch)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        await service.send(session_id, "TOOL_EXCEPTION please")
        await _wait_until(lambda: len(_terminated(service)) >= 1, timeout=5)
        failed_turn_id = _terminated(service)[0]["turn_id"]
        assert (await service.get(session_id))["queue_suspended_reason"] == "error"

        await service.retry(session_id, failed_turn_id, action="abandon")
        assert (await service.get(session_id))["queue_suspended_reason"] is None
    finally:
        await service.shutdown()


# ---------------------------------------------------------------------------
# R-N5 (controller ruling, 2026-09-20; 04-w5-interfaces.md §4.3, PRD 11.2/9.3):
# 单个 Run 最大 Step 数 (200) / 最大时长 (7200s) — `_handle_tool_call_start`
# enforces both as a 预算终止 (`kind="budget"`), with `card.budget` filled in.
# ---------------------------------------------------------------------------


async def test_step_count_budget_terminates_the_run_with_a_budget_card(tmp_path, monkeypatch):
    """"假 ACP agent 发 201 个 tool_call" (R-N5's own test description) — the
    fake agent's `MANY_TOOL_CALLS:201` marker (this round's own addition to
    `fake_acp_agent.py`, see its module docstring) sends 201 real
    `tool_call`/`tool_call_update` pairs over one real ACP round trip;
    `NullConfigResolver` (via `_make_service`) returns `{}`, so the Session
    is running against PRD 11.2's own default (200), not a test-configured
    override — exactly the "假 ACP agent 发 201 个 tool_call" scenario
    against the real default limit."""
    service = await _make_service(tmp_path, monkeypatch)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        await service.send(session_id, "MANY_TOOL_CALLS:201")
        await _wait_until(lambda: len(_terminated(service)) >= 1, timeout=5)

        card = _terminated(service)[0]
        assert card["kind"] == "budget"
        assert card["card"]["kind"] == ErrorKind.BUDGET.value
        assert card["card"]["actions"] == ["abandon"]
        assert card["card"]["budget"] == {
            "name": "单个 Run 最大 Step 数",
            "used": 201,
            "limit": 200,
            "unit": "步",
        }

        run = await run_in_db_thread(
            service_module.queries.get_run, service.ctx.db, card["run_id"]
        )
        # The 201st (over-limit) tool_call never got counted/inserted as a
        # Step — `terminated_step_seq` stays at the last one that actually ran.
        assert run["terminated_step_seq"] == 200

        steps = await service.run_steps(card["run_id"])
        assert len(steps) == 200

        # R-N4: a budget termination suspends the queue too (nothing queued
        # here, but the Turn/worker bookkeeping must still have unwound —
        # the runaway worker's still-streaming tool_call events must not
        # leave the session stuck "running" forever).
        await _wait_until(lambda: session_id not in service._active_turns, timeout=5)
    finally:
        await service.shutdown()


async def test_budget_termination_sends_a_real_acp_cancel_before_terminate_and_before_local_cancel(
    tmp_path, monkeypatch
):
    """R-N7 (controller ruling, round-6, 2026-09-20): round-5 review, correctly
    — "取消 _run_turn 只是放弃等待 prompt()，worker 里的 agent 照跑不误、继续调
    工具" — cancelling only the LOCAL `_run_turn` asyncio task (what this used
    to do) never told the real worker/agent anything happened. The fix walks
    the SAME path `stop()` (a user-initiated termination) already uses: a real
    ACP `session/cancel` notification to the actual fake-agent subprocess over
    the wire, BEFORE `_terminate_run`, and the local task only cancelled
    AFTER that.

    Monkeypatches `AcpClient.cancel` at the class level (not the `worker.
    client` instance — the worker doesn't exist yet when this patch has to go
    in, before `service.worker_manager.start()`) to record when it fires
    relative to `_terminated(service)` — real wire effect preserved, `await
    orig_cancel(self, session_id)` still runs, so this is a REAL round trip to
    the real `fake_acp_agent.py` subprocess, not a mock standing in for one."""
    from jones_daemon.kernel.acp_client import AcpClient

    order: list[str] = []
    orig_cancel = AcpClient.cancel

    async def _recording_cancel(self: AcpClient, session_id: str) -> None:
        # The strongest form of "cancel happens before terminate": assert it
        # right here, not just record it — a `run.terminated` broadcast
        # already having landed by the time this notification goes out would
        # mean R-N7's ordering regressed.
        assert _terminated(service) == []
        order.append("acp_cancel")
        await orig_cancel(self, session_id)

    monkeypatch.setattr(AcpClient, "cancel", _recording_cancel)
    service = await _make_service(tmp_path, monkeypatch)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        # "持续发 tool_call" (R-N7's own test description) — same real ACP
        # round trip `test_step_count_budget_terminates_the_run_with_a_budget_
        # card` above uses.
        await service.send(session_id, "MANY_TOOL_CALLS:201")
        task = service._turn_tasks[session_id]

        await _wait_until(lambda: len(_terminated(service)) >= 1, timeout=5)
        assert order == ["acp_cancel"]
        card = _terminated(service)[0]
        assert card["kind"] == "budget"

        run = await run_in_db_thread(
            service_module.queries.get_run, service.ctx.db, card["run_id"]
        )
        # 不再有新 Step 落库 (R-N7's own test description) past the limit —
        # same assertion the sibling test above makes.
        assert run["terminated_step_seq"] == 200
        steps = await service.run_steps(card["run_id"])
        assert len(steps) == 200

        # The LOCAL task actually got cancelled too (last, not skipped).
        await _wait_until(lambda: task.done(), timeout=5)
        assert task.cancelled()
    finally:
        await service.shutdown()


async def test_budget_termination_still_terminates_when_the_acp_cancel_notification_fails(
    tmp_path, monkeypatch
):
    """R-N7: "cancel 本身失败（worker 已死/超时）要记日志并继续终止流程，不能
    因此卡住" — mirrors `stop()`'s own `except (AcpProtocolError, AcpError)`
    handling. `AcpProtocolError` here (rather than actually killing the
    worker) is the simpler, deterministic way to exercise the SAME `except`
    branch `stop()`'s own tests already cover for `stop()` itself — this
    tests that `_terminate_run_for_exceeded_budget` doesn't hang or skip
    termination when it fires."""
    from jones_daemon.kernel.acp_client import AcpClient, AcpProtocolError

    async def _failing_cancel(self: AcpClient, session_id: str) -> None:
        raise AcpProtocolError("boom: simulated cancel failure")

    monkeypatch.setattr(AcpClient, "cancel", _failing_cancel)
    service = await _make_service(tmp_path, monkeypatch)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")
        await service.send(session_id, "MANY_TOOL_CALLS:201")
        await _wait_until(lambda: len(_terminated(service)) >= 1, timeout=5)
        card = _terminated(service)[0]
        assert card["kind"] == "budget"
        await _wait_until(lambda: session_id not in service._active_turns, timeout=5)
    finally:
        await service.shutdown()


async def test_run_duration_budget_terminates_a_tool_call_free_turn_via_the_resident_watchdog(
    tmp_path, monkeypatch
):
    """R-N8 (controller ruling, round-6, 2026-09-20) moved the 时长上限 check
    off `_handle_tool_call_start` entirely, onto `_check_run_durations` (a
    resident poll over `_active_turns`, `_run_budget_watchdog_loop`) — this
    supersedes the predecessor test that used to live here (round-5's "注入
    时钟超时长", `git log` has the full docstring on why it mutated `ctx_turn.
    started_at` directly rather than monkeypatching the real `time.monotonic`:
    that broke `workers/manager.py`'s own idle-worker reaper). This proves the
    NEW mechanism specifically for the ONE case the OLD tool_call-triggered
    check could never cover — R-N5's own report's documented scope gap: "纯
    文本、零工具调用的 Turn" (no `USE_TOOL` marker in the prompt at all — the
    predecessor test's own prompt, "SLEEP_MS:200 USE_TOOL", doesn't exist
    anymore because there's no longer a tool_call event for the check to hang
    off of).

    Uses the injectable `self._clock` (R-N8, `SessionService.__init__`'s own
    comment) — real time never advances, and `_check_run_durations` is called
    directly rather than waiting the real `_BUDGET_WATCHDOG_INTERVAL_S` (15s),
    the same "prove the mechanism, not the timer" approach `replay/
    retention.py`'s own tests already use for its sweep loop. Nothing else in
    the process reads `service._clock` (unlike the real `time.monotonic` the
    predecessor test avoided touching), so there's no `_reap_idle_loop`
    interaction to worry about here."""
    fake_now = [1_000.0]

    def _clock() -> float:
        return fake_now[0]

    service = await _make_service(tmp_path, monkeypatch, clock=_clock)
    await service.worker_manager.start()
    try:
        session_id = await _new_session(service, title="s1")

        # "SLEEP_MS:500", no "USE_TOOL" — this Turn calls zero tools; the fake
        # agent still streams its "Hel"/"lo" deltas first, giving this test a
        # deterministic ~500ms real-wall-time window (same synchronization
        # technique the predecessor test used) to act before it finishes on
        # its own.
        result = await service.send(session_id, "SLEEP_MS:500")
        assert result["queued"] is False
        await _wait_until(
            lambda: len(service.ctx.server.events("message.delta")) >= 1, timeout=5
        )

        ctx_turn = service._active_turns[session_id]
        assert ctx_turn.started_at == 1_000.0  # sanity: `_run_turn` used the injected clock
        fake_now[0] = 1_000.0 + ctx_turn.max_run_duration_s + 10.0
        await service._check_run_durations()

        await _wait_until(lambda: len(_terminated(service)) >= 1, timeout=5)
        card = _terminated(service)[0]
        assert card["kind"] == "budget"
        assert card["card"]["kind"] == ErrorKind.BUDGET.value
        budget = card["card"]["budget"]
        assert budget["name"] == "单个 Run 最大时长"
        assert budget["limit"] == 7200
        assert budget["unit"] == "秒"
        assert budget["used"] >= 7200

        # R-N4: a budget termination suspends the queue too, same as the
        # tool-call-triggered path's own test asserts.
        await _wait_until(lambda: session_id not in service._active_turns, timeout=5)
    finally:
        await service.shutdown()
