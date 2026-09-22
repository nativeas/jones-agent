"""Tests for the `capability.list` RPC handler (`capabilities/methods.py`;
Issue #17 §2, #19 daemon 侧; G21)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from jones_daemon.capabilities.methods import _build_result, register
from jones_daemon.context import DaemonContext
from jones_daemon.projects.bootstrap import bootstrap_projects_and_agents
from jones_daemon.rpc.errors import RpcError
from jones_daemon.rpc.server import RpcServer
from jones_daemon.sessions.service import DEFAULT_AGENT_ID, DEFAULT_PROJECT_ID
from jones_daemon.store import apply_pending, connect, run_in_db_thread


class _FakeServer:
    def __init__(self) -> None:
        self.broadcasts: list[tuple[str, str, Any]] = []
        self.broadcast_alls: list[tuple[str, Any]] = []

    async def broadcast(self, session_id: str, method: str, params: Any) -> None:
        self.broadcasts.append((session_id, method, params))

    async def broadcast_all(self, method: str, params: Any) -> None:
        self.broadcast_alls.append((method, params))


class _FakeConfig:
    def __init__(self, *, mcp_servers=None, permissions=None) -> None:
        self._mcp_servers = mcp_servers or []
        self._permissions = permissions or {"rules": []}

    def settings(self, project_id):
        return {}

    def permissions(self, project_id):
        return self._permissions

    def mcp_servers(self, project_id):
        return self._mcp_servers


@pytest.fixture
async def ctx(tmp_path, monkeypatch):
    def _open():
        conn = connect(tmp_path / "jones.db")
        apply_pending(conn)
        bootstrap_projects_and_agents(conn)
        return conn

    conn = await run_in_db_thread(_open)
    from jones_daemon import paths

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("JONES_HOME", str(home))
    return DaemonContext(
        db=conn, paths=paths, server=_FakeServer(),
        providers=None, config=_FakeConfig(),  # type: ignore[arg-type]
    )


def _insert_session(conn, session_id: str, *, mode: str = "task") -> None:
    now = "2026-09-19T00:00:00.000Z"
    conn.execute(
        "INSERT INTO sessions (id, project_id, agent_id, parent_id, is_main, mode, title, "
        "status, created_at, updated_at) VALUES (?, ?, ?, NULL, 0, ?, 't', 'active', ?, ?)",
        (session_id, DEFAULT_PROJECT_ID, DEFAULT_AGENT_ID, mode, now, now),
    )
    conn.commit()


def _set_agent_allowlist(conn, agent_id: str, allowlist: list[str]) -> None:
    conn.execute(
        "UPDATE agents SET tool_allowlist_json = ? WHERE id = ?",
        (json.dumps(allowlist), agent_id),
    )
    conn.commit()


async def _call(ctx, session_id: str) -> dict[str, Any]:
    server = RpcServer.__new__(RpcServer)  # register() only needs `.register`
    handlers: dict[str, Any] = {}
    server.register = lambda name, fn: handlers.__setitem__(name, fn)  # type: ignore[method-assign]
    register(server, ctx)
    return await handlers["capability.list"]({"session_id": session_id}, conn=None)


async def test_capability_list_reports_unrestricted_builtins_and_no_actual_data(ctx):
    await run_in_db_thread(_insert_session, ctx.db, "s1", mode="auto")
    result = await _call(ctx, "s1")
    assert result["session_id"] == "s1"
    assert result["actual_available"] is False
    assert result["drift"] == []
    by_name = {t["name"]: t for t in result["tools"]}
    assert by_name["read_file"]["enabled"] is True
    assert by_name["read_file"]["source"] == "builtin"


async def test_capability_list_chat_mode_hides_everything(ctx):
    await run_in_db_thread(_insert_session, ctx.db, "s1", mode="chat")
    result = await _call(ctx, "s1")
    assert all(t["enabled"] is False and t["hidden_reason"] == "mode_chat" for t in result["tools"])


async def test_capability_list_respects_agent_tool_allowlist(ctx):
    await run_in_db_thread(_insert_session, ctx.db, "s1", mode="task")
    await run_in_db_thread(_set_agent_allowlist, ctx.db, DEFAULT_AGENT_ID, ["read_file"])
    result = await _call(ctx, "s1")
    by_name = {t["name"]: t for t in result["tools"]}
    assert by_name["read_file"]["enabled"] is True
    assert by_name["terminal"]["enabled"] is False
    assert by_name["terminal"]["hidden_reason"] == "not_in_allowlist"


async def test_capability_list_reports_mcp_server_as_not_in_allowlist_by_default(ctx):
    ctx.config = _FakeConfig(mcp_servers=[{"name": "echo", "command": "python3"}])
    await run_in_db_thread(_insert_session, ctx.db, "s1", mode="auto")
    result = await _call(ctx, "s1")
    placeholder = next(t for t in result["tools"] if t["source"] == "mcp")
    assert placeholder["name"] == "mcp:echo"
    assert placeholder["enabled"] is False
    assert placeholder["hidden_reason"] == "not_in_allowlist"


async def test_capability_list_unknown_session_raises_not_found(ctx):
    with pytest.raises(RpcError):
        await _call(ctx, "does-not-exist")


async def test_capability_list_no_drift_when_a_hidden_tool_loaded_anyway(ctx):
    """Controller ruling R-H1: the worker assembling `terminal` into the
    schema even though the Agent's narrow whitelist hides it is the ORDINARY
    case (schema visibility ≠ execution permission — see `registry.py`'s
    module docstring), not a G21 violation. Before this round's fix, this
    exact input (a narrowed-allowlist session with a fuller `jones_tools.
    json`, which is what every real restricted Agent looks like) always
    reported drift — round-2 review findings #2/#5."""
    from jones_daemon.permissions import gate_config

    await run_in_db_thread(_insert_session, ctx.db, "s1", mode="task")
    await run_in_db_thread(_set_agent_allowlist, ctx.db, DEFAULT_AGENT_ID, ["read_file"])
    hermes_home = gate_config.hermes_home_for(ctx.paths.user_root(), "s1")
    hermes_home.mkdir(parents=True)
    (hermes_home / "jones_tools.json").write_text(
        json.dumps({"session_id": "s1", "tools": ["read_file", "terminal"], "mcp_servers": []}),
        encoding="utf-8",
    )
    result = await run_in_db_thread(_build_result, ctx, "s1")
    assert result["actual_available"] is True
    assert result["drift"] == []
    by_name = {t["name"]: t for t in result["tools"]}
    assert by_name["read_file"]["actually_loaded"] is True
    assert by_name["terminal"]["actually_loaded"] is True
    assert by_name["terminal"]["enabled"] is False


async def test_capability_list_treats_null_tools_same_as_a_missing_snapshot(ctx):
    """Round-1 review finding #4: `_tools_snapshot.py`'s `on_session_start` hook
    now always writes `jones_tools.json` once `jones_gate` loaded (Issue #38's
    fail-closed startup gate only checks the file's existence), but writes
    `tools: null` when the tool list itself couldn't be computed (an unrelated
    Hermes-side failure) — `capability.list` must degrade exactly like the file
    never existed at all, not crash or report an empty/drifted tool set."""
    from jones_daemon.permissions import gate_config

    await run_in_db_thread(_insert_session, ctx.db, "s1", mode="task")
    hermes_home = gate_config.hermes_home_for(ctx.paths.user_root(), "s1")
    hermes_home.mkdir(parents=True)
    (hermes_home / "jones_tools.json").write_text(
        json.dumps({
            "session_id": "s1", "tools": None,
            "tools_unavailable_reason": "ImportError: model_tools",
            "mcp_servers": None,
        }),
        encoding="utf-8",
    )
    result = await run_in_db_thread(_build_result, ctx, "s1")
    assert result["actual_available"] is False
    assert result["drift"] == []


async def test_capability_list_reads_real_jones_tools_json_and_reports_real_drift(ctx):
    """The direction R-H1 keeps: a tool expected ENABLED (in the Agent's
    whitelist) that the worker never actually loaded at all."""
    from jones_daemon.permissions import gate_config

    await run_in_db_thread(_insert_session, ctx.db, "s1", mode="task")
    await run_in_db_thread(_set_agent_allowlist, ctx.db, DEFAULT_AGENT_ID, ["read_file"])
    hermes_home = gate_config.hermes_home_for(ctx.paths.user_root(), "s1")
    hermes_home.mkdir(parents=True)
    # `read_file` is whitelisted (expected enabled) but the snapshot shows the
    # worker never actually registered it — a real G21 anomaly.
    (hermes_home / "jones_tools.json").write_text(
        json.dumps({"session_id": "s1", "tools": ["terminal"], "mcp_servers": []}),
        encoding="utf-8",
    )
    result = await run_in_db_thread(_build_result, ctx, "s1")
    assert result["actual_available"] is True
    assert result["drift"] == ["read_file"]

    # `_build_result` runs on the DB thread and never touches `ctx.server`
    # itself — broadcasting the drift is the async RPC wrapper's job (see
    # `test_capability_list_rpc_wrapper_broadcasts_daemon_error_on_drift`).
    assert ctx.server.broadcasts == []
    assert ctx.server.broadcast_alls == []


async def test_capability_list_rpc_wrapper_broadcasts_daemon_error_on_drift(ctx):
    from jones_daemon.permissions import gate_config

    await run_in_db_thread(_insert_session, ctx.db, "s1", mode="task")
    await run_in_db_thread(_set_agent_allowlist, ctx.db, DEFAULT_AGENT_ID, ["read_file"])
    hermes_home = gate_config.hermes_home_for(ctx.paths.user_root(), "s1")
    hermes_home.mkdir(parents=True)
    (hermes_home / "jones_tools.json").write_text(
        json.dumps({"session_id": "s1", "tools": ["terminal"], "mcp_servers": []}),
        encoding="utf-8",
    )
    server = RpcServer.__new__(RpcServer)
    handlers: dict[str, Any] = {}
    server.register = lambda name, fn: handlers.__setitem__(name, fn)  # type: ignore[method-assign]
    register(server, ctx)
    result = await handlers["capability.list"]({"session_id": "s1"}, conn=None)
    assert "_confirmed_down_mcp_servers" not in result  # internal-only, popped before return
    # R-H3: `daemon.error{code, message, detail}` (00-foundation.md §4.2/§4.3),
    # via `broadcast_all` (daemon-wide), not the per-session `broadcast`.
    assert ctx.server.broadcasts == []
    assert ctx.server.broadcast_alls
    method, params = ctx.server.broadcast_alls[0]
    assert method == "daemon.error"
    assert params["code"] == 1009  # CAPABILITY_DRIFT
    assert params["detail"]["drift"] == ["read_file"]


async def test_capability_list_broadcasts_mcp_server_down_when_confirmed(ctx):
    """Controller ruling R-H4: a configured, enabled MCP server the worker's
    snapshot confirms (discovery complete) never registered must produce a
    real `daemon.error`, not just a hidden_reason on the transparency page."""
    from jones_daemon.permissions import gate_config

    ctx.config = _FakeConfig(mcp_servers=[{"name": "echo", "command": "python3"}])
    await run_in_db_thread(_insert_session, ctx.db, "s1", mode="auto")
    hermes_home = gate_config.hermes_home_for(ctx.paths.user_root(), "s1")
    hermes_home.mkdir(parents=True)
    (hermes_home / "jones_tools.json").write_text(
        json.dumps(
            {
                "session_id": "s1",
                "tools": ["read_file"],
                "mcp_servers": [],  # discovery ran and found nothing for "echo"
                "mcp_discovery_complete": True,
            }
        ),
        encoding="utf-8",
    )
    server = RpcServer.__new__(RpcServer)
    handlers: dict[str, Any] = {}
    server.register = lambda name, fn: handlers.__setitem__(name, fn)  # type: ignore[method-assign]
    register(server, ctx)
    await handlers["capability.list"]({"session_id": "s1"}, conn=None)
    down = [p for m, p in ctx.server.broadcast_alls if p["code"] == 1008]  # MCP_SERVER_DOWN
    assert down
    assert down[0]["detail"]["mcp_server"] == "echo"


async def test_capability_list_does_not_confirm_down_when_discovery_incomplete(ctx):
    """Review round-2 finding #3: a snapshot with an empty `mcp_servers` list
    but NO `mcp_discovery_complete` marker must not be treated as evidence a
    configured server is down — discovery may simply not have reported back
    yet (the probe-turn snapshot is a one-time write, discovery is async)."""
    from jones_daemon.permissions import gate_config

    ctx.config = _FakeConfig(mcp_servers=[{"name": "echo", "command": "python3"}])
    await run_in_db_thread(_insert_session, ctx.db, "s1", mode="auto")
    hermes_home = gate_config.hermes_home_for(ctx.paths.user_root(), "s1")
    hermes_home.mkdir(parents=True)
    (hermes_home / "jones_tools.json").write_text(
        json.dumps({"session_id": "s1", "tools": ["read_file"], "mcp_servers": []}),
        encoding="utf-8",
    )
    result = await run_in_db_thread(_build_result, ctx, "s1")
    placeholder = next(t for t in result["tools"] if t["source"] == "mcp")
    assert placeholder["hidden_reason"] == "not_in_allowlist"  # not "mcp_server_down"
    assert result["_confirmed_down_mcp_servers"] == []
