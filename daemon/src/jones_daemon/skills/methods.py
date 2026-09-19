"""RPC `skill.list` (docs/design/00-foundation.md §4.1 — row added by this branch,
docs/design/03-w4-interfaces.md §5, issue #18/#19).

`register(server, ctx)` follows the module convention every branch uses
(`providers/methods.py`'s docstring documents it first): `ctx` only needs
`.db` (a `sqlite3.Connection`, for resolving a project's path when
`project_id` is given) — the real `DaemonContext` already has it.
"""

from __future__ import annotations

from typing import Any

from jones_daemon.projects.service import ProjectService
from jones_daemon.rpc.server import Connection, RpcServer
from jones_daemon.skills.service import list_skills
from jones_daemon.store import run_in_db_thread


def register(server: RpcServer, ctx: Any) -> None:
    def _resolve_project_path(project_id: str) -> str:
        return ProjectService(ctx.db).get(project_id)["path"]

    async def skill_list(params: dict[str, Any], conn: Connection) -> dict[str, Any]:
        project_id = params.get("project_id")
        project_path = (
            await run_in_db_thread(_resolve_project_path, project_id) if project_id else None
        )
        return {"skills": list_skills(project_path=project_path)}

    server.register("skill.list", skill_list)
