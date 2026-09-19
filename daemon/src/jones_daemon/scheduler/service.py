"""`CronService` — Issue #20 / docs/design/04-w5-interfaces.md §2.

Owns the `crons` table end to end (CRUD + the trigger loop). Dispatches triggered
Runs *only* through `SessionService`'s existing public surface — `create()`,
`send()`, `get()`, `run_get()`, `ensure_main_session()` — per §1's ownership table
("只通过 SessionService 公开方法（create/send/get）派发，不改 sessions 内部"); the
last two are pre-existing public methods too (unmodified by this branch), used
read-only to learn a dispatched Run's outcome and to find the main session, not to
dispatch anything.

Single-timer scheduling (PRD 11.2 "空闲不轮询、用系统定时器唤醒"): `_loop` computes
the one nearest `next_run_at` across every enabled cron and suspends on it via
`Clock.wait` (one `asyncio.Event.wait()` under an `asyncio.wait_for` timeout — not a
sleep-and-recheck poll); `upsert`/`delete` set that same `asyncio.Event` (`_wake`)
after writing, so a new/changed/removed schedule takes effect immediately instead
of waiting out whatever stale deadline `_loop` was already parked on.

Known, deliberate scope gap — flagged prominently in this branch's PR report, not
silently routed around: `SessionService.create()`'s own existing guard (PRD 9.6,
"父会话是任务模式，子会话不能是自动模式") rejects `mode="auto"` children of a
`mode="task"` parent, and `ensure_main_session()` (sessions/service.py, out of this
branch's reach — owned by A originally, shared code no W5 branch exclusively owns)
always creates the main session as `mode="task"`. PRD 9.1 says "Cron 触发的 Run
默认以自动模式运行" — by default, every cron's parent (the main session) is
`mode="task"`, so that default dispatch is rejected by the very guard 9.6 also
specifies, every time, out of the box. This module does not paper over that: a
rejected `create()` is recorded as an honest dispatch failure (counts toward the
3-strikes auto-disable, posts a card naming the real reason) exactly like any other
failure, per DEV.md 工程原则 #4. See the PR report for the reproduction and the
proposed fix (outside this branch's owned files).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any

from jones_daemon.context import DaemonContext
from jones_daemon.logging import get_logger
from jones_daemon.rpc.errors import INVALID_PARAMS, NOT_FOUND, RpcError
from jones_daemon.scheduler import queries
from jones_daemon.scheduler.clock import Clock, RealClock
from jones_daemon.scheduler.cron_expr import CronExprError, next_after, parse
from jones_daemon.store import run_in_db_thread

logger = get_logger("scheduler")

_VALID_MODES = {"chat", "task", "auto"}
# How often an in-flight dispatched Run is polled for completion via the public
# `SessionService.get()` (`latest_turn.status`) — bounded to only while a cron's
# own Run is actually active, never while idle (that's `_loop`'s single timer,
# above). Not itself the "空闲不轮询" surface DEV.md/12.3 care about; see the
# module docstring's ownership note for why a poll is what's left once dispatch is
# restricted to `SessionService`'s public methods (no completion callback exists
# on that surface to subscribe to instead).
_RUN_POLL_INTERVAL_SECONDS = 1.0
_ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"


def _iso(dt: datetime) -> str:
    return dt.strftime(_ISO_FORMAT)[:-3] + "Z"


def _parse_iso(text: str) -> datetime:
    return datetime.strptime(text, _ISO_FORMAT + "Z").replace(tzinfo=UTC)


def _humanize_name(cron: dict[str, Any]) -> str:
    return cron.get("name") or cron["id"]


class CronService:
    def __init__(
        self,
        ctx: DaemonContext,
        session_service: Any,
        *,
        clock: Clock | None = None,
        poll_interval_seconds: float = _RUN_POLL_INTERVAL_SECONDS,
    ) -> None:
        self.ctx = ctx
        self.session_service = session_service
        self.clock = clock or RealClock()
        # Overridable only for tests (a real deployment has no reason to poll
        # faster/slower than the default) — see `_await_completion`.
        self.poll_interval_seconds = poll_interval_seconds
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()
        # cron_id -> child session id currently running its dispatched Turn — the
        # overlap-skip check (§2 "若已有该 cron 的 Run 在跑则跳过并记
        # skipped_overlap"). In-memory only: a daemon restart already has
        # `SessionService.startup()`'s `interrupt_stale_runs` mark every
        # previously-'running' Run as terminated, so nothing survives a restart
        # for this to need to recover.
        self._in_flight: dict[str, str] = {}

    # -- lifecycle ----------------------------------------------------------------

    async def start(self) -> None:
        main_id = await self.session_service.ensure_main_session()
        await run_in_db_thread(self._reconcile_on_startup, main_id)
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
            self._loop_task = None
        if self._background_tasks:
            await asyncio.wait(self._background_tasks, timeout=5.0)

    def _reconcile_on_startup(self, main_session_id: str) -> None:
        """PRD 5.8/11.3 精神 + 04-w5-interfaces.md §2 "启动不补跑错过的触发": any
        enabled cron whose `next_run_at` is unset (freshly created, never scheduled)
        or in the past (the daemon was stopped through one or more trigger times)
        gets its `next_run_at` recomputed from *now*, skipping every missed
        occurrence — never dispatched retroactively. A past `next_run_at` also gets
        a structured log line and a system message in the main session, so "why
        didn't my cron run overnight" has an honest, visible answer instead of
        silently vanishing."""
        now = self.clock.now()
        now_iso = _iso(now)
        for cron in queries.list_enabled_crons(self.ctx.db):
            schedule = parse(cron["expr"])
            missed = cron["next_run_at"] is not None and cron["next_run_at"] <= now_iso
            if cron["next_run_at"] is None or missed:
                new_next = _iso(next_after(schedule, now))
                queries.set_cron_next_run_at(self.ctx.db, cron["id"], new_next)
                if missed:
                    logger.warning(
                        "cron trigger(s) missed while the daemon was stopped; not catching up",
                        extra={
                            "detail": {
                                "cron_id": cron["id"],
                                "name": cron["name"],
                                "was_due_at": cron["next_run_at"],
                                "next_run_at": new_next,
                            }
                        },
                    )
                    queries.insert_system_message(
                        self.ctx.db,
                        session_id=main_session_id,
                        text=(
                            f"Cron “{_humanize_name(cron)}” 在守护进程未运行期间错过了原定于 "
                            f"{cron['next_run_at']} 的触发，不会补跑；下一次将在 {new_next} 触发。"
                        ),
                        meta={"kind": "cron_missed", "cron_id": cron["id"]},
                    )

    # -- CRUD (`cron.list` / `cron.upsert` / `cron.delete` / `cron.run_now`) -------

    async def list(self, project_id: str | None = None) -> list[dict[str, Any]]:
        return await run_in_db_thread(queries.list_crons, self.ctx.db, project_id=project_id)

    async def upsert(
        self,
        *,
        id: str | None = None,  # noqa: A002 - matches the RPC param name (`Cron.id`)
        project_id: str,
        agent_id: str,
        name: str,
        expr: str,
        prompt: str,
        mode: str = "auto",
        enabled: bool = True,
    ) -> dict[str, Any]:
        if mode not in _VALID_MODES:
            raise RpcError(INVALID_PARAMS, f"invalid mode: {mode!r}", {"mode": mode})
        try:
            schedule = parse(expr)
        except CronExprError as exc:
            raise RpcError(
                INVALID_PARAMS, f"invalid cron expression: {exc}", {"expr": expr}
            ) from exc
        next_run_at = _iso(next_after(schedule, self.clock.now())) if enabled else None

        def _write() -> dict[str, Any]:
            if id is None:
                return queries.create_cron(
                    self.ctx.db, project_id=project_id, agent_id=agent_id, name=name,
                    expr=expr, prompt=prompt, mode=mode, enabled=enabled, next_run_at=next_run_at,
                )
            existing = queries.get_cron(self.ctx.db, id)
            if existing is None:
                raise RpcError(NOT_FOUND, "cron not found", {"id": id})
            row = queries.update_cron(
                self.ctx.db, id, project_id=project_id, agent_id=agent_id, name=name,
                expr=expr, prompt=prompt, mode=mode, enabled=enabled, next_run_at=next_run_at,
            )
            assert row is not None  # noqa: S101 - existence just checked above
            return row

        row = await run_in_db_thread(_write)
        # A new/changed schedule may be due sooner than whatever `_loop` is
        # currently waiting on.
        self._wake.set()
        return row

    async def delete(self, cron_id: str) -> dict[str, Any]:
        def _write() -> dict[str, Any]:
            row = queries.get_cron(self.ctx.db, cron_id)
            if row is None:
                raise RpcError(NOT_FOUND, "cron not found", {"id": cron_id})
            queries.delete_cron(self.ctx.db, cron_id)
            return row

        row = await run_in_db_thread(_write)
        # Deletion never reaches into an already-dispatched child session (PRD 9.6
        # "生命周期：子会话独立运行，不因父会话终止而终止" — the same independence
        # applies a fortiori to deleting the *cron definition* that spawned it);
        # only stop tracking it for the overlap check.
        self._in_flight.pop(cron_id, None)
        self._wake.set()
        return row

    async def run_now(self, cron_id: str) -> dict[str, Any]:
        row = await run_in_db_thread(queries.get_cron, self.ctx.db, cron_id)
        if row is None:
            raise RpcError(NOT_FOUND, "cron not found", {"id": cron_id})
        await self._dispatch(row)
        after = await run_in_db_thread(queries.get_cron, self.ctx.db, cron_id)
        return after if after is not None else row

    # -- the timer loop -------------------------------------------------------------

    async def _loop(self) -> None:
        while True:
            self._wake.clear()
            crons = await run_in_db_thread(queries.list_enabled_crons, self.ctx.db)
            now_iso = _iso(self.clock.now())
            due = [c for c in crons if c["next_run_at"] is not None and c["next_run_at"] <= now_iso]
            if due:
                for cron in due:
                    # Awaited HERE, not inside the spawned background task: this
                    # is what guarantees `next_run_at` is already committed past
                    # `now` before this loop can possibly re-list and see the same
                    # cron as "due" again (on the very next iteration, or after
                    # dispatching every other currently-due cron below) — a
                    # fire-and-forget advance would race the immediate re-list
                    # right below and could dispatch the same trigger twice.
                    await self._advance_schedule(cron)
                    self._spawn_dispatch(cron)
                continue
            upcoming = [c["next_run_at"] for c in crons if c["next_run_at"] is not None]
            if not upcoming:
                # Nothing scheduled at all: wait forever, woken only by a config
                # change (`_wake`) — the literal "空闲不轮询" case, no timer armed.
                await self.clock.wait(self._wake, timeout=None)
                continue
            delay = max(0.0, (_parse_iso(min(upcoming)) - self.clock.now()).total_seconds())
            await self.clock.wait(self._wake, timeout=delay)

    def _spawn_dispatch(self, cron: dict[str, Any]) -> None:
        """Spawn just the create/send/watch half of dispatch as a background
        task — the schedule-advance half has already been awaited by `_loop`
        before this is called (see its comment for why that order matters)."""
        task = asyncio.create_task(self._dispatch_body(cron))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    # -- dispatch: advance schedule → create → send → watch → report back -----------

    async def _dispatch(self, cron: dict[str, Any]) -> None:
        """Advance the schedule, then run the rest of dispatch, fully awaited —
        used by `run_now` (04-w5-interfaces.md §2 "run_now 走同一路径") and by
        tests that want a single call covering both halves. `_loop`'s own tick
        path calls the two halves separately (`_advance_schedule` awaited
        in-line, then `_spawn_dispatch`) so it never blocks the timer loop on a
        full dispatch — see `_loop`'s comment."""
        await self._advance_schedule(cron)
        await self._dispatch_body(cron)

    async def _advance_schedule(self, cron: dict[str, Any]) -> None:
        now = self.clock.now()
        try:
            schedule = parse(cron["expr"])
            next_run_at = _iso(next_after(schedule, now))
        except CronExprError:
            # An expression that validated at `upsert` time can't actually go bad
            # later (nothing here ever rewrites `expr` without re-validating) —
            # this is a defensive backstop, not an expected path: disable rather
            # than loop forever re-attempting an expression that can't schedule
            # (DEV.md 工程原则 #4, 诚实失败).
            logger.error(
                "cron expression failed to schedule its next run; disabling",
                exc_info=True,
                extra={"detail": {"cron_id": cron["id"], "expr": cron["expr"]}},
            )
            next_run_at = None
        await run_in_db_thread(
            queries.mark_cron_dispatched, self.ctx.db, cron["id"],
            last_run_at=_iso(now), next_run_at=next_run_at,
        )
        if next_run_at is None:
            await run_in_db_thread(queries.record_cron_failure, self.ctx.db, cron["id"])

    async def _dispatch_body(self, cron: dict[str, Any]) -> None:
        """Overlap check + create/send/watch — assumes the schedule has already
        been advanced (by `_dispatch` or by `_loop`'s in-line call) before this
        runs."""
        cron_id = cron["id"]
        main_session_id = await self.session_service.ensure_main_session()
        if cron_id in self._in_flight:
            logger.warning(
                "cron trigger skipped: a previous Run for this cron is still active",
                extra={
                    "detail": {
                        "cron_id": cron_id, "name": cron.get("name"), "event": "skipped_overlap",
                        "in_flight_session_id": self._in_flight[cron_id],
                    }
                },
            )
            return
        try:
            child = await self.session_service.create(
                project_id=cron["project_id"], agent_id=cron["agent_id"],
                parent_id=main_session_id, mode=cron["mode"], title=_humanize_name(cron),
            )
        except RpcError as exc:
            await self._record_outcome(
                cron, main_session_id, success=False,
                text=f"Cron “{_humanize_name(cron)}” 触发失败：{exc.message}",
                meta={"kind": "cron_dispatch_error", "cron_id": cron_id, "error": exc.message},
            )
            return
        await run_in_db_thread(
            queries.create_cron_task, self.ctx.db,
            session_id=child["id"], cron_id=cron_id, title=_humanize_name(cron),
        )
        self._in_flight[cron_id] = child["id"]
        try:
            await self.session_service.send(child["id"], cron["prompt"])
        except RpcError as exc:
            self._in_flight.pop(cron_id, None)
            await self._record_outcome(
                cron, main_session_id, success=False,
                text=f"Cron “{_humanize_name(cron)}” 触发失败：{exc.message}",
                meta={
                    "kind": "cron_dispatch_error", "cron_id": cron_id,
                    "child_session_id": child["id"], "error": exc.message,
                },
            )
            return
        watch = asyncio.create_task(self._watch_and_report(cron, main_session_id, child["id"]))
        self._background_tasks.add(watch)

        def _done(_task: asyncio.Task[None], *, cid: str = cron_id) -> None:
            self._background_tasks.discard(_task)
            self._in_flight.pop(cid, None)

        watch.add_done_callback(_done)

    async def _watch_and_report(
        self, cron: dict[str, Any], main_session_id: str, child_session_id: str
    ) -> None:
        status, run = await self._await_completion(child_session_id)
        success = status == "completed"
        if success:
            text = f"Cron “{_humanize_name(cron)}” 已完成。"
            meta: dict[str, Any] = {
                "kind": "cron_completed",
                "cron_id": cron["id"],
                "child_session_id": child_session_id,
            }
            if run is not None:
                meta["run_id"] = run["id"]
        else:
            fallback_reason = f"Turn ended with status {status!r}"
            card = {
                "kind": (run or {}).get("terminated_kind") or "error",
                "message": (run or {}).get("terminated_reason") or fallback_reason,
            }
            text = f"Cron “{_humanize_name(cron)}” 未成功完成：{card['message']}"
            meta = {
                "kind": "cron_failed", "cron_id": cron["id"], "child_session_id": child_session_id,
                "card": card,
            }
            if run is not None:
                meta["run_id"] = run["id"]
        await self._record_outcome(cron, main_session_id, success=success, text=text, meta=meta)

    async def _await_completion(
        self, child_session_id: str
    ) -> tuple[str, dict[str, Any] | None]:
        """Poll `SessionService.get()` (public, read-only) until the dispatched
        Turn leaves 'queued'/'running' — see the module docstring for why this is
        a bounded poll rather than a callback: nothing on the public
        create/send/get surface offers a "Turn finished" notification to await
        instead."""
        while True:
            session = await self.session_service.get(child_session_id)
            turn = session.get("latest_turn")
            if turn is not None and turn.get("status") not in ("queued", "running"):
                run = None
                run_id = turn.get("run_id")
                if run_id:
                    with contextlib.suppress(RpcError):
                        run = await self.session_service.run_get(run_id)
                return turn["status"], run
            await asyncio.sleep(self.poll_interval_seconds)

    async def _record_outcome(
        self,
        cron: dict[str, Any],
        main_session_id: str,
        *,
        success: bool,
        text: str,
        meta: dict[str, Any],
    ) -> None:
        def _write() -> tuple[dict[str, Any], int]:
            if success:
                queries.record_cron_success(self.ctx.db, cron["id"])
                fail_count = 0
            else:
                fail_count = queries.record_cron_failure(self.ctx.db, cron["id"])
            row = queries.insert_system_message(
                self.ctx.db, session_id=main_session_id, text=text, meta=meta
            )
            return row, fail_count

        row, fail_count = await run_in_db_thread(_write)
        await self.ctx.server.broadcast(main_session_id, "message.completed", row)
        if not success and fail_count >= queries.FAILURE_THRESHOLD:
            disabled_text = (
                f"Cron “{_humanize_name(cron)}” 连续失败 {fail_count} 次，已自动停用。"
            )
            disabled_row = await run_in_db_thread(
                queries.insert_system_message, self.ctx.db, session_id=main_session_id,
                text=disabled_text, meta={"kind": "cron_disabled", "cron_id": cron["id"]},
            )
            await self.ctx.server.broadcast(main_session_id, "message.completed", disabled_row)
