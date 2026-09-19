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
from jones_daemon.rpc.errors import (
    CAPABILITY_DRIFT,
    INVALID_PARAMS,
    MCP_SERVER_DOWN,
    NOT_FOUND,
    RpcError,
)
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
    # Review round-2 finding #3: MCP discovery is asynchronous
    # (`acp_adapter/entry.py` starts it on a background thread; the snapshot's
    # probe Turn only bounds it to ~1.5s before giving up) — an EMPTY
    # `mcp_servers` list in a snapshot taken before discovery finished is NOT
    # evidence a configured server is down, it's evidence discovery hadn't
    # reported back yet. `_tools_snapshot.py`'s `on_session_start` records
    # whether discovery had actually completed when it wrote the snapshot;
    # only when that's true does an absent-but-configured server become the
    # STRONGER "confirmed down" claim (`McpServerState.reachable=False`) —
    # otherwise `reachable` stays `None` ("not checked either way"), same as
    # when there's no snapshot at all.
    mcp_discovery_complete = False
    if snapshot is not None:
        raw_tools = snapshot.get("tools")
        if isinstance(raw_tools, list) and all(isinstance(t, str) for t in raw_tools):
            actual_tools = raw_tools
        raw_servers = snapshot.get("mcp_servers")
        if isinstance(raw_servers, list) and all(isinstance(s, str) for s in raw_servers):
            actual_mcp_servers = set(raw_servers)
        mcp_discovery_complete = snapshot.get("mcp_discovery_complete") is True

    mcp_states: list[registry.McpServerState] = []
    confirmed_down: list[str] = []
    for entry in ctx.config.mcp_servers(project_id):
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        enabled = entry.get("enabled", True) is not False
        reachable: bool | None = None
        if enabled and actual_mcp_servers is not None and mcp_discovery_complete:
            reachable = name in actual_mcp_servers
            if not reachable:
                confirmed_down.append(name)
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
    # `tool_allowlist`/`rules` passed through so a `mcp:<server>` placeholder
    # that expands into real per-tool names gets `enabled` RECOMPUTED per real
    # name rather than inheriting the placeholder's own value (controller
    # ruling R-H2 — see `registry.reconcile`'s docstring).
    result = registry.reconcile(
        expected, actual_tools, tool_allowlist=tool_allowlist, rules=rules
    )
    return {
        "session_id": session_id,
        **result.to_json(),
        # Consumed by the RPC wrapper below (back on the event loop) to decide
        # whether an `mcp_server_down` `daemon.error` is warranted — controller
        # ruling R-H4 ("不允许只 warning"). Real evidence only (see the
        # `mcp_discovery_complete` handling above) — never asserted from an
        # absent/incomplete snapshot.
        "_confirmed_down_mcp_servers": confirmed_down,
    }


def register(server: RpcServer, ctx: DaemonContext) -> None:
    async def capability_list(params: dict[str, Any], conn: Connection) -> dict[str, Any]:
        session_id = params.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise RpcError(INVALID_PARAMS, "session_id is required", {"params": params})
        result = await run_in_db_thread(_build_result, ctx, session_id)
        confirmed_down = result.pop("_confirmed_down_mcp_servers")
        # `daemon.error` payload shape per 00-foundation.md §4.2/§4.3:
        # `{code, message, detail}` — controller ruling R-H3 (this used to be a
        # bespoke `{reason, session_id, drift}` shape that didn't match the
        # contract every other `daemon.error` emitter uses). Broadcast via
        # `broadcast_all` (R-H3), not the per-session `broadcast` above: a
        # client watching a DIFFERENT session should still learn this
        # session's tool assembly disagreed with what the transparency page
        # promised, or that a configured MCP server is confirmed down — both
        # are daemon-wide "honest failure" signals (DEV.md 工程原则 #4), not
        # per-session chatter. `_build_result` runs on the DB thread and can't
        # safely call the async `RpcServer.broadcast_all` itself — do it here,
        # back on the event loop, after the DB-thread call returns.
        if result["drift"]:
            # G21: a non-empty drift is a real "the transparency page and what
            # the model actually got don't match" signal.
            await ctx.server.broadcast_all(
                "daemon.error",
                {
                    "code": CAPABILITY_DRIFT,
                    "message": f"session {session_id}: tool assembly drifted from what "
                    "capability.list expected",
                    "detail": {"session_id": session_id, "drift": result["drift"]},
                },
            )
        for server_name in confirmed_down:
            await ctx.server.broadcast_all(
                "daemon.error",
                {
                    "code": MCP_SERVER_DOWN,
                    "message": f"session {session_id}: configured MCP server "
                    f"{server_name!r} did not register any tools",
                    "detail": {"session_id": session_id, "mcp_server": server_name},
                },
            )
        return result

    server.register("capability.list", capability_list)
