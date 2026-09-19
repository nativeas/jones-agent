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


def test_reconcile_does_not_flag_drift_when_hidden_mcp_server_loaded_anyway():
    """Controller ruling R-H1: "expected hidden but Hermes assembled it into
    the schema anyway" is the ORDINARY case, not a G21 violation — Jones has
    no config-level lever to drop a tool from the model's schema, only from
    what `_decide` will let a call through to run (see registry.py's module
    docstring). The registry still reports `enabled=False`/`actually_loaded=
    True` on the entry so the transparency page shows the true schema
    visibility; it just doesn't count toward `drift`."""
    expected = registry.expected_capabilities(
        mode="auto", tool_allowlist=[],  # unrestricted for builtins, but N15 still
        # hides MCP by default -> "echo" is expected disabled.
        mcp_servers=[registry.McpServerState(name="echo")],
    )
    # Full builtin schema too, or every one of the 36 `BUILTIN_TOOLS` names
    # would show up as "expected enabled but never loaded" drift — unrelated
    # noise for a test about the MCP-hidden-but-loaded direction specifically.
    result = registry.reconcile(expected, [*registry.BUILTIN_TOOLS, "mcp__echo__ping"])
    assert "mcp__echo__ping" not in result.drift
    assert result.drift == []
    entry = next(t for t in result.tools if t.name == "mcp__echo__ping")
    assert entry.enabled is False
    assert entry.actually_loaded is True


def test_reconcile_no_drift_for_default_config_full_builtin_schema():
    """Controller ruling R-H1's acceptance bar: a completely default session
    (unrestricted Agent, no MCP servers, no rules) where the worker actually
    assembled every `BUILTIN_TOOLS` name into the schema — the ordinary,
    everyday shape of `jones_tools.json` — must report an EMPTY drift. Before
    this round's fix, this exact input produced 0 drift only by accident (no
    allowlist narrowing); `test_capability_list_...` in
    `test_capabilities_methods.py` covers the narrowed-allowlist case that
    used to manufacture drift on every single call."""
    expected = registry.expected_capabilities(mode="auto", tool_allowlist=[])
    result = registry.reconcile(expected, list(registry.BUILTIN_TOOLS))
    assert result.drift == []


def test_reconcile_conditional_builtin_absence_is_not_drift():
    """A `CONDITIONAL_BUILTIN_TOOLS` name (e.g. `web_search`, gated on a
    configured search API key Jones doesn't set up by default) being expected
    enabled but never actually loaded is a known-explainable state (R-H1), not
    a G21 anomaly — it's still visible via `actually_loaded: false`."""
    expected = registry.expected_capabilities(mode="auto", tool_allowlist=[])
    actual = [n for n in registry.BUILTIN_TOOLS if n != "web_search"]
    result = registry.reconcile(expected, actual)
    assert "web_search" not in result.drift
    assert result.drift == []
    entry = next(t for t in result.tools if t.name == "web_search")
    assert entry.enabled is True
    assert entry.actually_loaded is False


def test_reconcile_still_flags_drift_when_a_core_builtin_never_loads():
    """The other direction R-H1 keeps: a tool with no environment dependency
    (not in `CONDITIONAL_BUILTIN_TOOLS`) that's expected enabled but never
    actually assembled is a real anomaly worth flagging."""
    expected = registry.expected_capabilities(mode="auto", tool_allowlist=[])
    actual = [n for n in registry.BUILTIN_TOOLS if n != "read_file"]
    result = registry.reconcile(expected, actual)
    assert result.drift == ["read_file"]


def test_reconcile_recognizes_tool_search_bridge_names_as_explainable():
    """Hermes's own Tool Search bridge (`tool_search`/`tool_describe`/
    `tool_call`) replaces every deferrable tool's real schema entry once any
    MCP server is configured (review round-2 finding #6). Even though
    `_tools_snapshot.py`'s real fix is to bypass that assembly
    (`skip_tool_search_assembly=True`), the registry must not manufacture
    `unknown_tool` drift for these three names if they ever do show up in a
    snapshot (R-H1's defense-in-depth)."""
    expected = registry.expected_capabilities(mode="task", tool_allowlist=["read_file"])
    result = registry.reconcile(
        expected, ["read_file", "tool_search", "tool_describe", "tool_call"]
    )
    assert result.drift == []
    bridge_names = registry._policy.TOOL_SEARCH_BRIDGE_NAMES
    bridge = {t.name: t for t in result.tools if t.name in bridge_names}
    assert set(bridge) == {"tool_search", "tool_describe", "tool_call"}
    assert all(e.enabled and e.actually_loaded and e.hidden_reason is None for e in bridge.values())


def test_reconcile_attributes_unmatched_mcp_shaped_name_by_prefix():
    expected = registry.expected_capabilities(mode="task", tool_allowlist=["read_file"])
    result = registry.reconcile(expected, ["read_file", "mcp__docs__search"])
    entry = next(t for t in result.tools if t.name == "mcp__docs__search")
    assert entry.source == "mcp"
    assert entry.mcp_server == "docs"
    assert entry.hidden_reason == "unknown_tool"
    assert "mcp__docs__search" in result.drift
