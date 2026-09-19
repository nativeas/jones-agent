"""Tests for `capabilities/registry.py` (Issue #17 §2, #19 daemon 侧; G21, N15)."""

from __future__ import annotations

from jones_daemon.capabilities import registry


def test_chat_mode_hides_every_builtin_tool():
    entries = registry.expected_capabilities(mode="chat")
    assert entries  # BUILTIN_TOOLS is non-empty
    assert all(e.enabled is False and e.hidden_reason == "mode_chat" for e in entries)
    assert {e.name for e in entries} == set(registry.BUILTIN_TOOLS)


def test_unrestricted_allowlist_enables_every_builtin_but_no_mcp_or_skill():
    """N15: an empty (unrestricted) tool_allowlist means "every builtin is a
    candidate" but must NOT auto-enable third-party MCP/Skill tools."""
    entries = registry.expected_capabilities(
        mode="auto",
        tool_allowlist=[],
        mcp_servers=[registry.McpServerState(name="echo", tools=["ping"])],
        skill_tools={"research": ["deep_research"]},
    )
    by_name = {e.name: e for e in entries}
    assert all(e.enabled for e in entries if e.source == "builtin")
    assert by_name["ping"].source == "mcp"
    assert by_name["ping"].enabled is False
    assert by_name["ping"].hidden_reason == "not_in_allowlist"
    assert by_name["deep_research"].source == "skill"
    assert by_name["deep_research"].enabled is False
    assert by_name["deep_research"].hidden_reason == "not_in_allowlist"


def test_explicit_allowlist_entry_enables_third_party_tool():
    entries = registry.expected_capabilities(
        mode="auto",
        tool_allowlist=["ping"],
        mcp_servers=[registry.McpServerState(name="echo", tools=["ping"])],
    )
    entry = next(e for e in entries if e.name == "ping")
    assert entry.enabled is True
    assert entry.hidden_reason is None


def test_narrowed_allowlist_hides_unlisted_builtin():
    entries = registry.expected_capabilities(mode="task", tool_allowlist=["read_file"])
    by_name = {e.name: e for e in entries}
    assert by_name["read_file"].enabled is True
    assert by_name["terminal"].enabled is False
    assert by_name["terminal"].hidden_reason == "not_in_allowlist"


def test_rule_gate_deny_hides_a_whitelisted_builtin():
    entries = registry.expected_capabilities(
        mode="auto", tool_allowlist=[], rules=[{"match": "terminal", "action": "deny"}]
    )
    entry = next(e for e in entries if e.name == "terminal")
    assert entry.enabled is False
    assert entry.hidden_reason == "denied_by_rule"


def test_mcp_server_marked_unreachable_shows_placeholder_down():
    entries = registry.expected_capabilities(
        mode="auto", tool_allowlist=["mcp:echo"],
        mcp_servers=[registry.McpServerState(name="echo", reachable=False)],
    )
    assert len(entries) == len(registry.BUILTIN_TOOLS) + 1
    placeholder = next(e for e in entries if e.source == "mcp")
    assert placeholder.name == "mcp:echo"
    assert placeholder.enabled is False
    assert placeholder.hidden_reason == "mcp_server_down"


def test_config_disabled_mcp_server_is_reported_down():
    entries = registry.expected_capabilities(
        mode="auto", mcp_servers=[registry.McpServerState(name="echo", enabled=False)],
    )
    placeholder = next(e for e in entries if e.source == "mcp")
    assert placeholder.hidden_reason == "mcp_server_down"


def test_reconcile_actual_available_false_when_snapshot_missing():
    expected = registry.expected_capabilities(mode="task", tool_allowlist=["read_file"])
    result = registry.reconcile(expected, None)
    assert result.actual_available is False
    assert result.drift == []
    assert len(result.tools) == len(expected)


def test_reconcile_no_drift_when_actual_matches_expected():
    expected = registry.expected_capabilities(mode="task", tool_allowlist=["read_file"])
    result = registry.reconcile(expected, ["read_file"])
    assert result.actual_available is True
    assert result.drift == []
    loaded = {t.name: t.actually_loaded for t in result.tools}
    assert loaded["read_file"] is True
    assert loaded["terminal"] is False


def test_reconcile_flags_drift_when_expected_enabled_tool_never_loaded():
    expected = registry.expected_capabilities(mode="task", tool_allowlist=["read_file"])
    result = registry.reconcile(expected, [])
    assert "read_file" in result.drift


def test_reconcile_flags_drift_for_unexpected_actually_loaded_tool():
    expected = registry.expected_capabilities(mode="task", tool_allowlist=["read_file"])
    result = registry.reconcile(expected, ["read_file", "some_future_tool"])
    assert "some_future_tool" in result.drift
    extra = next(t for t in result.tools if t.name == "some_future_tool")
    assert extra.actually_loaded is True
    assert extra.hidden_reason == "unknown_tool"


def test_reconcile_expands_mcp_placeholder_into_real_tool_names_when_enabled():
    expected = registry.expected_capabilities(
        mode="auto", tool_allowlist=["mcp:echo"],
        mcp_servers=[registry.McpServerState(name="echo")],
    )
    result = registry.reconcile(expected, ["mcp__echo__ping", "mcp__echo__pong"])
    assert result.drift == []
    names = {t.name for t in result.tools if t.source == "mcp"}
    assert names == {"mcp__echo__ping", "mcp__echo__pong"}
    assert all(t.actually_loaded for t in result.tools if t.source == "mcp")


def test_reconcile_flags_drift_when_hidden_mcp_server_loaded_anyway():
    """A G21 violation: the registry expected this server hidden (not in the
    allowlist) but the worker actually registered its tools."""
    expected = registry.expected_capabilities(
        mode="auto", tool_allowlist=[],  # unrestricted for builtins, but N15 still
        # hides MCP by default -> "echo" is expected disabled.
        mcp_servers=[registry.McpServerState(name="echo")],
    )
    result = registry.reconcile(expected, ["mcp__echo__ping"])
    assert "mcp__echo__ping" in result.drift
    entry = next(t for t in result.tools if t.name == "mcp__echo__ping")
    assert entry.enabled is False
    assert entry.actually_loaded is True


def test_reconcile_attributes_unmatched_mcp_shaped_name_by_prefix():
    expected = registry.expected_capabilities(mode="task", tool_allowlist=["read_file"])
    result = registry.reconcile(expected, ["read_file", "mcp__docs__search"])
    entry = next(t for t in result.tools if t.name == "mcp__docs__search")
    assert entry.source == "mcp"
    assert entry.mcp_server == "docs"
    assert entry.hidden_reason == "unknown_tool"
    assert "mcp__docs__search" in result.drift
