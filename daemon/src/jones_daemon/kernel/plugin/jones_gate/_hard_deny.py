"""Hard-deny classifier — the rule gate's unconfigurable floor (Issue #11,
docs/design/02-w3-interfaces.md §1.1: "硬禁止清单（代码常量，不可配置放宽）").

Self-contained (stdlib only, no `jones_daemon` import — see the package
docstring in `__init__.py`): this file is physically copied into every
worker's `HERMES_HOME/plugins/jones_gate/` and runs inside Hermes's own
Python process, not the daemon's.

Two kinds of hard denial, both **never** influenced by `jones_gate.json` (no
mode, no permissions.json rule, no Agent whitelist can ever loosen either
one — that file only ever supplies the *paths* `command`/`args` are checked
against, never a policy switch):

1. `classify_command`: a small lexical (shlex-based, not substring-matching —
   02-w3-interfaces.md §1.1 is explicit that a "命令分类器" is required, not a
   blacklist of strings) classifier for the terminal tool's `command` arg:
   `rm -r`/`rm -rf` outside a temp dir, `trash`/empty-recycle-bin, `shred`,
   `mkfs*`, `diskutil erase*`, `git push --force` to the default branch.
2. `is_protected_path`: writes/deletes touching `~/.jones/` (any path under
   it) or a project's `.jones/permissions.json` specifically (PRD 10.4/N10).

A command this module cannot safely parse (unbalanced quotes) is NOT hard-
denied here — hard-deny is reserved for patterns we can positively identify;
an unparseable command instead falls through to the review gate, where
`permissions/review.py::classify()` marks it high-risk for exactly the same
reason it couldn't be parsed here, which routes it to the user gate in every
mode. Silently hard-blocking anything we merely fail to understand would be
its own kind of dishonesty (DEV.md 工程原则 #4 covers failing loud, not
failing by guessing).

## Shell-wrapper unwrapping (review finding #2, 2026-09-19)

`classify_command`/`command_touches_protected_path` originally only looked at
each shell segment's OWN argv — `bash -c 'rm -rf /Users/alice'` classified as
a call to `bash` with two harmless-looking args, never looking inside the
`-c` payload it actually executes. Combined with a `permissions.json` allow
rule for `terminal` (a legitimate, intentional config — "allow this whole
tool" — 02-w3-interfaces.md §1.1's exact-tool-name match shape), that meant
the one gate PRD 5.7 says "any config" can never loosen had a hole a single
`sh -c`/`bash -c` wrapper drove straight through it.

`_expand_wrapped_argv` recursively unwraps a bounded set of known wrappers
before the existing per-argv checks run, so the checks below see the REAL
command being executed, not just the wrapper invoking it:
  - `sh`/`bash`/`zsh`/`dash`/`ksh -c "<script>"` — the script argument is
    itself a full shell command string, re-split into its own segments
    (`_split_shell_segments`, same function the top-level command already
    goes through) and each of those recursively unwrapped too (nested
    wrapping, e.g. `bash -c "env FOO=1 sh -c 'rm -rf /'"`, up to
    `_MAX_UNWRAP_DEPTH`).
  - `env`/`nohup`/`timeout <cmd> ...` — these exec a real command with the
    rest of their own argv, after skipping their own flags/args
    (`_strip_leading_wrapper_argv`); the remainder is unwrapped the same way
    (still just one argv, no re-splitting needed — no shell is involved).
  - `xargs [options] <cmd> [initial-args]` — best-effort: the command
    xargs would invoke, after xargs's own flags. `xargs`'s real invocation
    also appends args read from stdin at runtime, which this static analysis
    can never see — `_rm_verdict` already fails closed on that (an `rm -rf`
    with no VISIBLE target argument is treated as "unproven safe", see its
    own docstring), which is exactly the right degradation here too.

A wrapper form this function doesn't recognize, or a `-c` payload that fails
to parse, is simply not unwrapped — same "don't hard-deny what we can't
positively identify" rule as everywhere else in this module; it still falls
through to the review gate for whatever the wrapper's own argv looks like
verbatim.

## Combined short-option `-c` forms (review finding #1, round 2, 2026-09-19)

The first cut of the shell-interpreter branch above only recognized a
standalone `-c` token (`argv.index("-c")`) — real-world shell invocations
routinely combine `-c` with another single-letter flag in one token
(`bash -lc '...'` for a login shell, `bash -ic '...'` interactive, `sh -xc
'...'` xtrace, `-ec`, etc.), which is exactly as common as bare `-c` and was
not recognized at all: the whole payload fell through `except ValueError`
(no, worse — `argv.index("-c")` just raised `ValueError` because no token
equals the string `"-c"` exactly) and the function returned `[argv]`
unexpanded, silently reopening the same hole finding #2 closed for the bare
form. `_shell_dash_c_index` now matches the standalone `-c` **or** any
single-dash cluster of letters ending in `c` (`-lc`, `-ic`, `-xc`, `-ec`,
`-ilc`, …) — `getopt`-style combined short options are unordered, but `c`'s
own convention is to always be the flag that consumes the next argv as its
payload, so matching "ends in `c`" (rather than "contains `c`") stays
conservative about what counts as a `-c` cluster instead of guessing.
"""

from __future__ import annotations

import shlex
import tempfile
from dataclasses import dataclass
from pathlib import Path

_SHELL_OPERATORS = frozenset({"&&", "||", ";", "|"})
_RECURSIVE_FORCE_RM_FLAGS = frozenset({"-r", "-rf", "-fr", "-R", "-Rf", "-fR"})
_TRASH_PROGRAMS = frozenset({"trash", "rmtrash"})
_DEFAULT_BRANCHES = frozenset({"main", "master"})

# Review finding #2: known shell-wrapper programs whose argv this module
# recurses into instead of stopping at (see module docstring's "Shell-wrapper
# unwrapping" section).
_SHELL_INTERPRETERS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_ENV_LIKE_WRAPPERS = frozenset({"env", "nohup", "timeout"})
_XARGS_VALUE_FLAGS = frozenset(
    {"-I", "-L", "-n", "-P", "-s", "-d", "--delimiter", "--max-args", "--max-procs", "--replace"}
)
_MAX_UNWRAP_DEPTH = 4


@dataclass(frozen=True)
class Verdict:
    denied: bool
    reason: str = ""


def _temp_roots() -> tuple[Path, ...]:
    # Real, resolved roots — macOS's `/tmp` is a symlink to `/private/tmp`;
    # resolving both the root and the candidate path the same way is what
    # makes the `is_relative_to` check below mean anything.
    raw = {tempfile.gettempdir(), "/tmp", "/private/tmp", "/var/tmp"}
    out = []
    for r in raw:
        try:
            out.append(Path(r).resolve(strict=False))
        except OSError:
            continue
    return tuple(out)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_best_effort(raw: str, *, cwd: str | None) -> Path | None:
    try:
        p = Path(raw).expanduser()
        if not p.is_absolute() and cwd:
            p = Path(cwd).expanduser() / p
        return p.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        # `ValueError` (review finding #11, 2026-09-19): `Path.resolve()`
        # raises it (not `OSError`) for a path containing an embedded NUL
        # byte (`lstat: embedded null character in path`) — reproduced
        # against this exact function before this fix. Fails closed the
        # same way an `OSError` already did (see `is_protected_path`'s
        # caller: an unresolvable path is treated as protected, an
        # unresolvable `rm` target is treated as not-provably-temp).
        return None


def _is_temp_path(raw: str, *, cwd: str | None) -> bool:
    resolved = _resolve_best_effort(raw, cwd=cwd)
    if resolved is None:
        # Can't resolve it -> can't prove it's safely inside a temp root ->
        # fail closed (treat as NOT temp, i.e. this rm target stays hard-denied).
        return False
    return any(_is_relative_to(resolved, root) for root in _temp_roots())


def _split_shell_segments(command: str) -> list[list[str]] | None:
    """Split on top-level `&&`/`||`/`;`/`|` into one argv per segment.

    `shlex.split` doesn't treat these as operators on its own — they come
    back as ordinary word tokens (e.g. `"a && b"` -> `["a", "&&", "b"]`) — so
    a second pass groups tokens between them. Returns `None` (not raises) on
    unbalanced quoting, matching the "don't hard-deny what we can't parse"
    rule in the module docstring.
    """
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in _SHELL_OPERATORS:
            if current:
                segments.append(current)
            current = []
        else:
            current.append(tok)
    if current:
        segments.append(current)
    return segments


def _strip_leading_wrapper_argv(prog: str, argv: list[str]) -> list[str] | None:
    """`env`/`nohup`/`timeout`: return the argv of the command they'd
    actually exec, after skipping their own flags/args. `None` when nothing
    identifiable follows (e.g. `env` with no command at all)."""
    rest = argv[1:]
    if prog == "env":
        i = 0
        while i < len(rest):
            tok = rest[i]
            if tok.startswith("-"):
                i += 1
                continue
            if "=" in tok:  # a VAR=VALUE assignment, `env`'s own syntax
                i += 1
                continue
            break
        rest = rest[i:]
    elif prog == "timeout":
        i = 0
        consumed_duration = False
        while i < len(rest):
            tok = rest[i]
            if tok.startswith("-"):
                i += 1
                continue
            if not consumed_duration:
                i += 1
                consumed_duration = True
                continue
            break
        rest = rest[i:]
    # `nohup <cmd> ...` takes no flags of its own before the command.
    return rest or None


def _strip_xargs_wrapper(argv: list[str]) -> list[str] | None:
    """Best-effort: the argv of the command `xargs` would invoke, after its
    own options. `xargs` also appends args it reads from stdin at runtime —
    invisible to this static analysis — so the returned argv may be missing
    trailing arguments the real invocation would have; callers (`_rm_verdict`
    in particular) already fail closed on a target-less `rm -rf`, which is
    the correct degradation for that gap, not a hole in it."""
    rest = argv[1:]
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--":
            i += 1
            break
        if not tok.startswith("-"):
            break
        if tok in _XARGS_VALUE_FLAGS:
            i += 2
        else:
            i += 1
    rest = rest[i:]
    return rest or None


def _shell_dash_c_index(argv: list[str]) -> int | None:
    """Index of a shell `-c`-equivalent flag in `argv`: either the standalone
    `-c` token, or a single-dash combined short-option cluster ENDING in `c`
    (`-lc`, `-ic`, `-xc`, `-ec`, …) — see the module docstring's "Combined
    short-option `-c` forms" section. `--` (a long option, or the end-of-
    options marker) never matches. Returns `None` when no such flag is
    present."""
    for i, tok in enumerate(argv):
        if tok.startswith("--"):
            continue
        if not tok.startswith("-"):
            continue
        letters = tok[1:]
        if letters and letters.isalpha() and letters[-1] == "c":
            return i
    return None


def _expand_wrapped_argv(argv: list[str], depth: int) -> list[list[str]]:
    """Return `[argv]` plus, for a recognized wrapper, every argv it would
    actually go on to run (recursively, up to `depth`) — see the module
    docstring's "Shell-wrapper unwrapping" section. Always includes the
    original `argv` itself (a wrapper's own name/flags never denied by
    anything below, but harmless to also check)."""
    out = [argv]
    if depth <= 0 or not argv:
        return out
    prog = Path(argv[0]).name
    if prog in _SHELL_INTERPRETERS:
        c_index = _shell_dash_c_index(argv)
        if c_index is None:
            return out
        if c_index + 1 >= len(argv):
            return out
        inner_segments = _split_shell_segments(argv[c_index + 1])
        if inner_segments is None:
            return out
        for seg in inner_segments:
            if seg:
                out.extend(_expand_wrapped_argv(seg, depth - 1))
        return out
    if prog in _ENV_LIKE_WRAPPERS:
        rest = _strip_leading_wrapper_argv(prog, argv)
        if rest:
            out.extend(_expand_wrapped_argv(rest, depth - 1))
        return out
    if prog == "xargs":
        rest = _strip_xargs_wrapper(argv)
        if rest:
            out.extend(_expand_wrapped_argv(rest, depth - 1))
        return out
    return out


def _rm_verdict(argv: list[str], *, cwd: str | None) -> str | None:
    prog = Path(argv[0]).name
    if prog != "rm":
        return None
    flags = [a for a in argv[1:] if a.startswith("-")]
    targets = [a for a in argv[1:] if not a.startswith("-")]
    recursive_force = any(f in _RECURSIVE_FORCE_RM_FLAGS for f in flags) or any(
        # combined short flags like `-fr`, `-Rf`, or a bundled `-rf` spelled
        # with other short flags mixed in (e.g. `-vrf`): recursive AND force
        # both present in one token.
        f.startswith("-") and not f.startswith("--") and "r" in f.lower() and "f" in f.lower()
        for f in flags
    )
    if not recursive_force:
        return None
    if not targets or any(not _is_temp_path(t, cwd=cwd) for t in targets):
        return "rm -r/-rf targeting a non-temporary path is never allowed (PRD 5.7)"
    return None


def _git_push_force_verdict(argv: list[str]) -> str | None:
    if Path(argv[0]).name != "git" or len(argv) < 2 or argv[1] != "push":
        return None
    rest = argv[2:]
    force = any(
        a in ("--force", "-f", "--force-with-lease") or a.startswith("--force-with-lease=")
        for a in rest
    )
    if not force:
        return None
    refs = [a for a in rest if not a.startswith("-")]
    if len(refs) >= 2:
        target = refs[-1].split(":")[-1]
    elif len(refs) == 1 and ":" in refs[0]:
        target = refs[0].split(":")[-1]
    elif len(refs) == 1:
        # A single non-flag arg to `git push --force` is the remote, not a
        # refspec (e.g. `git push --force origin`) -> pushes whatever branch
        # is currently checked out. Ambiguous, so treated the same as "no
        # explicit target": conservatively assumed to be the default branch.
        target = None
    else:
        target = None
    if target is None or target in _DEFAULT_BRANCHES:
        return "git push --force to the default branch is never allowed (PRD 5.7)"
    return None


def classify_command(command: str, *, cwd: str | None = None) -> Verdict:
    """Classify a terminal `command` string. `cwd` (the worker's working
    directory, when known) resolves relative `rm` targets; without it a
    relative target can't be proven temp and is treated conservatively (see
    `_resolve_best_effort`)."""
    segments = _split_shell_segments(command)
    if segments is None:
        return Verdict(False)  # unparseable -> not hard-denied here, see module docstring
    for top_argv in segments:
        if not top_argv:
            continue
        for argv in _expand_wrapped_argv(top_argv, _MAX_UNWRAP_DEPTH):
            prog = Path(argv[0]).name
            reason = _rm_verdict(argv, cwd=cwd)
            if reason:
                return Verdict(True, reason)
            if prog in _TRASH_PROGRAMS:
                return Verdict(
                    True, "moving files to Trash / emptying it is never allowed (PRD 5.7)"
                )
            if prog == "shred":
                return Verdict(True, "shred is never allowed (PRD 5.7)")
            if prog.startswith("mkfs"):
                return Verdict(True, "mkfs* is never allowed (PRD 5.7)")
            if prog == "diskutil" and len(argv) > 1 and argv[1].lower().startswith("erase"):
                return Verdict(True, "diskutil erase* is never allowed (PRD 5.7)")
            if prog == "find" and any(a == "-delete" for a in argv[1:]):
                return Verdict(
                    True, "find -delete performs an irreversible delete and is never allowed "
                    "(PRD 5.7)"
                )
            reason = _git_push_force_verdict(argv)
            if reason:
                return Verdict(True, reason)
    return Verdict(False)


def command_touches_protected_path(
    command: str, *, user_root: str, project_permissions_path: str | None, cwd: str | None
) -> bool:
    """Broad, deliberately over-inclusive heuristic for terminal commands: any
    token (once a leading `-`/`>`/`>>` is stripped, so `rm -rf ~/.jones` and
    `cat >~/.jones/x` both match on the operand, not the flag) that resolves
    under a protected path hard-denies the whole command — including a plain
    *read* of something under `~/.jones/` (`cat ~/.jones/secrets/vault.enc`),
    which is broader than the letter of "写删" in 02-w3-interfaces.md §1.1 but
    is the simpler, safer rule for an opaque shell command where "is this
    argument actually a write" isn't reliably decidable without a full
    per-program argument grammar. An unparseable command returns `False`
    here for the same reason `classify_command` does (see its docstring) —
    the review gate is the honest fallback for "can't tell", not a silent
    hard-block."""
    segments = _split_shell_segments(command)
    if segments is None:
        return False
    for top_argv in segments:
        for argv in _expand_wrapped_argv(top_argv, _MAX_UNWRAP_DEPTH):
            for tok in argv:
                candidate = tok.lstrip("-")
                for prefix in (">>", ">"):
                    if candidate.startswith(prefix):
                        candidate = candidate[len(prefix) :]
                        break
                if not candidate:
                    continue
                if is_protected_path(
                    candidate, user_root=user_root,
                    project_permissions_path=project_permissions_path,
                ):
                    return True
    return False


def is_protected_path(
    raw_path: str, *, user_root: str, project_permissions_path: str | None
) -> bool:
    """Whether `raw_path` (a tool's `path` argument, or a terminal command's
    target) touches `~/.jones/` (any path under it) or the current project's
    `.jones/permissions.json` specifically (PRD 10.4, N10). Unresolvable
    (e.g. `..` escapes that fail to normalize) fails closed -> protected."""
    resolved = _resolve_best_effort(raw_path, cwd=None)
    if resolved is None:
        return True
    try:
        root = Path(user_root).expanduser().resolve(strict=False)
    except OSError:
        root = None
    if root is not None and _is_relative_to(resolved, root):
        return True
    if project_permissions_path:
        try:
            protected_file = Path(project_permissions_path).expanduser().resolve(strict=False)
        except OSError:
            protected_file = None
        if protected_file is not None and resolved == protected_file:
            return True
    return False
