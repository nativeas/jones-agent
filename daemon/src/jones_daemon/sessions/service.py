"""SessionService — Session/Turn/queue orchestration + the ACP event -> DB/broadcast
translation (docs/design/01-w2-interfaces.md §2, 00-foundation.md §4/§7/§8).

One instance is constructed in `__main__.py` and shared by every `sessions/methods.py`
RPC handler. It owns:
  - the Session/Turn/Message/Run/Step/queue state machine (PRD 9.2, 9.3),
  - translating `AcpClient` callbacks (`session/update`, `session/request_permission`)
    into DB writes + `RpcServer.broadcast` notifications — this is the "daemon 侧写"
    half of 00-foundation.md §7's "审计写入时序": the `jones_gate` plugin's `approve`
    branch never writes anything itself, it only ever returns immediately (see that
    file's module docstring) and the actual audit trail is written here, driven by
    ACP events the worker later reports back.

Known W2 scope gaps (see the PR report): per-mode tool suppression (chat mode should
refuse to run tools at all — PRD 9.1/N12) and Goal/budget-triggered termination (PRD
9.3's third termination kind — Goals are FR17, not #10) are not implemented here;
nothing in this file pretends otherwise.

W3/#12 (docs/design/02-w3-interfaces.md §2) closed two adjacent pieces of the W2
gap list above, scoped narrowly to what FR06 回放 actually needs:
  - `_handle_tool_call_update` now also writes a Step's full `rawOutput` to
    `replay/store.py` (`payload_ref`) — `result_summary` stays a truncated text
    summary for quick UI rendering, the payload file is the full-fidelity replay
    source.
  - `_run_turn` now calls `ctx.providers.resolve()` as a fail-fast pre-flight
    check *before* asking `WorkerManager` to spawn a worker — a Session whose
    Agent has no usable provider/Key terminates immediately with a
    `provider_error`-flavored `run.terminated{kind:"error"}` card instead of
    either silently trying to launch a worker that can't do anything useful, or
    (worse, on a machine without `hermes-agent`'s `worker` extra installed —
    every CI runner and most dev machines, see docs/DEV.md) failing with an
    opaque "worker startup failed" card that looks identical whether the real
    problem is "no API key" or "hermes-agent isn't installed". This does **not**
    wire the resolved `ProviderBinding` (env vars, `hermes_config`) into the
    worker's actual subprocess env/`config.yaml` — that plumbing lives in
    `WorkerManager.ensure_started`/`_worker_env`/`_prepare_hermes_home`, which
    02-w3-interfaces.md §0 assigns to F, not G; a worker started after this
    check passes still launches with no `model:`/`providers:` block, exactly as
    before. See the PR report's "契约变更" section.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from jones_daemon.context import DaemonContext
from jones_daemon.kernel.acp_client import AcpError, AcpProtocolError
from jones_daemon.kernel.ids import new_ulid
from jones_daemon.logging import get_logger
from jones_daemon.projects.service import ProjectService
from jones_daemon.providers.resolver import ProviderNotConfiguredError
from jones_daemon.replay import retention as replay_retention
from jones_daemon.replay import store as replay_store
from jones_daemon.rpc.errors import INVALID_PARAMS, INVALID_STATE, NOT_FOUND, RpcError
from jones_daemon.sessions import queries
from jones_daemon.store import run_in_db_thread
from jones_daemon.workers.manager import WorkerManager, WorkerStartupError

logger = get_logger("sessions")

_VALID_MODES = {"chat", "task", "auto"}

# Fixed by 002_seed_defaults_and_queue_turn.sql — see that file's header for why
# these ids (not their row contents) are the stable contract surface until C
# (#8/#9) lands real Project/Agent.
DEFAULT_PROJECT_ID = "proj_default"
DEFAULT_AGENT_ID = "agent_default"


def _extract_text(content: Any) -> str:
    if isinstance(content, dict):
        return content.get("text") or ""
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def _select_permission_option(
    options: list[dict[str, Any]], decision: str, remember: str | None
) -> str:
    wanted_kind = {
        ("allow", False): "allow_once",
        ("allow", True): "allow_always",
        ("deny", False): "reject_once",
        ("deny", True): "reject_always",
    }[(decision, bool(remember))]
    for opt in options:
        if opt.get("kind") == wanted_kind:
            return opt["optionId"]
    prefix = "allow_" if decision == "allow" else "reject_"
    for opt in options:
        if str(opt.get("kind", "")).startswith(prefix):
            return opt["optionId"]
    raise RpcError(
        INVALID_STATE,
        f"worker did not offer a {decision!r} permission option",
        {"options": options},
    )


@dataclass
class _TurnContext:
    turn_id: str
    run_id: str
    session_id: str
    assistant_message_id: str | None = None
    assistant_text: str = ""
    thinking_message_id: str | None = None
    thinking_text: str = ""
    tool_call_steps: dict[str, str] = field(default_factory=dict)
    step_seq: int = 0
    step_started_at: dict[str, float] = field(default_factory=dict)
    # step_id -> its own `seq` — `_write_step_payload` (FR06 回放, 02-w3-interfaces.md
    # §2) needs the seq *that step* was assigned at start time, not `step_seq`'s
    # current value (which may have advanced past it by the time a `tool_call_update`
    # for an earlier, still-in-flight call arrives).
    step_seq_by_id: dict[str, int] = field(default_factory=dict)


@dataclass
class _PendingPermission:
    session_id: str
    params: dict[str, Any]
    future: asyncio.Future[dict[str, Any]]


class SessionService:
    def __init__(self, ctx: DaemonContext, *, worker_cmd: list[str] | None = None) -> None:
        self.ctx = ctx
        self.worker_manager = WorkerManager(
            user_root=ctx.paths.user_root(),
            on_session_update=self._on_session_update,
            on_request_permission=self._on_request_permission,
            on_worker_crash=self._on_worker_crash,
            worker_cmd=worker_cmd,
        )
        self._active_turns: dict[str, _TurnContext] = {}
        self._turn_tasks: dict[str, asyncio.Task[None]] = {}
        self._pending_permissions: dict[str, _PendingPermission] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        # FR06 回放 (02-w3-interfaces.md §2): background Step-payload writes
        # (`_write_step_payload`) and the retention sweep loop
        # (`replay/retention.py`) are tracked here so `shutdown()` can wait for
        # in-flight writes and cancel the sweep cleanly, instead of leaving
        # dangling tasks nobody awaits.
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._retention_task: asyncio.Task[None] | None = None

    def _lock(self, session_id: str) -> asyncio.Lock:
        return self._session_locks.setdefault(session_id, asyncio.Lock())

    # -- lifecycle --------------------------------------------------------------

    async def startup(self) -> None:
        interrupted = await run_in_db_thread(queries.interrupt_stale_runs, self.ctx.db)
        if interrupted:
            logger.warning(
                "marked stale Runs from a previous process as terminated",
                extra={"detail": {"count": interrupted}},
            )
        await self.worker_manager.start()
        await self.ensure_main_session()
        # FR06 回放保留策略 (02-w3-interfaces.md §2/§3: "清理在空闲时跑") — a plain
        # background loop, not tied to any request's critical path.
        self._retention_task = asyncio.create_task(replay_retention.run_sweep_loop(self.ctx))

    async def shutdown(self) -> None:
        # Close off every still-pending permission wait before tearing workers
        # down — otherwise `AcpClient`'s background task answering it (kernel/
        # acp_client.py) is left awaiting a future nobody will ever resolve.
        self._resolve_pending_permissions(reason="daemon shutdown")
        await self.worker_manager.stop()
        if self._retention_task is not None:
            self._retention_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._retention_task
        # Let already-scheduled Step-payload writes (`_write_step_payload`)
        # finish rather than abandoning them mid-write — bounded, not indefinite:
        # a shutdown must still make forward progress even if a write is stuck
        # (e.g. a full disk). `return_exceptions=True` because a background
        # write task already logs its own failures (see `_write_step_payload`);
        # this wait exists to give it time to do so, not to re-raise here.
        if self._background_tasks:
            await asyncio.wait(self._background_tasks, timeout=5.0)

    def _resolve_pending_permissions(self, *, session_id: str | None = None, reason: str) -> None:
        """Close off pending `session/request_permission` waits (all of them, or
        just one session's) with a `cancelled` outcome.

        A pending permission blocks the *worker's* own progress on our answer,
        not the other way around — if the worker's read/dispatch loop is itself
        stuck waiting on this Turn's tool call, nothing (`stop()`, a crash,
        shutdown) can make forward progress on that Turn until we answer it one
        way or another (00-foundation.md §8.1/§7). `kernel/acp_client.py`'s read
        loop no longer blocks on answering this itself (round-1 review fix — see
        this PR report's "第 1 轮修复记录"), but nothing else ever resolves this
        future for a session that got stopped or whose worker died mid-request
        without this.
        """
        for entry in list(self._pending_permissions.values()):
            if session_id is not None and entry.session_id != session_id:
                continue
            if not entry.future.done():
                entry.future.set_result({"outcome": {"outcome": "cancelled"}})

    async def ensure_main_session(self) -> str:
        existing = await run_in_db_thread(queries.get_main_session, self.ctx.db)
        if existing is not None:
            return existing["id"]
        session_id = new_ulid()
        await run_in_db_thread(
            queries.create_session,
            self.ctx.db,
            session_id=session_id,
            project_id=DEFAULT_PROJECT_ID,
            agent_id=DEFAULT_AGENT_ID,
            parent_id=None,
            is_main=True,
            mode="task",
            title="Main",
        )
        logger.info("created main session", extra={"detail": {"session_id": session_id}})
        return session_id

    async def _cwd_for_project(self, project_id: str) -> str:
        """02-w3-interfaces.md §2 集成收口 #1: C (#8/#9) has since landed real
        `projects` rows (including `proj_default`'s real, per-machine path via
        `ProjectService.ensure_default_project()` at daemon startup — see
        __main__.py) — use `ProjectService.get()` for every project, not just a
        hardcoded home-directory special case for the default one. `get()` itself
        already raises `RpcError(NOT_FOUND, ...)` for an unknown id, which is the
        right error here too (a Session referencing a deleted Project)."""
        project = await run_in_db_thread(ProjectService(self.ctx.db).get, project_id)
        return project["path"]

    # -- Session CRUD -------------------------------------------------------------

    async def create(
        self,
        *,
        project_id: str,
        agent_id: str,
        parent_id: str | None = None,
        mode: str = "task",
        title: str | None = None,
    ) -> dict[str, Any]:
        if mode not in _VALID_MODES:
            raise RpcError(INVALID_PARAMS, f"invalid mode: {mode!r}", {"mode": mode})
        if parent_id is not None:
            parent = await run_in_db_thread(queries.get_session, self.ctx.db, parent_id)
            if parent is None:
                raise RpcError(NOT_FOUND, "parent session not found", {"parent_id": parent_id})
            if parent["mode"] == "task" and mode == "auto":
                # PRD 9.6 "权限不放大" / N13: a task-mode parent may not derive an
                # auto-mode child (tightening is fine, the reverse is not).
                raise RpcError(
                    INVALID_STATE,
                    "child session cannot be mode=auto when its parent is mode=task (PRD 9.6, N13)",
                    {"parent_mode": parent["mode"], "requested_mode": mode},
                )
        session_id = new_ulid()
        return await run_in_db_thread(
            queries.create_session,
            self.ctx.db,
            session_id=session_id,
            project_id=project_id,
            agent_id=agent_id,
            parent_id=parent_id,
            is_main=False,
            mode=mode,
            title=title,
        )

    async def get(self, session_id: str) -> dict[str, Any]:
        row = await run_in_db_thread(queries.get_session, self.ctx.db, session_id)
        if row is None:
            raise RpcError(NOT_FOUND, "session not found", {"id": session_id})
        turn = await run_in_db_thread(queries.latest_turn, self.ctx.db, session_id)
        return {**row, "latest_turn": turn}

    async def list(self, project_id: str | None = None) -> list[dict[str, Any]]:
        return await run_in_db_thread(queries.list_sessions, self.ctx.db, project_id=project_id)

    async def set_mode(self, session_id: str, mode: str) -> dict[str, Any]:
        if mode not in _VALID_MODES:
            raise RpcError(INVALID_PARAMS, f"invalid mode: {mode!r}", {"mode": mode})
        session = await run_in_db_thread(queries.get_session, self.ctx.db, session_id)
        if session is None:
            raise RpcError(NOT_FOUND, "session not found", {"id": session_id})
        if session["parent_id"]:
            parent = await run_in_db_thread(queries.get_session, self.ctx.db, session["parent_id"])
            if parent is not None and parent["mode"] == "task" and mode == "auto":
                raise RpcError(
                    INVALID_STATE,
                    "child session cannot be mode=auto when its parent is mode=task (PRD 9.6, N13)",
                    {"parent_mode": parent["mode"], "requested_mode": mode},
                )
        row = await run_in_db_thread(queries.set_session_mode, self.ctx.db, session_id, mode)
        if row is None:
            raise RpcError(NOT_FOUND, "session not found", {"id": session_id})
        return row

    # -- send / queue -------------------------------------------------------------

    async def send(
        self, session_id: str, text: str, attachments: list[Any] | None = None
    ) -> dict[str, Any]:
        session = await run_in_db_thread(queries.get_session, self.ctx.db, session_id)
        if session is None:
            raise RpcError(NOT_FOUND, "session not found", {"id": session_id})
        async with self._lock(session_id):
            running = session_id in self._active_turns or (
                session_id in self._turn_tasks and not self._turn_tasks[session_id].done()
            )
            turn_id = new_ulid()
            message_id = new_ulid()
            await run_in_db_thread(
                queries.create_turn_and_user_message,
                self.ctx.db,
                turn_id=turn_id,
                message_id=message_id,
                session_id=session_id,
                text=text,
                queued=running,
            )
            if running:
                await run_in_db_thread(
                    queries.enqueue,
                    self.ctx.db,
                    session_id=session_id,
                    turn_id=turn_id,
                    text=text,
                    attachments=attachments,
                )
                items = await run_in_db_thread(queries.list_queue_items, self.ctx.db, session_id)
                await self.ctx.server.broadcast(
                    session_id, "queue.changed", {"session_id": session_id, "items": items}
                )
                return {"turn_id": turn_id, "queued": True}
            self._start_turn(session_id, turn_id, text)
            return {"turn_id": turn_id, "queued": False}

    async def stop(self, session_id: str) -> dict[str, Any]:
        session = await run_in_db_thread(queries.get_session, self.ctx.db, session_id)
        if session is None:
            raise RpcError(NOT_FOUND, "session not found", {"id": session_id})
        task = self._turn_tasks.get(session_id)
        if task is None or task.done():
            return {"stopped": False}
        worker = self.worker_manager.get(session_id)
        if worker is not None and worker.client is not None and worker.acp_session_id is not None:
            try:
                # A notification, not a hard kill: PRD 9.3 "用户终止" requires the
                # in-flight tool call to finish cleanly. `cancel()` sets Hermes's
                # cancel_event; `_run_turn`'s `prompt()` call returns normally with
                # `stopReason="cancelled"` once the agent notices, which
                # `_finalize_turn_success` turns into `run.terminated{kind:"user"}`.
                await worker.client.cancel(worker.acp_session_id)
            except (AcpProtocolError, AcpError) as exc:
                logger.warning(
                    "session/cancel notification failed (worker likely already gone)",
                    extra={"detail": {"session_id": session_id, "error": str(exc)}},
                )
        # If this Turn's in-flight tool call is sitting on a pending
        # `session/request_permission` (no one has answered it yet — the user
        # just clicked "stop" instead of allow/deny), `cancel()` alone can't
        # unblock it: the worker won't notice the cancellation until *we* answer
        # its pending request one way or another. Round-1 review fix — without
        # this, `stop()` during a pending approval never produces `run.terminated`
        # (see this PR report's "第 1 轮修复记录").
        self._resolve_pending_permissions(session_id=session_id, reason="stopped by user")
        return {"stopped": True}

    async def queue(self, session_id: str) -> list[dict[str, Any]]:
        return await run_in_db_thread(queries.list_queue_items, self.ctx.db, session_id)

    async def queue_remove(self, session_id: str, item_id: str) -> list[dict[str, Any]]:
        async with self._lock(session_id):
            turn_id = await run_in_db_thread(
                queries.remove_queue_item, self.ctx.db, session_id=session_id, item_id=item_id
            )
            if turn_id is None:
                raise RpcError(NOT_FOUND, "queue item not found", {"item_id": item_id})
            await run_in_db_thread(queries.cancel_turn, self.ctx.db, turn_id)
        items = await run_in_db_thread(queries.list_queue_items, self.ctx.db, session_id)
        await self.ctx.server.broadcast(
            session_id, "queue.changed", {"session_id": session_id, "items": items}
        )
        return items

    async def queue_reorder(self, session_id: str, item_ids: list[str]) -> list[dict[str, Any]]:
        async with self._lock(session_id):
            await run_in_db_thread(
                queries.reorder_queue_items, self.ctx.db, session_id=session_id, item_ids=item_ids
            )
        items = await run_in_db_thread(queries.list_queue_items, self.ctx.db, session_id)
        await self.ctx.server.broadcast(
            session_id, "queue.changed", {"session_id": session_id, "items": items}
        )
        return items

    async def turn_messages(
        self, session_id: str, *, before: int | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        return await run_in_db_thread(
            queries.list_turn_messages,
            self.ctx.db,
            session_id=session_id,
            before_seq=before,
            limit=limit,
        )

    async def run_list(self, session_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """回放视图"选一个 Run"（02-w3-interfaces.md §2) — `run.list`, a contract
        addition on top of 00-foundation.md §4.1 (see this file's module
        docstring / the PR report's "契约变更"): that table never had a way to
        discover a historical Run id besides the live `turn.started` notification."""
        return await run_in_db_thread(
            queries.list_runs_for_session, self.ctx.db, session_id, limit=limit
        )

    async def run_get(self, run_id: str) -> dict[str, Any]:
        """FR06 回放 (02-w3-interfaces.md §2): the `runs` row already carries every
        piece of "终止信息" this method needs to return — `terminated_kind`/
        `terminated_reason` (existing) plus `terminated_step_seq` (this issue,
        migration 005) — and `prompt_snapshot_ref` is the readable reference the
        client fetches via `run.payload` (never inlined here: it can be
        arbitrarily large, same reasoning as Step payloads)."""
        row = await run_in_db_thread(queries.get_run, self.ctx.db, run_id)
        if row is None:
            raise RpcError(NOT_FOUND, "run not found", {"run_id": run_id})
        return row

    async def run_steps(
        self, run_id: str, *, after_seq: int | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """FR06 回放分页 (02-w3-interfaces.md §2): `after_seq`/`limit` page forward
        through a Run's Steps in `seq` order — the same order the replay UI's
        前进/后退 stepping walks, so "next page" and "step forward past what's
        loaded" are the same request shape."""
        return await run_in_db_thread(
            queries.list_run_steps, self.ctx.db, run_id, after_seq=after_seq, limit=limit
        )

    async def run_payload(
        self, ref: str, *, offset: int = 0, limit: int | None = None
    ) -> dict[str, Any]:
        """FR06 回放, 02-w3-interfaces.md §2: `run.payload {ref}` — full-fidelity
        Step output / prompt snapshot, fetched on demand (never pushed proactively
        — "回放时按需加载", PRD 10.3). `>1MB` chunking is `offset`/`limit`, not a
        separate method — see `replay/store.py::CHUNK_THRESHOLD_BYTES`.

        `ref` must resolve to a real file under `<user_root>/runs/` — see
        `replay/store.py::_resolve_ref` for why that's checked even though every
        `ref` this method is ever called with should already have come from a
        `steps.payload_ref`/`runs.prompt_snapshot_ref` column (defense in depth,
        not a first line of defense)."""
        user_root = self.ctx.paths.user_root()
        try:
            size = await asyncio.to_thread(replay_store.payload_size, user_root, ref)
            data = await asyncio.to_thread(
                replay_store.read_payload, user_root, ref, offset=offset, limit=limit
            )
        except replay_store.PayloadRefError as exc:
            raise RpcError(NOT_FOUND, str(exc), {"ref": ref}) from exc
        return {
            "ref": ref,
            "offset": offset,
            "size": size,
            "data_base64": base64.b64encode(data).decode("ascii"),
            "eof": offset + len(data) >= size,
        }

    def active_turn_session_ids(self) -> list[str]:
        """`daemon.status`'s `sessions_active` (02-w3-interfaces.md §2 集成收口 #2,
        `rpc/methods.py::register_daemon_status`) — a plain accessor over
        `_active_turns`, the same dict `stop()`/`_advance_queue()` already treat
        as "this Session currently has a Turn running"."""
        return list(self._active_turns.keys())

    # -- permissions ----------------------------------------------------------------

    async def permission_pending(self, session_id: str | None = None) -> list[dict[str, Any]]:
        out = []
        for decision_id, entry in self._pending_permissions.items():
            if session_id is not None and entry.session_id != session_id:
                continue
            tool_call = entry.params.get("toolCall") or {}
            out.append(
                {
                    "request_id": decision_id,
                    "session_id": entry.session_id,
                    "gate": "user",
                    # Risk scoring is W3 FR05 (review gate) scope — W2 only ever
                    # produces user-gate requests (jones_gate's approve branch),
                    # so there is nothing here to classify yet.
                    "risk": "unclassified",
                    "tool_call": tool_call,
                    "options": entry.params.get("options"),
                }
            )
        return out

    async def permission_decide(
        self, request_id: str, decision: str, remember: str | None = None
    ) -> dict[str, Any]:
        if decision not in ("allow", "deny"):
            raise RpcError(
                INVALID_PARAMS, f"invalid decision: {decision!r}", {"decision": decision}
            )
        entry = self._pending_permissions.get(request_id)
        if entry is None:
            raise RpcError(NOT_FOUND, "no pending permission request", {"request_id": request_id})
        option_id = _select_permission_option(entry.params.get("options") or [], decision, remember)
        row = await run_in_db_thread(
            queries.decide_permission, self.ctx.db, request_id, decision=decision, decided_by="user"
        )
        if not entry.future.done():
            entry.future.set_result({"outcome": {"outcome": "selected", "optionId": option_id}})
        assert row is not None  # noqa: S101 - just written by decide_permission above
        await self.ctx.server.broadcast(entry.session_id, "permission.decided", row)
        return row

    # -- turn execution ---------------------------------------------------------------

    def _start_turn(self, session_id: str, turn_id: str, text: str) -> None:
        task = asyncio.create_task(self._run_turn(session_id, turn_id, text))
        self._turn_tasks[session_id] = task

        def _cleanup(_t: asyncio.Task[None]) -> None:
            if self._turn_tasks.get(session_id) is task:
                del self._turn_tasks[session_id]

        task.add_done_callback(_cleanup)

    async def _run_turn(self, session_id: str, turn_id: str, text: str) -> None:
        run_id = new_ulid()
        await run_in_db_thread(
            queries.create_run, self.ctx.db, run_id=run_id, turn_id=turn_id, session_id=session_id
        )
        # FR06 回放, 02-w3-interfaces.md §2: `runs.prompt_snapshot_ref` — ACP
        # doesn't expose the fully assembled prompt actually sent to the model
        # (00-foundation.md §7), so this records, honestly, only what's available
        # at Run-start: the user's own message plus enough identifiers to look up
        # the rest (Agent/mode) — not a fabricated "here's the system prompt".
        # Best-effort: a failure to write this must not abort the Turn itself
        # (fail loud via the log, not fail the whole Run over a replay nicety).
        try:
            snapshot_ref = await asyncio.to_thread(
                replay_store.write_prompt_snapshot,
                self.ctx.paths.user_root(),
                run_id,
                {
                    "note": (
                        "Hermes ACP does not expose the fully assembled prompt sent to "
                        "the model (docs/design/00-foundation.md §7); recording what is "
                        "available."
                    ),
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "run_id": run_id,
                    "user_message": text,
                    "captured_at": queries.iso_now(),
                },
            )
            await run_in_db_thread(
                queries.set_prompt_snapshot_ref, self.ctx.db, run_id, snapshot_ref
            )
        except OSError:
            logger.error(
                "failed to write prompt snapshot for a Run",
                exc_info=True,
                extra={"detail": {"run_id": run_id}},
            )
        ctx_turn = _TurnContext(turn_id=turn_id, run_id=run_id, session_id=session_id)
        self._active_turns[session_id] = ctx_turn
        self.worker_manager.mark_busy(session_id, True)
        # RPC v0 §4.2's `turn.started {session_id, turn_id, run_id}` — the only
        # signal the front end has that a queued Turn actually started running
        # (the queue-auto-advance path never calls `send()`, so it has nothing
        # else to watch for this).
        await self.ctx.server.broadcast(
            session_id, "turn.started",
            {"session_id": session_id, "turn_id": turn_id, "run_id": run_id},
        )
        try:
            try:
                session = await run_in_db_thread(queries.get_session, self.ctx.db, session_id)
                cwd = await self._cwd_for_project(session["project_id"])
                # 集成收口 (02-w3-interfaces.md §2 item 4's `pnpm e2e` needs this to
                # be a real, distinguishable failure mode, not just documentation):
                # fail fast on a Session whose Agent has no usable provider/Key
                # *before* ever spawning a worker subprocess — otherwise "no key
                # configured" and "hermes-agent isn't installed on this machine"
                # (see docs/DEV.md — the common case for every CI runner and most
                # dev checkouts) both surface as the same opaque "worker startup
                # failed" card. Does not wire the resolved `ProviderBinding` into
                # the worker itself — see this file's module docstring.
                model_pref = await run_in_db_thread(
                    queries.get_agent_model_pref, self.ctx.db, session["agent_id"]
                )
                try:
                    await run_in_db_thread(self.ctx.providers.resolve, model_pref)
                except (RpcError, ProviderNotConfiguredError) as exc:
                    message = exc.message if isinstance(exc, RpcError) else str(exc)
                    await self._terminate_run(
                        ctx_turn, kind="error", reason=f"provider_error: {message}"
                    )
                    return
                try:
                    worker = await self.worker_manager.ensure_started(session_id, cwd=cwd)
                except WorkerStartupError as exc:
                    await self._terminate_run(
                        ctx_turn, kind="error", reason=f"worker startup failed: {exc}"
                    )
                    return
                assert worker.client is not None and worker.acp_session_id is not None  # noqa: S101
                try:
                    response = await worker.client.prompt(worker.acp_session_id, text)
                except (AcpError, AcpProtocolError) as exc:
                    # The worker may have streamed part of an assistant reply via
                    # `message.delta` before crashing/disconnecting — that text only
                    # lives in `ctx_turn` until finalized, so without this the DB
                    # (and `turn.messages` replay, 00-foundation.md §5/PRD FR06)
                    # would show an empty assistant message even though the user
                    # already saw real text stream past.
                    await self._finalize_streamed_messages(ctx_turn)
                    await self._terminate_run(
                        ctx_turn, kind="error", reason=f"ACP prompt failed: {exc}"
                    )
                    return
                await self._finalize_turn_success(ctx_turn, response)
            except Exception as exc:  # noqa: BLE001 - contract §7: any worker/subprocess
                # failure must become `run.terminated` or `daemon.error`, never a
                # silently-dropped task (this coroutine is never awaited by anyone
                # once scheduled via `asyncio.create_task` in `_start_turn`) — this
                # is the fail-closed backstop for failure modes the two narrower
                # `except` blocks above don't name (e.g. `_prepare_hermes_home`'s
                # filesystem errors surfacing as something other than
                # `WorkerStartupError`, `_cwd_for_project`'s `RpcError`, a DB write
                # failure). DEV.md 工程原则 #4 "诚实失败".
                logger.error(
                    "_run_turn failed with an unhandled exception",
                    exc_info=True,
                    extra={"detail": {"session_id": session_id, "turn_id": turn_id}},
                )
                await self._finalize_streamed_messages(ctx_turn)
                await self._terminate_run(
                    ctx_turn, kind="error", reason=f"unexpected error: {exc}"
                )
        finally:
            await self._advance_queue(session_id)

    async def _finalize_streamed_messages(self, ctx_turn: _TurnContext) -> None:
        """Persist whatever assistant/thinking text was streamed via `message.delta`
        so far and broadcast `message.completed` for it. Safe to call more than
        once or with nothing streamed yet (both message ids stay `None`, so this is
        a no-op) — every termination path (success, user stop, worker error) must
        call this before `_terminate_run`/`mark_run_completed`, or the streamed
        text a user already saw never reaches the `messages` table (00-foundation.md
        §5's replay source of truth, PRD FR06)."""
        if ctx_turn.assistant_message_id:
            row = await run_in_db_thread(
                queries.finalize_message, self.ctx.db, ctx_turn.assistant_message_id,
                kind="text", text=ctx_turn.assistant_text,
            )
            await self.ctx.server.broadcast(ctx_turn.session_id, "message.completed", row)
            ctx_turn.assistant_message_id = None
        if ctx_turn.thinking_message_id:
            row = await run_in_db_thread(
                queries.finalize_message, self.ctx.db, ctx_turn.thinking_message_id,
                kind="thinking", text=ctx_turn.thinking_text,
            )
            await self.ctx.server.broadcast(ctx_turn.session_id, "message.completed", row)
            ctx_turn.thinking_message_id = None

    async def _finalize_turn_success(
        self, ctx_turn: _TurnContext, response: dict[str, Any]
    ) -> None:
        await self._finalize_streamed_messages(ctx_turn)
        if response.get("stopReason") == "cancelled":
            await self._terminate_run(ctx_turn, kind="user", reason="stopped by user")
            return
        await run_in_db_thread(
            queries.mark_run_completed, self.ctx.db, ctx_turn.run_id, ctx_turn.turn_id
        )

    async def _terminate_run(self, ctx_turn: _TurnContext, *, kind: str, reason: str) -> None:
        # FR06 回放 "终止记录" (02-w3-interfaces.md §2): `ctx_turn.step_seq` is the
        # seq of the last Step started on this Run (0 if none ever started) —
        # exactly "终止时的 step_seq", so replay can show which Step was in
        # flight (or that none had started yet) when the Run ended.
        await run_in_db_thread(
            queries.mark_run_terminated,
            self.ctx.db,
            ctx_turn.run_id,
            ctx_turn.turn_id,
            kind=kind,
            reason=reason,
            terminated_step_seq=ctx_turn.step_seq or None,
        )
        card = {"kind": kind, "message": reason}
        await self.ctx.server.broadcast(
            ctx_turn.session_id,
            "run.terminated",
            {"run_id": ctx_turn.run_id, "kind": kind, "reason": reason, "card": card},
        )

    async def _advance_queue(self, session_id: str) -> None:
        # Deliberately NOT called from `startup()` — this is what keeps normal
        # queue auto-advance (PRD 9.2) and "restart never auto-sends" (PRD 9.2/
        # G10/N04) from being the same code path: a restart marks Runs
        # "terminated" via `interrupt_stale_runs`, it never calls this, so queued
        # items simply stay `pending` until a user explicitly re-sends.
        #
        # Everything that decides "is this session still running" must happen
        # under `self._lock(session_id)` — the same lock `send()` takes to decide
        # immediate-run vs. enqueue. Without it, a `send()` racing in right as a
        # Turn finishes can read `_turn_tasks[session_id]` as "not done yet" (the
        # finishing task hasn't returned from this very function yet), enqueue
        # behind it, and then find this function already committed to "nothing
        # left to pop" and returned — nothing will ever call `_advance_queue`
        # again for that item, so it stays `pending` forever. Round-1 review fix;
        # see this PR report's "第 1 轮修复记录" for the reproduction.
        async with self._lock(session_id):
            self._active_turns.pop(session_id, None)
            self.worker_manager.mark_busy(session_id, False)
            item = await run_in_db_thread(queries.pop_next_queue_item, self.ctx.db, session_id)
            if item is not None:
                # No explicit "mark this queued Turn running" write here: `_run_turn`
                # (via `queries.create_run`) already flips `turns.status` to
                # 'running' as soon as it starts, so a separate call here would
                # just be the same write twice.
                self._start_turn(session_id, item["turn_id"], item["text"])
            else:
                # Nothing more queued: drop *this* (finishing) task's own
                # registration now, while still holding the lock, instead of
                # waiting for its `add_done_callback` cleanup to fire after this
                # coroutine returns — a `send()` blocked on the same lock must see
                # "not running" the instant we've committed to "nothing else is
                # coming", not some fraction of a second later once the task
                # object itself transitions to done().
                current = self._turn_tasks.get(session_id)
                if current is asyncio.current_task():
                    del self._turn_tasks[session_id]
        items = await run_in_db_thread(queries.list_queue_items, self.ctx.db, session_id)
        await self.ctx.server.broadcast(
            session_id, "queue.changed", {"session_id": session_id, "items": items}
        )

    # -- ACP event handlers (bound into WorkerManager at construction) ------------------

    async def _on_session_update(self, session_id: str, params: dict[str, Any]) -> None:
        ctx_turn = self._active_turns.get(session_id)
        if ctx_turn is None:
            logger.debug(
                "session/update with no active turn, ignoring",
                extra={"detail": {"session_id": session_id}},
            )
            return
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            await self._handle_text_delta(ctx_turn, update, thinking=False)
        elif kind == "agent_thought_chunk":
            await self._handle_text_delta(ctx_turn, update, thinking=True)
        elif kind == "tool_call":
            await self._handle_tool_call_start(ctx_turn, update)
        elif kind == "tool_call_update":
            await self._handle_tool_call_update(ctx_turn, update)
        # plan / available_commands_update / current_mode_update / config_option_update
        # / session_info_update / usage_update: no RPC v0 notification or schema
        # column exists for these yet (see module docstring's scope-gap note) —
        # dropping them here is a deliberate, documented gap, not a silent one.

    async def _handle_text_delta(
        self, ctx_turn: _TurnContext, update: dict[str, Any], *, thinking: bool
    ) -> None:
        text = _extract_text(update.get("content"))
        if not text:
            return
        if thinking:
            if ctx_turn.thinking_message_id is None:
                ctx_turn.thinking_message_id = new_ulid()
                await run_in_db_thread(
                    queries.insert_assistant_message, self.ctx.db,
                    message_id=ctx_turn.thinking_message_id, session_id=ctx_turn.session_id,
                    turn_id=ctx_turn.turn_id, kind="thinking",
                )
            ctx_turn.thinking_text += text
            message_id = ctx_turn.thinking_message_id
        else:
            if ctx_turn.assistant_message_id is None:
                ctx_turn.assistant_message_id = new_ulid()
                await run_in_db_thread(
                    queries.insert_assistant_message, self.ctx.db,
                    message_id=ctx_turn.assistant_message_id, session_id=ctx_turn.session_id,
                    turn_id=ctx_turn.turn_id, kind="text",
                )
            ctx_turn.assistant_text += text
            message_id = ctx_turn.assistant_message_id
        await self.ctx.server.broadcast(
            ctx_turn.session_id,
            "message.delta",
            {
                "session_id": ctx_turn.session_id,
                "turn_id": ctx_turn.turn_id,
                "message_id": message_id,
                "delta": text,
            },
        )

    async def _handle_tool_call_start(
        self, ctx_turn: _TurnContext, update: dict[str, Any]
    ) -> None:
        tool_call_id = update.get("toolCallId")
        step_id = new_ulid()
        ctx_turn.step_seq += 1
        ctx_turn.step_seq_by_id[step_id] = ctx_turn.step_seq
        if tool_call_id:
            ctx_turn.tool_call_steps[tool_call_id] = step_id
            ctx_turn.step_started_at[step_id] = time.monotonic()
        # ACP's ToolCall schema carries a human-readable `title`, not a raw tool
        # name (see kernel/acp_client.py's docstring evidence) — `title` is the
        # closest honest value for the `tool` column until Hermes's wire schema
        # exposes one, not a made-up name.
        tool_label = update.get("title") or tool_call_id or "tool"
        status = update.get("status") or "pending"
        row = await run_in_db_thread(
            queries.insert_step, self.ctx.db, step_id=step_id, run_id=ctx_turn.run_id,
            seq=ctx_turn.step_seq, tool=tool_label, args=update.get("rawInput"), status=status,
        )
        await self.ctx.server.broadcast(ctx_turn.session_id, "step.started", row)

    async def _handle_tool_call_update(
        self, ctx_turn: _TurnContext, update: dict[str, Any]
    ) -> None:
        tool_call_id = update.get("toolCallId")
        step_id = ctx_turn.tool_call_steps.get(tool_call_id) if tool_call_id else None
        if step_id is None:
            logger.warning(
                "tool_call_update for an unknown tool_call_id, ignoring",
                extra={"detail": {"session_id": ctx_turn.session_id, "tool_call_id": tool_call_id}},
            )
            return
        status = update.get("status")
        raw_output = update.get("rawOutput")
        duration_ms = None
        started = ctx_turn.step_started_at.get(step_id)
        if started is not None and status in ("completed", "failed"):
            duration_ms = int((time.monotonic() - started) * 1000)
        result_summary = None
        dumped_output: str | None = None
        if raw_output is not None:
            # Serialize `raw_output` exactly once. Round-1 review fix: this used
            # to be dumped twice — once here (truncated to 4000 chars for
            # `result_summary`) and again, independently, inside
            # `_write_step_payload` — and the second dump ran on *this* event
            # loop thread, before ever reaching `asyncio.to_thread` (CPython's C
            # json encoder holds the GIL throughout, so moving it into a thread
            # later doesn't give the loop a chance to run anything else while it
            # dumps). For a large tool `rawOutput` that roughly doubled the CPU
            # the ACP read loop pays per `tool_call_update` — exactly the cost
            # 02-w3-interfaces.md §3's "回放 payload 写入异步、不阻塞 ACP 读循环"
            # is about. `dumped_output` (the one dump) feeds both the truncated
            # summary and the full-fidelity payload below.
            dumped_output = json.dumps(raw_output, default=str, ensure_ascii=False)
            result_summary = dumped_output[:4000]
        row = await run_in_db_thread(
            queries.update_step, self.ctx.db, step_id,
            status=status, result_summary=result_summary, duration_ms=duration_ms,
        )
        if status in ("completed", "failed"):
            await self.ctx.server.broadcast(ctx_turn.session_id, "step.completed", row)
        if dumped_output is not None and status in ("completed", "failed"):
            # Scheduled, not awaited: `_on_session_update` runs inline on
            # `AcpClient._read_loop`'s await chain (kernel/acp_client.py awaits
            # `on_session_update` for every incoming line before reading the
            # next) — blocking here on disk I/O would stall the ACP read loop
            # itself (02-w3-interfaces.md §3 "回放 payload 写入异步、不阻塞 ACP
            # 读循环"). `_write_step_payload` logs and swallows its own failures
            # (DEV.md 工程原则 #4) since nothing awaits this task's result.
            seq = ctx_turn.step_seq_by_id.get(step_id, ctx_turn.step_seq)
            task = asyncio.create_task(
                self._write_step_payload(ctx_turn.run_id, step_id, seq, dumped_output)
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    async def _write_step_payload(
        self, run_id: str, step_id: str, seq: int, dumped_output: str
    ) -> None:
        """FR06 回放 full-fidelity Step payload (02-w3-interfaces.md §2) —
        background write scheduled by `_handle_tool_call_update`, see its
        docstring comment for why this must never be awaited inline there.

        Takes the already-`json.dumps`-ed text (not the raw object) — see that
        same comment for why this must not re-serialize it a second time."""
        try:
            data = dumped_output.encode("utf-8")
            ref = await asyncio.to_thread(
                replay_store.write_payload, self.ctx.paths.user_root(), run_id, seq, data, "json"
            )
            await run_in_db_thread(
                queries.update_step, self.ctx.db, step_id,
                status=None, result_summary=None, duration_ms=None, payload_ref=ref,
            )
        except Exception:  # noqa: BLE001 - a background write must never vanish
            # silently (DEV.md 工程原则 #4) — nothing else awaits this task or
            # otherwise observes its outcome, so this `except` is the only place
            # a failure here can possibly be reported.
            logger.error(
                "failed to write full Step payload for replay",
                exc_info=True,
                extra={"detail": {"run_id": run_id, "step_id": step_id, "seq": seq}},
            )

    async def _on_request_permission(
        self, session_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        decision_id = new_ulid()
        tool_call = params.get("toolCall") or {}
        tool_call_id = tool_call.get("toolCallId")
        ctx_turn = self._active_turns.get(session_id)
        step_id = ctx_turn.tool_call_steps.get(tool_call_id) if ctx_turn and tool_call_id else None
        await run_in_db_thread(
            queries.insert_permission_decision,
            self.ctx.db,
            decision_id=decision_id,
            step_id=step_id,
            gate="user",
            risk="unclassified",
            request=params,
        )
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_permissions[decision_id] = _PendingPermission(
            session_id=session_id, params=params, future=fut
        )
        await self.ctx.server.broadcast(
            session_id, "permission.requested",
            {
                "request_id": decision_id, "session_id": session_id, "gate": "user",
                "risk": "unclassified", "tool_call": tool_call, "options": params.get("options"),
            },
        )
        try:
            return await fut
        finally:
            self._pending_permissions.pop(decision_id, None)

    async def _on_worker_crash(self, session_id: str, returncode: int | None) -> None:
        # A crashed worker can never answer a `session/request_permission` it
        # already sent — close that wait off now rather than leaving `AcpClient`'s
        # background task for it (kernel/acp_client.py) awaiting a future no one
        # will ever resolve (round-1 review fix).
        self._resolve_pending_permissions(session_id=session_id, reason="worker crashed")
        ctx_turn = self._active_turns.get(session_id)
        if ctx_turn is not None:
            # The in-flight `worker.client.prompt()` call in `_run_turn` will itself
            # observe the closed stdio pipe (AcpClient's read loop fails every
            # pending future once the process's stdout hits EOF) and terminate the
            # Run through its own `except (AcpError, AcpProtocolError)` branch —
            # handling it a second time here would double-write the same Run.
            logger.info(
                "worker crashed during an active turn; the in-flight prompt() call will report it",
                extra={"detail": {"session_id": session_id, "returncode": returncode}},
            )
            return
        logger.warning(
            "idle worker exited unexpectedly; a fresh one will spawn on the next session.send",
            extra={"detail": {"session_id": session_id, "returncode": returncode}},
        )
