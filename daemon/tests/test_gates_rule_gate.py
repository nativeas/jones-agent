"""Integration-within-one-process tests for the rule gate's full decision
tree, `kernel/plugin/jones_gate/_on_pre_tool_call` (Issue #11 G04/G05/G06/N01/
N12). Writes a real `jones_gate.json` to a temp `HERMES_HOME` and calls the
hook function directly — this is the honest way to test it: the hook itself
never talks to the daemon (see its module docstring), so there is nothing
`fake_acp_agent.py`/`SessionService` would add here."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from jones_daemon.kernel.plugin.jones_gate import (
    PROBE_TOOL_NAME,
    RULE_GATE_BLOCK_PREFIX,
    _on_pre_tool_call,
)


def _write_config(hermes_home: Path, **overrides) -> None:
    base = {
        "mode": "task",
        "user_root": str(hermes_home / "not_the_real_user_root"),
        "project_permissions_path": None,
        "cwd": None,
        "rules": [],
        "tool_allowlist": [],
    }
    base.update(overrides)
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "jones_gate.json").write_text(json.dumps(base), encoding="utf-8")


@pytest.fixture(autouse=True)
def _hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    # `_config.py` caches by (path, mtime) at module scope — a stale cache
    # entry from module import time (or a previous test importing this
    # module and calling load() against a different tmp_path) must not leak
    # across tests using a different HERMES_HOME.
    from jones_daemon.kernel.plugin.jones_gate import _config

    _config._cache.clear()
    return home


def test_probe_tool_is_always_blocked_even_with_no_config_file():
    result = _on_pre_tool_call(tool_name=PROBE_TOOL_NAME)
    assert result["action"] == "block"


def test_missing_config_fails_closed_for_a_real_tool():
    result = _on_pre_tool_call(tool_name="terminal", args={"command": "ls"})
    assert result is not None and result["action"] == "block"
    assert result["message"].startswith(RULE_GATE_BLOCK_PREFIX)


def test_task_mode_undecided_tool_escalates_to_approve(_hermes_home):
    _write_config(_hermes_home, mode="task")
    result = _on_pre_tool_call(tool_name="terminal", args={"command": "ls -la"}, tool_call_id="tc1")
    assert result["action"] == "approve"
    assert "rule_key" in result


def test_chat_mode_blocks_every_tool_call_N12(_hermes_home):
    _write_config(_hermes_home, mode="chat")
    for tool_name, args in (
        ("terminal", {"command": "ls"}),
        ("read_file", {"path": "/tmp/x"}),
        ("write_file", {"path": "/tmp/x", "content": "hi"}),
    ):
        result = _on_pre_tool_call(tool_name=tool_name, args=args)
        assert result is not None and result["action"] == "block", tool_name
        assert "N12" in result["message"] or "chat mode" in result["message"]


def test_chat_mode_still_blocks_even_with_a_matching_allow_rule(_hermes_home):
    # PRD 9.1's mode boundary is absolute: no permissions.json rule can carve
    # a hole in chat mode.
    _write_config(_hermes_home, mode="chat", rules=[{"match": "terminal", "action": "allow"}])
    result = _on_pre_tool_call(tool_name="terminal", args={"command": "ls"})
    assert result["action"] == "block"


def test_permissions_json_deny_rule_blocks_regardless_of_mode(_hermes_home):
    for mode in ("task", "auto"):
        _write_config(_hermes_home, mode=mode, rules=[{"match": "terminal", "action": "deny"}])
        result = _on_pre_tool_call(tool_name="terminal", args={"command": "ls"})
        assert result["action"] == "block", mode


def test_permissions_json_allow_rule_passes_through_with_zero_ipc(_hermes_home):
    _write_config(_hermes_home, mode="task", rules=[{"match": "terminal", "action": "allow"}])
    result = _on_pre_tool_call(tool_name="terminal", args={"command": "ls"})
    assert result is None  # direct allow: no directive at all


def test_command_prefix_rule_matches_only_the_named_prefix(_hermes_home):
    _write_config(
        _hermes_home, mode="task", rules=[{"match": "git status", "action": "allow"}]
    )
    allowed = _on_pre_tool_call(tool_name="terminal", args={"command": "git status --short"})
    assert allowed is None
    still_gated = _on_pre_tool_call(tool_name="terminal", args={"command": "git push --force"})
    assert still_gated is not None  # not matched by the "git status" rule; falls through
    # ...and since "git push --force" has no explicit ref, it's ALSO hard-denied
    assert still_gated["action"] == "block"


def test_hard_deny_wins_even_with_an_allow_rule_for_the_same_tool(_hermes_home):
    _write_config(_hermes_home, mode="auto", rules=[{"match": "terminal", "action": "allow"}])
    result = _on_pre_tool_call(tool_name="terminal", args={"command": "rm -rf /Users/alice"})
    assert result["action"] == "block"
    assert result["message"].startswith(RULE_GATE_BLOCK_PREFIX)


def test_hard_deny_applies_in_every_mode_G05(_hermes_home):
    for mode in ("chat", "task", "auto"):
        _write_config(_hermes_home, mode=mode)
        result = _on_pre_tool_call(tool_name="terminal", args={"command": "rm -rf /Users/alice"})
        assert result["action"] == "block", mode


def test_agent_tool_whitelist_blocks_tools_outside_it(_hermes_home):
    _write_config(_hermes_home, mode="task", tool_allowlist=["read_file"])
    blocked = _on_pre_tool_call(tool_name="terminal", args={"command": "ls"})
    assert blocked["action"] == "block"
    allowed_through = _on_pre_tool_call(tool_name="read_file", args={"path": "/tmp/x"})
    # read_file is in _READ_ONLY set for the review gate but the rule gate
    # itself has no opinion beyond the whitelist -> escalates for the human
    # gate to see (review gate would mark it low-risk once it reaches
    # `_on_request_permission`, see tests/test_sessions_service.py's
    # gate-related coverage).
    assert allowed_through["action"] == "approve"


def test_write_file_defers_to_edit_approval_path_instead_of_escalating(_hermes_home):
    _write_config(_hermes_home, mode="task")
    result = _on_pre_tool_call(tool_name="write_file", args={"path": "/tmp/x", "content": "hi"})
    assert result is None
    result = _on_pre_tool_call(
        tool_name="patch", args={"path": "/tmp/x", "old_string": "a", "new_string": "b"}
    )
    assert result is None


def test_write_file_to_protected_jones_path_is_hard_denied(_hermes_home):
    _write_config(_hermes_home, mode="auto", user_root=str(_hermes_home / "jones_user_root"))
    protected = str(_hermes_home / "jones_user_root" / "config" / "permissions.json")
    result = _on_pre_tool_call(tool_name="write_file", args={"path": protected, "content": "{}"})
    assert result is not None and result["action"] == "block"


def test_rule_key_is_unique_per_call_not_just_the_tool_name(_hermes_home):
    _write_config(_hermes_home, mode="task")
    first = _on_pre_tool_call(tool_name="terminal", args={"command": "ls"}, tool_call_id="a")
    second = _on_pre_tool_call(tool_name="terminal", args={"command": "ls"}, tool_call_id="b")
    assert first["rule_key"] != second["rule_key"]


def test_malformed_config_file_fails_closed(_hermes_home):
    _hermes_home.mkdir(parents=True, exist_ok=True)
    (_hermes_home / "jones_gate.json").write_text("{not valid json", encoding="utf-8")
    result = _on_pre_tool_call(tool_name="terminal", args={"command": "ls"})
    assert result["action"] == "block"


def test_degraded_rules_do_not_allow_a_direct_bypass(_hermes_home):
    # An `allow` rule can't be trusted for a zero-IPC passthrough if the
    # permissions.json merge that produced it might be missing a `deny`
    # (config/resolver.py::Permissions.degraded) — must still escalate.
    _write_config(
        _hermes_home, mode="task", rules=[{"match": "terminal", "action": "allow"}],
        rules_degraded=True,
    )
    result = _on_pre_tool_call(tool_name="terminal", args={"command": "ls"})
    assert result is not None and result["action"] == "approve"


def test_config_is_reread_after_mtime_changes(_hermes_home):
    _write_config(_hermes_home, mode="chat")
    blocked = _on_pre_tool_call(tool_name="terminal", args={"command": "ls"})
    assert blocked["action"] == "block"
    # Force a distinct mtime (some filesystems have 1s mtime granularity).
    os.utime(_hermes_home / "jones_gate.json", (0, 1_000_000))
    _write_config(_hermes_home, mode="task", rules=[{"match": "terminal", "action": "allow"}])
    os.utime(_hermes_home / "jones_gate.json", None)
    allowed = _on_pre_tool_call(tool_name="terminal", args={"command": "ls"})
    assert allowed is None
