"""Tests for `permissions/gate_config.py` — the daemon-side jones_gate.json
builder/writer, including N13's tool-allowlist narrowing (Issue #11)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from jones_daemon.permissions import gate_config
from jones_daemon.store import apply_pending, connect

_NOW = "2026-09-19T00:00:00.000Z"


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    c = connect(tmp_path / "t.db")
    apply_pending(c)
    return c


def _insert_agent(conn: sqlite3.Connection, agent_id: str, tool_allowlist: list[str]) -> None:
    conn.execute(
        "INSERT INTO agents (id, project_id, name, persona, tone, principles, "
        "tool_allowlist_json, skills_json, model_pref_json, created_at, updated_at) "
        "VALUES (?, NULL, ?, NULL, NULL, NULL, ?, '[]', '{}', ?, ?)",
        (agent_id, agent_id, json.dumps(tool_allowlist), _NOW, _NOW),
    )
    conn.commit()


def _insert_session(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    agent_id: str,
    parent_id: str | None,
    mode: str = "task",
) -> None:
    conn.execute(
        "INSERT INTO sessions (id, project_id, agent_id, parent_id, is_main, mode, title, "
        "status, created_at, updated_at) "
        "VALUES (?, 'proj_default', ?, ?, 0, ?, 't', 'active', ?, ?)",
        (session_id, agent_id, parent_id, mode, _NOW, _NOW),
    )
    conn.commit()


def _session_row(conn: sqlite3.Connection, session_id: str) -> dict:
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row)


def test_unrestricted_agent_yields_empty_tool_allowlist_in_json(conn):
    _insert_agent(conn, "a1", [])
    _insert_session(conn, "s1", agent_id="a1", parent_id=None)
    config = gate_config.build(
        conn=conn, permissions_result={}, session=_session_row(conn, "s1"),
        user_root=Path("/tmp/jones_home"), project_path=None,
    )
    assert config["tool_allowlist"] == []
    assert config["mode"] == "task"


def test_own_agent_allowlist_is_used_with_no_parent(conn):
    _insert_agent(conn, "a1", ["shell", "browser"])
    _insert_session(conn, "s1", agent_id="a1", parent_id=None)
    config = gate_config.build(
        conn=conn, permissions_result={}, session=_session_row(conn, "s1"),
        user_root=Path("/tmp/jones_home"), project_path=None,
    )
    assert sorted(config["tool_allowlist"]) == ["browser", "shell"]


def test_child_allowlist_is_narrowed_by_parent_agent_N13(conn):
    _insert_agent(conn, "parent_agent", ["shell", "browser", "write_file"])
    _insert_agent(conn, "child_agent", ["shell", "browser"])
    _insert_session(conn, "parent", agent_id="parent_agent", parent_id=None)
    _insert_session(conn, "child", agent_id="child_agent", parent_id="parent")
    config = gate_config.build(
        conn=conn, permissions_result={}, session=_session_row(conn, "child"),
        user_root=Path("/tmp/jones_home"), project_path=None,
    )
    assert sorted(config["tool_allowlist"]) == ["browser", "shell"]


def test_child_allowlist_is_intersected_down_even_when_disjoint_from_parent_N13(conn):
    _insert_agent(conn, "parent_agent", ["shell"])
    _insert_agent(conn, "child_agent", ["browser"])
    _insert_session(conn, "parent", agent_id="parent_agent", parent_id=None)
    _insert_session(conn, "child", agent_id="child_agent", parent_id="parent")
    config = gate_config.build(
        conn=conn, permissions_result={}, session=_session_row(conn, "child"),
        user_root=Path("/tmp/jones_home"), project_path=None,
    )
    # Zero real overlap -> genuinely "no tools allowed", NOT the empty-list
    # ("unrestricted") shape (see gate_config._tool_allowlist_for_json).
    assert config["tool_allowlist"] != []
    assert "browser" not in config["tool_allowlist"]
    assert "shell" not in config["tool_allowlist"]


def test_unrestricted_parent_does_not_narrow_a_restricted_child(conn):
    _insert_agent(conn, "parent_agent", [])  # unrestricted
    _insert_agent(conn, "child_agent", ["shell"])
    _insert_session(conn, "parent", agent_id="parent_agent", parent_id=None)
    _insert_session(conn, "child", agent_id="child_agent", parent_id="parent")
    config = gate_config.build(
        conn=conn, permissions_result={}, session=_session_row(conn, "child"),
        user_root=Path("/tmp/jones_home"), project_path=None,
    )
    assert config["tool_allowlist"] == ["shell"]


def test_grandparent_narrows_too(conn):
    _insert_agent(conn, "gp_agent", ["shell"])
    _insert_agent(conn, "parent_agent", ["shell", "browser"])
    _insert_agent(conn, "child_agent", ["shell", "browser", "write_file"])
    _insert_session(conn, "gp", agent_id="gp_agent", parent_id=None)
    _insert_session(conn, "parent", agent_id="parent_agent", parent_id="gp")
    _insert_session(conn, "child", agent_id="child_agent", parent_id="parent")
    config = gate_config.build(
        conn=conn, permissions_result={}, session=_session_row(conn, "child"),
        user_root=Path("/tmp/jones_home"), project_path=None,
    )
    assert config["tool_allowlist"] == ["shell"]


def test_permissions_result_as_null_config_resolver_dict_is_handled(conn):
    _insert_agent(conn, "a1", [])
    _insert_session(conn, "s1", agent_id="a1", parent_id=None)
    # NullConfigResolver.permissions() returns {} (see context.py) — must not raise.
    config = gate_config.build(
        conn=conn, permissions_result={}, session=_session_row(conn, "s1"),
        user_root=Path("/tmp/jones_home"), project_path=None,
    )
    assert config["rules"] == []


def test_permissions_result_as_real_dataclass_shape_is_handled(conn):
    from jones_daemon.config.resolver import PermissionRule, Permissions

    _insert_agent(conn, "a1", [])
    _insert_session(conn, "s1", agent_id="a1", parent_id=None)
    perms = Permissions(rules=(PermissionRule(match="terminal", action="deny"),))
    config = gate_config.build(
        conn=conn, permissions_result=perms, session=_session_row(conn, "s1"),
        user_root=Path("/tmp/jones_home"), project_path=None,
    )
    assert config["rules"] == [{"match": "terminal", "action": "deny"}]


def test_write_round_trips_through_json(tmp_path, conn):
    _insert_agent(conn, "a1", ["shell"])
    _insert_session(conn, "s1", agent_id="a1", parent_id=None)
    config = gate_config.build(
        conn=conn, permissions_result={}, session=_session_row(conn, "s1"),
        user_root=tmp_path / "user_root", project_path=None,
    )
    hermes_home = tmp_path / "hermes_home"
    written = gate_config.write(hermes_home, config)
    assert written == gate_config.gate_config_path(hermes_home)
    assert json.loads(written.read_text(encoding="utf-8")) == config


def test_hermes_home_for_matches_workers_manager_formula(tmp_path):
    from jones_daemon.workers.manager import WorkerManager

    wm = WorkerManager(
        user_root=tmp_path,
        on_session_update=None,  # type: ignore[arg-type]
        on_request_permission=None,  # type: ignore[arg-type]
        on_worker_crash=None,  # type: ignore[arg-type]
    )
    assert gate_config.hermes_home_for(tmp_path, "sess1") == wm._hermes_home_for("sess1")
