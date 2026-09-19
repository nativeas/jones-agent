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

# Round-2 review, Issue #23: `store/maintenance.py::delete_project` gained the
# same `PartialDeleteError` post-commit-failure contract `delete_session`/
# `delete_run` already have (04-w5-interfaces.md §6 "诚实失败") — reused here
# rather than re-implemented, since `sessions/methods.py::_run_delete_honestly`
# already is exactly "run a maintenance delete on the DB thread, translate
# `PartialDeleteError` into `daemon.error` + the right RPC-visible outcome",
# with nothing session-specific in it.
from jones_daemon.sessions.methods import _run_delete_honestly
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
        return await _run_delete_honestly(server, service.delete, project_id)

    server.register("project.list", project_list)
    server.register("project.create", project_create)
    server.register("project.delete", project_delete)
