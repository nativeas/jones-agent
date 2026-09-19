"""RPC `capability.list` (Issue #17 §2, #19 daemon 侧; 03-w4-interfaces.md §2; G21).

`register(server, ctx)` follows the module convention every branch uses
(`agents/methods.py`'s docstring). This module owns no persistent state of its
own — every call recomputes fresh from the same session/Agent/config rows the
rule gate itself reads, plus (when available) the worker's own
`<HERMES_HOME>/jones_tools.json` snapshot (`kernel/plugin/jones_gate/
_tools_snapshot.py`'s `on_session_start` hook writes it — see that module's
docstring for why the daemon can't just ask a live worker for its tool list
directly: there is no ACP method for it, and the daemon is not an MCP client of
its own, so per-server MCP tool schemas are only knowable from what the worker
itself reported having actually registered).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jones_daemon.capabilities import registry
from jones_daemon.context import DaemonContext
from jones_daemon.permissions import gate_config
from jones_daemon.projects.service import ProjectService
from jones_daemon.rpc.errors import INVALID_PARAMS, NOT_FOUND, RpcError
from jones_daemon.rpc.server import Connection, RpcServer
from jones_daemon.sessions import queries
from jones_daemon.store import run_in_db_thread


def _read_jones_tools(hermes_home: Path) -> dict[str, Any] | None:
    """Daemon-side read of the same file `_tools_snapshot.py` writes inside the
    worker. `None` (never raises) if it's missing, unreadable, or not a JSON
    object — a fresh session that has never run a Turn is the ordinary case,
    not an error (see `registry.reconcile`'s `actual_available` handling)."""
    try:
        raw = (hermes_home / "jones_tools.json").read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _agent_skills(conn, agent_id: str) -> list[str]:
    row = conn.execute("SELECT skills_json FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if row is None:
        return []
    try:
        parsed = json.loads(row["skills_json"] or "[]")
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) and all(isinstance(s, str) for s in parsed) else []


def _build_result(ctx: DaemonContext, session_id: str) -> dict[str, Any]:
    """Runs entirely on the DB thread (sqlite3 + `gate_config`'s own synchronous
    file I/O, same rule `permissions/gate_config.py`'s module docstring states)."""
    session = queries.get_session(ctx.db, session_id)
    if session is None:
        raise RpcError(NOT_FOUND, "session not found", {"id": session_id})

    project_id = session["project_id"]
    try:
        project = ProjectService(ctx.db).get(project_id)
        project_path = project["path"]
    except RpcError:
        project_path = None  # a Session referencing a deleted Project — degrade, don't crash

    permissions_result = ctx.config.permissions(project_id)
    gate = gate_config.build(
        conn=ctx.db,
        permissions_result=permissions_result,
        session=session,
        user_root=ctx.paths.user_root(),
        project_path=project_path,
    )
    mode = gate["mode"]
    rules = gate["rules"]
    tool_allowlist = gate["tool_allowlist"]

    hermes_home = gate_config.hermes_home_for(ctx.paths.user_root(), session_id)
    snapshot = _read_jones_tools(hermes_home)
    actual_tools: list[str] | None = None
    actual_mcp_servers: set[str] | None = None
    if snapshot is not None:
        raw_tools = snapshot.get("tools")
        if isinstance(raw_tools, list) and all(isinstance(t, str) for t in raw_tools):
            actual_tools = raw_tools
        raw_servers = snapshot.get("mcp_servers")
        if isinstance(raw_servers, list) and all(isinstance(s, str) for s in raw_servers):
            actual_mcp_servers = set(raw_servers)

    mcp_states: list[registry.McpServerState] = []
    for entry in ctx.config.mcp_servers(project_id):
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        enabled = entry.get("enabled", True) is not False
        reachable: bool | None = None
        if enabled and actual_mcp_servers is not None:
            reachable = name in actual_mcp_servers
        mcp_states.append(registry.McpServerState(name=name, enabled=enabled, reachable=reachable))

    # Skill tool enumeration is #18/K's (`skills.worker_skill_dirs` isn't landed
    # yet — see `capabilities/registry.py`'s module docstring and the PR
    # report). `capability.list` still reports every ACTUALLY loaded tool
    # (skills included) via `reconcile`'s unmatched-name handling even with an
    # empty `skill_tools` here — it just can't yet label them `source="skill"`
    # ahead of time or explain *why* one is hidden before it's ever run.
    skill_tools: dict[str, list[str]] = {}

    expected = registry.expected_capabilities(
        mode=mode, tool_allowlist=tool_allowlist, rules=rules,
        mcp_servers=mcp_states, skill_tools=skill_tools,
    )
    result = registry.reconcile(expected, actual_tools)
    return {"session_id": session_id, **result.to_json()}


def register(server: RpcServer, ctx: DaemonContext) -> None:
    async def capability_list(params: dict[str, Any], conn: Connection) -> dict[str, Any]:
        session_id = params.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise RpcError(INVALID_PARAMS, "session_id is required", {"params": params})
        result = await run_in_db_thread(_build_result, ctx, session_id)
        if result["drift"]:
            # G21: a non-empty drift is a real "the transparency page and what
            # the model actually got don't match" signal (DEV.md 工程原则 #4:
            # 诚实失败). `_build_result` runs on the DB thread and can't safely
            # call the async `RpcServer.broadcast` itself — do it here, back on
            # the event loop, after the DB-thread call returns.
            await ctx.server.broadcast(
                session_id, "daemon.error",
                {"reason": "capability_drift", "session_id": session_id, "drift": result["drift"]},
            )
        return result

    server.register("capability.list", capability_list)
