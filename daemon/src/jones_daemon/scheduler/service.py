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

第 1 轮修复记录（round-1 review fixes, see the PR report's own section for detail
on each）:

- **Default `mode` was changed to `"task"`, not `"auto"`** (review #1/#13):
  PRD 9.6/N13's gate on `SessionService.create()` (out of this branch's reach
  at the time) rejects a `mode=auto` child of a `mode=task` parent, and
  `ensure_main_session()` always builds the main session as `mode=task` — so
  the old `mode=auto` default made every out-of-the-box cron dispatch fail,
  every time, 100% of the time. This was the "改前提，不打补丁" fix within
  what this branch could reach at the time: `cron.upsert`'s default became
  `mode="task"`, and the residual gap (an explicit `mode="auto"` cron still
  hitting the 9.6 gate) was left as an open, documented cross-branch item.
  **Superseded in round-3 below**: the cross-branch decision landed, the
  default reverts to PRD 9.1's `"auto"`, and this paragraph is kept only as
  the historical record of round-1's interim choice — see round-3's entry
  and 04-w5-interfaces.md §2.2 for the current state.
- **Cron fields are now interpreted in the local system timezone** (review #4):
  `next_after` itself is timezone-agnostic (operates on whatever tzinfo `dt`
  carries — see `cron_expr.py`); this branch was calling it with `Clock.now()`
  (UTC) directly, so `"0 9 * * *"` fired at 9am UTC, not the user's local 9am — a
  silent wrong-timezone bug in a desktop scheduler, not a documented decision.
  `_next_after_local` now does the UTC<->local boundary conversion around
  `next_after`; storage stays UTC ISO (00-foundation.md §4.1) as before — only the
  *interpretation* of the expression's fields changed.
- **`runtime/` next-trigger snapshot implemented** (review #2): `_write_runtime_
  snapshot` best-effort mirrors every enabled cron's `next_run_at` to
  `runtime/cron_schedule.json` (PRD 10.1's "运行时状态...Cron 下次触发时间"). The DB
  (`crons.next_run_at`) stays the actual authority `_loop` schedules against — this
  file is written for anything that inspects `runtime/` without opening the DB, and
  a write failure here is logged and swallowed, never allowed to take a dispatch
  down with it.
- **Pending approvals on a cron child now reach the main session** (review #3):
  `_watch_and_report`'s poll loop also checks for `gate="user"`/`decision="pending"`
  permission requests on the dispatched child session (`queries.
  list_pending_user_permissions` — a plain read against the shared
  `permission_decisions`/`steps`/`runs` tables, not an import from `sessions/`) and
  posts a one-time system-message reminder into the main session per request
  (PRD 9.4 "待审动作：推回主会话提醒"). This is best-effort: a permission request
  whose `step_id` is `None` (no `ctx_turn` tracked, rare) can't be traced back to a
  session through this join and won't be relayed — a real, documented gap, not a
  silent one; see the PR report.
- **`tasks` rows are no longer write-only** (review #5): `_record_outcome` now
  updates the cron-dispatched `tasks` row to `status="completed"`/`"failed"`
  alongside the existing `crons.fail_count` bookkeeping. `runs.task_id` staying
  `NULL` is `sessions/queries.py::create_run`'s doing, not this branch's files —
  still an open cross-branch item, see the report.
- **Background dispatch/watch tasks no longer swallow non-`RpcError` exceptions**
  (review #6): `_dispatch_body` and `_watch_and_report` each now wrap their whole
  body in `try/except Exception`, logging with `exc_info=True` and still attempting
  an honest `_record_outcome(success=False, ...)` (itself guarded, so a failure
  recording the failure can't compound) — N09/DEV.md 工程原则 #4, matching the
  existing fire-and-forget convention `sessions/service.py::_write_step_payload`
  already uses (log-and-swallow *inside* the coroutine, not via an unretrieved
  task exception).
- **Completion detection now checks the actual dispatched Turn, not just
  "whatever's newest"** (review #7): `send()`'s returned `turn_id` is now threaded
  through to `_watch_and_report`, which treats a `latest_turn` whose `id` doesn't
  match it as "can't tell" (a user's own message became the session's newest Turn)
  rather than silently grading the cron's Run against someone else's Turn.
- **The overlap-skip check is now atomic** (review #9): `_dispatch_body` claims
  `self._in_flight[cron_id]` synchronously, in the same unawaited stretch as the
  membership check, before its first `await` — closing the check-then-set race a
  concurrent `run_now`/tick could previously win.
- **`_loop` no longer dies silently on a DB hiccup** (review #10): each tick now
  runs inside `try/except Exception`; an unexpected error is logged and the loop
  backs off and retries instead of ending the task (which previously meant every
  cron stopped firing forever, with nothing surfacing that anywhere before process
  exit).
- **`_await_completion`/`_watch_and_report` now has a wall-clock ceiling** (review
  #11): a Run stuck `running`/`queued` past `_MAX_RUN_WAIT_SECONDS` is now reported
  as an honest failure (counts toward the 3-strikes auto-disable, frees
  `_in_flight`) instead of polling forever and quietly wedging that cron's overlap
  guard shut for good. `session_service.get()` raising (child session deleted
  mid-poll, e.g. by O's `session.delete`) is now caught the same way, not left to
  end the watch task by exception.
- **`stop()` now cancels stragglers instead of abandoning them** (review #12): if
  the 5s `asyncio.wait` times out, remaining background tasks are `cancel()`led and
  awaited (`return_exceptions=True`) before `stop()` returns — closing the window
  where a still-polling watch task could call into a since-`shutdown()`'d
  `SessionService` or write to an already-`close()`d DB connection after
  `__main__.py`'s shutdown sequence moved on.

第 2 轮修复记录（round-2 review fix）:

- **A pending user approval no longer counts against `max_run_wait_seconds`**
  (review #1, round 2): round-1's wall-clock ceiling (review #11, above) and
  round-1's pending-approval relay (review #3, above) combined into a new bug —
  the ceiling didn't distinguish a genuinely hung Run from one alive and simply
  parked at `gate="user"` waiting on the same user the relay had just proven was
  reachable-but-away (PRD 9.4's "挂起等待用户" is long-lived *by design*). Every
  poll in `_poll_until_done` that still observes at least one pending approval
  now pushes `deadline` back out — false "timeout" failures (and the false
  3-strikes auto-disables and the reopened overlap-guard hole they caused; see
  `_poll_until_done`'s docstring) only happen once nobody is actually waiting on
  the user's decision anymore.

第 3 轮修复记录（round-3, controller 跨分支裁定, 2026-09-19）：

- **Review #1/#13's residual cross-branch gap is now closed, and `mode`'s
  default reverts to PRD 9.1's `"auto"`**: round-1 left a documented, open
  choice among (a) main session defaults to `auto`, (b) a system-dispatch
  exemption from the 9.6 gate, (c) cron stays `mode=task` by default. The
  controller ruled (b): `SessionService.create()` gained a `system_dispatch:
  bool = False` kwarg (not reachable from the `session.create` RPC — only
  this service's own dispatch path can set it) that skips the parent-mode-
  narrowing check for exactly this case — a cron's `mode` is the user's own
  prior, explicit authorization (set in the cron definition, not chosen live
  by an Agent), which is what that gate is meant to allow through; the Agent
  tool-allowlist half of N13 is untouched, still enforced continuously by
  `permissions/gate_config.py`. `_dispatch_body_claimed` now passes
  `system_dispatch=True`. With the actual blocker gone, `cron.upsert`'s
  default `mode` reverts to `"auto"` (PRD 9.1's literal text, and the point
  of FR11's "开箱即用") — round-1's `"task"` default was only ever a stopgap
  against a gate that no longer applies to this path; see `upsert`'s
  docstring and 04-w5-interfaces.md §2/§2.2.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from datetime import UTC, datetime
from typing import Any

from jones_daemon.context import DaemonContext
from jones_daemon.logging import get_logger
from jones_daemon.rpc.errors import INVALID_PARAMS, NOT_FOUND, RpcError
from jones_daemon.scheduler import queries
from jones_daemon.scheduler.clock import Clock, RealClock
from jones_daemon.scheduler.cron_expr import CronExprError, CronSchedule, next_after, parse
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
# Round-1 fix (review #11): a wall-clock ceiling on how long a single dispatched
# Run is watched before this service gives up and reports it as a failure rather
# than polling forever. Not a token/step budget (PRD 11.2's Step/duration caps, if
# any, are `sessions/`'s to enforce on the Run itself) — a conservative backstop
# so a stuck worker/ACP hang can't wedge this cron's overlap guard shut forever.
_MAX_RUN_WAIT_SECONDS = 3600.0
# Round-1 fix (review #10): backoff between `_loop` ticks after an unexpected
# exception (e.g. a transient `sqlite3.OperationalError`) — long enough not to
# hot-loop retrying a wedged DB, short enough that a transient hiccup only delays
# the next real trigger by this much, not forever.
_LOOP_ERROR_BACKOFF_SECONDS = 30.0
# Placeholder written into `_in_flight` for the brief window between "claimed the
# overlap-guard slot" and "the child session id is known" (round-1 fix, review
# #9) — never observed outside that window, just needs to be truthy/distinct.
_CLAIMED_PENDING = "<pending>"
_RUNTIME_SNAPSHOT_FILENAME = "cron_schedule.json"
# How long `stop()` gives in-flight background tasks to finish on their own
# before cancelling whatever's left (review #12) — a module-level constant, not
# a hardcoded literal in `stop()`, purely so tests can shrink it instead of
# actually waiting out 5 real seconds to exercise the cancel-stragglers path.
_STOP_BACKGROUND_WAIT_SECONDS = 5.0
_ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"


def _iso(dt: datetime) -> str:
    return dt.strftime(_ISO_FORMAT)[:-3] + "Z"


def _parse_iso(text: str) -> datetime:
    return datetime.strptime(text, _ISO_FORMAT + "Z").replace(tzinfo=UTC)


def _humanize_name(cron: dict[str, Any]) -> str:
    return cron.get("name") or cron["id"]


def _next_after_local(schedule: CronSchedule, now_utc: datetime) -> datetime:
    """Round-1 fix (review #4): `cron_expr.next_after` is deliberately
    timezone-agnostic (it operates on whatever tzinfo `dt` carries — see its own
    docstring); this is the one place that matters, converting the UTC instant
    `Clock.now()` gives us to the machine's local wall-clock time before asking
    "what's the next match", then converting the (local) answer back to UTC for
    storage. `cron_expr` fields are interpreted in the local system timezone — a
    single-user desktop scheduler where `"0 9 * * *"` should mean *this machine's*
    9am, not UTC 9am (04-w5-interfaces.md §2 records this as the explicit decision
    review #4 asked for; storage format is unaffected, still UTC ISO per
    00-foundation.md §4.1)."""
    local_now = now_utc.astimezone()
    local_next = next_after(schedule, local_now)
    return local_next.astimezone(UTC)


class CronService:
    def __init__(
        self,
        ctx: DaemonContext,
        session_service: Any,
        *,
        clock: Clock | None = None,
        poll_interval_seconds: float = _RUN_POLL_INTERVAL_SECONDS,
        max_run_wait_seconds: float = _MAX_RUN_WAIT_SECONDS,
    ) -> None:
        self.ctx = ctx
        self.session_service = session_service
        self.clock = clock or RealClock()
        # Overridable only for tests (a real deployment has no reason to poll
        # faster/slower than the default) — see `_watch_and_report`.
        self.poll_interval_seconds = poll_interval_seconds
        self.max_run_wait_seconds = max_run_wait_seconds
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()
        # cron_id -> child session id currently running its dispatched Turn — the
        # overlap-skip check (§2 "若已有该 cron 的 Run 在跑则跳过"). Claimed
        # synchronously (see `_CLAIMED_PENDING`) before the child session id is
        # even known, so the check-then-set can't race a concurrent trigger for
        # the same cron (round-1 fix, review #9). In-memory only: a daemon restart
        # already has `SessionService.startup()`'s `interrupt_stale_runs` mark
        # every previously-'running' Run as terminated, so nothing survives a
        # restart for this to need to recover.
        self._in_flight: dict[str, str] = {}

    # -- lifecycle ----------------------------------------------------------------

    async def start(self) -> None:
        main_id = await self.session_service.ensure_main_session()
        await run_in_db_thread(self._reconcile_on_startup, main_id)
        await self._write_runtime_snapshot()
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
            self._loop_task = None
        if self._background_tasks:
            # Round-1 fix (review #12): `asyncio.wait`'s timeout alone neither
            # cancels nor reports on tasks still running past it — they'd keep
            # calling into `session_service`/the DB connection well after
            # `__main__.py` closes both. Cancel whatever's left, then actually
            # wait for that cancellation to land (`return_exceptions=True`: a
            # straggler's `CancelledError`, or any other exception it raises
            # while unwinding, must not stop the others from being awaited too).
            _done, pending = await asyncio.wait(
                self._background_tasks, timeout=_STOP_BACKGROUND_WAIT_SECONDS
            )
            if pending:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

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
                new_next = _iso(_next_after_local(schedule, now))
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

    async def _write_runtime_snapshot(self) -> None:
        """Round-1 fix (review #2): PRD 10.1's 第2类数据 lists "Cron 下次触发时间"
        under `runtime/`. `crons.next_run_at` in the DB is what `_loop` actually
        schedules against (SQLite already survives a crash — 00-foundation.md §6),
        so this file is a redundant, best-effort mirror for anything inspecting
        `runtime/` without opening the DB — not a path this service itself ever
        reads back from. See `_write_runtime_snapshot_sync` for the write itself."""
        crons = await run_in_db_thread(queries.list_enabled_crons, self.ctx.db)
        await asyncio.to_thread(self._write_runtime_snapshot_sync, crons)

    def _write_runtime_snapshot_sync(self, crons: list[dict[str, Any]]) -> None:
        try:
            runtime_dir = self.ctx.paths.runtime_dir()
            payload = {
                "generated_at": _iso(self.clock.now()),
                "crons": [
                    {"id": c["id"], "name": c.get("name"), "next_run_at": c["next_run_at"]}
                    for c in crons
                ],
            }
            target = runtime_dir / _RUNTIME_SNAPSHOT_FILENAME
            tmp_path = target.with_name(f".{target.name}.tmp-{os.getpid()}")
            tmp_path.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp_path, target)  # atomic on the same filesystem
        except OSError:
            # A write failure here must never take a cron trigger down with it —
            # the DB stays the real source of truth (DEV.md 工程原则 #4: still an
            # honest log, just not a fatal one for a file nothing depends on).
            logger.warning("failed to write cron runtime snapshot", exc_info=True)

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
        # PRD 9.1 "Cron 触发的 Run 默认以自动模式运行". Round-1 (review #1/#13)
        # temporarily changed this to `"task"` because the real `create()` gate
        # rejected every default dispatch outright (see the module docstring's
        # "第 1 轮修复记录"); the 跨分支裁定 landed in round-3 below
        # (`system_dispatch`) removes that blocker, so the default reverts to
        # what PRD 9.1 actually specifies. A cron can still be set to
        # `mode="task"` explicitly (PRD 9.1's "用户可在 Cron 定义里改为任务模式").
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
        next_run_at = _iso(_next_after_local(schedule, self.clock.now())) if enabled else None

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
        await self._write_runtime_snapshot()
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
        await self._write_runtime_snapshot()
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
            try:
                await self._loop_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Round-1 fix (review #10): this used to be entirely unguarded —
                # any exception (a transient `sqlite3.OperationalError` from a
                # locked/busy DB, say) ended `_loop_task` for good, silently, with
                # no `except`/done-callback anywhere to ever retrieve it. Every
                # cron would simply stop firing, forever, with nothing surfacing
                # that until process exit (asyncio's own "exception was never
                # retrieved" warning, which nothing forwards to the user). Log and
                # back off instead of dying — one bad tick shouldn't be terminal.
                logger.error(
                    "cron loop tick failed with an unexpected error; backing off "
                    "and retrying rather than letting the loop task die silently",
                    exc_info=True,
                )
                # Plain `asyncio.sleep`, not `self.clock.wait` — the injectable
                # `Clock` exists for schedule-relevant time (§2's "可注入时钟测
                # 试"), not error backoff, and `ManualClock.wait` never times out
                # on its own (it only ever unblocks via `_wake.set()`), which
                # would make an unset-`_wake` retry hang forever under a test
                # clock instead of actually retrying.
                await asyncio.sleep(_LOOP_ERROR_BACKOFF_SECONDS)

    async def _loop_tick(self) -> None:
        self._wake.clear()
        crons = await run_in_db_thread(queries.list_enabled_crons, self.ctx.db)
        now_iso = _iso(self.clock.now())
        due = [c for c in crons if c["next_run_at"] is not None and c["next_run_at"] <= now_iso]
        if due:
            for cron in due:
                # Awaited HERE, not inside the spawned background task: this is
                # what guarantees `next_run_at` is already committed past `now`
                # before this loop can possibly re-list and see the same cron as
                # "due" again (on the very next tick) — a fire-and-forget advance
                # would race the immediate re-list right after and could dispatch
                # the same trigger twice.
                await self._advance_schedule(cron)
                self._spawn_dispatch(cron)
            return
        upcoming = [c["next_run_at"] for c in crons if c["next_run_at"] is not None]
        if not upcoming:
            # Nothing scheduled at all: wait forever, woken only by a config
            # change (`_wake`) — the literal "空闲不轮询" case, no timer armed.
            await self.clock.wait(self._wake, timeout=None)
            return
        delay = max(0.0, (_parse_iso(min(upcoming)) - self.clock.now()).total_seconds())
        await self.clock.wait(self._wake, timeout=delay)

    def _spawn_dispatch(self, cron: dict[str, Any]) -> None:
        """Spawn just the create/send/watch half of dispatch as a background
        task — the schedule-advance half has already been awaited by `_loop_tick`
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
        full dispatch — see `_loop_tick`'s comment."""
        await self._advance_schedule(cron)
        await self._dispatch_body(cron)

    async def _advance_schedule(self, cron: dict[str, Any]) -> None:
        now = self.clock.now()
        try:
            schedule = parse(cron["expr"])
            next_run_at = _iso(_next_after_local(schedule, now))
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
        await self._write_runtime_snapshot()

    async def _dispatch_body(self, cron: dict[str, Any]) -> None:
        """Overlap check + create/send/watch — assumes the schedule has already
        been advanced (by `_dispatch` or by `_loop_tick`'s in-line call) before
        this runs. The overlap check-and-claim (first two lines) is deliberately
        the very first thing this coroutine does, with no `await` between them —
        see `_CLAIMED_PENDING`'s comment and review #9 in the module docstring."""
        cron_id = cron["id"]
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
        self._in_flight[cron_id] = _CLAIMED_PENDING
        try:
            await self._dispatch_body_claimed(cron)
        except Exception:
            # Round-1 fix (review #6): this used to have no top-level guard at
            # all — a `create_cron_task` sqlite error, or anything else raised
            # between the two `try/except RpcError` blocks below, propagated out
            # of the `asyncio.create_task`-spawned coroutine and was silently
            # discarded (`add_done_callback(self._background_tasks.discard)`
            # never retrieves the exception). Log it, clear the overlap guard so
            # this cron isn't wedged shut, and make one best-effort attempt to
            # tell the main session something went wrong instead of just going
            # quiet.
            self._in_flight.pop(cron_id, None)
            logger.error(
                "cron dispatch failed with an unexpected error",
                exc_info=True,
                extra={"detail": {"cron_id": cron_id, "name": cron.get("name")}},
            )
            with contextlib.suppress(Exception):
                main_session_id = await self.session_service.ensure_main_session()
                await self._record_outcome(
                    cron, main_session_id, task_id=None, success=False,
                    text=f"Cron “{_humanize_name(cron)}” 触发失败：内部错误。",
                    meta={
                        "kind": "cron_dispatch_error", "cron_id": cron_id,
                        "error": "internal_error",
                    },
                )

    async def _dispatch_body_claimed(self, cron: dict[str, Any]) -> None:
        cron_id = cron["id"]
        main_session_id = await self.session_service.ensure_main_session()
        try:
            child = await self.session_service.create(
                project_id=cron["project_id"], agent_id=cron["agent_id"],
                parent_id=main_session_id, mode=cron["mode"], title=_humanize_name(cron),
                # Issue #20 跨分支裁定 (2026-09-19, sessions/service.py::create's
                # `system_dispatch` docstring): a cron's mode is the user's own
                # explicit, prior authorization (set in the cron definition, not
                # chosen live by an Agent) — PRD 9.6/N13's "不得比父宽" gate is
                # for agent self-escalation, not this. The Agent tool-allowlist
                # half of N13 is unaffected (enforced independently by
                # permissions/gate_config.py).
                system_dispatch=True,
            )
        except RpcError as exc:
            self._in_flight.pop(cron_id, None)
            await self._record_outcome(
                cron, main_session_id, task_id=None, success=False,
                text=f"Cron “{_humanize_name(cron)}” 触发失败：{exc.message}",
                meta={"kind": "cron_dispatch_error", "cron_id": cron_id, "error": exc.message},
            )
            return
        self._in_flight[cron_id] = child["id"]
        task = await run_in_db_thread(
            queries.create_cron_task, self.ctx.db,
            session_id=child["id"], cron_id=cron_id, title=_humanize_name(cron),
        )
        try:
            sent = await self.session_service.send(child["id"], cron["prompt"])
        except RpcError as exc:
            self._in_flight.pop(cron_id, None)
            await self._record_outcome(
                cron, main_session_id, task_id=task["id"], success=False,
                text=f"Cron “{_humanize_name(cron)}” 触发失败：{exc.message}",
                meta={
                    "kind": "cron_dispatch_error", "cron_id": cron_id,
                    "child_session_id": child["id"], "error": exc.message,
                },
            )
            return
        watch = asyncio.create_task(
            self._watch_and_report(
                cron, main_session_id, child["id"], task["id"], sent.get("turn_id"),
            )
        )
        self._background_tasks.add(watch)

        def _done(_task: asyncio.Task[None], *, cid: str = cron_id) -> None:
            self._background_tasks.discard(_task)
            self._in_flight.pop(cid, None)

        watch.add_done_callback(_done)

    async def _watch_and_report(
        self,
        cron: dict[str, Any],
        main_session_id: str,
        child_session_id: str,
        task_id: str,
        expected_turn_id: str | None,
    ) -> None:
        try:
            outcome, run = await self._poll_until_done(
                cron, main_session_id, child_session_id, expected_turn_id,
            )
        except Exception:
            # Round-1 fix (review #6): same rationale as `_dispatch_body`'s outer
            # guard — a poll-loop exception (other than the ones `_poll_until_done`
            # itself already turns into an honest outcome below) must not just end
            # this task silently. `_done`'s callback still clears `_in_flight`
            # either way; this additionally makes sure the failure is logged and,
            # best-effort, reported to the main session.
            logger.error(
                "cron watch task failed with an unexpected error",
                exc_info=True,
                extra={
                    "detail": {
                        "cron_id": cron["id"], "child_session_id": child_session_id,
                    }
                },
            )
            with contextlib.suppress(Exception):
                await self._record_outcome(
                    cron, main_session_id, task_id=task_id, success=False,
                    text=f"Cron “{_humanize_name(cron)}” 无法判定完成状态：内部错误。",
                    meta={
                        "kind": "cron_watch_error", "cron_id": cron["id"],
                        "child_session_id": child_session_id,
                    },
                )
            return

        if outcome == "completed":
            text = f"Cron “{_humanize_name(cron)}” 已完成。"
            meta: dict[str, Any] = {
                "kind": "cron_completed",
                "cron_id": cron["id"],
                "child_session_id": child_session_id,
            }
            if run is not None:
                meta["run_id"] = run["id"]
            await self._record_outcome(
                cron, main_session_id, task_id=task_id, success=True, text=text, meta=meta,
            )
            return

        # Every other `outcome` value is an honest failure — see
        # `_poll_until_done` for what each one means.
        reason = {
            "gone": "子会话已不存在（可能已被删除），无法判定 cron 结果。",
            "mismatched_turn": (
                "无法判定：子会话最新的 Turn 不是本次 cron 派发的那一个"
                "（可能是用户自己在这个子会话里发了消息）。"
            ),
            "timeout": f"等待超过 {int(self.max_run_wait_seconds)} 秒仍未结束，判定为失败。",
        }.get(outcome)
        if reason is None:
            fallback_reason = f"Turn ended with status {outcome!r}"
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
        else:
            text = f"Cron “{_humanize_name(cron)}” 未成功完成：{reason}"
            meta = {
                "kind": "cron_failed", "cron_id": cron["id"], "child_session_id": child_session_id,
                "card": {"kind": outcome, "message": reason},
            }
        await self._record_outcome(
            cron, main_session_id, task_id=task_id, success=False, text=text, meta=meta,
        )

    async def _poll_until_done(
        self,
        cron: dict[str, Any],
        main_session_id: str,
        child_session_id: str,
        expected_turn_id: str | None,
    ) -> tuple[str, dict[str, Any] | None]:
        """Poll `SessionService.get()` (public, read-only) until the dispatched
        Turn leaves 'queued'/'running' — see the module docstring for why this is
        a bounded poll rather than a callback: nothing on the public
        create/send/get surface offers a "Turn finished" notification to await
        instead.

        Returns `(outcome, run)` where `outcome` is either a real Turn status
        (e.g. `"completed"`/`"terminated"`) or one of three sentinel "couldn't
        tell" outcomes the caller renders as an honest failure: `"gone"` (the
        child session no longer exists — `session.get()` raised `RpcError`),
        `"mismatched_turn"` (the session's newest Turn isn't the one this cron
        dispatched — round-1 fix, review #7), or `"timeout"` (round-1 fix, review
        #11 — `max_run_wait_seconds` elapsed with no resolution *and no pending
        approval seen*; see round-2 fix below).

        Also relays any new `gate="user"` pending permission request on the child
        session back to the main session as it's noticed (round-1 fix, review #3)
        — PRD 9.4 "待审动作：推回主会话提醒".

        Round-2 fix (review #1): `max_run_wait_seconds` is a backstop against a
        genuinely stuck/hung Run (round-1 review #11), not a budget for "how long
        may a Run legitimately wait on the user". A Run parked at `gate="user"`
        on a pending approval is *alive*, not stuck — PRD 9.4's "挂起等待用户"
        is long-lived by design (a user can be away for hours), and
        `list_pending_user_permissions` proves that state is the normal, common
        one for an unattended cron, not an edge case. Treating it as indistinguishable
        from a hung worker used to (a) post a false "判定为失败" system message
        for a Run that hadn't actually failed, (b) count that false failure
        toward the 3-strikes auto-disable (a cron could get disabled purely
        because nobody was at the keyboard for 3 hours), and (c) release
        `_in_flight` out from under a Run that was still very much alive,
        reopening exactly the overlap-guard hole round-1 review #9 closed (a new
        Run could then be dispatched for the same cron while the first still sat
        waiting for approval). So: every poll that still observes at least one
        pending approval pushes `deadline` back out another full
        `max_run_wait_seconds` — the ceiling only ever fires against genuine
        silence (no pending approval *and* no Turn progress), never against an
        honest, visible wait for the user. Once the approval is decided (denied
        or approved), `pending` goes empty on the next poll and the ordinary
        ceiling applies again from there.
        """
        deadline = time.monotonic() + self.max_run_wait_seconds
        notified_permissions: set[str] = set()
        while True:
            pending = await run_in_db_thread(
                queries.list_pending_user_permissions, self.ctx.db, child_session_id,
            )
            if pending:
                deadline = time.monotonic() + self.max_run_wait_seconds
            for req in pending:
                if req["decision_id"] in notified_permissions:
                    continue
                notified_permissions.add(req["decision_id"])
                permission_row = await run_in_db_thread(
                    queries.insert_system_message,
                    self.ctx.db,
                    session_id=main_session_id,
                    text=(
                        f"Cron “{_humanize_name(cron)}” 的子会话有一个待审动作（风险："
                        f"{req['risk']}），请前往子会话查看并处理。"
                    ),
                    meta={
                        "kind": "cron_permission_pending",
                        "cron_id": cron["id"],
                        "child_session_id": child_session_id,
                        "decision_id": req["decision_id"],
                        "risk": req["risk"],
                    },
                )
                # Mirrors `_record_outcome`'s own broadcast — the main session's
                # subscribed connections (if any) find out the same way they do
                # for any other cron-posted system message.
                await self.ctx.server.broadcast(
                    main_session_id, "message.completed", permission_row
                )

            try:
                session = await self.session_service.get(child_session_id)
            except RpcError:
                return "gone", None

            turn = session.get("latest_turn")
            if turn is not None and turn.get("status") not in ("queued", "running"):
                if expected_turn_id is not None and turn.get("id") != expected_turn_id:
                    return "mismatched_turn", None
                run = None
                run_id = turn.get("run_id")
                if run_id:
                    with contextlib.suppress(RpcError):
                        run = await self.session_service.run_get(run_id)
                return turn["status"], run

            if time.monotonic() >= deadline:
                return "timeout", None
            await asyncio.sleep(self.poll_interval_seconds)

    async def _record_outcome(
        self,
        cron: dict[str, Any],
        main_session_id: str,
        *,
        task_id: str | None,
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
            # Round-1 fix (review #5): the `tasks` row this cron dispatch created
            # (`create_cron_task`) used to never be updated again — every cron
            # Run's task stayed `status="running"` forever, success or failure.
            if task_id is not None:
                queries.mark_cron_task_status(
                    self.ctx.db, task_id, status="completed" if success else "failed",
                )
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
            await self._write_runtime_snapshot()
