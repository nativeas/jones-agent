"""Builds and writes `<HERMES_HOME>/jones_gate.json` — the daemon-side half of
the rule-gate config contract (docs/design/02-w3-interfaces.md §1.1: "把该会话
生效的规则（ctx.config.permissions(project_id) 合并结果 + 会话模式 + Agent 工具
白名单）写成 <HERMES_HOME>/jones_gate.json"). The reader half is
`kernel/plugin/jones_gate/_config.py`; keep this module's `_JSON schema`
comment and that one's in sync by hand (that package can't import this one,
see its docstring).

Called from `sessions/service.py::send()`'s one gate-refresh call, always on
the DB thread (`store.run_in_db_thread`) — every function here does
synchronous sqlite3 + file I/O, same rule as `sessions/queries.py`.

## N13 tool-allowlist enforcement lives here, not at session creation

`sessions/service.py::create()`/`set_mode()` already enforce PRD 9.6's mode-
narrowing half of N13 ("父任务模式不得派生自动模式子会话") at creation/mode-
change time — that code is #10's (A's), unmodified by this branch. The
*tool-allowlist* half ("子会话的工具白名单 ⊆ 父会话") is enforced here instead,
every time this file is written, by walking the session's parent chain and
intersecting every ancestor's own non-empty Agent tool_allowlist into the
child's effective one (`_effective_tool_allowlist`) — continuously re-derived
from the live Agent/session rows rather than checked once at creation. This
is deliberately the stronger guarantee: it holds even if an Agent's
`tool_allowlist` is edited *after* a child session already exists (creation-
time-only validation couldn't catch that), and it's the only place in this
PR's scope where `agents/policy.py::is_tool_allowlist_subset` — named
explicitly for N13 in 02-w3-interfaces.md §1.1 — is actually exercised
end-to-end against real session/Agent rows (also unit-tested directly in
`tests/test_agents_policy.py`, pre-existing).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from jones_daemon import paths
from jones_daemon.agents.policy import is_tool_allowlist_subset
from jones_daemon.logging import get_logger

logger = get_logger("permissions")

# A tool name no real Hermes/MCP tool schema will ever register (leading/
# trailing double-underscore, a colon, and a Jones-namespaced prefix all in
# one — well outside `[a-z0-9_]+`-style real tool names) — see
# `_tool_allowlist_for_json`'s docstring for why this sentinel exists at all.
_NO_TOOLS_SENTINEL = "__jones:no-tools-allowed__"

_FILE_NAME = "jones_gate.json"


def hermes_home_for(user_root: Path, session_id: str) -> Path:
    """Same path formula as `workers/manager.py::WorkerManager._hermes_home_for`
    (same session -> same HERMES_HOME) — duplicated by hand rather than
    imported: that method is private and this branch's shared-file allowance
    for `workers/manager.py` doesn't include adding a public accessor (see
    the PR report's "契约变更" section). Keep in sync if that formula ever
    changes."""
    return user_root / "workers" / session_id / "hermes"


def gate_config_path(hermes_home: Path) -> Path:
    return hermes_home / _FILE_NAME


def _extract_permission_rules(permissions_result: Any) -> list[dict[str, str]]:
    """`ctx.config.permissions(project_id)` returns a `config/resolver.py::
    Permissions` dataclass in production (`.rules` of `PermissionRule(match,
    action)`), but tests commonly wire `NullConfigResolver`, whose
    `permissions()` returns a plain `{}` (see `context.py`'s `Permissions =
    dict[str, Any]` Protocol alias — the real dataclass predates that comment
    going stale, see 01-w2-interfaces.md §1). Accept either shape; anything
    else (a missing/malformed rule) is dropped rather than raising — a
    malformed *rule* is not the same failure as "permissions.json itself is
    unreadable" (`Permissions.degraded`, checked separately by
    `sessions/service.py::send()` before this is even called)."""
    if isinstance(permissions_result, dict):
        rules = permissions_result.get("rules") or []
    else:
        rules = getattr(permissions_result, "rules", None) or ()
    out: list[dict[str, str]] = []
    for r in rules:
        if isinstance(r, dict):
            match, action = r.get("match"), r.get("action")
        else:
            match, action = getattr(r, "match", None), getattr(r, "action", None)
        if isinstance(match, str) and action in ("allow", "deny"):
            out.append({"match": match, "action": action})
    return out


def _agent_tool_allowlist(conn: sqlite3.Connection, agent_id: str) -> list[str]:
    row = conn.execute(
        "SELECT tool_allowlist_json FROM agents WHERE id = ?", (agent_id,)
    ).fetchone()
    if row is None:
        return []
    try:
        parsed = json.loads(row["tool_allowlist_json"] or "[]")
    except json.JSONDecodeError:
        logger.warning(
            "agents.tool_allowlist_json is not valid JSON; treating as empty (unrestricted)",
            extra={"detail": {"agent_id": agent_id}},
        )
        return []
    return parsed if isinstance(parsed, list) and all(isinstance(t, str) for t in parsed) else []


def _parent_chain(conn: sqlite3.Connection, session: dict[str, Any]) -> list[dict[str, Any]]:
    chain: list[dict[str, Any]] = []
    seen = {session["id"]}
    parent_id = session.get("parent_id")
    while parent_id and parent_id not in seen:
        row = conn.execute(
            "SELECT id, parent_id, agent_id FROM sessions WHERE id = ?", (parent_id,)
        ).fetchone()
        if row is None:
            break
        entry = dict(row)
        chain.append(entry)
        seen.add(parent_id)
        parent_id = entry.get("parent_id")
    return chain


def _effective_tool_allowlist(
    conn: sqlite3.Connection, session: dict[str, Any]
) -> list[str] | None:
    """`None` = unrestricted, `[]` = every ancestor's non-empty allowlist
    intersected down to nothing (genuinely zero tools allowed — see
    `_tool_allowlist_for_json`), a non-empty list = the narrowed set."""
    effective: list[str] | None = _agent_tool_allowlist(conn, session["agent_id"]) or None
    for ancestor in _parent_chain(conn, session):
        ancestor_list = _agent_tool_allowlist(conn, ancestor["agent_id"])
        if not ancestor_list:
            continue  # this ancestor is itself unrestricted -> nothing to narrow against
        if effective is None:
            effective = list(ancestor_list)
            continue
        narrowed = [t for t in effective if t in ancestor_list]
        # `agents/policy.py::is_tool_allowlist_subset` can't directly verify
        # `narrowed` here: its own contract treats an EMPTY list as
        # "unrestricted" (see that module's docstring), whereas an empty
        # `narrowed` in THIS function means the opposite — a real
        # intersection down to zero shared tools (see this function's
        # docstring and `_tool_allowlist_for_json`). It's still the right
        # tool for the non-empty case, where its "empty parent accepts
        # anything" special case doesn't come into play: a `narrowed` that
        # is non-empty and was built as `[t for t in effective if t in
        # ancestor_list]` is a literal subset of `ancestor_list` by
        # construction, and asserting that with the named PRD 9.6/N13
        # helper (rather than trusting the comprehension silently) is what
        # makes it a checked fact here, not an assumption.
        if narrowed and not is_tool_allowlist_subset(narrowed, ancestor_list):  # pragma: no cover
            raise AssertionError("narrowed tool_allowlist was not a subset of its ancestor's")
        if set(narrowed) != set(effective):
            logger.warning(
                "session's effective tool_allowlist narrowed by an ancestor's Agent "
                "whitelist (PRD 9.6, N13)",
                extra={
                    "detail": {
                        "session_id": session["id"], "ancestor_session_id": ancestor["id"],
                        "before": effective, "after": narrowed,
                    }
                },
            )
        effective = narrowed
    return effective


def _tool_allowlist_for_json(effective: list[str] | None) -> list[str]:
    """`None` (unrestricted) -> `[]`, the convention both this file's writer
    and the rule gate's reader (`kernel/plugin/jones_gate/__init__.py`'s
    `allowlist = config.get("tool_allowlist") or []`) already use elsewhere.
    An empty list *out of the intersection* means something different
    (genuinely zero tools allowed) and must NOT collapse to the same `[]` —
    that would silently read back as "unrestricted", the opposite of what a
    real narrowing-to-nothing means (PRD 9.6/N13) — so it's represented by a
    sentinel no real tool name can ever equal instead."""
    if effective is None:
        return []
    if not effective:
        return [_NO_TOOLS_SENTINEL]
    return effective


def build(
    *,
    conn: sqlite3.Connection,
    permissions_result: Any,
    session: dict[str, Any],
    user_root: Path,
    project_path: str | None,
    extra_rules: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Pure computation (no I/O beyond the read-only `conn` queries) — see
    `write()` for the file-write half. `project_path` is the session's
    resolved project directory (already handled the "default project = user
    home" special case, or `None` when it couldn't be resolved — see
    `sessions/service.py::send()`'s call site) — used only to locate the
    project's `permissions.json` for the hard-deny gate's protected-path
    check and to give the review gate a workspace root for write-in/out-of-
    workspace classification; its absence degrades those two checks, it
    never widens what the rule gate allows. `extra_rules` are
    `permission.decide(remember="session")`'s in-memory, session-scoped
    `allow` entries (`sessions/service.py::_remember_allow`) — appended
    after the file-backed rules; always `action="allow"` by construction
    (an explicit in-the-moment user decision, never a config file trying to
    sneak past a user-level `deny` — PRD 10.1's "只能收紧" governs
    permissions.json merging, not this)."""
    rules = _extract_permission_rules(permissions_result) + list(extra_rules or [])
    allowlist = _effective_tool_allowlist(conn, session)
    return {
        "mode": session["mode"],
        "user_root": str(user_root),
        "project_permissions_path": (
            str(paths.project_permissions_path(project_path, create=False))
            if project_path is not None
            else None
        ),
        "cwd": project_path,
        "rules": rules,
        # `config/resolver.py::Permissions.degraded`: a permissions.json
        # existed but failed to parse, so `rules` above may be silently
        # missing a `deny` that real file would have contributed — reading
        # `rules` alone can't tell "no rules configured" apart from "rules
        # unreadable" (see that dataclass's docstring, design review #7).
        # `NullConfigResolver`'s plain `{}` has no such attribute -> False
        # (nothing configured, not degraded).
        "rules_degraded": bool(getattr(permissions_result, "degraded", False)),
        "tool_allowlist": _tool_allowlist_for_json(allowlist),
    }


def read(hermes_home: Path) -> dict[str, Any] | None:
    """Daemon-side counterpart to `kernel/plugin/jones_gate/_config.py::
    load()` — reads back the SAME `jones_gate.json` snapshot the rule gate
    read moments earlier for this Turn (controller ruling R10, round 6,
    final): `sessions/service.py::_on_request_permission`'s defense-in-depth
    hard-deny/rule-match recheck for terminal-class tools must agree with
    what the plugin already saw, not a fresh recompute that could race a
    live `permissions.json` edit mid-Turn — the same "两个 gate 必须对同一份
    快照达成一致" principle `_extract_tool_call`'s mode hint already follows
    (see that function's docstring). `None` (never raises) if the file is
    missing or not valid JSON — callers must fail closed (no `user_root`, no
    rule match), never assume "nothing configured"."""
    try:
        raw = gate_config_path(hermes_home).read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def write(hermes_home: Path, config: dict[str, Any]) -> Path:
    """Atomic (write-temp-then-rename) so `_config.py`'s mtime-cached reader
    in the worker process never observes a half-written file mid-write."""
    hermes_home.mkdir(parents=True, exist_ok=True)
    target = gate_config_path(hermes_home)
    tmp = hermes_home / f"{_FILE_NAME}.tmp"
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)
    return target
