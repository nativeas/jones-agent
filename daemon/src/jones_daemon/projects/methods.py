"""RPC `project.list` / `project.create` / `project.delete` (design §4.1, issue #8).

Signature matches the existing `rpc/methods.py` style: `async def handler(params:
dict, conn: Connection) -> Any`. `register(server, ctx)` is the one entry point
other modules (and `__main__.py`) call — per 01-w2-interfaces.md §0.
"""

from __future__ import annotations

from typing import Any

from jones_daemon.projects.service import ProjectService
from jones_daemon.rpc.errors import INVALID_PARAMS, RpcError
from jones_daemon.rpc.server import Connection, RpcServer
from jones_daemon.store import run_in_db_thread


def register(server: RpcServer, ctx: Any) -> None:
    """`ctx` only needs a `.db` attribute (a sqlite3.Connection opened via
    store.connect()). Typed as `Any` rather than the shared `DaemonContext`
    (docs/design/01-w2-interfaces.md §1) because that module is created by branch A
    (`jones_daemon/context.py`) and isn't present in this branch's history — this
    module is duck-typed on purpose so wiring the real `DaemonContext` in at merge
    time needs no change here, only in the `__main__.py` call site.
    """
    service = ProjectService(ctx.db)

    async def project_list(params: dict[str, Any], conn: Connection) -> list[dict[str, Any]]:
        return await run_in_db_thread(service.list)

    async def project_create(params: dict[str, Any], conn: Connection) -> dict[str, Any]:
        path = params.get("path")
        if not path:
            raise RpcError(INVALID_PARAMS, "path is required")
        return await run_in_db_thread(service.create, path)

    async def project_delete(params: dict[str, Any], conn: Connection) -> dict[str, Any]:
        project_id = params.get("id")
        if not project_id:
            raise RpcError(INVALID_PARAMS, "id is required")
        await run_in_db_thread(service.delete, project_id)
        return {"deleted": True}

    server.register("project.list", project_list)
    server.register("project.create", project_create)
    server.register("project.delete", project_delete)
