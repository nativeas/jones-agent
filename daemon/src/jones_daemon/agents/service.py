"""AgentService: file is the source of truth, `agents` table is a synced index
(design §4, issue #9 FR03).

`project_id` on an Agent marks which scope *owns* the definition (NULL = user-level,
otherwise project-scoped) — it is not an "override the same id across scopes"
mechanism: `id` is a globally unique ULID (§5: every table's PK), so a project-level
Agent always has its own id, distinct from any user-level one. "项目级 Agent 覆盖
用户级" (PRD FR03) is realized one level up, through `ConfigResolver.settings()`'s
`default_agent_id`: a project's settings.json can point `default_agent_id` at a
project-scoped Agent instead of inheriting the user-level default — that's the
override. This is a deliberate reading of an ambiguous PRD/design phrase (agents
table has a single-column `id TEXT PRIMARY KEY`, so literal same-id shadowing across
two scopes isn't representable as two rows) — flagged explicitly in the branch C
report for review.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from jones_daemon import paths
from jones_daemon.agents.store import AgentStore
from jones_daemon.config.ids import new_ulid, now_iso
from jones_daemon.logging import get_logger
from jones_daemon.rpc.errors import INVALID_PARAMS, INVALID_STATE, NOT_FOUND, RpcError

logger = get_logger("agents")

# Built-in default Agent, seeded by migration 004 (docs/design/01-w2-interfaces.md
# §2: A's 002 migration references this same fixed id for the main session before C
# lands — never regenerate it). Minimal persona, empty tool_allowlist (= 全部工具经
# 闸, see agents/policy.py), no model preference.
DEFAULT_AGENT_ID = "agent_default"

_JSON_FIELDS = ("tool_allowlist", "skills", "model_pref")


def _row_to_agent(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "name": row["name"],
        "persona": row["persona"],
        "tone": row["tone"],
        "principles": row["principles"],
        "tool_allowlist": json.loads(row["tool_allowlist_json"]),
        "skills": json.loads(row["skills_json"]),
        "model_pref": json.loads(row["model_pref_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class AgentService:
    def __init__(self, conn: sqlite3.Connection, store: AgentStore | None = None) -> None:
        self._conn = conn
        self._store = store or AgentStore()

    def _project_path(self, project_id: str | None) -> str | None:
        if project_id is None:
            return None
        row = self._conn.execute(
            "SELECT path FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise RpcError(NOT_FOUND, f"project not found: {project_id}")
        return row["path"]

    # -- DB index -------------------------------------------------------------

    def _upsert_index_row(self, agent: dict[str, Any], *, project_id: str | None) -> None:
        # `agents.name` is `NOT NULL` (001_init.sql) — reject a malformed record
        # here, with a message that says what's wrong, instead of letting a hand-
        # edited `agent.yaml` with a missing/null `name` reach the DB and blow up
        # as a raw sqlite3.IntegrityError on the daemon's startup path (review #4).
        if not agent.get("name"):
            raise ValueError(f"agent {agent.get('id')!r} is missing required field 'name'")
        exists = self._conn.execute(
            "SELECT 1 FROM agents WHERE id = ?", (agent["id"],)
        ).fetchone()
        values = (
            project_id,
            agent["name"],
            agent.get("persona"),
            agent.get("tone"),
            agent.get("principles"),
            json.dumps(agent.get("tool_allowlist") or [], ensure_ascii=False),
            json.dumps(agent.get("skills") or [], ensure_ascii=False),
            json.dumps(agent.get("model_pref") or {}, ensure_ascii=False),
        )
        if exists is None:
            created_at = agent.get("created_at") or now_iso()
            updated_at = agent.get("updated_at") or now_iso()
            self._conn.execute(
                "INSERT INTO agents (id, project_id, name, persona, tone, principles, "
                "tool_allowlist_json, skills_json, model_pref_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (agent["id"], *values, created_at, updated_at),
            )
        else:
            self._conn.execute(
                "UPDATE agents SET project_id = ?, name = ?, persona = ?, tone = ?, "
                "principles = ?, tool_allowlist_json = ?, skills_json = ?, model_pref_json = ?, "
                "updated_at = ? WHERE id = ?",
                (*values, agent.get("updated_at") or now_iso(), agent["id"]),
            )

    def sync_from_files(self) -> int:
        """Re-scan every agent.yaml on disk (user-level + every known project) and
        upsert the `agents` index table from it. Called at daemon startup and after
        every `agent.upsert` (design §4). Does not delete index rows for files that
        vanished out from under it (e.g. a hand-deleted directory) — `agent.delete`
        is the only path that removes a row, keeping "deleted via the API" and
        "file went missing on disk" distinguishable rather than silently pruning.

        Two failure-isolation properties, both load-bearing on the daemon startup
        path (review #4 — a hand-edited agent.yaml or an unreachable project
        directory must not stop the daemon from binding its socket):
          - Projects whose resolved agents directory *is* the user-level agents
            directory are skipped in the per-project pass (review #1). This is the
            real shape for `proj_default`, whose `path` is the user's home
            directory (§2/§4): `paths.project_agents_dir(home)` and
            `paths.agents_dir()` are then literally the same directory, already
            covered by the `project_path=None` pass above — scanning it again
            would silently re-home every user-level agent (`agent_default`
            included) under `proj_default`'s `project_id`.
          - Each agent (`_sync_one`) and each project's directory listing is
            isolated in its own try/except: a bad `agent.yaml` (unparsable YAML,
            a non-mapping top level, a missing `name`) or an unreadable project
            path (an unmounted external volume) is logged as a warning and
            skipped, not raised — "文件为事实源 + 允许手改" (PRD 10.3) means a
            typo in one file can't be allowed to take the whole daemon down.
        """
        synced = 0
        for agent_id in self._store.list_ids(project_path=None):
            if self._sync_one(agent_id, project_path=None, project_id=None):
                synced += 1

        user_agents_dir = paths.agents_dir(create=False).resolve()
        for project in self._conn.execute("SELECT id, path FROM projects").fetchall():
            if paths.project_agents_dir(project["path"], create=False).resolve() == user_agents_dir:
                continue
            try:
                agent_ids = self._store.list_ids(project_path=project["path"])
            except OSError as exc:
                logger.warning(
                    "failed to list agents for project, skipping",
                    extra={
                        "detail": {
                            "project_id": project["id"],
                            "path": project["path"],
                            "error": str(exc),
                        }
                    },
                )
                continue
            for agent_id in agent_ids:
                if self._sync_one(agent_id, project_path=project["path"], project_id=project["id"]):
                    synced += 1

        self._conn.commit()
        return synced

    def _sync_one(self, agent_id: str, *, project_path: str | None, project_id: str | None) -> bool:
        try:
            data = self._store.read(agent_id, project_path=project_path)
            if data is None:
                return False
            self._upsert_index_row(data, project_id=project_id)
            return True
        except (OSError, ValueError, sqlite3.Error) as exc:
            logger.warning(
                "failed to sync agent from file, skipping",
                extra={
                    "detail": {
                        "agent_id": agent_id,
                        "project_id": project_id,
                        "error": str(exc),
                    }
                },
            )
            return False

    def ensure_default_agent_file(self) -> None:
        """Materialize `agent_default`'s agent.yaml from the DB row if no file
        exists yet. Migration 004 seeds the DB row directly (a daemon needs some
        default agent to exist before any file write has ever happened); once the
        file exists it's authoritative and this never touches it again — a user's
        hand edit is never overwritten by a later daemon restart.

        No-ops (with a log, not a raise) if migration 004 hasn't applied yet, same
        reasoning as `ProjectService.ensure_default_project`.
        """
        if self._store.read(DEFAULT_AGENT_ID, project_path=None) is not None:
            return
        try:
            row = self.get(DEFAULT_AGENT_ID)
        except RpcError:
            return
        self._store.write(row, project_path=None)

    # -- CRUD -------------------------------------------------------------

    def list(self, project_id: str | None = None) -> list[dict[str, Any]]:
        """User-level agents, plus the given project's own project-scoped agents
        (not a same-id override — see module docstring)."""
        if project_id is None:
            rows = self._conn.execute(
                "SELECT * FROM agents WHERE project_id IS NULL ORDER BY created_at"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM agents WHERE project_id IS NULL OR project_id = ? "
                "ORDER BY created_at",
                (project_id,),
            ).fetchall()
        return [_row_to_agent(r) for r in rows]

    def get(self, agent_id: str) -> dict[str, Any]:
        row = self._conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if row is None:
            raise RpcError(NOT_FOUND, f"agent not found: {agent_id}")
        return _row_to_agent(row)

    def upsert(self, agent: dict[str, Any]) -> dict[str, Any]:
        """`agent`: `{id?, project_id?, name, persona?, tone?, principles?,
        tool_allowlist?, skills?, model_pref?}`. Omitting `id` creates a new Agent
        (fresh ULID); passing an existing `id` updates it in place, at whichever
        scope (`project_id`) it already belongs to (changing `project_id` on an
        existing agent — moving it between scopes — is rejected: that would strand
        the old file and orphan the sessions bound to it; delete + recreate
        instead).
        """
        agent_id = agent.get("id")
        is_new = agent_id is None
        if is_new:
            agent_id = new_ulid()
            project_id = agent.get("project_id")
            existing_file = None
        else:
            existing_row = self._conn.execute(
                "SELECT project_id FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
            if existing_row is None:
                raise RpcError(NOT_FOUND, f"agent not found: {agent_id}")
            project_id = existing_row["project_id"]
            if "project_id" in agent and agent["project_id"] != project_id:
                raise RpcError(
                    INVALID_PARAMS,
                    "cannot change an existing agent's project_id; delete and recreate instead",
                )
            existing_file = self._store.read(agent_id, project_path=self._project_path(project_id))

        project_path = self._project_path(project_id)

        existing_file = existing_file or {}
        name = agent.get("name") if "name" in agent else existing_file.get("name")
        if not name:
            raise RpcError(INVALID_PARAMS, "name is required")
        persona = agent.get("persona") if "persona" in agent else existing_file.get("persona")
        tone = agent.get("tone") if "tone" in agent else existing_file.get("tone")
        principles = (
            agent.get("principles") if "principles" in agent else existing_file.get("principles")
        )
        tool_allowlist = (
            agent.get("tool_allowlist")
            if "tool_allowlist" in agent
            else existing_file.get("tool_allowlist") or []
        )
        skills = agent.get("skills") if "skills" in agent else existing_file.get("skills") or []
        model_pref = (
            agent.get("model_pref")
            if "model_pref" in agent
            else existing_file.get("model_pref") or {}
        )

        now = now_iso()
        record = {
            "id": agent_id,
            "name": name,
            "persona": persona,
            "tone": tone,
            "principles": principles,
            "tool_allowlist": tool_allowlist,
            "skills": skills,
            "model_pref": model_pref,
            "created_at": existing_file.get("created_at") or now,
            "updated_at": now,
        }

        self._store.write(record, project_path=project_path)
        self._upsert_index_row(record, project_id=project_id)
        self._conn.commit()
        return self.get(agent_id)

    def delete(self, agent_id: str) -> None:
        row = self.get(agent_id)  # raises not_found
        if agent_id == DEFAULT_AGENT_ID:
            raise RpcError(INVALID_STATE, "cannot delete the built-in default agent")

        in_use = self._conn.execute(
            "SELECT COUNT(*) AS c FROM sessions WHERE agent_id = ?", (agent_id,)
        ).fetchone()["c"]
        if in_use:
            raise RpcError(
                INVALID_STATE,
                f"agent is bound to {in_use} session(s); cannot delete",
                {"session_count": in_use},
            )

        project_path = self._project_path(row["project_id"])
        self._store.delete(agent_id, project_path=project_path)
        self._conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        self._conn.commit()
