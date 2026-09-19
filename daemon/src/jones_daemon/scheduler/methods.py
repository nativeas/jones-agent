"""RPC `cron.list` / `cron.upsert` / `cron.delete` / `cron.run_now`
(00-foundation.md §4.1 — already in the v0 contract table; this branch is what
implements them, docs/design/04-w5-interfaces.md §2).

Same thin `params -> CronService call -> result` shape as `sessions/methods.py`;
the actual scheduling/dispatch state machine lives in `scheduler/service.py`.
"""

from __future__ import annotations

from typing import Any

from jones_daemon.context import DaemonContext
from jones_daemon.rpc.errors import INVALID_PARAMS, RpcError
from jones_daemon.rpc.server import Connection, RpcServer
from jones_daemon.scheduler.service import CronService


def _require_str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise RpcError(INVALID_PARAMS, f"{key!r} must be a non-empty string", {"params": params})
    return value


def register(
    server: RpcServer, ctx: DaemonContext, session_service: Any
) -> CronService:
    """Builds and returns the `CronService` so `__main__.py` can drive its
    `start()`/`stop()` lifecycle — mirrors `sessions/methods.py::register`."""
    service = CronService(ctx, session_service)

    async def cron_list(params: dict[str, Any], conn: Connection) -> Any:
        return await service.list(params.get("project_id"))

    async def cron_upsert(params: dict[str, Any], conn: Connection) -> Any:
        enabled = params.get("enabled", True)
        if not isinstance(enabled, bool):
            raise RpcError(INVALID_PARAMS, "'enabled' must be a boolean", {"params": params})
        return await service.upsert(
            id=params.get("id"),
            project_id=_require_str(params, "project_id"),
            agent_id=_require_str(params, "agent_id"),
            name=_require_str(params, "name"),
            expr=_require_str(params, "expr"),
            prompt=_require_str(params, "prompt"),
            # Round-1 fix (review #1/#13): was `"auto"` — see `service.py`'s
            # module docstring "第 1 轮修复记录" and 04-w5-interfaces.md §2.
            mode=params.get("mode", "task"),
            enabled=enabled,
        )

    async def cron_delete(params: dict[str, Any], conn: Connection) -> Any:
        return await service.delete(_require_str(params, "id"))

    async def cron_run_now(params: dict[str, Any], conn: Connection) -> Any:
        return await service.run_now(_require_str(params, "id"))

    server.register("cron.list", cron_list)
    server.register("cron.upsert", cron_upsert)
    server.register("cron.delete", cron_delete)
    server.register("cron.run_now", cron_run_now)
    return service
