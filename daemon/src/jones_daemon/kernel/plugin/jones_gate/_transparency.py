"""`transparency(command)` — the round-5 controller ruling's R5 invariant
(docs/design/02-w3-interfaces.md §1.4, 2026-09-19, final round, not
overturnable): **"不可静态分析 → 用户闸"**.

## Why this module exists (the premise behind R5)

Rounds 1-4 each tried to make the rule gate's ALLOW fast path and the review
gate's risk classifier *understand* shell syntax well enough to prove a
command safe — a flat token-stream scan (round 4), then progressively wider
attempts to unwrap wrappers, substitutions and redirections. Round 4's own
adversarial re-review still found three more ways to hide a real command from
that understanding: `rm -rf` sitting inside a **double-quoted** `$(...)`
(the tokenizer correctly keeps quoted content as one token — exactly right
for an ordinary quoted argument, exactly wrong for text that's actually going
to be re-parsed as shell syntax once substituted), bash's `$'...'` ANSI-C
quoting prefix (a real shell would run the word it quotes as a program name;
this codebase's simple `'...'`/`"..."`-only quote tracker doesn't know that
form exists and folds the leading `$` into the token instead), and a bare
redirection (`>`/`>>`) writing into a protected path under a *blanket*
`{"match":"terminal","action":"allow"}` rule (a blanket rule's `match` isn't
compared against the command text at all, and `_rules.py`'s old `compound`
marker set didn't include `<`/`>`).

Every one of those is the SAME premise failure repeating: trying to
enumerate every shell construct that could hide something, and finding a
gap. R5 changes the premise instead of finding a 4th gap: **stop trying to
prove a command is safe. Classify whether it's provable AT ALL first** —
`plain` (nothing here defeats simple, literal reasoning about it) or
`opaque` (it contains ANY construct — quoting, substitution, redirection, an
operator, or an indirect-execution program name — that could make its real
effect different from what its literal text suggests). An `opaque` command
is never treated as more benign than `high` risk and never takes the rule
gate's zero-IPC allow path, regardless of whether this module's own trigger
list happens to be complete — an incomplete trigger list here can only ever
cost one extra question to a human, never a silent bypass, because
`_hard_deny.py`'s independent raw-text/token-stream scan (R6) and this
module's own callers (`_rules.py`'s opacity check, `permissions/review.py`'s
`classify()`) never rely on `opaque` alone to justify blocking anything —
they rely on it to justify ESCALATING. The accepted cost (R5/R9, written down
rather than left implicit): plenty of everyday commands are `opaque` under
this definition (`python3 -m pytest`, `bash build.sh`, `awk '{print $1}'
file`, any command touching a shell rc file's `>`) — they aren't hard-denied,
aren't silently allowed either; they land on the user gate every time, in
every mode. That's the deliberate trade this ruling makes.

Self-contained (stdlib only, no `jones_daemon` import) — same rule as every
other module in this package (see `__init__.py`'s docstring): reuses
`_hard_deny.tokenize`/`_hard_deny._prog` (same package, relative import) —
`_hard_deny.py` and this module are copied into every worker's
`HERMES_HOME/plugins/jones_gate/` together.

`permissions/review.py` (daemon-side) imports THIS SAME module directly
(`from jones_daemon.kernel.plugin.jones_gate import _transparency`) rather
than re-implementing the same rules — R7's "两处 import 同一份": the daemon
process can import this package as an ordinary Python package (it already
does, for `_review_payload`/`_hard_deny`), it's only the WORKER's copy of it
that has to be dependency-free.

## Round 6 (2026-09-19, controller ruling R11 — final, not overturnable)

Every interpreter/indirect-execution program-name trigger below is now
case-insensitive: `BASH script.sh`/`PYTHON3 -c ...` are `opaque` exactly
like their lowercase spellings, via `_hard_deny._prog()`'s own lowercasing
plus an explicit `.lower()` at each comparison site that also checks the
raw token. The operator/quoting substrings (`$(`, `` ` ``, `;`, `&`, `|`,
redirection, …) and `$IFS`/`${IFS` are untouched — they're punctuation and
a real (case-sensitive by shell convention) environment variable name, not
program-name spelling variance.
"""

from __future__ import annotations

from typing import Literal

from . import _hard_deny

Transparency = Literal["plain", "opaque"]

# R5, verbatim: "任何引号内含 $ 或反引号、$' / $" 前缀、$(、反引号、任何重定向
# < > >> 2> &>、; & | 、换行、\$IFS". `>>`/`2>`/`&>` all contain a bare `>`
# already, so a single `>`/`<` substring check covers every redirection
# spelling R5 names without listing each one — same "don't enumerate what a
# single character class already covers" reasoning `_hard_deny.py`'s own
# `_OPERATOR_CHARS` uses. `$IFS` is checked both bare (`rm$IFS-rf`) and
# brace-wrapped (`rm${IFS}-rf`, arguably the more common obfuscation form in
# the wild, since it's unambiguous about where the variable name ends) —
# neither spelling is more "correct" than the other per R5's text, and
# missing either would leave a real word-splitting bypass unclassified.
_OPAQUE_SUBSTRINGS = ("$(", "`", "$'", '$"', "<", ">", ";", "&", "|", "\n", "$IFS", "${IFS")

# R5's "解释器/间接执行程序名 token" list, minus `python*` (prefix match, handled
# separately below) and `find`/`sed` (both listed with an explicit flag
# qualifier in R5's own text — "find(含 -delete/-exec)", "sed(含 -i)" — so
# checked with their own helper instead of unconditional membership here) and
# `.` (see `_has_leading_dot_source` docstring for why it's checked
# differently from every other name in this set).
_OPAQUE_PROGRAM_NAMES = frozenset(
    {
        "sh", "bash", "zsh", "dash", "ksh", "fish",
        "env", "nohup", "timeout", "xargs",
        "eval", "exec", "source",
        "node", "perl", "ruby", "php",
        "osascript", "base64", "awk", "tee",
    }
)


def _has_quoted_dollar_or_backtick(command: str) -> bool:
    """R5: "任何引号内含 $ 或反引号" — a bare `$VAR`/`` ` `` sitting inside a
    `'...'`/`"..."` span that the global `$(`/`` ` `` substring checks above
    wouldn't otherwise catch on their own (e.g. `echo "$HOME"` has no `$(`
    and no un-quoted backtick, but is still a variable expansion this module
    can't reason about). A minimal quote-state scan — not the full
    backslash/escape handling `_hard_deny.tokenize()` does, because all this
    needs to know is "was `$`/`` ` `` seen while a quote was open", not what
    the quoted token's final text is."""
    quote: str | None = None
    for ch in command:
        if quote is not None:
            if ch == quote:
                quote = None
            elif ch in ("$", "`"):
                return True
            continue
        if ch in ("'", '"'):
            quote = ch
    return False


def _has_find_delete_or_exec(tokens: list[str], index: int) -> bool:
    rest = [t.lower() for t in tokens[index + 1 :]]
    return "-delete" in rest or "-exec" in rest


def _has_sed_in_place(tokens: list[str], index: int) -> bool:
    rest = [t.lower() for t in tokens[index + 1 :]]
    return any(
        t == "-i" or t.startswith("-i") or t == "--in-place" or t.startswith("--in-place")
        for t in rest
    )


def _has_leading_dot_source(tokens: list[str]) -> bool:
    """R5 lists bare `.` (the POSIX `source` shorthand) among the opaque
    program names. Checked ONLY at `tokens[0]` — not "anywhere in the flat
    stream" like every other name in `_OPAQUE_PROGRAM_NAMES` — because `.`
    doubles as an ordinary "current directory" path argument (`find .`,
    `grep -r foo .`, `cp -r . dest`) far more often than it's ever the
    `source` builtin, and unlike a program name (`awk`, `tee`, ...) there is
    no basename normalization that tells the two apart (`Path(".").name` is
    `''`, not `.`). Restricting the check to the leading token still catches
    the one shape that actually matters — a bare `. some_script.sh` command —
    without making every `find .`/`grep ... .` opaque; a `.` reached via `&&`/
    `;`/newline is still caught by those operators' own substring triggers
    (see `_OPAQUE_SUBSTRINGS`) before this function is ever reached."""
    return bool(tokens) and tokens[0] == "."


def _has_opaque_program_token(tokens: list[str]) -> bool:
    for index, tok in enumerate(tokens):
        # `_hard_deny._prog()` already lowercases (controller ruling R11,
        # round 6, final) — `tok.lower()` alongside it catches a raw token
        # that isn't itself a bare program name (e.g. carries a path) but
        # still happens to equal one of these names case-insensitively.
        name = _hard_deny._prog(tok)
        lowered_tok = tok.lower()
        if name in _OPAQUE_PROGRAM_NAMES or lowered_tok in _OPAQUE_PROGRAM_NAMES:
            return True
        if name.startswith("python") or lowered_tok.startswith("python"):
            return True
        if (name == "find" or tok == "find") and _has_find_delete_or_exec(tokens, index):
            return True
        if (name == "sed" or tok == "sed") and _has_sed_in_place(tokens, index):
            return True
    return _has_leading_dot_source(tokens)


def classify(command: str) -> Transparency:
    """`plain` or `opaque` for one terminal `command` string. Never raises —
    an unparseable command (an unterminated quote — see
    `_hard_deny.tokenize()`'s docstring) is `opaque` by definition: if this
    module can't even tokenize it, it certainly can't prove it's simple."""
    if not isinstance(command, str) or not command:
        return "plain"
    if any(marker in command for marker in _OPAQUE_SUBSTRINGS):
        return "opaque"
    if _has_quoted_dollar_or_backtick(command):
        return "opaque"
    tokens = _hard_deny.tokenize(command)
    if tokens is None:
        return "opaque"  # unparseable -> can't prove it's safe -> opaque
    if _has_opaque_program_token(tokens):
        return "opaque"
    return "plain"
