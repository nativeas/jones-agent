"""ConfigResolver (docs/design/01-w2-interfaces.md §4): merges user-level
`~/.jones/config/*.json` with project-level `<project>/.jones/*.json`. Settings and
MCP server lists are a plain override-by-key merge; permission rules follow PRD
10.1's asymmetric rule — project level can only *tighten*, never loosen, and a
project rule that tries to loosen is dropped and logged as a warning rather than
silently applied or hard-erroring (violating it isn't a protocol error, it's a
project author mis-configuring something, per PRD G14/N11).

`settings_json`/permissions.json schema is specified here (design §4 says: "C 允许
追加本节") — this docstring plus the dataclasses below are that specification; a
matching summary is appended to docs/design/01-w2-interfaces.md §4.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Literal

from jones_daemon import paths
from jones_daemon.config.jsonfile import logger, read_json, read_json_result
from jones_daemon.rpc.errors import NOT_FOUND, RpcError

# Built-in defaults for settings.json (design §4 addendum): every key a resolved
# Settings dict is guaranteed to have, even with no settings.json anywhere.
DEFAULT_SETTINGS: dict[str, Any] = {
    "default_mode": "task",  # chat | task | auto (PRD 9.1)
    "default_agent_id": "agent_default",
    "concurrency_limit": 4,
    "approval_timeout_minutes": None,  # None = 不超时 (PRD 9.4 default)
    # R-N5 (controller ruling, 2026-09-20; PRD 11.2's own numbers, 01-w2-
    # interfaces.md §4.1): `sessions/service.py::_run_turn`/
    # `_handle_tool_call_start` read these to enforce the 单 Run Step 数/时长
    # upper bounds as a 预算终止 (PRD 9.3).
    "max_steps_per_run": 200,
    "max_run_duration_s": 7200,  # 2h
}

RuleAction = Literal["allow", "deny"]


@dataclass(frozen=True)
class PermissionRule:
    # `match` is an opaque identity key for "the same restriction" across user and
    # project scope — a tool name or a command-prefix string; W2 does no glob/regex
    # overlap detection (that belongs to the W3 rule-gate engine that actually
    # evaluates these against a live tool call, per 01-w2-interfaces.md §2's "裁决
    # 逻辑是 W3 FR05"). Two rules "conflict" (same restriction) iff `match` is equal.
    match: str
    action: RuleAction


@dataclass(frozen=True)
class Permissions:
    rules: tuple[PermissionRule, ...]
    # Human-readable notes about project-level rules that were dropped because they
    # tried to loosen a user-level restriction (PRD 10.1), *and* about a
    # permissions.json that existed but failed to parse (see `degraded` below).
    # Also logged via `logger.warning` as they're produced — returned here too so
    # callers (tests, a future settings UI) can surface them without scraping logs.
    warnings: tuple[str, ...] = field(default_factory=tuple)
    # True when a permissions.json existed at some scope (user or project) but
    # failed to parse, so `rules` is missing whatever that file would have
    # contributed — as opposed to the normal "nothing configured" case, where
    # `degraded` is False and an empty `rules` genuinely means no restrictions set.
    # A caller deciding whether to allow a tool call (the W3 rule gate) MUST check
    # this and fail closed (deny) when it's True — reading `rules` alone can't
    # distinguish "no rules configured" from "rules unreadable", and treating the
    # latter as the former is a fail-open hole (see docs/design review #7).
    degraded: bool = False


def _merge_permission_rules(
    user_rules: list[dict[str, Any]], project_rules: list[dict[str, Any]]
) -> Permissions:
    effective: dict[str, RuleAction] = {}
    for raw in user_rules:
        effective[raw["match"]] = raw["action"]

    warnings: list[str] = []
    for raw in project_rules:
        match, action = raw["match"], raw["action"]
        user_action = effective.get(match)
        if user_action == "deny" and action == "allow":
            # Tightening only (PRD 10.1 / G14 / N11): a project can't punch a hole
            # in a user-level deny. Drop it, keep the user's deny in effect.
            warnings.append(
                f"project permission rule {match!r} (allow) ignored: "
                "user-level rule denies it and project level can only tighten"
            )
            continue
        # Anything else is a tightening (new deny, or a deny re-affirmed) or a
        # no-op (same action already in effect) — accept.
        effective[match] = action

    rules = tuple(PermissionRule(match=k, action=v) for k, v in effective.items())
    return Permissions(rules=rules, warnings=tuple(warnings))


class ConfigResolver:
    """Protocol shape from 01-w2-interfaces.md §4 (documented here as an ABC-free
    structural contract — see `DefaultConfigResolver` for the implementation A/B
    depend on via this interface, not the concrete class).
    """

    def settings(self, project_id: str | None) -> dict[str, Any]: ...

    def permissions(self, project_id: str | None) -> Permissions: ...

    def mcp_servers(self, project_id: str | None) -> list[dict[str, Any]]: ...


class DefaultConfigResolver:
    """The real `ConfigResolver` implementation (issue #8/#9). Takes the shared
    sqlite3.Connection (only to resolve `project_id` -> project directory path;
    all actual config content lives in JSON files, not the DB — PRD §10.2/§10.3).

    Every method here does synchronous file + sqlite I/O — callers on the asyncio
    event loop thread must offload through `store.run_in_db_thread` (same rule as
    every other sqlite3.Connection use in this codebase, see store/db.py).
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def _project_path(self, project_id: str | None) -> str | None:
        if project_id is None:
            return None
        row = self._conn.execute(
            "SELECT path FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise RpcError(NOT_FOUND, f"project not found: {project_id}")
        return row["path"]

    def settings(self, project_id: str | None) -> dict[str, Any]:
        merged = dict(DEFAULT_SETTINGS)
        merged.update(read_json(paths.config_dir() / "settings.json", {}))
        project_path = self._project_path(project_id)
        if project_path is not None:
            merged.update(
                read_json(paths.project_settings_path(project_path, create=False), {})
            )
        return merged

    def permissions(self, project_id: str | None) -> Permissions:
        # Uses `read_json_result` (not the `read_json`/`{}` fallback every other
        # config file here uses) specifically so a permissions.json that exists
        # but fails to parse is distinguishable from one that was never
        # configured — see `Permissions.degraded`'s docstring and design review
        # #7: falling back silently here would drop every deny rule with no
        # signal, which is a fail-open security bug, not a UX nicety.
        user_data, user_ok = read_json_result(paths.config_dir() / "permissions.json")
        user_rules = (user_data or {}).get("rules", [])

        project_path = self._project_path(project_id)
        project_rules: list[dict[str, Any]] = []
        project_ok = True
        if project_path is not None:
            project_data, project_ok = read_json_result(
                paths.project_permissions_path(project_path, create=False)
            )
            project_rules = (project_data or {}).get("rules", [])

        merged = _merge_permission_rules(user_rules, project_rules)
        warnings = list(merged.warnings)
        if not user_ok:
            warnings.append(
                "user-level permissions.json exists but failed to parse; its rules "
                "are unavailable this resolve (degraded — treat as fail-closed, not "
                "as \"no rules configured\")"
            )
        if not project_ok:
            warnings.append(
                f"project-level permissions.json for project_id={project_id!r} exists "
                "but failed to parse; its rules are unavailable this resolve (degraded)"
            )
        for warning in warnings:
            logger.warning(warning, extra={"detail": {"project_id": project_id}})

        return Permissions(
            rules=merged.rules,
            warnings=tuple(warnings),
            degraded=not user_ok or not project_ok,
        )

    def mcp_servers(self, project_id: str | None) -> list[dict[str, Any]]:
        user_servers = read_json(paths.config_dir() / "mcp.json", {}).get("servers", [])
        project_path = self._project_path(project_id)
        if project_path is None:
            return list(user_servers)
        project_servers = read_json(
            paths.project_mcp_path(project_path, create=False), {}
        ).get("servers", [])
        # Not a permission — no tightening constraint. Project entries override a
        # user entry of the same name, new names are appended.
        by_name: dict[str, dict[str, Any]] = {s["name"]: s for s in user_servers}
        for server in project_servers:
            by_name[server["name"]] = server
        return list(by_name.values())
