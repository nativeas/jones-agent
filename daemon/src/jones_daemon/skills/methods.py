"""RPC `skill.list` (docs/design/00-foundation.md §4.1 — row added by this branch,
docs/design/03-w4-interfaces.md §5, issue #18/#19).

`register(server, ctx)` follows the module convention every branch uses
(`providers/methods.py`'s docstring documents it first): `ctx` only needs
`.db` (a `sqlite3.Connection`, for resolving a project's path when
`project_id` is given) — the real `DaemonContext` already has it.
"""

from __future__ import annotations

import asyncio
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
        # list_skills() does a recursive filesystem walk (rglob + read_text per
        # SKILL.md) — it never touches the shared sqlite connection (unlike
        # _resolve_project_path above), so asyncio.to_thread — not
        # run_in_db_thread — is the right offload, same reasoning as
        # providers/methods.py::_make_model_list's to_thread comment: this
        # daemon's single event loop also carries the RPC server, ACP
        # session/update forwarding and server.broadcast() for every session,
        # and a synchronous walk of a skills directory (which can contain
        # node_modules/.venv-sized noise — see _EXCLUDED_DIR_NAMES) would
        # stall all of that for the duration (评审第 1 轮 #3).
        return {"skills": await asyncio.to_thread(list_skills, project_path=project_path)}

    server.register("skill.list", skill_list)
