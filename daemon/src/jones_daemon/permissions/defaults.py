"""Default-deny data for FR07/FR08 (Issue #13/#14, G15): the canonical set of
sensitive filesystem locations agent tool calls should never silently touch,
plus two small terminal-command program-name sets `permissions/review.py`
uses to complete its risk classification (docs/design/03-w4-interfaces.md
§3, "I" row: "敏感目录默认 deny、终端高危分类补全").

## Where this plugs in (and where it deliberately does NOT)

03-w4-interfaces.md §3's own wording says this default-deny layer lives "在
规则闸的 gate_config 中" (in the rule gate's `jones_gate.json`, i.e. inside
`kernel/plugin/jones_gate/`/`permissions/gate_config.py`). Implementation
found that premise doesn't fit the existing, already-hardened (six review
rounds, docs/design/02-w3-interfaces.md §1.1-§1.5) rule-gate architecture
without a structural change outside this branch's file ownership
(03-w4-interfaces.md §1's table gives branch I only this new file plus
"只加分类规则不改结构" access to `permissions/review.py` —
`kernel/plugin/jones_gate/_rules.py`/`_hard_deny.py`/`__init__.py` belong to
no W4 branch and are 02-w3's finished, "不可推翻" work):

- The rule gate's `permissions.json` matcher (`_rules.py::decide`) only ever
  compares a TOOL NAME (or, for `terminal` only, an entire normalized command
  string) — 01-w2-interfaces.md §4.1 explicitly deferred any path/glob
  matching as out of scope, and 02-w3's six rounds never added it. There is
  no existing mechanism to write "`read_file` is allowed EXCEPT under
  `~/.ssh`" as a `jones_gate.json` rule; building one would be new argument-
  aware rule-matching machinery for non-terminal tools, a scope well beyond
  "只加分类规则".
- The review gate (`permissions/review.py`, daemon-side, "③" in
  02-w3-interfaces.md §1.1's decision tree) is explicitly the layer built for
  exactly this kind of per-call, args-aware judgment (`_classify_write`
  already does the identical reasoning for `write_file`/`patch` — see that
  function). Wiring this default-deny set in there, instead, needs no new
  plugin-side machinery at all.

This still delivers the exact behavior 03-w4-interfaces.md §3 asks for:
- **Default is deny.** `permissions/review.py::classify()` now returns
  `high` (never `low`) for a `read_file`/`search_files`/`write_file`/`patch`
  call whose path — or a `terminal` call whose command text — resolves under
  one of `SENSITIVE_HOME_RELATIVE_DIRS`/`SENSITIVE_ABSOLUTE_DIRS` below.
  `high` risk never auto-allows in ANY session mode (`sessions/service.py::
  _on_request_permission`'s decision tree only auto-allows `low`) — every
  attempt to touch one of these paths goes through the user gate with a
  reason naming exactly which sensitive category it hit (G15's "走权限闸提
  示").
- **User-level `permissions.json` CAN explicitly reopen it (PRD 11.3), still
  defaulting to deny otherwise.** Not through a NEW path-matching rule (see
  above) — through the rule gate's EXISTING, already-hardened tool-name
  allow fast path: a user who adds `{"match": "read_file", "action":
  "allow"}` to `permissions.json` makes the rule gate return `allow` for
  every `read_file` call and take the "直接放行，零 IPC" branch
  (`kernel/plugin/jones_gate/__init__.py::_decide`) BEFORE this module's
  daemon-side check is ever reached — the call never gets to
  `permissions/review.py::classify()` at all. Coarser than a per-path
  override (the existing rule-matching grain is per-tool, not per-path —
  see above), but it is a real, working, already-tested "显式放开" path
  that needed zero new code, and it degrades safely: the user opted a whole
  TOOL open, never silently just this one sensitive path.
- **Distinct from hard-deny.** `kernel/plugin/jones_gate/_hard_deny.py`'s
  classifier (unconfigurable code constants, PRD 5.7) is untouched by this
  module — nothing here can ever be bypassed by an unconfigurable route
  either, because nothing here bypasses ANYTHING unconfigurable; it is
  purely an additional risk-classification input on the review gate, which
  a user's own `permissions.json` was already able to override for the
  whole tool before this branch existed.

See the PR report's "契约变更" section for the amendment this implies to
03-w4-interfaces.md §3's literal "在规则闸的 gate_config 中" wording.

## Round 2 review fixes (2026-09-19)

Findings #1/#7: `matches()`/`sensitive_roots()` used to compare resolved
paths with plain equality/`Path.relative_to()` — both case-SENSITIVE string
comparisons, and `relative_to()` uses a raised `ValueError` as its
"didn't match" signal. Two independent bugs that happened to live in the
same two functions:

- **Case sensitivity (finding #1, critical):** macOS's default filesystem
  (APFS) is case-insensitive but case-PRESERVING — `~/.SSH` and `~/.ssh` are
  the same directory on disk, `Path.resolve(strict=False)` does NOT fold
  case for a path that exists (verified: `ls -ld ~/.SSH` on a real macOS
  APFS volume returns the real `~/.ssh`'s own stat line), so `resolved ==
  root` silently failed to match a same-directory, different-case spelling.
  This is the exact case-insensitivity bug controller ruling commit
  `43d56d3` already fixed for the protected-`.jones`-path check
  (`kernel/plugin/jones_gate/_hard_deny.py::_protected_path_referenced`,
  `.lower()` on both sides) — this module hadn't picked up that precedent
  because it didn't exist yet when this file was first written. Fixed the
  same way: both sides `.lower()`'d before comparing (PRD v1 is macOS-only,
  00-foundation.md §12.1 G18, so this doesn't need a per-platform branch).
- **Exceptions as control flow (finding #7, important):** `resolved.
  relative_to(root)` inside a `try/except ValueError` re-evaluated on every
  single token of every `terminal` call (`_terminal_token_sensitive_root` in
  `permissions/review.py` calls `matches()` once per token) — a 500-token
  command measured at 165ms of synchronous blocking on the daemon's single
  event loop (FR08's whole 200ms streaming-latency budget, self-inflicted).
  Replaced with a plain string-prefix check (no exception raised on the
  common "doesn't match" path) plus caching `sensitive_roots()` per
  resolved-home value (`_sensitive_roots_for_home`, `functools.lru_cache`)
  so repeated calls stop re-running `Path.home().resolve()` and
  reconstructing 20+ `Path` objects every time.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

# ---------------------------------------------------------------------------
# G15 sensitive directories (Issue #13's explicit list: "~/.ssh、~/.aws、浏览
# 器 profile 目录、~/.jones/secrets/、系统密钥链"; PRD 12.1 G15 names ~/.ssh,
# ~/.aws, ~/.jones/secrets/ as the three acceptance-test paths).
# ---------------------------------------------------------------------------

# Home-relative: a directory (protects everything under it) or, where PRD/
# Issue text names one exact file, that file. `.gnupg` isn't in Issue #13's
# own list verbatim but IS in PRD 12.1's neighboring FR05 guidance
# (00-foundation.md's own credential enumeration) and was already covered by
# `permissions/review.py::_SENSITIVE_HOME_RELATIVE_PATHS` before this
# branch — kept here as the now-single canonical source, see that module's
# `_is_sensitive_home_path` for how the two merge.
SENSITIVE_HOME_RELATIVE_DIRS: tuple[str, ...] = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".jones/secrets",
    # Real (non-Jones) browser profile directories — the agent's OWN managed
    # automation profile lives under `user_root()/browser/profile`
    # (00-foundation.md §9.2), never under any of these, so this set only
    # ever matches a real system browser's saved cookies/logins/history,
    # which the agent has no standing reason to read directly.
    "Library/Application Support/Google/Chrome",
    "Library/Application Support/Chromium",
    "Library/Application Support/BraveSoftware",
    "Library/Application Support/Microsoft Edge",
    "Library/Application Support/Firefox",
    "Library/Safari",
    ".config/google-chrome",
    ".config/chromium",
    ".config/microsoft-edge",
    ".mozilla/firefox",
    # macOS per-user keychain ("系统密钥链").
    "Library/Keychains",
)

# System-wide (not home-relative) keychain locations on macOS — PRD v1 is
# macOS-only (12.1 G18), so this is the platform actually in scope; a path
# that doesn't exist on the running OS simply never matches anything below
# (`Path.relative_to`/equality checks don't require existence).
SENSITIVE_ABSOLUTE_DIRS: tuple[str, ...] = (
    "/Library/Keychains",
    "/System/Library/Keychains",
)


@lru_cache(maxsize=256)
def _sensitive_roots_for_home(resolved_home: str | None) -> tuple[Path, ...]:
    """The cacheable half of `sensitive_roots()` — keyed on the already-
    resolved home directory string (or `None` when it couldn't be resolved
    at all), so repeated calls with the SAME home (the overwhelmingly common
    case: one real `Path.home()` value for the daemon's whole process
    lifetime) skip reconstructing 20+ `Path` objects every time (review
    finding #7). A test suite that monkeypatches `$HOME` across many
    different `tmp_path` values just grows a few more cache entries — still
    correct, only a cache-size tradeoff, not a staleness bug, since the key
    IS the resolved home value itself."""
    roots: list[Path] = [Path(p) for p in SENSITIVE_ABSOLUTE_DIRS]
    if resolved_home is not None:
        home_path = Path(resolved_home)
        roots.extend(home_path / rel for rel in SENSITIVE_HOME_RELATIVE_DIRS)
    return tuple(roots)


def sensitive_roots(home: Path | None = None) -> tuple[Path, ...]:
    """Every default-deny root as a resolved absolute `Path` — home-relative
    entries joined against `home` (default: `Path.home()`), plus the
    absolute system ones. `home` resolution failing (no `$HOME`, a broken
    symlink) drops only the home-relative half rather than raising — a
    caller that can't resolve `Path.home()` has bigger problems than this
    one classifier, and the absolute system paths are still worth
    returning."""
    try:
        resolved_home = (home or Path.home()).resolve(strict=False)
    except OSError:
        return _sensitive_roots_for_home(None)
    return _sensitive_roots_for_home(str(resolved_home))


def _casefold(path_str: str) -> str:
    """macOS's default filesystem (APFS) is case-insensitive but case-
    PRESERVING (review finding #1) — `~/.SSH` and `~/.ssh` are the same
    directory on disk, so every comparison in this module folds case first.
    `.lower()`, not `os.path.normcase()`: `normcase` is a no-op on POSIX
    (it only folds case on Windows), which would silently do nothing here —
    matches the precedent already set by `kernel/plugin/jones_gate/
    _hard_deny.py::_protected_path_referenced` (controller ruling, commit
    `43d56d3`)."""
    return path_str.lower()


def matches(resolved: Path, *, home: Path | None = None) -> Path | None:
    """The specific default-deny root `resolved` (already `Path.resolve()`d
    by the caller) sits at or under, or `None`. Callers use the return value
    both as a truthy check and to name which root triggered the match in
    their own reason text (`str(root)`).

    Case-insensitive (review finding #1) and exception-free on the common
    "doesn't match" path (review finding #7: `Path.relative_to()`'s
    `ValueError`-as-control-flow was ~9ms/call, the dominant cost in a
    500-token terminal command's 165ms of event-loop blocking) — a plain
    string-prefix comparison instead."""
    resolved_cf = _casefold(str(resolved))
    for root in sensitive_roots(home):
        root_cf = _casefold(str(root))
        if resolved_cf == root_cf:
            return root
        prefix = root_cf if root_cf.endswith(os.sep) else root_cf + os.sep
        if resolved_cf.startswith(prefix):
            return root
    return None


def ancestor_root_under(resolved: Path, *, home: Path | None = None) -> Path | None:
    """The reverse direction from `matches()`: a default-deny root that sits
    AT OR UNDER `resolved` — i.e. `resolved` is an ancestor of (or equal to)
    that root, so recursively walking `resolved` would reach it. Review
    findings #2/#5: `_classify_read`'s `directory_scope` already used this
    exact reasoning for `search_files` (a directory argument can CONTAIN a
    sensitive root even when it isn't one itself — `~/.ssh` is *under* `~`,
    not equal to it); `permissions/review.py::
    _terminal_recursive_ancestor_of_sensitive_root` applies the same check
    to a `terminal` command's recursive/traversal targets (`grep -r`, `find`,
    `tar` on a directory that contains `~/.ssh`)."""
    resolved_cf = _casefold(str(resolved))
    prefix = resolved_cf if resolved_cf.endswith(os.sep) else resolved_cf + os.sep
    for root in sensitive_roots(home):
        root_cf = _casefold(str(root))
        if root_cf == resolved_cf or root_cf.startswith(prefix):
            return root
    return None


# ---------------------------------------------------------------------------
# Issue #14 terminal danger-classification completions
# (docs/design/03-w4-interfaces.md §3: "高危命令分类补全（sudo、curl|sh、
# chmod -R、dd、git push --force、写 shell rc 文件）进审查闸 high（不改
# 02-w3 已定模型，只加规则）"). Four of these six are already `high` under
# 02-w3-interfaces.md's existing rules without any change here — see
# `permissions/review.py::_classify_terminal`'s own comments for exactly
# which existing branch already covers `sudo` (privilege escalation),
# `curl|sh` (network-egress program name AND the `|` operator making it
# `opaque`), and "写 shell rc 文件" (any redirection makes a command
# `opaque`, and a shell rc path is already in `_SENSITIVE_HOME_RELATIVE_
# PATHS`/`SENSITIVE_HOME_RELATIVE_DIRS`-adjacent territory — see that
# module). This module supplies the two genuinely missing pieces: `dd` (not
# classified at all before this branch — fell through to the `low` default)
# and `chmod`/`chown`'s *recursive* form (plain `chmod` on one file was
# already `medium` via `_MUTATING_PROGRAMS`; `-R` specifically is what makes
# it as dangerous as `rm -rf`'s blast radius, hence `high`, not just
# `medium`). `git push --force` to a NON-default branch (force-push to
# `main`/`master` is already hard-denied outright, a different gate
# entirely) is handled directly in `_classify_terminal` (needs the `push`
# subcommand text, not just a program-name set) rather than a data table
# here.
# ---------------------------------------------------------------------------

# Unconditionally `high`: capable of destroying an entire disk/partition in
# one invocation (`dd if=... of=/dev/diskN`), unlike the generic
# `_MUTATING_PROGRAMS` `medium` floor (file/process/package mutation,
# assumed recoverable) this deliberately sits above.
DATA_DESTRUCTIVE_TERMINAL_PROGRAMS: frozenset[str] = frozenset({"dd"})

# `medium` (via `_MUTATING_PROGRAMS`) when used on a single, named target;
# `high` specifically when combined with a recursive flag (`-r`/`-R`/`-rf`/
# `--recursive`, same detector `kernel/plugin/jones_gate/_hard_deny.py`'s
# `rm -rf` check uses) — recursion is what turns "change one file's mode" into
# "change every file under an arbitrary directory tree", `rm -rf`'s own risk
# shape.
RECURSIVE_ESCALATES_TO_HIGH_PROGRAMS: frozenset[str] = frozenset({"chmod", "chown"})

# Round 2 review findings #2/#5 (2026-09-19): programs whose normal, no-flags-
# needed behavior already recursively walks a directory argument (`find`
# always traverses; `tar`/`zip` archive a directory's full contents when
# given one as an argument) — used by `permissions/review.py::
# _terminal_recursive_ancestor_of_sensitive_root` alongside an explicit
# `-r`/`-R`/`--recursive` flag (any program) to decide whether a command's
# directory argument needs the reverse "does it CONTAIN a sensitive root"
# check at all, not to change `dd`'s data-destructive handling above.
RECURSIVE_TRAVERSAL_PROGRAMS: frozenset[str] = frozenset({"find", "tar", "zip"})
