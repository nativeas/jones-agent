"""RPC `agent.list` / `agent.get` / `agent.upsert` / `agent.delete` (design §4.1,
issue #9).

`register(server, ctx)` — same pattern as projects/methods.py; `ctx` is duck-typed
to a `.db` attribute for the same reason (see that module's docstring).
"""

from __future__ import annotations

from typing import Any

from jones_daemon.agents.service import AgentService
from jones_daemon.rpc.errors import INVALID_PARAMS, RpcError
from jones_daemon.rpc.server import Connection, RpcServer
from jones_daemon.store import run_in_db_thread


def register(server: RpcServer, ctx: Any) -> None:
    service = AgentService(ctx.db)

    async def agent_list(params: dict[str, Any], conn: Connection) -> list[dict[str, Any]]:
        return await run_in_db_thread(service.list, params.get("project_id"))

    async def agent_get(params: dict[str, Any], conn: Connection) -> dict[str, Any]:
        agent_id = params.get("id")
        if not agent_id:
            raise RpcError(INVALID_PARAMS, "id is required")
        return await run_in_db_thread(service.get, agent_id)

    async def agent_upsert(params: dict[str, Any], conn: Connection) -> dict[str, Any]:
        if not isinstance(params, dict) or not params:
            raise RpcError(INVALID_PARAMS, "agent fields are required")
        # `service.upsert` already writes the file and syncs *that* row into the
        # index in one step — a full `sync_from_files()` re-scan here would also
        # be correct (design §4: "agent.upsert 时同步入表") but re-reading every
        # project's agent tree on every single upsert is unwarranted I/O for what
        # a targeted single-row sync already covers; the full re-scan still runs
        # at daemon startup, which is what actually needs to catch drift from
        # hand-edited files.
        return await run_in_db_thread(service.upsert, params)

    async def agent_delete(params: dict[str, Any], conn: Connection) -> dict[str, Any]:
        agent_id = params.get("id")
        if not agent_id:
            raise RpcError(INVALID_PARAMS, "id is required")
        await run_in_db_thread(service.delete, agent_id)
        return {"deleted": True}

    server.register("agent.list", agent_list)
    server.register("agent.get", agent_get)
    server.register("agent.upsert", agent_upsert)
    server.register("agent.delete", agent_delete)
