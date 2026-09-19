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


class FakeServer:
    def __init__(self) -> None:
        self.broadcasts: list[tuple[str, str, Any]] = []

    async def broadcast(self, session_id: str, method: str, params: Any) -> None:
        self.broadcasts.append((session_id, method, params))

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


def _terminated(service: SessionService) -> list[dict[str, Any]]:
    return [p for _sid, m, p in service.ctx.server.broadcasts if m == "run.terminated"]


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
        # Round-2 review fix (#7) moved `task.cancel()` to the very last thing
        # this function does (after its own finalize+terminate, see that
        # function's comment) — `cancel()` only *schedules* delivery of
        # `CancelledError`, it doesn't synchronously run it, so with nothing
        # left to `await` afterward inside `_on_worker_crash`, the event loop
        # hasn't necessarily had a turn to actually mark the task cancelled by
        # the time this coroutine resumes here. Yield once so it does — a test
        # concern only; production code has no such ordering dependency.
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


async def test_worker_crash_watchdog_broadcasts_before_cancelling_the_stuck_task(
    tmp_path, monkeypatch
):
    """Round-2 review fix (#7), the actual contract: no matter what asyncio
    scheduling does to the stuck task, this watchdog's own finalize+terminate
    (and therefore its `run.terminated` broadcast) must happen BEFORE it calls
    `task.cancel()` on the stuck task — not after. Verified by call order, not
    by trying to win a real race against `store/db.py`'s single DB thread
    (the failure mode this fix closes needs the interrupted task to be blocked
    genuinely inside a `run_in_executor` future at the moment of cancellation,
    which isn't something a test can force deterministically without invasive
    mocking of the DB thread itself — the order guarantee this test checks is
    what makes that scenario safe regardless of exact timing)."""
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

        items = await service.queue(session_id)
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
