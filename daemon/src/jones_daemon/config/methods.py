"""RPC `settings.get` / `settings.set` (design §4.1, issue #8/#9).

Distinct from `ConfigResolver.settings()`: the resolver returns the *merged
effective* view (defaults <- user <- project) for internal callers like the
session/permission engine; these two RPC methods read/write the *raw, per-scope*
settings.json file the settings page edits — showing only what's actually stored at
that scope (so a project editor shows overrides, not an indistinguishable merged
blob), matching PRD 10.3 ("设置页编辑；也可手改文件").
"""

from __future__ import annotations

import json
from typing import Any

from jones_daemon import paths
from jones_daemon.config.ids import now_iso
from jones_daemon.config.jsonfile import read_json, write_json
from jones_daemon.projects.service import ProjectService
from jones_daemon.rpc.errors import INVALID_PARAMS, RpcError
from jones_daemon.rpc.server import Connection, RpcServer
from jones_daemon.store import run_in_db_thread

_VALID_SCOPES = ("user", "project")


def _validate_scope(params: dict[str, Any]) -> tuple[str, str | None]:
    scope = params.get("scope")
    if scope not in _VALID_SCOPES:
        raise RpcError(INVALID_PARAMS, "scope must be 'user' or 'project'")
    project_id = params.get("project_id")
    if scope == "project" and not project_id:
        raise RpcError(INVALID_PARAMS, "project_id is required when scope='project'")
    return scope, project_id


def register(server: RpcServer, ctx: Any) -> None:
    """`ctx` only needs a `.db` attribute (a sqlite3.Connection) — see
    projects/methods.py's `register()` docstring for why this is duck-typed rather
    than importing the (not-yet-present-in-this-branch) `jones_daemon.context`.
    """
    projects = ProjectService(ctx.db)

    def _settings_path(scope: str, project_id: str | None):
        if scope == "user":
            return paths.config_dir() / "settings.json"
        project = projects.get(project_id)  # raises RpcError(not_found)
        return paths.project_settings_path(project["path"])

    async def settings_get(params: dict[str, Any], conn: Connection) -> dict[str, Any]:
        scope, project_id = _validate_scope(params)

        def _do() -> dict[str, Any]:
            return read_json(_settings_path(scope, project_id), {})

        return await run_in_db_thread(_do)

    async def settings_set(params: dict[str, Any], conn: Connection) -> dict[str, Any]:
        scope, project_id = _validate_scope(params)
        patch = params.get("patch")
        if not isinstance(patch, dict):
            raise RpcError(INVALID_PARAMS, "patch must be an object")

        def _do() -> dict[str, Any]:
            path = _settings_path(scope, project_id)
            current = read_json(path, {})
            current.update(patch)
            write_json(path, current)
            if scope == "project":
                # `projects.settings_json` is a synced index/cache of the file (same
                # "file is truth, table is a read-optimized index" pattern as the
                # `agents` table, see agents/service.py) — kept mirrored here so a
                # plain `SELECT` against `projects` doesn't need a filesystem read.
                ctx.db.execute(
                    "UPDATE projects SET settings_json = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(current, ensure_ascii=False), now_iso(), project_id),
                )
                ctx.db.commit()
            return current

        return await run_in_db_thread(_do)

    server.register("settings.get", settings_get)
    server.register("settings.set", settings_set)
