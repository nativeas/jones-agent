"""End-to-end assertion (controller ruling R-H2) that the transparency page's
`enabled` field and the REAL rule gate's verdict never disagree — the exact
failure mode round-1 review findings #1/#7 reported: `capabilities/
registry.py` and `kernel/plugin/jones_gate/__init__.py::_decide` used to
implement N15/`mcp:<server>` allowlist matching separately, with a docstring
falsely claiming they were kept in sync by hand. They now both call
`kernel.plugin.jones_gate._policy.tool_allowed` — this file drives the REAL
`_on_pre_tool_call` hook (not a re-implementation of it, same honest-testing
rationale `test_gates_rule_gate.py`'s module docstring gives) against the same
`tool_allowlist` the registry computes `expected_capabilities` from, for every
case §2/N15 cares about, and asserts the two answers match.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jones_daemon.capabilities import registry
from jones_daemon.kernel.plugin.jones_gate import _on_pre_tool_call


def _write_config(hermes_home: Path, *, tool_allowlist: list[str]) -> None:
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "jones_gate.json").write_text(
        json.dumps(
            {
                "mode": "auto",
                "user_root": str(hermes_home / "not_the_real_user_root"),
                "project_permissions_path": None,
                "cwd": None,
                "rules": [],
                "tool_allowlist": tool_allowlist,
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    from jones_daemon.kernel.plugin.jones_gate import _config

    _config._cache.clear()
    return home


def _gate_allows(tool_name: str) -> bool:
    """`True` iff the real gate's verdict for this tool is NOT an immediate
    block — i.e. either `approve` (escalates to a human, but the CALL is not
    rejected outright by the rule gate itself) or a zero-IPC pass-through.
    `registry`'s `enabled` means the same thing: "not hidden by the rule
    gate's own allowlist/rule decision" — chat mode and hard-deny are outside
    N15's scope and not exercised here (see `test_gates_rule_gate.py` for
    those)."""
    result = _on_pre_tool_call(tool_name=tool_name, args={}, tool_call_id="tc1")
    if result is None:
        return True
    return result["action"] != "block"


@pytest.mark.parametrize(
    ("tool_allowlist", "tool_name", "expect_enabled"),
    [
        # Builtin, unrestricted allowlist: allowed on both sides.
        ([], "read_file", True),
        # Builtin, narrowed allowlist excluding it: blocked on both sides.
        (["read_file"], "terminal", False),
        # MCP tool, unrestricted allowlist: N15 — hidden/blocked on both sides
        # even though the allowlist is otherwise "unrestricted" for builtins.
        ([], "mcp__echo__ping", False),
        # MCP tool named exactly in the allowlist: enabled on both sides.
        (["mcp__echo__ping"], "mcp__echo__ping", True),
        # MCP tool enabled via the whole-server `mcp:<server>` placeholder —
        # this is the exact scenario round-1 review finding #7 reported as
        # broken (transparency page said enabled, gate blocked every call).
        (["mcp:echo"], "mcp__echo__ping", True),
        # `mcp:<server>` placeholder for a DIFFERENT server doesn't leak.
        (["mcp:other"], "mcp__echo__ping", False),
    ],
)
def test_registry_enabled_matches_real_gate_verdict(
    _hermes_home, tool_allowlist, tool_name, expect_enabled
):
    _write_config(_hermes_home, tool_allowlist=tool_allowlist)

    gate_allows = _gate_allows(tool_name)
    assert gate_allows is expect_enabled, "real gate disagreed with the test's own expectation"

    expected = registry.expected_capabilities(
        mode="auto", tool_allowlist=tool_allowlist,
        mcp_servers=[registry.McpServerState(name="echo")],
    )
    # Real per-tool MCP schema isn't known ahead of a live worker (see
    # `McpServerState.tools`'s docstring) — reconcile against a snapshot that
    # includes the tool by its real name, the same way `capability.list`
    # would once a worker has actually reported it, so the placeholder
    # expands to a concrete `mcp__echo__ping` entry to compare against.
    result = registry.reconcile(
        expected, ["read_file", "terminal", "mcp__echo__ping"], tool_allowlist=tool_allowlist
    )
    entry = next(t for t in result.tools if t.name == tool_name)
    assert entry.enabled is gate_allows is expect_enabled
