"""ProjectService: Project CRUD anchored on a directory path (PRD 7.1/8.1 FR02).

All methods here do synchronous sqlite + filesystem I/O — callers on the asyncio
event loop must offload through `store.run_in_db_thread` (see store/db.py); this
class itself is transport-agnostic and is exercised directly in tests without any
RPC server.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from jones_daemon import paths
from jones_daemon.config.ids import new_ulid, now_iso
from jones_daemon.rpc.errors import INVALID_PARAMS, INVALID_STATE, NOT_FOUND, RpcError
from jones_daemon.store import maintenance

# Fixed id for the implicit Project anchored at the user's home directory, seeded by
# migration 004 (docs/design/01-w2-interfaces.md §2: A's 002 migration seeds a
# placeholder row under this same id for the main session to reference before C
# lands; §4: 004 fills in its real fields). The id is a contract other modules
# depend on — never regenerate it.
DEFAULT_PROJECT_ID = "proj_default"


def _row_to_project(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "path": row["path"],
        "name": row["name"],
        "settings_json": row["settings_json"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class ProjectService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def list(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM projects ORDER BY created_at").fetchall()
        return [_row_to_project(r) for r in rows]

    def get(self, project_id: str) -> dict[str, Any]:
        row = self._conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise RpcError(NOT_FOUND, f"project not found: {project_id}")
        return _row_to_project(row)

    def get_by_path(self, path: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM projects WHERE path = ?", (path,)).fetchone()
        return None if row is None else _row_to_project(row)

    def create(self, path: str) -> dict[str, Any]:
        """选目录即建 Project (FR02). Idempotent on path: re-selecting a directory
        that's already a Project returns the existing one rather than erroring or
        creating a duplicate (the `path` column is UNIQUE, so this also avoids a
        constraint-violation round trip for what the UI presents as one action:
        "pick this folder").
        """
        if not path:
            raise RpcError(INVALID_PARAMS, "path is required")
        norm_path = str(Path(path).expanduser().resolve())

        existing = self.get_by_path(norm_path)
        if existing is not None:
            return existing

        if not Path(norm_path).is_dir():
            raise RpcError(INVALID_PARAMS, f"not a directory: {norm_path}")

        # Creates <norm_path>/.jones/ on first access (paths.py).
        paths.project_root(norm_path)

        project_id = new_ulid()
        now = now_iso()
        name = Path(norm_path).name or norm_path
        self._conn.execute(
            "INSERT INTO projects (id, path, name, settings_json, created_at, updated_at) "
            "VALUES (?, ?, ?, '{}', ?, ?)",
            (project_id, norm_path, name, now, now),
        )
        self._conn.commit()
        return self.get(project_id)

    def delete(self, project_id: str) -> None:
        """Project 删除 = SQLite 行 + `<user_root>/projects/<id>/`（附件目录），不删
        用户目录里的 `.jones/`（那是用户数据，见 01-w2-interfaces.md §4）。

        Existence and the "can't delete the default Project" checks stay here
        (Project-domain rules — `DEFAULT_PROJECT_ID` is this module's own
        constant). The reference-count refusals (诚实失败, not a silent cascade,
        for *every* table that can hold a `project_id` — `sessions.project_id`
        and `agents.project_id` are both `REFERENCES projects(id)`,
        00-foundation.md §5, and this connection runs with `PRAGMA
        foreign_keys=ON`, store/db.py) plus the actual row/attachments-directory
        deletion and WAL checkpoint now live in `store/maintenance.py::
        delete_project` (Issue #23, 04-w5-interfaces.md §5: "project.delete（已有，
        改为调 maintenance）") — this method is a thin call into it.
        Batch-archiving/deleting a Project's sessions first is a `session.*`
        operation (owned by branch A) — not implemented here since it isn't in the
        RPC v0 method table (design §4.1 lists only `project.list/create/delete`).
        """
        self.get(project_id)  # raises not_found
        if project_id == DEFAULT_PROJECT_ID:
            raise RpcError(INVALID_STATE, "cannot delete the default project")

        maintenance.delete_project(self._conn, paths.user_root(), project_id)

    def ensure_default_project(self, home_path: str) -> dict[str, Any] | None:
        """Idempotently correct `proj_default`'s `path`/`name` to the real,
        per-machine user home directory.

        Migration 004 seeds this row's environment-independent fields (id, name
        placeholder, settings_json) by static SQL, but a SQL migration file can't
        know the machine's home directory — that's resolved here, in Python, once
        at daemon startup (see __main__.py). No-ops if the row is already correct,
        or if migration 004 hasn't run yet (returns None rather than raising: this
        is a startup nicety, not something that should crash the daemon over a
        migration ordering the caller doesn't control in every environment, e.g. an
        older schema_version during a rolling upgrade window).
        """
        norm_path = str(Path(home_path).expanduser().resolve())
        row = self._conn.execute(
            "SELECT path FROM projects WHERE id = ?", (DEFAULT_PROJECT_ID,)
        ).fetchone()
        if row is None:
            return None
        paths.project_root(norm_path)
        if row["path"] == norm_path:
            return self.get(DEFAULT_PROJECT_ID)
        self._conn.execute(
            "UPDATE projects SET path = ?, name = ?, updated_at = ? WHERE id = ?",
            (norm_path, Path(norm_path).name or "Home", now_iso(), DEFAULT_PROJECT_ID),
        )
        self._conn.commit()
        return self.get(DEFAULT_PROJECT_ID)
