"""`permissions.json` rule matching against one tool call — the part
01-w2-interfaces.md §4.1 explicitly deferred: "match 是...不透明标识...W2 不做
glob/正则重叠判定——裁决引擎是 W3 FR05 的范围，这里只做'两条规则是否管同一件事'的
key 相等比较". This module IS that engine (Issue #11 / 02-w3-interfaces.md §1.1).

A rule's `match` is either:
  - an exact tool name (matches every call to that tool), or
  - a command-prefix string, matched against a terminal call's `command` arg
    by comparing the leading shlex tokens of `match` against the leading
    tokens of `command` (so `git push` matches `git push --force origin`,
    but not `git pushxyz`).

Self-contained (stdlib only) — see `__init__.py`'s module docstring.
"""

from __future__ import annotations

import shlex
from typing import Any, Literal

Action = Literal["allow", "deny"]


def _command_text(tool_name: str, args: dict[str, Any]) -> str | None:
    if tool_name == "terminal":
        command = args.get("command")
        return command if isinstance(command, str) and command else None
    return None


def _is_command_prefix(match: str, command: str) -> bool:
    try:
        match_tokens = shlex.split(match, posix=True)
        command_tokens = shlex.split(command, posix=True)
    except ValueError:
        return False
    if not match_tokens:
        return False
    return command_tokens[: len(match_tokens)] == match_tokens


def _rule_matches(match: str, tool_name: str, args: dict[str, Any]) -> bool:
    if match == tool_name:
        return True
    command = _command_text(tool_name, args)
    if command is not None:
        return _is_command_prefix(match, command)
    return False


def decide(rules: list[dict[str, Any]], tool_name: str, args: dict[str, Any]) -> Action | None:
    """Return `"deny"`/`"allow"` if any rule matches this call, `None` if no
    rule applies. Deny takes precedence over allow when both somehow match
    (shouldn't happen — `config/resolver.py::_merge_permission_rules` keeps
    at most one action per `match` key — but a `match` collision between an
    exact tool-name rule and a command-prefix rule is possible, and deny-
    wins is the fail-closed choice for that edge case)."""
    verdict: Action | None = None
    for rule in rules:
        match, action = rule.get("match"), rule.get("action")
        if not isinstance(match, str) or action not in ("allow", "deny"):
            continue
        if _rule_matches(match, tool_name, args):
            if action == "deny":
                return "deny"
            verdict = "allow"
    return verdict
