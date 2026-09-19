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

    async def broadcast(self, session_id: str, method: str, params: Any) -> None:
        self.broadcasts.append((session_id, method, params))


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


async def test_capability_list_reads_real_jones_tools_json_and_reports_drift(ctx):
    from jones_daemon.permissions import gate_config

    await run_in_db_thread(_insert_session, ctx.db, "s1", mode="task")
    await run_in_db_thread(_set_agent_allowlist, ctx.db, DEFAULT_AGENT_ID, ["read_file"])
    hermes_home = gate_config.hermes_home_for(ctx.paths.user_root(), "s1")
    hermes_home.mkdir(parents=True)
    # The worker actually loaded `terminal` too, even though the Agent's
    # whitelist doesn't include it — a real G21 drift.
    (hermes_home / "jones_tools.json").write_text(
        json.dumps({"session_id": "s1", "tools": ["read_file", "terminal"], "mcp_servers": []}),
        encoding="utf-8",
    )
    result = await run_in_db_thread(_build_result, ctx, "s1")
    assert result["actual_available"] is True
    assert "terminal" in result["drift"]
    by_name = {t["name"]: t for t in result["tools"]}
    assert by_name["read_file"]["actually_loaded"] is True
    assert by_name["terminal"]["actually_loaded"] is True
    assert by_name["terminal"]["enabled"] is False

    # `_build_result` runs on the DB thread and never touches `ctx.server`
    # itself — broadcasting the drift is the async RPC wrapper's job (see
    # `test_capability_list_rpc_wrapper_broadcasts_daemon_error_on_drift`).
    assert ctx.server.broadcasts == []


async def test_capability_list_rpc_wrapper_broadcasts_daemon_error_on_drift(ctx):
    from jones_daemon.permissions import gate_config

    await run_in_db_thread(_insert_session, ctx.db, "s1", mode="task")
    await run_in_db_thread(_set_agent_allowlist, ctx.db, DEFAULT_AGENT_ID, ["read_file"])
    hermes_home = gate_config.hermes_home_for(ctx.paths.user_root(), "s1")
    hermes_home.mkdir(parents=True)
    (hermes_home / "jones_tools.json").write_text(
        json.dumps({"session_id": "s1", "tools": ["read_file", "terminal"], "mcp_servers": []}),
        encoding="utf-8",
    )
    server = RpcServer.__new__(RpcServer)
    handlers: dict[str, Any] = {}
    server.register = lambda name, fn: handlers.__setitem__(name, fn)  # type: ignore[method-assign]
    register(server, ctx)
    await handlers["capability.list"]({"session_id": "s1"}, conn=None)
    assert ctx.server.broadcasts
    sid, method, params = ctx.server.broadcasts[0]
    assert method == "daemon.error"
    assert params["reason"] == "capability_drift"
    assert "terminal" in params["drift"]
