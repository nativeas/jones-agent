"""Hard-deny classifier — the rule gate's unconfigurable floor (Issue #11,
docs/design/02-w3-interfaces.md §1.1: "硬禁止清单（代码常量，不可配置放宽）").

Self-contained (stdlib only, no `jones_daemon` import — see the package
docstring in `__init__.py`): this file is physically copied into every
worker's `HERMES_HOME/plugins/jones_gate/` and runs inside Hermes's own
Python process, not the daemon's.

## Round 4 rewrite (2026-09-19, controller ruling R2 — "改前提，不打补丁")

Rounds 1–3 each closed one more shell-syntax hole in a segment-based
classifier (`argv[0]` of each `&&`/`;`/`|`-delimited "segment", wrapper
programs unwrapped one at a time, `$(...)`/redirection detected as "unproven"
inside a segment, then `&`/bare-newline added as segment boundaries too) —
three rounds of the exact "patch on patch" DEV.md 工程原则 #2 forbids, because
the premise was wrong: trying to understand *which shell invocation a token
belongs to* before deciding whether the command is dangerous. A classifier
built that way can only ever be as complete as the list of shell constructs
its author thought to unwrap — 4 rounds of review finding a 5th were exactly
that premise failing, not 4 rounds of sloppy implementation.

The new premise: **stop trying to understand command boundaries at all.**
`tokenize()` below turns the whole command string into ONE flat, quote-aware
token stream — splitting on whitespace AND on every shell operator character
(`; & | < > ( ) \\`` and a literal newline, all DISCARDED, never returned as
tokens of their own — unlike round 1–3's segment splitter, which kept `&&`/
`;`/`|` as tokens and then grouped around them) — and the checks below look
for a hard-deny PATTERN anywhere in that flat stream, never asking "is this
token part of the same shell command as that one". This can never be fooled
by a joiner this file didn't happen to enumerate (there's no joiner-list to
be incomplete) — `env`/`nohup`/`timeout`/`xargs`/`find -exec` wrapping `rm
-rf` all fall out for free (their argv sits directly in the flat stream, no
per-wrapper unwrapping code needed at all, see `test_gates_hard_deny.py`'s
`test_xargs_rm_rf_is_denied` for why `xargs rm -rf` needs zero special-casing
now). The one wrapper that genuinely needs help is a shell interpreter's
`-c`/`-lc`/`-xc`/… payload, because `tokenize()` correctly keeps a QUOTED
string as one token (the whole payload), so its own `rm`/`-rf` tokens are
inside that one token's text, not separate entries in the stream — see
`_shell_dash_c_payloads` below, the one piece of "recursion" this module
still does, and it does it by re-running the SAME flat scan on the payload
text, not by adding a new case to a wrapper-specific unwrap table.

The tradeoff, made explicit rather than left implicit (PRD 5.7 explicitly
allows it — "宁可误拒，不允许漏挡"): this scanner is a deliberate OVER-
approximation. It no longer resolves an `rm -rf` target against `cwd` to
carve out "this happens to point at a temp directory" (round 1–3's
`_rm_verdict` did) — ANY `rm` token followed anywhere later in the stream by
a recursive/force flag is denied, full stop, because proving a target is
"safely temporary" is exactly the kind of per-invocation understanding this
rewrite stops attempting. Likewise `command_touches_protected_path`'s old
"any token that resolves under `~/.jones/`, read or write, hard-denies the
whole command" is gone — the new rule requires a write/delete-verb token to
also be present in the same stream (see `_protected_path_denied`), a
deliberate NARROWING the controller's ruling asked for explicitly; a plain
read (`cat ~/.jones/x`) is no longer hard-denied by this file (it still
isn't silently allowed either: any command containing an operator character,
or any other construct `_transparency.classify()` doesn't understand, is
`opaque` and never takes the rule gate's allow fast path — see
`_rules.py::decide` — so an *opaque* read attempt still lands on the daemon
review gate for a human to see; only a bare, non-opaque `cat ~/.jones/x` with
no matching config rule at all reaches this file's judgment alone).

## Round 5 addition (2026-09-19, controller ruling R6 — final, not
overturnable): raw-text regex scan alongside the token-stream scan

Round 4's re-review (the one that produced R5-R9) found the flat
tokenizer above still has a blind spot the token-stream scan alone can't
close: `_prog()`/`_rm_denied()` compare a WHOLE token's basename against
`"rm"` — exactly right for an ordinary token, exactly wrong for `rm -rf`
text that's been folded into a *different* single token by quoting this
module's `tokenize()` doesn't unwrap: `rm -rf` sitting inside a
double-quoted `$(...)` (`tokenize()` correctly keeps the whole quoted span
as one token — that's right for a literal argument, wrong once the content
is going to be re-interpreted as shell syntax after substitution), or
`$'rm'` (bash's ANSI-C quoting: this tokenizer's simple `'...'`/`"..."`-only
quote tracker folds the leading `$` onto the quoted content, producing the
single token `"$rm"`, which is not `"rm"`). Rather than teach the tokenizer
a third shell construct (`_hard_deny.py`'s whole history above is what
happens when this module tries to become a more complete shell parser one
construct at a time), R6 adds an entirely independent, much cruder check
that runs ALONGSIDE the token scan, not instead of it: strip every `'`/`"`
character out of the raw command text (a blunt, non-parsing operation — see
`_strip_quote_chars`) and run a handful of regexes against what's left,
looking for the same patterns `_scan()` already looks for on the token
stream (`rm ... -r/-rf/--recursive`, `shred`, `mkfs`, `diskutil erase`,
`trash`, `git push --force ... main/master`) PLUS a raw-text version of the
protected-path check (`_raw_protected_path_denied`) that no longer requires
the write-verb to be its own clean token, just present somewhere in the raw
text alongside a `>`/`>>`. Either scan denying is enough (`classify_command`
runs both, unconditionally). The accepted cost, PRD 5.7's "宁可误拒" put
into practice rather than left as a slogan: this raw-text scan cannot tell
"a real `rm -rf` invocation" apart from "the literal four characters `rm -rf`
sitting inside an ordinary string a program will just print or search for"
— `echo "rm -rf /tmp/x"` (printing the text, never running it) is hard-
denied by this scan exactly as if it were real. That specific false-reject
is intentional and documented (docs/design/02-w3-interfaces.md §1.4's "已知
误拒" list), not a bug to eventually fix.

## Round 6 (2026-09-19, controller ruling R11/R12 — final, not overturnable)

R11: every check in this module is now **case-insensitive** — `RM -rf`,
`Rm -Rf`, `SHRED`, `FIND ... -delete`, `GIT PUSH --force` all deny exactly
like their lowercase spellings (macOS's own filesystem is already
case-insensitive; `RM` really is `rm` there). All `_RAW_*` regexes below
carry `re.IGNORECASE`; every token-stream comparison goes through `_prog()`
(now lowercasing) or an explicit `.lower()` at the comparison site.

R12: the protected-`.jones`-path check is no longer "write/delete verb
present" (`_WRITE_DELETE_VERBS` and everything built on it — deleted, not
kept around for compatibility) — it's now a **read-only whitelist**:
referencing a protected path is denied UNLESS the command is provably
`plain` (`_transparency.classify`, imported from the sibling module — the
one exception to "no cross-module understanding" this file's history above
otherwise holds to, because R12 explicitly asks for it) AND its first
program is one of a fixed read-only set (`cat`, `ls`, `head`, `tail`,
`less`, `more`, `wc`, `stat`, `file`, `grep`, `rg`, `diff` without `-i`,
`find` without `-delete`/`-exec`, `tree`, `du`, `jq`, `yq`) AND the raw text
carries no redirection at all. No more per-verb enumeration to keep in sync
with `sed -i`/`tee`/whatever else can write a file — a fixed, auditable
"safe to read with" list is easier to reason about than an open-ended "not
yet known to be a writer" one, and it's what `_transparency.classify()`
already had to build to satisfy R5.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import _transparency

# Characters that end a token AND are discarded (never returned as tokens of
# their own) — whitespace does the same job but isn't listed here since
# `str.isspace()` already covers it (including the literal `\n` the ruling
# calls out by name).
_OPERATOR_CHARS = frozenset(";&|<>()`")

_MAX_SHELL_C_DEPTH = 3  # controller ruling R2: "深度 ≤ 3"
_SHELL_INTERPRETERS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_TRASH_PROGRAMS = frozenset({"trash", "rmtrash"})
_DEFAULT_BRANCHES = frozenset({"main", "master"})

# -- Round 6 (controller ruling R12): the read-only whitelist a command      -
# referencing a protected `.jones` path must satisfy to NOT be hard-denied
# -- see `_protected_path_denied` below. Replaces round 1-5's "write/delete
# verb list" approach entirely (no more verb table to keep in sync).
_PROTECTED_PATH_NEEDLES = ("~/.jones", "$HOME/.jones", "/.jones/")
_READONLY_PROTECTED_PATH_PROGRAMS = frozenset({
    "cat", "ls", "head", "tail", "less", "more", "wc", "stat", "file",
    "grep", "rg", "diff", "find", "tree", "du", "jq", "yq",
})

# -- Round 5 (controller ruling R6): raw-text regex scan, alongside the ------
# token-stream scan above, not instead of it -- see the module docstring's
# "Round 5 addition" section for why. These run against `_strip_quote_chars`'
# output (the raw command text with every `'`/`"` character simply deleted,
# NOT a re-parse) -- a deliberate over-approximation, not a second tokenizer.
# Round 6 (controller ruling R11): every one of these now carries
# `re.IGNORECASE` -- `RM -RF`/`SHRED`/`GIT PUSH --FORCE` must deny exactly
# like their lowercase spellings.

# "\brm\b 后随任意 -r/-R/-rf/-fr/--recursive（不要求相邻）" (controller ruling
# R6, verbatim) -- matched on whatever text follows an `rm` word anywhere
# later in the (quote-stripped) string, not necessarily its own argv.
_RAW_RM_WORD_RE = re.compile(r"\brm\b", re.IGNORECASE)
_RAW_RECURSIVE_FLAG_RE = re.compile(
    r"(?:^|\s)-[A-Za-z]*[rR][A-Za-z]*(?:\s|$)|--recursive\b", re.IGNORECASE
)
_RAW_SHRED_RE = re.compile(r"\bshred\b", re.IGNORECASE)
_RAW_MKFS_RE = re.compile(r"\bmkfs", re.IGNORECASE)
_RAW_DISKUTIL_ERASE_RE = re.compile(r"\bdiskutil\s+erase", re.IGNORECASE)
_RAW_TRASH_RE = re.compile(r"\btrash\b", re.IGNORECASE)
_RAW_GIT_PUSH_FORCE_MAIN_RE = re.compile(
    r"\bgit\s+push\b[^\n]*(?:--force\b|-f\b)[^\n]*\b(?:main|master)\b", re.IGNORECASE
)


def _strip_quote_chars(command: str) -> str:
    """"去掉引号字符後的原文字符串" (controller ruling R6, verbatim) — a blunt
    character deletion, NOT quote-aware parsing (that's what `tokenize()`
    above already does, and is exactly the kind of understanding this raw
    scan deliberately does WITHOUT, see the module docstring): `rm -rf` that
    `tokenize()` folded into the single token `"$(rm -rf ~/x)"` (double-
    quoted) or `"$rm"` (`$'rm'` ANSI-C quoting) still contains the literal
    substring `rm -rf` once the quote characters themselves are gone, so a
    plain regex search over this finds it without needing to know why it was
    quoted in the first place."""
    return command.replace("'", "").replace('"', "")


def _raw_text_pattern_denied(command: str) -> Verdict:
    stripped = _strip_quote_chars(command)
    for match in _RAW_RM_WORD_RE.finditer(stripped):
        if _RAW_RECURSIVE_FLAG_RE.search(stripped, match.end()):
            return Verdict(
                True,
                "rm ... -r/-rf/--recursive found in the raw command text "
                "(PRD 5.7, controller ruling R6 raw-text scan)",
            )
    if _RAW_SHRED_RE.search(stripped):
        return Verdict(True, "shred is never allowed (PRD 5.7)")
    if _RAW_MKFS_RE.search(stripped):
        return Verdict(True, "mkfs* is never allowed (PRD 5.7)")
    if _RAW_DISKUTIL_ERASE_RE.search(stripped):
        return Verdict(True, "diskutil erase* is never allowed (PRD 5.7)")
    if _RAW_TRASH_RE.search(stripped):
        return Verdict(True, "moving files to Trash / emptying it is never allowed (PRD 5.7)")
    if _RAW_GIT_PUSH_FORCE_MAIN_RE.search(stripped):
        return Verdict(True, "git push --force to the default branch is never allowed (PRD 5.7)")
    return Verdict(False)


def _protected_path_referenced(
    command: str, *, user_root: str | None, project_permissions_path: str | None
) -> bool:
    """Does `command`'s (quote-stripped, per controller ruling R12's own
    wording — "原文（去引号后）出现...") text mention a protected `.jones`
    path at all? Just the "does it touch it" half of `_protected_path_denied`
    — see that function for the round-6 read-only-whitelist half that
    decides whether touching it is actually denied."""
    # R11 applies here too: macOS's filesystem is case-insensitive, so
    # `~/.JONES/permissions.json` IS `~/.jones/permissions.json`. Compare
    # lower-cased haystack against lower-cased needles.
    stripped = _strip_quote_chars(command).lower()
    needles: list[str] = [n.lower() for n in _PROTECTED_PATH_NEEDLES]
    if user_root:
        needles.append(user_root.lower())
    if project_permissions_path:
        needles.append(project_permissions_path.lower())
    return any(needle in stripped for needle in needles)


def _protected_path_readonly_whitelisted(command: str) -> bool:
    """Controller ruling R12 (round 6, final): a command that references a
    protected `.jones` path is only NOT hard-denied when it's provably
    read-only — `plain` (`_transparency.classify`, so any redirection,
    substitution, operator or indirect-execution program name already
    disqualifies it on its own) AND its first program is in the fixed
    read-only whitelist AND (belt and suspenders — `plain` already forbids
    `<`/`>` on its own, see `_transparency`'s `_OPAQUE_SUBSTRINGS`, but R12
    names "无重定向" as its own explicit condition) the raw text carries no
    redirection character at all. `diff` additionally requires no `-i` flag
    (R12, verbatim: "diff(无 -i)") and `find` no `-delete`/`-exec` (R12:
    "find(无 -delete/-exec)") — both over-cautious, matching this whole
    module's "sacrifice completeness for safety" tradeoff (`find -delete`/
    `-exec` already makes the command `opaque` on its own via
    `_transparency`'s own find-flag check, so that half is redundant in
    practice; kept explicit anyway because R12 names it by name)."""
    if _transparency.classify(command) != "plain":
        return False
    if "<" in command or ">" in command:
        return False
    tokens = tokenize(command)
    if not tokens:
        return False
    first = _prog(tokens[0])
    if first not in _READONLY_PROTECTED_PATH_PROGRAMS:
        return False
    rest = tokens[1:]
    if first == "diff" and any(t.lower() == "-i" or t.lower().startswith("-i") for t in rest):
        return False
    if first == "find" and any(t.lower() in ("-delete", "-exec") for t in rest):
        return False
    return True


def _protected_path_denied(
    command: str, *, user_root: str | None, project_permissions_path: str | None
) -> bool:
    """Controller ruling R12 (round 6, final; supersedes round 4/5's
    write/delete-verb-based `_protected_path_denied`/
    `_raw_protected_path_denied` pair, both deleted): a command referencing
    `~/.jones`/`$HOME/.jones`/`/.jones/` (any Project's `.jones`, or the
    resolved `user_root`/`project_permissions_path`) is denied UNLESS it's
    one of a fixed read-only whitelist of commands — no more write/delete
    verb table to keep in sync."""
    if not _protected_path_referenced(
        command, user_root=user_root, project_permissions_path=project_permissions_path
    ):
        return False
    return not _protected_path_readonly_whitelisted(command)


@dataclass(frozen=True)
class Verdict:
    denied: bool
    reason: str = ""


def tokenize(command: str) -> list[str] | None:
    """Quote-aware scanner (controller ruling R2): cuts `command` into tokens
    at every whitespace character AND at every character in
    `_OPERATOR_CHARS` — both kinds of delimiter are discarded, never
    returned as a token — EXCEPT while inside a `'...'`/`"..."` quoted span,
    where the entire quoted content becomes (part of) one token regardless
    of what it contains (an operator character or whitespace inside quotes
    is just data). Backslash escaping is POSIX: outside quotes and inside
    double quotes, `\\` makes the next character literal (never a delimiter,
    never quote-processed); inside single quotes nothing is escaped, not
    even `\\` itself, until the closing `'`. Returns `None` (never raises)
    for a command with an unterminated quote — the same "can't safely parse
    it -> don't hard-deny it here, let the review gate's own 'could not
    parse' high-risk classification handle it" contract every round of this
    module has kept (see `permissions/review.py::_classify_terminal`)."""
    tokens: list[str] = []
    current: list[str] = []
    token_open = False  # True once we've started a token, even an empty one (e.g. `""`)
    quote: str | None = None
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if quote == "'":
            if ch == "'":
                quote = None
            else:
                current.append(ch)
            i += 1
            continue
        if quote == '"':
            if ch == "\\" and i + 1 < n and command[i + 1] in ('"', "\\", "$", "`", "\n"):
                current.append(command[i + 1])
                i += 2
                continue
            if ch == '"':
                quote = None
                i += 1
                continue
            current.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            current.append(command[i + 1])
            token_open = True
            i += 2
            continue
        if ch in ("'", '"'):
            quote = ch
            token_open = True
            i += 1
            continue
        if ch.isspace() or ch in _OPERATOR_CHARS:
            if token_open or current:
                tokens.append("".join(current))
            current = []
            token_open = False
            i += 1
            continue
        current.append(ch)
        token_open = True
        i += 1
    if quote is not None:
        return None  # unterminated quote -> unparseable, see docstring
    if token_open or current:
        tokens.append("".join(current))
    return tokens


def _prog(tok: str) -> str:
    """Basename-normalize a single token for a program-name comparison (so a
    rule matching `rm` still catches `/bin/rm`) — deliberately applied only
    at the point of a program-name comparison, never to every token in the
    stream, because doing that to a path ARGUMENT (e.g. `/Users/alice/
    .jones/x`) would throw away the directory components a protected-path
    check needs to see. Lowercased (controller ruling R11, round 6, final):
    `RM`/`Rm`/`SHRED` must be caught exactly like their lowercase spellings
    — macOS's own filesystem is already case-insensitive, so `RM` really is
    `rm` there."""
    return Path(tok).name.lower()


def _is_recursive_flag(tok: str) -> bool:
    """"含 -r/-R/-rf/-fr（或 --recursive）" (controller ruling R2) — matched
    generously: any single-dash cluster of letters containing `r`/`R`
    (covers `-r`, `-R`, `-rf`, `-fr`, `-vrf`, …), or `--recursive` itself.
    Over-inclusive on purpose (see module docstring's "sacrifice precision
    for an over-approximation" tradeoff)."""
    if tok == "--recursive" or tok.startswith("--recursive="):
        return True
    if tok.startswith("--") or not tok.startswith("-") or len(tok) < 2:
        return False
    letters = tok[1:]
    return letters.isalpha() and ("r" in letters or "R" in letters)


def _rm_denied(tokens: list[str]) -> bool:
    """"出现 rm 且其后任一 token 含 ... → deny" — literally: an `rm` token
    anywhere in the stream with a recursive/force flag token anywhere AFTER
    it (not necessarily adjacent, not necessarily "its own" argv — see
    module docstring for why command-boundary precision is exactly what this
    rewrite stops attempting). No `cwd`/temp-directory carve-out any more
    (round 1–3's `_rm_verdict` had one; the controller's ruling doesn't, and
    proving a target is safely temporary is the kind of per-invocation
    understanding this module no longer does — PRD 5.7 explicitly permits
    the resulting over-denial)."""
    for i, tok in enumerate(tokens):
        if _prog(tok) != "rm":
            continue
        if any(_is_recursive_flag(t) for t in tokens[i + 1 :]):
            return True
    return False


def _find_delete_denied(tokens: list[str]) -> bool:
    return any(_prog(t) == "find" for t in tokens) and any(
        t.lower() == "-delete" for t in tokens
    )


def _trash_denied(tokens: list[str]) -> bool:
    return any(_prog(t) in _TRASH_PROGRAMS for t in tokens)


def _shred_denied(tokens: list[str]) -> bool:
    return any(_prog(t) == "shred" for t in tokens)


def _mkfs_denied(tokens: list[str]) -> bool:
    return any(_prog(t).startswith("mkfs") for t in tokens)


def _diskutil_erase_denied(tokens: list[str]) -> bool:
    for i, tok in enumerate(tokens):
        if _prog(tok) != "diskutil":
            continue
        if any(t.lower().startswith("erase") for t in tokens[i + 1 :]):
            return True
    return False


def _git_push_force_denied(tokens: list[str]) -> bool:
    lowered = [t.lower() for t in tokens]
    if not any(_prog(t) == "git" for t in tokens) or "push" not in lowered:
        return False
    force = any(
        t in ("--force", "-f", "--force-with-lease") or t.startswith("--force-with-lease=")
        for t in lowered
    )
    if not force:
        return False
    push_idx = lowered.index("push")
    refs = [t for t in tokens[push_idx + 1 :] if not t.startswith("-")]
    if len(refs) >= 2:
        target = refs[-1].split(":")[-1]
    elif len(refs) == 1 and ":" in refs[0]:
        target = refs[0].split(":")[-1]
    else:
        # Zero refs, or one bare ref (the remote name, not a refspec) -> the
        # branch actually pushed is whatever's checked out -> ambiguous ->
        # conservatively treated as the default branch (unchanged from
        # round 1's heuristic, only the token-stream plumbing around it).
        target = None
    return target is None or target.lower() in _DEFAULT_BRANCHES


def _shell_dash_c_payloads(tokens: list[str]) -> list[str]:
    """Token immediately after a shell interpreter's `-c`-equivalent flag
    (the standalone `-c`, or a combined single-dash short-option cluster
    ENDING in `c` — `-lc`/`-ic`/`-xc`/`-ec`/…, `getopt`-style clusters are
    unordered but `c`'s own convention is always "consumes the next argv as
    its payload") — this is the ONE recursion this module still performs,
    see the module docstring for why: `tokenize()` correctly keeps that
    payload as a single (quoted) token, so its own `rm`/`-rf`/… words never
    show up as separate entries in the outer flat stream on their own."""
    payloads = []
    for i, tok in enumerate(tokens):
        if _prog(tok) not in _SHELL_INTERPRETERS or i + 1 >= len(tokens):
            continue
        flag = tokens[i + 1]
        if flag.startswith("--") or not flag.startswith("-"):
            continue
        letters = flag[1:]
        if letters and letters.isalpha() and letters[-1].lower() == "c" and i + 2 < len(tokens):
            payloads.append(tokens[i + 2])
    return payloads


def _scan(
    tokens: list[str],
    *,
    depth: int,
) -> Verdict:
    # Round 6 (controller ruling R12): the protected-`.jones`-path check
    # moved OUT of this token-stream scan and into `classify_command`
    # itself, run once on the raw command text before tokenizing at all —
    # it needs `_transparency.classify()` (whole-command reasoning) and the
    # FIRST program specifically, neither of which fits this flat,
    # order-blind token scan. See `_protected_path_denied` above.
    if _rm_denied(tokens):
        return Verdict(True, "rm -r/-rf (or --recursive) is never allowed (PRD 5.7)")
    if _find_delete_denied(tokens):
        return Verdict(
            True, "find -delete performs an irreversible delete and is never allowed (PRD 5.7)"
        )
    if _trash_denied(tokens):
        return Verdict(True, "moving files to Trash / emptying it is never allowed (PRD 5.7)")
    if _shred_denied(tokens):
        return Verdict(True, "shred is never allowed (PRD 5.7)")
    if _mkfs_denied(tokens):
        return Verdict(True, "mkfs* is never allowed (PRD 5.7)")
    if _diskutil_erase_denied(tokens):
        return Verdict(True, "diskutil erase* is never allowed (PRD 5.7)")
    if _git_push_force_denied(tokens):
        return Verdict(True, "git push --force to the default branch is never allowed (PRD 5.7)")
    if depth > 0:
        for payload in _shell_dash_c_payloads(tokens):
            inner = tokenize(payload)
            if inner is None:
                continue
            verdict = _scan(inner, depth=depth - 1)
            if verdict.denied:
                return verdict
    return Verdict(False)


def classify_command(
    command: str, *, user_root: str | None = None, project_permissions_path: str | None = None
) -> Verdict:
    """Classify a terminal `command` string — the single entry point that
    replaces round 1–3's separate `classify_command`/
    `command_touches_protected_path` pair (they now share one tokenization
    and one flat scan, see module docstring).

    Round 5 (controller ruling R6): runs the raw-text regex scan (see the
    module docstring's "Round 5 addition") UNCONDITIONALLY, before even
    attempting to tokenize — it needs no successful tokenization to work,
    and an unparseable command (an unterminated quote) is exactly the kind
    of input the token-stream scan below has to skip; the raw-text scan
    still gets a look at it. Either scan denying is enough.

    Round 6 (controller ruling R12): the protected-`.jones`-path check runs
    next, also on the raw command text — same "no successful tokenization
    needed" reasoning, plus it needs `_transparency.classify()`'s whole-
    command judgment, which doesn't fit inside the flat token-stream scan
    below (see `_protected_path_denied`'s docstring)."""
    raw_verdict = _raw_text_pattern_denied(command)
    if raw_verdict.denied:
        return raw_verdict
    if _protected_path_denied(
        command, user_root=user_root, project_permissions_path=project_permissions_path
    ):
        return Verdict(
            True,
            "this command references Jones's own protected .jones path and isn't one of the "
            "read-only commands allowed to touch it (PRD 10.4, N10, controller ruling R12)",
        )
    tokens = tokenize(command)
    if tokens is None:
        return Verdict(False)  # unparseable -> not hard-denied by the token scan, see tokenize()
    return _scan(tokens, depth=_MAX_SHELL_C_DEPTH)


def is_protected_path(
    raw_path: str, *, user_root: str, project_permissions_path: str | None
) -> bool:
    """Whether `raw_path` (a `write_file`/`patch` tool call's `path` argument
    — a plain string, no shell involved, so this function is untouched by
    the round-4 rewrite above) touches `~/.jones/` (any path under it) or
    the current project's `.jones/permissions.json` specifically (PRD 10.4,
    N10). Unresolvable (e.g. a `..` escape that fails to normalize, or an
    embedded NUL byte) fails closed -> protected."""
    try:
        resolved = Path(raw_path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return True
    try:
        root = Path(user_root).expanduser().resolve(strict=False)
    except OSError:
        root = None
    if root is not None:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            pass
    if project_permissions_path:
        try:
            protected_file = Path(project_permissions_path).expanduser().resolve(strict=False)
        except OSError:
            protected_file = None
        if protected_file is not None and resolved == protected_file:
            return True
    return False
