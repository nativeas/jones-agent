"""RPC methods `session.*` / `turn.*` / `run.*` / `permission.*`
(docs/design/00-foundation.md §4.1/§4.2, docs/design/01-w2-interfaces.md §2).

`register(server, ctx)` follows the module convention every W2 branch uses
(01-w2-interfaces.md §0): builds one `SessionService` bound to the shared
`DaemonContext` and registers every handler this module owns against it. Every
handler here is a thin `params -> SessionService call -> result` translation;
the actual state machine lives in `sessions/service.py`.

`session.subscribe` / `session.unsubscribe` are the one piece of state that
belongs to the RPC layer, not `SessionService` — they mutate the calling
`Connection`'s own `subscriptions` set (`rpc/server.py`, the file A may only
extend additively per 01-w2-interfaces.md §2), so they're handled directly here
rather than routed through the service.
"""

from __future__ import annotations

from typing import Any

from jones_daemon.context import DaemonContext
from jones_daemon.rpc.errors import INVALID_PARAMS, INVALID_STATE, RpcError
from jones_daemon.rpc.server import Connection, RpcServer
from jones_daemon.sessions.service import SessionService
from jones_daemon.store import maintenance, run_in_db_thread


def _require_str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise RpcError(INVALID_PARAMS, f"{key!r} must be a non-empty string", {"params": params})
    return value


async def _run_delete_honestly(
    server: RpcServer, fn: Any, *args: Any, **kwargs: Any
) -> dict[str, Any]:
    """Shared `session.delete`/`run.delete` tail: run a `store/maintenance.py`
    delete on the DB thread and translate `maintenance.PartialDeleteError`
    (04-w5-interfaces.md §6 "诚实失败") into the two outcomes it actually
    represents, instead of letting both fall through to `rpc/server.py::
    _dispatch`'s generic `except Exception` -> opaque `INTERNAL_ERROR` (round-1
    review: that path also never told anyone else on the daemon — `daemon.error`
    — regardless of which of the two shapes below it was).

    Every `PartialDeleteError` is broadcast as `daemon.error` unconditionally —
    "记录部分完成状态并 daemon.error" applies whether or not the delete itself
    ends up reported to *this* caller as success:
      - `detail["fully_deleted"]` True (only the trailing WAL checkpoint stayed
        busy — rows and payload are already fully gone): reported to the caller
        as an ordinary success. Turning a completed delete into a client-visible
        error would be the opposite mistake the review flagged.
      - otherwise (a payload purge failed partway): re-raised as a structured
        `RpcError(INVALID_STATE, ...)` carrying the same detail, so the caller
        gets something actionable instead of a bare `PartialDeleteError:` string.
    """
    try:
        await run_in_db_thread(fn, *args, **kwargs)
    except maintenance.PartialDeleteError as exc:
        await server.broadcast_all(
            "daemon.error",
            {"code": "delete_partially_failed", "message": str(exc), "detail": exc.detail},
        )
        if not exc.detail.get("fully_deleted"):
            raise RpcError(INVALID_STATE, str(exc), exc.detail) from exc
    return {"deleted": True}


def register(server: RpcServer, ctx: DaemonContext) -> SessionService:
    """Builds and returns the `SessionService` so `__main__.py` can call its
    `startup()`/`shutdown()` lifecycle hooks — the RPC registration itself doesn't
    need those, but nothing else in this module owns constructing the service.
    """
    service = SessionService(ctx)

    async def session_list(params: dict[str, Any], conn: Connection) -> Any:
        return await service.list(params.get("project_id"))

    async def session_create(params: dict[str, Any], conn: Connection) -> Any:
        return await service.create(
            project_id=_require_str(params, "project_id"),
            agent_id=_require_str(params, "agent_id"),
            parent_id=params.get("parent_id"),
            mode=params.get("mode", "task"),
            title=params.get("title"),
        )

    async def session_get(params: dict[str, Any], conn: Connection) -> Any:
        return await service.get(_require_str(params, "id"))

    async def session_set_mode(params: dict[str, Any], conn: Connection) -> Any:
        return await service.set_mode(_require_str(params, "id"), _require_str(params, "mode"))

    async def session_send(params: dict[str, Any], conn: Connection) -> Any:
        return await service.send(
            _require_str(params, "id"),
            _require_str(params, "text"),
            attachments=params.get("attachments"),
        )

    async def session_stop(params: dict[str, Any], conn: Connection) -> Any:
        return await service.stop(_require_str(params, "id"))

    async def session_queue(params: dict[str, Any], conn: Connection) -> Any:
        return await service.queue(_require_str(params, "id"))

    async def session_queue_remove(params: dict[str, Any], conn: Connection) -> Any:
        session_id = _require_str(params, "id")
        item_id = _require_str(params, "item_id")
        return await service.queue_remove(session_id, item_id)

    async def session_queue_reorder(params: dict[str, Any], conn: Connection) -> Any:
        item_ids = params.get("item_ids")
        if not isinstance(item_ids, list) or not all(isinstance(i, str) for i in item_ids):
            raise RpcError(
                INVALID_PARAMS, "'item_ids' must be a list of strings", {"params": params}
            )
        return await service.queue_reorder(_require_str(params, "id"), item_ids)

    async def session_subscribe(params: dict[str, Any], conn: Connection) -> Any:
        conn.subscriptions.add(_require_str(params, "id"))
        return {"subscribed": True}

    async def session_unsubscribe(params: dict[str, Any], conn: Connection) -> Any:
        conn.subscriptions.discard(_require_str(params, "id"))
        return {"subscribed": False}

    async def turn_messages(params: dict[str, Any], conn: Connection) -> Any:
        return await service.turn_messages(
            _require_str(params, "session_id"),
            before=params.get("before"),
            limit=params.get("limit", 50),
        )

    async def run_list(params: dict[str, Any], conn: Connection) -> Any:
        return await service.run_list(
            _require_str(params, "session_id"), limit=params.get("limit", 50)
        )

    async def run_get(params: dict[str, Any], conn: Connection) -> Any:
        return await service.run_get(_require_str(params, "run_id"))

    async def run_steps(params: dict[str, Any], conn: Connection) -> Any:
        # FR06 回放分页 (02-w3-interfaces.md §2) — both optional, `run.steps` with
        # neither behaves exactly as before this issue (every Step, in order).
        after_seq = params.get("after_seq")
        limit = params.get("limit")
        if after_seq is not None and not isinstance(after_seq, int):
            raise RpcError(INVALID_PARAMS, "'after_seq' must be an integer", {"params": params})
        if limit is not None and not isinstance(limit, int):
            raise RpcError(INVALID_PARAMS, "'limit' must be an integer", {"params": params})
        return await service.run_steps(
            _require_str(params, "run_id"), after_seq=after_seq, limit=limit
        )

    async def run_payload(params: dict[str, Any], conn: Connection) -> Any:
        offset = params.get("offset", 0)
        limit = params.get("limit")
        if not isinstance(offset, int) or offset < 0:
            raise RpcError(
                INVALID_PARAMS, "'offset' must be a non-negative integer", {"params": params}
            )
        if limit is not None and (not isinstance(limit, int) or limit <= 0):
            raise RpcError(
                INVALID_PARAMS, "'limit' must be a positive integer", {"params": params}
            )
        return await service.run_payload(_require_str(params, "ref"), offset=offset, limit=limit)

    async def session_delete(params: dict[str, Any], conn: Connection) -> Any:
        # Issue #23 (04-w5-interfaces.md §5, G20 真删): the cascade-delete +
        # refusal rules still live entirely in `maintenance.delete_session`,
        # this handler is still a thin params -> call translation. Round-2
        # review: the DB-only "is a Run running" check inside
        # `maintenance.delete_session` races `SessionService`'s own in-memory
        # authority on that same question (`_active_turns`/`_turn_tasks` under
        # `self._lock(session_id)`) — closing that race needs the session lock
        # itself, which only `SessionService` holds, hence `service.
        # delete_guard` (04-w5-interfaces.md §1 grants this branch
        # `sessions/service.py` access for exactly this — see that method's
        # own docstring).
        session_id = _require_str(params, "id")
        user_root = ctx.paths.user_root()
        return await service.delete_guard(
            session_id,
            _run_delete_honestly,
            server,
            maintenance.delete_session,
            ctx.db,
            user_root,
            session_id,
        )

    async def session_export(params: dict[str, Any], conn: Connection) -> Any:
        session_id = _require_str(params, "id")
        delete_after = params.get("delete_after", False)
        if not isinstance(delete_after, bool):
            raise RpcError(
                INVALID_PARAMS, "'delete_after' must be a boolean", {"params": params}
            )
        try:
            path = await run_in_db_thread(
                maintenance.export_session,
                ctx.db,
                ctx.paths.user_root(),
                session_id,
                delete_after=delete_after,
            )
        except maintenance.PartialDeleteError as exc:
            # Round-2 review: `delete_after=True`'s inner `delete_session` call
            # can raise this exact same `PartialDeleteError` `session.delete`
            # does — this handler used to let it fall straight through to
            # `rpc/server.py::_dispatch`'s generic `except Exception`, an opaque
            # `INTERNAL_ERROR` with no `daemon.error` broadcast, the identical
            # bug round-1 review already fixed for `session.delete`/`run.delete`
            # via `_run_delete_honestly` just never reached here.
            await server.broadcast_all(
                "daemon.error",
                {"code": "delete_partially_failed", "message": str(exc), "detail": exc.detail},
            )
            if not exc.detail.get("fully_deleted"):
                raise RpcError(INVALID_STATE, str(exc), exc.detail) from exc
            # fully_deleted True: the export file, the session's rows, and every
            # run's payload are all genuinely gone — only the trailing WAL
            # checkpoint stayed busy. `export_session` stashes the export path
            # into `exc.detail` for exactly this branch (see its docstring) so
            # the caller still gets a normal success response instead of losing
            # an export that is, in fact, sitting durably on disk.
            return {"path": exc.detail["export_path"]}
        return {"path": str(path)}

    async def run_delete(params: dict[str, Any], conn: Connection) -> Any:
        run_id = _require_str(params, "run_id")
        return await _run_delete_honestly(
            server, maintenance.delete_run, ctx.db, ctx.paths.user_root(), run_id
        )

    async def permission_pending(params: dict[str, Any], conn: Connection) -> Any:
        return await service.permission_pending(params.get("session_id"))

    async def permission_decide(params: dict[str, Any], conn: Connection) -> Any:
        decision = params.get("decision")
        if decision not in ("allow", "deny"):
            raise RpcError(INVALID_PARAMS, f"invalid decision: {decision!r}", {"params": params})
        return await service.permission_decide(
            _require_str(params, "request_id"), decision, remember=params.get("remember")
        )

    server.register("session.list", session_list)
    server.register("session.create", session_create)
    server.register("session.get", session_get)
    server.register("session.set_mode", session_set_mode)
    server.register("session.send", session_send)
    server.register("session.stop", session_stop)
    server.register("session.queue", session_queue)
    server.register("session.queue_remove", session_queue_remove)
    server.register("session.queue_reorder", session_queue_reorder)
    server.register("session.subscribe", session_subscribe)
    server.register("session.unsubscribe", session_unsubscribe)
    server.register("session.delete", session_delete)
    server.register("session.export", session_export)
    server.register("turn.messages", turn_messages)
    server.register("run.list", run_list)
    server.register("run.get", run_get)
    server.register("run.steps", run_steps)
    server.register("run.payload", run_payload)
    server.register("run.delete", run_delete)
    server.register("permission.pending", permission_pending)
    server.register("permission.decide", permission_decide)
    return service
