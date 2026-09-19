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
refuse to run tools at all — PRD 9.1/N12), Goal/budget-triggered termination (PRD
9.3's third termination kind — Goals are FR17, not #10), and full `payload_ref`
offload storage for large tool results (`FR06` 回放 completeness) are not
implemented here; nothing in this file pretends otherwise.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jones_daemon.context import DaemonContext
from jones_daemon.kernel.acp_client import AcpError, AcpProtocolError
from jones_daemon.kernel.ids import new_ulid
from jones_daemon.logging import get_logger
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

    async def shutdown(self) -> None:
        await self.worker_manager.stop()

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

    def _cwd_for_project(self, project_id: str) -> str:
        if project_id == DEFAULT_PROJECT_ID:
            # `proj_default`'s `path` column is a placeholder sentinel (see the 002
            # migration header) — PRD 7.1/01-w2-interfaces.md §2 says the implicit
            # default project IS the user's home directory, so use that directly
            # rather than the sentinel until C (#8/#9) lands real Project rows.
            return str(Path.home())
        raise RpcError(
            INVALID_STATE,
            "non-default projects aren't implemented until C (#8/#9) lands",
            {"project_id": project_id},
        )

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

    async def run_get(self, run_id: str) -> dict[str, Any]:
        row = await run_in_db_thread(queries.get_run, self.ctx.db, run_id)
        if row is None:
            raise RpcError(NOT_FOUND, "run not found", {"run_id": run_id})
        return row

    async def run_steps(self, run_id: str) -> list[dict[str, Any]]:
        return await run_in_db_thread(queries.list_run_steps, self.ctx.db, run_id)

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
        ctx_turn = _TurnContext(turn_id=turn_id, run_id=run_id, session_id=session_id)
        self._active_turns[session_id] = ctx_turn
        self.worker_manager.mark_busy(session_id, True)
        try:
            session = await run_in_db_thread(queries.get_session, self.ctx.db, session_id)
            cwd = self._cwd_for_project(session["project_id"])
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
                await self._terminate_run(
                    ctx_turn, kind="error", reason=f"ACP prompt failed: {exc}"
                )
                return
            await self._finalize_turn_success(ctx_turn, response)
        finally:
            self._active_turns.pop(session_id, None)
            self.worker_manager.mark_busy(session_id, False)
            await self._advance_queue(session_id)

    async def _finalize_turn_success(
        self, ctx_turn: _TurnContext, response: dict[str, Any]
    ) -> None:
        if ctx_turn.assistant_message_id:
            row = await run_in_db_thread(
                queries.finalize_message, self.ctx.db, ctx_turn.assistant_message_id,
                kind="text", text=ctx_turn.assistant_text,
            )
            await self.ctx.server.broadcast(ctx_turn.session_id, "message.completed", row)
        if ctx_turn.thinking_message_id:
            row = await run_in_db_thread(
                queries.finalize_message, self.ctx.db, ctx_turn.thinking_message_id,
                kind="thinking", text=ctx_turn.thinking_text,
            )
            await self.ctx.server.broadcast(ctx_turn.session_id, "message.completed", row)

        if response.get("stopReason") == "cancelled":
            await self._terminate_run(ctx_turn, kind="user", reason="stopped by user")
            return
        await run_in_db_thread(
            queries.mark_run_completed, self.ctx.db, ctx_turn.run_id, ctx_turn.turn_id
        )

    async def _terminate_run(self, ctx_turn: _TurnContext, *, kind: str, reason: str) -> None:
        await run_in_db_thread(
            queries.mark_run_terminated,
            self.ctx.db,
            ctx_turn.run_id,
            ctx_turn.turn_id,
            kind=kind,
            reason=reason,
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
        item = await run_in_db_thread(queries.pop_next_queue_item, self.ctx.db, session_id)
        items = await run_in_db_thread(queries.list_queue_items, self.ctx.db, session_id)
        await self.ctx.server.broadcast(
            session_id, "queue.changed", {"session_id": session_id, "items": items}
        )
        if item is not None:
            # No explicit "mark this queued Turn running" write here: `_run_turn`
            # (via `queries.create_run`) already flips `turns.status` to 'running'
            # as soon as it starts, so a separate call here would just be the same
            # write twice.
            self._start_turn(session_id, item["turn_id"], item["text"])

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
        if raw_output is not None:
            # Truncated text summary, not full payload storage — `payload_ref`
            # (full-fidelity replay storage, FR06) is out of #10's scope; see the
            # module docstring's scope-gap note.
            result_summary = json.dumps(raw_output, default=str, ensure_ascii=False)[:4000]
        row = await run_in_db_thread(
            queries.update_step, self.ctx.db, step_id,
            status=status, result_summary=result_summary, duration_ms=duration_ms,
        )
        if status in ("completed", "failed"):
            await self.ctx.server.broadcast(ctx_turn.session_id, "step.completed", row)

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
