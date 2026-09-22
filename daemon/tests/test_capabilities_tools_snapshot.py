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

    def _get_tool_definitions(
        *, enabled_toolsets, quiet_mode=True, skip_tool_search_assembly=False
    ):
        state["last_enabled_toolsets"] = enabled_toolsets
        state["last_skip_tool_search_assembly"] = skip_tool_search_assembly
        return state["tools"]

    model_tools_mod.get_tool_definitions = _get_tool_definitions

    mcp_startup_mod = types.ModuleType("hermes_cli.mcp_startup")
    mcp_startup_mod.mcp_discovery_in_flight = lambda: state.get("discovery_in_flight", False)
    mcp_startup_mod.join_mcp_discovery = lambda timeout=None: not state.get(
        "discovery_in_flight", False
    )
    hermes_cli_pkg = types.ModuleType("hermes_cli")
    hermes_cli_pkg.mcp_startup = mcp_startup_mod

    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.mcp_tool_discovery", mcp_discovery_mod)
    monkeypatch.setitem(sys.modules, "model_tools", model_tools_mod)
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.mcp_startup", mcp_startup_mod)
    return state


def test_on_session_start_writes_jones_tools_json(tmp_path, monkeypatch, _fake_hermes_modules):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _tools_snapshot.on_session_start(session_id="s1")

    written = json.loads((tmp_path / "jones_tools.json").read_text(encoding="utf-8"))
    assert written["session_id"] == "s1"
    assert sorted(written["tools"]) == ["mcp__echo__ping", "read_file"]
    assert written["mcp_servers"] == ["echo"]
    assert written["mcp_discovery_complete"] is True
    assert isinstance(written["written_at"], float)


def test_on_session_start_requests_unfolded_schema_via_skip_tool_search_assembly(
    tmp_path, monkeypatch, _fake_hermes_modules
):
    """Review round-2 finding #6: without this, Hermes's own Tool Search
    bridge folds every MCP tool's real name away behind `tool_search`/
    `tool_describe`/`tool_call` the moment any MCP server is configured —
    defeating this hook's entire "what got REALLY assembled" purpose."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _tools_snapshot.on_session_start(session_id="s1")
    assert _fake_hermes_modules["last_skip_tool_search_assembly"] is True


def test_on_session_start_records_discovery_incomplete(tmp_path, monkeypatch, _fake_hermes_modules):
    """Review round-2 finding #3: a slow MCP server still mid-handshake when
    this hook fires must be recorded as "discovery incomplete", not silently
    indistinguishable from "confirmed nothing there"."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _fake_hermes_modules["discovery_in_flight"] = True
    _tools_snapshot.on_session_start(session_id="s1")
    written = json.loads((tmp_path / "jones_tools.json").read_text(encoding="utf-8"))
    assert written["mcp_discovery_complete"] is False


def test_on_session_start_records_discovery_incomplete_when_unimportable(
    tmp_path, monkeypatch, _fake_hermes_modules
):
    """`hermes_cli.mcp_startup` being unimportable must never be read as
    "discovery complete" — that's the one claim `McpServerState.reachable`
    requires real evidence for (`capabilities/registry.py`)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setitem(sys.modules, "hermes_cli.mcp_startup", None)
    monkeypatch.setitem(sys.modules, "hermes_cli", None)
    _tools_snapshot.on_session_start(session_id="s1")
    written = json.loads((tmp_path / "jones_tools.json").read_text(encoding="utf-8"))
    assert written["mcp_discovery_complete"] is False


def test_on_session_start_joins_discovery_before_reading_mcp_names(
    tmp_path, monkeypatch, _fake_hermes_modules
):
    """Review round-2 finding #2: the original code read `mcp_names`/computed
    `tools` BEFORE joining discovery, and only called `_mcp_discovery_complete`
    (which itself joins) afterwards — so a server that finishes registering
    DURING that 1s join was invisible in `tools`/`mcp_servers` while
    `mcp_discovery_complete` still came back `True`, a snapshot that looks
    complete but isn't. Simulates exactly that: `mcp_servers` is empty and
    discovery is in flight until `join_mcp_discovery` is actually called, at
    which point the "echo" server (and its tool) becomes visible — the fixed
    hook must join FIRST, so the one `mcp_names` read it makes already sees
    "echo", and `tools`/`mcp_servers`/`mcp_discovery_complete` all agree."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _fake_hermes_modules["mcp_servers"] = []
    _fake_hermes_modules["discovery_in_flight"] = True

    def _join(timeout=None):
        # The join is what makes the late-arriving server actually show up —
        # mirrors a real slow MCP handshake completing inside the wait.
        _fake_hermes_modules["discovery_in_flight"] = False
        _fake_hermes_modules["mcp_servers"] = ["echo"]
        return True

    import sys as _sys

    _sys.modules["hermes_cli.mcp_startup"].join_mcp_discovery = _join

    _tools_snapshot.on_session_start(session_id="s1")
    written = json.loads((tmp_path / "jones_tools.json").read_text(encoding="utf-8"))
    assert written["mcp_discovery_complete"] is True
    # Must NOT be the pre-join empty read — the whole point of joining first.
    assert written["mcp_servers"] == ["echo"]
    assert "mcp__echo__ping" in written["tools"]


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


def test_on_session_start_still_writes_the_file_when_model_tools_is_unimportable(
    tmp_path, monkeypatch
):
    """Round-1 review finding #4: `workers/manager.py::_wait_for_tools_snapshot`
    (Issue #38's PRIMARY, fail-closed startup gate) only checks this file's
    EXISTENCE as proof `jones_gate` loaded — an unrelated Hermes-side failure
    computing the tool list must not look identical to the plugin never having
    loaded at all. The file must still appear, with `tools: null` and a reason,
    not be skipped."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setitem(sys.modules, "model_tools", None)  # forces ImportError on import
    _tools_snapshot.on_session_start(session_id="s1")  # must not raise
    written = json.loads((tmp_path / "jones_tools.json").read_text(encoding="utf-8"))
    assert written["session_id"] == "s1"
    assert written["tools"] is None
    assert written["tools_unavailable_reason"]


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
