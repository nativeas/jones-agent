"""Tests for `kernel/plugin/jones_gate/_tools_snapshot.py`'s `on_session_start`
hook (Issue #17 §2, #19 daemon 侧) and `jones_gate.register()` wiring it up.

The real Hermes packages (`model_tools`, `tools.mcp_tool_discovery`) aren't a
dependency of the daemon's own venv (docs/DEV.md — only `uv sync --group
worker` pulls in a real `hermes-agent` checkout) — these tests inject minimal
fake modules into `sys.modules` rather than skip, so the hook's own logic (not
just "did it not crash") is actually exercised without `JONES_E2E`.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from jones_daemon.kernel.plugin import jones_gate
from jones_daemon.kernel.plugin.jones_gate import _tools_snapshot


@pytest.fixture
def _fake_hermes_modules(monkeypatch):
    """Installs `model_tools` and `tools.mcp_tool_discovery` fakes; yields a
    dict the test can mutate to control what they report."""
    state = {"mcp_servers": ["echo"], "tools": [{"function": {"name": "read_file"}},
                                                 {"function": {"name": "mcp__echo__ping"}}]}

    mcp_discovery_mod = types.ModuleType("tools.mcp_tool_discovery")
    mcp_discovery_mod.get_registered_mcp_server_names = lambda: set(state["mcp_servers"])

    tools_pkg = types.ModuleType("tools")
    tools_pkg.mcp_tool_discovery = mcp_discovery_mod

    model_tools_mod = types.ModuleType("model_tools")

    def _get_tool_definitions(*, enabled_toolsets, quiet_mode=True):
        state["last_enabled_toolsets"] = enabled_toolsets
        return state["tools"]

    model_tools_mod.get_tool_definitions = _get_tool_definitions

    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.mcp_tool_discovery", mcp_discovery_mod)
    monkeypatch.setitem(sys.modules, "model_tools", model_tools_mod)
    return state


def test_on_session_start_writes_jones_tools_json(tmp_path, monkeypatch, _fake_hermes_modules):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _tools_snapshot.on_session_start(session_id="s1")

    written = json.loads((tmp_path / "jones_tools.json").read_text(encoding="utf-8"))
    assert written["session_id"] == "s1"
    assert sorted(written["tools"]) == ["mcp__echo__ping", "read_file"]
    assert written["mcp_servers"] == ["echo"]
    assert isinstance(written["written_at"], float)


def test_on_session_start_expands_enabled_toolsets_with_connected_mcp_servers(
    tmp_path, monkeypatch, _fake_hermes_modules
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _fake_hermes_modules["mcp_servers"] = ["echo", "docs"]
    _tools_snapshot.on_session_start(session_id="s1")
    assert _fake_hermes_modules["last_enabled_toolsets"] == [
        "hermes-acp", "mcp-docs", "mcp-echo",
    ]


def test_on_session_start_is_a_noop_without_hermes_home(
    monkeypatch, tmp_path, _fake_hermes_modules
):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    _tools_snapshot.on_session_start(session_id="s1")  # must not raise
    assert not (tmp_path / "jones_tools.json").exists()


def test_on_session_start_is_a_noop_when_model_tools_is_unimportable(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setitem(sys.modules, "model_tools", None)  # forces ImportError on import
    _tools_snapshot.on_session_start(session_id="s1")  # must not raise
    assert not (tmp_path / "jones_tools.json").exists()


def test_on_session_start_never_raises_on_unexpected_kwargs(
    tmp_path, monkeypatch, _fake_hermes_modules
):
    """The real hook fires with `session_id`, `model`, `platform` — extra kwargs
    (or a caller passing none at all) must never crash it (it's invoked from
    inside Hermes's own hook dispatch)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _tools_snapshot.on_session_start(model="claude", platform="acp", some_future_kwarg=1)
    written = json.loads((tmp_path / "jones_tools.json").read_text(encoding="utf-8"))
    assert written["session_id"] == ""


def test_register_wires_both_pre_tool_call_and_on_session_start_hooks():
    registered: dict[str, object] = {}

    class _FakeCtx:
        def register_hook(self, name, fn):
            registered[name] = fn

        def register_tool(self, **kwargs):
            pass

    jones_gate.register(_FakeCtx())
    assert registered["pre_tool_call"] is jones_gate._on_pre_tool_call
    assert registered["on_session_start"] is _tools_snapshot.on_session_start
