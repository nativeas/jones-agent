"""The review gate's risk classifier (Issue #11, docs/design/02-w3-interfaces.md
§1.1's "②"): "对动作做风险分级". v1 is deterministic rules (tool name +
argument features), not a second model call — the contract is explicit that
this is a documented, temporary simplification: "PRD 说审查闸是'模型对高危动作
二次判断'，v1 用规则先满足 G04/G05/G06 的可测性，模型判断作为 W4+ 增强并在 PRD
中标注". `classify()` is the one function a future model-backed version would
replace; nothing else in this module should need to change.

Only two of the three `Risk.level` values actually change a gating decision
today (`sessions/service.py::_on_request_permission`, per 02-w3-interfaces.md
§1.1's decision tree: "低风险 且 auto 模式 → 自动 allow" / "高风险 或 task 模式
→ 用户闸" — anything that isn't `low` behaves like `high` for that branch).
`medium` exists for the UI's "标红" (G04's review-gate flagging requirement)
to have a real middle ground to show, and so a future W4+ model-classifier
swap-in has somewhere to land results that are elevated-but-not-alarming
without changing the binary gating rule above it.

## Round 6 (2026-09-19, controller ruling R10 — final, not overturnable)

`terminal` (and every other tool in `kernel/plugin/jones_gate/__init__.py::
TERMINAL_LIKE_TOOLS`) no longer follows the generic decision tree above —
R10 removed the plugin-side allow fast path for these tools entirely
(§1.1's rule gate can now only `block`/`approve` a terminal-class call), so
`sessions/service.py::_on_request_permission` gates them through its own,
stricter rule instead: `low` alone is no longer sufficient to auto-allow —
it also needs `transparency(command) == "plain"` AND (a `permissions.json`/
`remember` rule whose `match` normalizes to the exact command, OR `auto`
mode). `_classify_terminal` below is what makes `low` achievable again for
a `terminal` call (round 4/5 had it floor at `medium` — see that function's
own docstring for why that floor is gone now, not merely widened).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

# Round 5 (controller ruling R7, 2026-09-19, final): the terminal classifier
# below must not use `shlex.split` any more — reuse `kernel/plugin/
# jones_gate`'s own tokenizer/transparency-classifier instead of a second,
# independent implementation (R7: "复用 jones_gate 的 tokenize...两处 import
# 同一份"). The daemon process can import that package as an ordinary
# Python package (it already does, for `_review_payload`) — only the
# WORKER's copy of it has to stay dependency-free, see that package's
# `__init__.py` docstring.
from jones_daemon.kernel.plugin.jones_gate import _hard_deny, _transparency
from jones_daemon.permissions import defaults

RiskLevel = Literal["low", "medium", "high"]

# Browser tools whose read-only-ness is knowable from the name alone (no args
# needed) — 00-foundation.md §9.2's rule-gate-eligible subset. `read_file`/
# `search_files` used to be in this set too (see git history) — Issue #13/#14
# (G15) moved them to `_classify_read` below, since "read-only" and "safe to
# auto-allow regardless of path" turned out not to be the same claim (see that
# function's docstring). NOTE: these are classified `low` here for cases
# where a caller routes them through the review gate anyway (e.g. review runs
# before a rule-gate allow-rule would have short-circuited it) — the rule
# gate (`kernel/plugin/jones_gate`) already allows these outright per
# 00-foundation.md §9.2.
_READ_ONLY_LOW = frozenset(
    {"browser_navigate", "browser_snapshot", "browser_take_screenshot", "browser_wait_for"}
)

# 02-w3-interfaces.md §1.1: "终端命令是否含网络外发 curl|wget|ssh|scp"; round 4
# (controller ruling R2, 2026-09-19) named `nc` explicitly alongside them
# ("compound 命令...含 curl|wget|ssh|scp|nc 给 high"); round 5 (controller
# ruling R7, 2026-09-19, final) adds `rsync`/`ftp` ("网络外发程序名（curl wget
# ssh scp nc rsync ftp）在 token 流任意位置出现 → high").
_NETWORK_EGRESS_PROGRAMS = frozenset({"curl", "wget", "ssh", "scp", "nc", "rsync", "ftp"})

# 00-foundation.md §9.2's user-gate condition ③ for `browser_evaluate`: "求值的
# 表达式里含网络请求...或存储写入". Deliberately coarse (a substring scan, not a
# JS parser) — same "v1 用确定性规则" scope as everything else in this module.
_JS_NETWORK_OR_STORAGE_MARKERS = (
    "fetch(", "XMLHttpRequest", "localStorage", "sessionStorage", "indexedDB",
    "document.cookie",
)

# 00-foundation.md §9.2's three-tier table for the browser tools that DO need
# review-gate judgment (the read-only ones are in `_READ_ONLY_LOW` instead, and
# `browser_evaluate` gets its own args-aware rule below).
_BROWSER_REVIEW_TOOLS = frozenset(
    {"browser_click", "browser_fill_form", "browser_type", "browser_press_key",
     "browser_drag", "browser_select_option", "browser_hover", "browser_file_upload",
     "browser_tabs"}
)


@dataclass(frozen=True)
class Risk:
    level: RiskLevel
    reasons: tuple[str, ...] = field(default_factory=tuple)


def _low(*reasons: str) -> Risk:
    return Risk("low", tuple(reasons))


def _medium(*reasons: str) -> Risk:
    return Risk("medium", tuple(reasons))


def _high(*reasons: str) -> Risk:
    return Risk("high", tuple(reasons))


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# Review findings #7/#9 (2026-09-19): `_cwd_for_project` (sessions/service.py)
# resolves the still-only-implemented DEFAULT_PROJECT_ID to `Path.home()` —
# an 01-w2-interfaces.md-documented placeholder for real Project paths (C,
# #8/#9), not a real workspace boundary. `_classify_write` below treats
# "inside cwd" as `low` risk, so with that placeholder in place, EVERY path
# under the user's entire home directory (`~/.ssh/authorized_keys`,
# `~/.zshrc`, `~/Library/LaunchAgents/*.plist`, ...) was classified `low` in
# auto mode, which `sessions/service.py::_on_request_permission` then
# auto-allows with no `permission.requested` broadcast at all — the "工作区
# 内写风险低" review-gate rule's premise ("workspace" is actually a bounded
# project directory) silently false for the one Project that exists today.
# Two independent, defense-in-depth fixes (both suggested by the review,
# doing both is cheap and each covers a gap the other doesn't):
_SENSITIVE_HOME_RELATIVE_PATHS = (
    ".ssh", ".aws", ".gnupg",
    ".zshrc", ".zshenv", ".zprofile", ".bashrc", ".bash_profile", ".bash_login", ".profile",
    "Library/LaunchAgents", "Library/LaunchDaemons",
    ".hermes", ".claude",
)


def _is_sensitive_home_path(resolved: Path) -> bool:
    """A denylist that applies REGARDLESS of the workspace boundary below —
    covers the case where a future real Project path legitimately contains
    (or symlinks to) one of these, not just today's "workspace = home"
    placeholder."""
    try:
        home = Path.home().resolve(strict=False)
    except OSError:
        home = None
    if home is not None:
        for rel in _SENSITIVE_HOME_RELATIVE_PATHS:
            sensitive_root = home / rel
            if resolved == sensitive_root or _is_relative_to(resolved, sensitive_root):
                return True
    # Issue #13/#14 (G15): `permissions/defaults.py` is the canonical
    # default-deny set (browser profiles, ~/.jones/secrets, system keychain,
    # ~/.ssh/~/.aws/~/.gnupg again — overlap with the list above is
    # intentional, not a bug: that list predates this branch and covers a
    # few write-specific entries (shell rc files, ~/.hermes, ~/.claude)
    # `defaults.py` doesn't repeat) — merged in here rather than duplicated,
    # see that module's docstring for why this is where it plugs in instead
    # of the rule gate's `gate_config`.
    return defaults.matches(resolved) is not None


def _workspace_root_too_wide(root: Path) -> bool:
    """`True` when `root` (the resolved `cwd`) IS the user's home directory,
    or is an ancestor of it — i.e. it's too broad to mean anything by "inside
    the workspace". Covers today's actual placeholder value (`root == home`)
    and the review's stated fallback ("home 的直接父级")."""
    try:
        home = Path.home().resolve(strict=False)
    except OSError:
        return False
    return _is_relative_to(home, root)


def _classify_path_access(
    path: Any, *, cwd: str | None, verb: str, escalate_too_wide_workspace: bool = True
) -> Risk:
    """Shared "which real filesystem location does this touch" reasoning for
    both a write-family call (`_classify_write`) and, since Issue #13/#14
    (G15) found read_file/search_files needed the identical judgment (see
    `_classify_read`'s docstring for why), a read-family one too. `verb` only
    varies the wording of the "no resolvable path" fallback message.

    Round 1 fix (2026-09-19, review finding #3): `escalate_too_wide_workspace`
    controls whether "the workspace root IS $HOME (today's placeholder
    Project boundary, 01-w2-interfaces.md §2.2)" downgrades an otherwise-`low`
    result to `medium`. This step exists to bound WRITE risk — an over-wide
    workspace means "could write anywhere in $HOME", which is worth a second
    look regardless of which single path was named. It does not carry over to
    a single-file READ: `read_file` only ever discloses the ONE path it was
    given, which the sensitive-root check just above and the
    escapes-the-workspace check just below already bound; degrading it to
    `medium` too made EVERY `read_file` call medium under today's $HOME
    placeholder (nothing under $HOME can ever be "narrow enough"), which
    forced a `permission.requested` user-gate stop on every single read in
    task/auto mode — a direct violation of PRD 9.1's "任务模式：只读工具直接
    放行" and G06 (see `_classify_read`'s `directory_scope` parameter for
    which callers pass which value, and why `search_files` keeps the
    escalation on)."""
    if not isinstance(path, str) or not path:
        return _medium(f"{verb} tool call with no resolvable path")
    try:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            if cwd is None:
                return _medium(
                    f"path {path!r} not classified: this session's workspace root is unknown"
                )
            candidate = Path(cwd).expanduser() / candidate
        resolved = candidate.resolve(strict=False)
    except OSError:
        return _high(f"could not resolve path {path!r} to classify it")

    if _is_sensitive_home_path(resolved):
        return _high(
            f"path {path!r} resolves under a sensitive user-home location (~/.ssh, ~/.aws, "
            "~/.gnupg, a shell rc file, a real browser profile, ~/.jones/secrets, the system "
            "keychain, ~/Library/LaunchAgents, ~/.hermes, ~/.claude, …) — never low risk "
            "regardless of the workspace boundary (review finding #9; PRD 12.1 G15; "
            "reopen explicitly via a permissions.json allow rule for this tool, PRD 11.3)"
        )

    if cwd is None:
        return _medium(f"path {path!r} not classified: this session's workspace root is unknown")
    try:
        root = Path(cwd).expanduser().resolve(strict=False)
    except OSError:
        return _high(f"could not resolve workspace root {cwd!r} to classify {path!r}")

    if escalate_too_wide_workspace and _workspace_root_too_wide(root):
        return _medium(
            f"workspace root {cwd!r} is the user's home directory (or an ancestor of it) — "
            "'inside the workspace' can't be treated as a low-risk boundary here (the "
            "default Project's cwd is $HOME until real Project paths land, review "
            "findings #7/#9)"
        )
    if _is_relative_to(resolved, root):
        return _low(f"path {path!r} is inside the project workspace")
    return _high(f"path {path!r} escapes the project workspace ({cwd!r}) — FR07's 越界路径走权限闸")


def _classify_write(path: Any, *, cwd: str | None) -> Risk:
    return _classify_path_access(path, cwd=cwd, verb="write-family")


def _classify_read(path: Any, *, cwd: str | None, directory_scope: bool) -> Risk:
    """Issue #13/#14 (G15): `read_file`/`search_files` used to be
    unconditionally `_low` (via `_READ_ONLY_LOW`) regardless of `path` — a
    sensitive-path read (or one outside the Project workspace, FR07's "越界
    路径走权限闸") was silently auto-allowed in auto/task mode with no
    user-gate visibility at all. Read and write share the exact same "which
    real filesystem location does this touch" question, so this reuses
    `_classify_write`'s reasoning via `_classify_path_access` rather than a
    second, independent implementation.

    `directory_scope` (round 1 fix, review findings #2/#3/#5/#6) is the one
    place read and write genuinely diverge, and it is NOT a knob a caller
    picks freely — it is `tool_name == "search_files"`, set by `classify()`:
    - `read_file` (`directory_scope=False`): the `path` names exactly the one
      file that will be disclosed. The sensitive-root check bounds that, and
      the escapes-the-workspace check bounds it further; there is nothing
      left for a "workspace too wide" downgrade to protect against, so it's
      skipped — see `_classify_path_access`'s docstring for why leaving it on
      broke PRD 9.1/G06 for ordinary reads under today's $HOME placeholder.
    - `search_files` (`directory_scope=True`): the `path` is a ROOT the tool
      recursively walks and greps — unlike a single file, a directory can
      CONTAIN a sensitive root (`~/.ssh` is *under* `~`, not equal to it) that
      the sensitive-root check alone would never catch, because that check
      only fires when the resolved path IS (or is under) a sensitive root,
      not the reverse. Keeping the "workspace too wide" escalation on is what
      stops `search_files` with `path="."` at today's $HOME-cwd placeholder
      from grepping the entire home directory — including `~/.ssh`,
      `~/.aws`, `~/.jones/secrets` — at `low` risk (review finding #5's exact
      repro). It only reaches `medium`, not `high`, because this is a
      structural "the scope is too broad to bound" signal, not proof the
      search actually touched a sensitive file — same non-`low` floor
      `_classify_path_access` already uses for "no resolvable path"/"unknown
      workspace", just for a different reason.

    A missing/blank `path` is no longer special-cased to `_low` here (review
    findings #2/#5: that made "omit the argument entirely" the single
    lowest-risk way to call `search_files`, strictly safer than passing its
    own documented default `"."` explicitly) — `classify()` now substitutes
    `"."` before calling this function at all, so a missing path and an
    explicit `path="."` are the exact same call by the time it gets here.
    The `isinstance` guard below is a defensive fallback for a hypothetical
    future caller that skips that substitution, not a path any current input
    reaches."""
    if not isinstance(path, str) or not path:
        return _medium("read-family tool call with no resolvable path")
    return _classify_path_access(
        path, cwd=cwd, verb="read-family", escalate_too_wide_workspace=directory_scope
    )


_PRIVILEGE_ESCALATION_PROGRAMS = frozenset({"sudo", "doas", "su", "pkexec"})
_MUTATING_PROGRAMS = frozenset({
    "chmod", "chown", "chgrp", "dd", "kill", "pkill", "killall", "launchctl", "systemctl",
    "brew", "npm", "pnpm", "yarn", "pip", "pip3", "uv", "cargo", "apt", "apt-get", "yum", "dnf",
    "mv", "cp", "ln", "mkdir", "rmdir", "touch", "truncate", "install", "crontab", "defaults",
})
_GIT_MUTATING_SUBCOMMANDS = frozenset({
    "push", "reset", "clean", "rebase", "checkout", "switch", "restore", "stash", "branch",
    "tag", "commit", "merge", "cherry-pick", "revert", "am", "apply",
})


def _classify_terminal(args: dict[str, Any], *, cwd: str | None) -> Risk:
    # Round 5 (controller ruling R5/R7, 2026-09-19): the FIRST thing this
    # function does is the same `transparency(command)` judgment the rule
    # gate's allow fast path uses (`kernel/plugin/jones_gate/_rules.py`) —
    # R5, verbatim: "opaque 命令...永不被审查闸判 low/medium，直接 high →
    # 用户闸". An opaque command skips `low`/`medium` entirely and goes
    # straight to `high`, in every mode, because this codebase cannot prove
    # by static analysis alone that its literal text is what actually runs
    # (quoting that could hide a substitution, an indirect-execution program
    # name, ... — see `_transparency.py`'s docstring for the full trigger
    # list).
    #
    # Round 6 (controller ruling R10, 2026-09-19, final): round 4/5's
    # `medium` FLOOR for a `plain`, non-network-egress command is gone — it
    # now classifies `low`. That floor existed only because, pre-R10, the
    # PLUGIN's own allow fast path already handled a trusted, non-opaque
    # command with zero daemon involvement — this function only ever saw a
    # `terminal` call that had ALREADY failed to zero-IPC-allow, so treating
    # "no specific risk signal" as `medium` (never quite trusted) was the
    # safety net for whatever the plugin's own matching had gotten wrong.
    # R10 removes that plugin-side bypass entirely for terminal-class tools
    # (`kernel/plugin/jones_gate/__init__.py::TERMINAL_LIKE_TOOLS`) and
    # makes the DAEMON (`sessions/service.py::_on_request_permission`) the
    # only place such a call can ever auto-allow — which needs this
    # function to genuinely be able to say `low` again, otherwise R10's
    # "transparency=plain 且 review=low 且 存在匹配的 allow 规则（或 auto 模
    # 式）→ 自动 allow" condition could never fire for anything. `medium`
    # still exists for the UI's "标红" (unchanged tools below still use it);
    # for `terminal` specifically, its only remaining source is an
    # unparseable-but-not-opaque command, which can't happen (an
    # unparseable command IS opaque, see `_transparency.classify`) — kept as
    # a defensive fallback, not a reachable path.
    command = args.get("command")
    if not isinstance(command, str) or not command:
        return _high("terminal call with no command text to analyze")
    if _transparency.classify(command) == "opaque":
        return _high(
            "该命令无法静态分析，请人工确认 — this command contains shell syntax "
            "(quoting, substitution, redirection, an operator, or an "
            "indirect-execution/interpreter program name) that cannot be "
            "proven safe by static analysis alone (controller ruling R5)"
        )
    # R7: "在 token 流上做已有扫描" — reuse `_hard_deny.tokenize()` (shared with
    # the rule gate, see this module's import comment) instead of a second,
    # independent `shlex.split` implementation. A `None` result here means
    # `command` is unparseable, which `_transparency.classify()` above
    # already treats as `opaque` and returns early for — this branch is a
    # defensive fallback, not a path any test input is expected to reach.
    tokens = _hard_deny.tokenize(command)
    if tokens is None:
        return _high("could not parse this command for risk analysis")
    # Issue #13/#14 (G15): a `plain` command can still reference one of
    # `permissions/defaults.py`'s default-deny sensitive locations by an
    # absolute, `~`-expanded, OR workspace-relative path token (`cat
    # ~/.ssh/id_rsa` as much as plain `cat .ssh/id_rsa` when `cwd` is
    # $HOME — round 1 fix, review findings #1/#6: a relative token used to
    # `continue` past this check untested, so the exact same target the `~`
    # form correctly caught was silently `low` when spelled without the
    # `~`) without tripping any operator/quoting-based `opaque` signal —
    # checked before the network-egress/mutation checks below since
    # touching a sensitive location is the more specific, more important
    # reason to escalate.
    sensitive_root = _terminal_token_sensitive_root(tokens, cwd=cwd)
    if sensitive_root is not None:
        return _high(
            f"command references a default-deny sensitive location ({sensitive_root}) — "
            "PRD 12.1 G15; reopen explicitly via a permissions.json allow rule for the "
            "terminal tool, PRD 11.3"
        )
    # Round 6 (controller ruling R11): lowercased — `CURL`/`Wget` must be
    # flagged exactly like their lowercase spellings.
    programs = {Path(t).name.lower() for t in tokens}
    hit = programs & _NETWORK_EGRESS_PROGRAMS
    if hit:
        return _high(f"command includes a network-egress tool: {', '.join(sorted(hit))}")
    # Controller adjudication after round 6: R10 made `low` reachable again, but
    # "plain and not egress" is not the same as "harmless". Privilege escalation
    # is `high`; programs that mutate the filesystem / processes / installed
    # software / git history keep a `medium` floor so auto mode never runs them
    # without either an exact-match allow rule or the user gate.
    esc = programs & _PRIVILEGE_ESCALATION_PROGRAMS
    if esc:
        return _high(f"command escalates privileges: {', '.join(sorted(esc))}")
    # Issue #14's "高危命令分类补全" list, the two entries not already `high`
    # above (`sudo`/`curl|sh` are covered by the two checks just above; "写
    # shell rc 文件" is covered by the sensitive-path check above this
    # function plus the opaque/redirection check earlier — see
    # `permissions/defaults.py`'s module docstring for the full mapping):
    destructive = programs & defaults.DATA_DESTRUCTIVE_TERMINAL_PROGRAMS
    if destructive:
        return _high(
            f"command uses a data-destructive tool that can wipe an entire disk/partition "
            f"in one call: {', '.join(sorted(destructive))}"
        )
    recursive_escalates = programs & defaults.RECURSIVE_ESCALATES_TO_HIGH_PROGRAMS
    if recursive_escalates and any(_hard_deny._is_recursive_flag(t) for t in tokens):
        return _high(
            f"recursive {', '.join(sorted(recursive_escalates))} can change an entire "
            "directory tree's permissions/ownership in one call"
        )
    lowered_tokens = [t.lower() for t in tokens]
    if "git" in programs and "push" in lowered_tokens and any(
        t in ("--force", "-f", "--force-with-lease") or t.startswith("--force-with-lease=")
        for t in lowered_tokens
    ):
        # `git push --force` to `main`/`master` is already hard-denied by a
        # different gate entirely (`kernel/plugin/jones_gate/_hard_deny.py`,
        # unconfigurable); this covers every OTHER target, which the hard-
        # deny gate deliberately does not touch — force-pushing any branch
        # can still overwrite another collaborator's history irreversibly.
        return _high("git push --force can overwrite remote history irreversibly")
    mut = programs & _MUTATING_PROGRAMS
    if mut:
        return _medium(f"command mutates state: {', '.join(sorted(mut))}")
    if "git" in programs and programs & _GIT_MUTATING_SUBCOMMANDS:
        return _medium("git command rewrites history or remote state")
    return _low(
        "plain terminal command with no network-egress or state-mutation signal "
        "(controller ruling R10, round 6)"
    )


def _terminal_token_sensitive_root(tokens: list[str], *, cwd: str | None) -> Path | None:
    """Does any token in a `terminal` command's flat token stream, once
    `~`-expanded (and, for a still-relative token, resolved against `cwd`),
    resolve under a `permissions/defaults.py` default-deny root?

    Round 1 fix (2026-09-19, review findings #1/#6): this used to `continue`
    past any token that was still relative after `~`-expansion, reasoning
    that the realistic G15 shape always uses `~` or an already-absolute
    path — that was wrong on the exact case that matters most: the DEFAULT
    Project's `cwd` IS `$HOME` (01-w2-interfaces.md §2.2's documented
    placeholder), so `cat .ssh/id_rsa` run from it targets the identical
    file `cat ~/.ssh/id_rsa` does, with neither a `~` nor a leading `/` in
    the command text to catch. Mirrors the three-line "expand relative
    against cwd" step `_classify_path_access` already does for the
    file-tool equivalent of this same access, rather than a second,
    independent implementation of it. A token that's still relative with no
    `cwd` available is left unresolved (skipped, as before) — with no
    workspace root to resolve against there's nothing more specific to
    check here than the plain-command `low` default already gives it."""
    for tok in tokens:
        try:
            candidate = Path(tok).expanduser()
        except (OSError, ValueError):
            continue
        if not candidate.is_absolute():
            if cwd is None:
                continue
            try:
                candidate = Path(cwd).expanduser() / candidate
            except (OSError, ValueError):
                continue
        try:
            resolved = candidate.resolve(strict=False)
        except OSError:
            continue
        root = defaults.matches(resolved)
        if root is not None:
            return root
    return None


def _classify_browser_evaluate(args: dict[str, Any]) -> Risk:
    expr = args.get("function") or args.get("expression") or args.get("code") or ""
    expr = expr if isinstance(expr, str) else ""
    hit = [m for m in _JS_NETWORK_OR_STORAGE_MARKERS if m in expr]
    if hit:
        return _high(f"evaluated expression touches network/storage: {', '.join(hit)}")
    return _medium("browser_evaluate with no detected network/storage access")


def classify(tool_name: str, args: dict[str, Any] | None, *, cwd: str | None = None) -> Risk:
    """Deterministic risk classification for the review gate. `args` may be
    `{}`/incomplete (e.g. a `write_file`/`patch` call whose real structured
    args weren't recoverable — see `sessions/service.py::_on_request_permission`'s
    docstring) — every branch here degrades to a safe non-`low` default rather
    than guessing `low` from absence of data (never auto-bypasses on missing
    information)."""
    args = args if isinstance(args, dict) else {}
    if tool_name in _READ_ONLY_LOW:
        return _low(f"{tool_name} is read-only")
    if tool_name in ("read_file", "search_files"):
        # Round 1 fix (review findings #2/#5): a missing/blank `path` is
        # substituted with `search_files`'s own documented default (`"."`,
        # verified against Hermes's `SEARCH_FILES_SCHEMA`) BEFORE
        # classification, not treated as a special always-`low` case inside
        # `_classify_read` — see that function's docstring for why "the
        # caller didn't say" must never be a lower-risk signal than "the
        # caller said the default explicitly".
        path = args.get("path")
        if not isinstance(path, str) or not path:
            path = "."
        return _classify_read(path, cwd=cwd, directory_scope=tool_name == "search_files")
    if tool_name in ("write_file", "patch"):
        return _classify_write(args.get("path"), cwd=cwd)
    if tool_name == "terminal":
        return _classify_terminal(args, cwd=cwd)
    if tool_name == "browser_evaluate":
        return _classify_browser_evaluate(args)
    if tool_name in _BROWSER_REVIEW_TOOLS:
        return _medium(f"{tool_name} has side effects and needs review")
    # 00-foundation.md §9.2's fail-closed catch-all ("上面三档没有点名的任何工具
    # ...一律用户闸") for anything this function doesn't specifically recognize —
    # `medium` (not `low`) so it never auto-bypasses in auto mode.
    return _medium(f"no specific risk rule for tool {tool_name!r}; defaulting to reviewed")
