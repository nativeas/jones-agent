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

## Round 2 review fixes (2026-09-19)

Five more G15/FR08 gaps, all fixed within this module and `permissions/
defaults.py` (no change to `kernel/plugin/jones_gate/`, still outside branch
I's ownership — see that module's docstring):

- **#1/#7 (critical/important):** `defaults.matches()` compared paths with
  case-sensitive equality and used `Path.relative_to()`'s raised
  `ValueError` as its "no match" signal — macOS's default APFS filesystem is
  case-insensitive (`~/.SSH` IS `~/.ssh`), so `~/.SSH/id_rsa` fell through to
  `low`; and the exception-per-token cost made a 500-token command block the
  daemon's event loop for ~165ms. Both fixed in `defaults.py`: case-folded
  string-prefix comparison, plus a cached `sensitive_roots()`.
- **#2/#5 (important):** the sensitive-path check only ever asked "is this
  token AT/UNDER a sensitive root" — never the reverse, "is this token an
  ANCESTOR of one" (`grep -r ... ~` never names `~/.ssh` directly, but
  recursively walks straight into it). `_terminal_recursive_ancestor_of_
  sensitive_root` below adds that check, gated on the command actually
  having recursive/traversal semantics, mirroring `_classify_read`'s
  existing `directory_scope` reasoning for `search_files`.
- **#4 (critical):** an unquoted `$HOME`/`${HOME}` in a command doesn't trip
  `_transparency`'s opaque check, so `cat $HOME/.ssh/id_rsa` tokenized as
  `plain` and then resolved as a nonsense relative path
  (`$HOME/$HOME/.ssh/id_rsa`) that never matched anything — see
  `_substitute_home_env_var`.
- **#6 (important):** `Path.expanduser()` raises `RuntimeError` (not
  `OSError`) for an unresolvable `~user` form — uncaught, this escaped
  `classify()` entirely and turned a permission request into a JSON-RPC
  `-32603` error. Every `except OSError` around an `expanduser()` call in
  this module now also catches `RuntimeError`.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

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

# Round-1 post-merge-review fixes (2026-09-19, findings #1/#6/#8): this whole
# browser section used to name Playwright-MCP tools (browser_take_screenshot,
# browser_fill_form, browser_evaluate, ...) that don't exist in the alpha
# (Hermes-native browser_*) toolset this branch actually shipped. 00-foundation.md
# section 9.3 had already been rewritten to the real tool names but this
# classifier never followed, so every real browser tool except browser_click/
# browser_type was silently falling through to the medium catch-all at the
# bottom of classify() instead of the tier section 9.3 actually specifies for
# it. Rewritten below to match section 9.3 (round-3 rewrite) tool name for
# tool name.

# Browser tools whose read-only-ness is knowable from the name alone (no args
# needed) — 00-foundation.md §9.2's rule-gate-eligible browser subset. `read_file`/
# `search_files` used to be in this set too (see git history) — Issue #13/#14
# (G15) moved them to `_classify_read` below, since "read-only" and "safe to
# auto-allow regardless of path" turned out not to be the same claim (see that
# function's docstring), so only the browser read-only tools remain here. NOTE:
# these are classified `low` here for cases where a caller routes them through
# the review gate anyway (e.g. review runs before a rule-gate allow-rule would
# have short-circuited it) — the rule gate (`kernel/plugin/jones_gate`)
# already allows these outright per 00-foundation.md §9.2.
_READ_ONLY_LOW = frozenset({"browser_snapshot", "browser_get_images", "browser_vision"})

# section 9.3's "constant user gate" browser tools -- the tool name alone IS
# the high-risk signal, never downgraded by args, never routed through
# review-gate judgment. browser_cdp is the raw-CDP escape hatch (can bypass
# every other semantic tier); the browser_vault_* four touch the credential
# vault.
_BROWSER_ALWAYS_HIGH = frozenset(
    {"browser_cdp", "browser_vault_unlock", "browser_vault_fill",
     "browser_vault_save_login", "browser_vault_enter_code"}
)

# 02-w3-interfaces.md §1.1: "终端命令是否含网络外发 curl|wget|ssh|scp"; round 4
# (controller ruling R2, 2026-09-19) named `nc` explicitly alongside them
# ("compound 命令...含 curl|wget|ssh|scp|nc 给 high"); round 5 (controller
# ruling R7, 2026-09-19, final) adds `rsync`/`ftp` ("网络外发程序名（curl wget
# ssh scp nc rsync ftp）在 token 流任意位置出现 → high").
_NETWORK_EGRESS_PROGRAMS = frozenset({"curl", "wget", "ssh", "scp", "nc", "rsync", "ftp"})

# section 9.3's user-gate condition (3) for browser_console (evaluated WITH an
# expression -- formerly wired to the nonexistent browser_evaluate tool,
# findings #1/#8): "the evaluated expression touches a network request...or a
# storage write". Deliberately coarse (a substring scan, not a JS parser) --
# same "v1 uses deterministic rules" scope as everything else in this module.
_JS_NETWORK_OR_STORAGE_MARKERS = (
    "fetch(", "XMLHttpRequest", "localStorage", "sessionStorage", "indexedDB",
    "document.cookie",
)

# section 9.3's review-gate tier for browser tools that have side effects but
# whose tool name alone isn't a high-risk signal -- v1 has no model-backed
# semantic judgment (this module's own docstring: "v1 uses deterministic rules
# first...model judgment is a W4+ enhancement"), so these floor at `medium`;
# the "is this actually a form submission" escalation section 9.3 describes is
# explicitly model-judged, not a deterministic rule this function implements.
_BROWSER_REVIEW_TOOLS = frozenset(
    {"browser_click", "browser_type", "browser_scroll", "browser_back", "browser_press",
     "browser_dialog"}
)

_SAFE_URL_SCHEMES = frozenset({"http", "https"})


# Round-2 review finding #2: CPython's `ipaddress` module does not classify
# 100.64.0.0/10 (CGNAT -- what Tailscale/most cloud VPCs hand out) as private/
# loopback/link-local at all (verified: `ip_address("100.64.1.1").is_private`
# is `False`) -- Hermes's own `tools/url_safety.py::_is_blocked_ip` explicitly
# special-cases `_CGNAT_NETWORK` for exactly this reason. `_looks_private_or_
# loopback` below matches that.
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


def _parse_loose_ipv4(hostname: str) -> ipaddress.IPv4Address | None:
    """Round-2 review finding #2/#8: Chrome's URL parser (WHATWG URL "IPv4
    parser") accepts decimal (`2130706433`), octal (`0177.0.0.1`), hex
    (`0x7f000001`), and short/"dotted" forms (`127.1`) as spellings of an
    IPv4 address, and resolves all of them navigating with the browser -- but
    `ipaddress.ip_address()` (used by `_looks_private_or_loopback` below)
    raises `ValueError` on every one of them (verified empirically), so a
    literal-only check using it alone falls through to `False` ("not
    private") for `http://127.1/`, `http://2130706433/`, `http://0177.0.0.1/`
    and `http://0x7f000001/` -- all four of which Chrome sends straight to
    `127.0.0.1`. This is a deliberately loose reimplementation of that
    algorithm (not a full WHATWG conformance target -- just enough to not be
    fooled by these four well-known obfuscations); returns `None` for
    anything that isn't a plausible numeric-IPv4 spelling (an ordinary
    hostname like `example.com` correctly falls through to `None` here)."""
    parts = hostname.split(".")
    if not (1 <= len(parts) <= 4) or any(p == "" for p in parts):
        return None
    numbers: list[int] = []
    for part in parts:
        digits, radix = part, 10
        if len(part) >= 2 and part[:2].lower() == "0x":
            digits, radix = part[2:], 16
        elif len(part) >= 2 and part[0] == "0":
            digits, radix = part[1:], 8
        if digits == "" or not all(c in "0123456789abcdefABCDEF" for c in digits):
            return None
        try:
            value = int(digits, radix)
        except ValueError:
            return None
        if value > 0xFFFFFFFF:
            return None
        numbers.append(value)
    if len(numbers) > 1 and any(n > 0xFF for n in numbers[:-1]):
        return None
    last = numbers[-1]
    if len(numbers) > 1 and last >= 256 ** (5 - len(numbers)):
        return None
    ipv4 = last
    for i, n in enumerate(numbers[:-1]):
        ipv4 += n * (256 ** (3 - i))
    try:
        return ipaddress.IPv4Address(ipv4)
    except ipaddress.AddressValueError:
        return None


def _looks_private_or_loopback(hostname: str | None) -> bool:
    """Literal-string/IP check only -- no DNS resolution (`classify()` must
    stay synchronous with no I/O). Catches the common literal spellings
    (`localhost`, `127.0.0.1`, `10.x`, `192.168.x`, link-local, `::1`,
    IPv4-mapped `::ffff:127.0.0.1`, CGNAT `100.64.0.0/10`, and the decimal/
    octal/hex/short obfuscated IPv4 spellings a browser's URL parser
    normalizes -- round-2 finding #2/#8) and does NOT catch a private
    hostname that only *resolves* to a private address (would need a network
    lookup this function deliberately doesn't do; `nip.io`-style DNS rebinding
    is an accepted, documented gap -- see `browser_worker_config`'s docstring
    for why Hermes's own real DNS-resolving check is the actual backstop for
    that class, R-J1)."""
    if not hostname:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        addr: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(hostname)
    except ValueError:
        loose = _parse_loose_ipv4(hostname)
        if loose is None:
            return False
        addr = loose
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if isinstance(addr, ipaddress.IPv4Address) and addr in _CGNAT_NETWORK:
        return True
    return addr.is_private or addr.is_loopback or addr.is_link_local


def _classify_browser_navigate(args: dict[str, Any]) -> Risk:
    """Round-1 fix (review finding #6, critical) + round-2 controller ruling
    R-J1: `capabilities/browser.py::browser_worker_config` does NOT set
    `browser.allow_private_urls` (round-2 removed that forced config value --
    see its docstring for why: the flag has no CDP-attach-scoped variant in
    this hermes-agent version, and also silently lifts SSRF protection for
    `web_extract`/vision/skills_hub, not just the browser). Without that flag,
    Hermes's own `tools/browser_tool.py::_url_policy_error` already refuses a
    non-http(s) scheme (e.g. `file://`) or a private/loopback navigation
    target on a CDP-override backend -- so this function's `high` escalation
    for those cases is DEFENSE IN DEPTH, not the only remaining check it was
    when finding #6 was first written: it makes sure the user gate visibly
    flags the attempt (auto/task mode would otherwise silently see only
    Hermes's own internal refusal, with no `permission.requested` at all, if
    this classifier had stayed at a name-only `low`) even on a future
    hermes-agent version, or a future Jones-side patch to the vendored
    dependency, that narrows the flag to only exempt the CDP attach itself and
    stops blocking navigation targets outright."""
    url = args.get("url")
    if not isinstance(url, str) or not url:
        return _medium("browser_navigate call with no resolvable url to classify")
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in _SAFE_URL_SCHEMES:
        return _high(
            f"browser_navigate targets a non-http(s) scheme ({scheme or '(none)'!r}) -- "
            "controller ruling R-J1: never low/medium for this, regardless of what "
            "Hermes's own url_safety does with this call"
        )
    if _looks_private_or_loopback(parts.hostname):
        return _high(
            f"browser_navigate targets a private/loopback host ({parts.hostname!r}) -- "
            "controller ruling R-J1: never low/medium for this, regardless of what "
            "Hermes's own url_safety does with this call"
        )
    return _low(f"browser_navigate targets a public http(s) url (host={parts.hostname!r})")


def _classify_browser_console(args: dict[str, Any]) -> Risk:
    """section 9.3: browser_console is read-only (low) when neither
    `expression` nor `clear` was passed (a plain log fetch); passing
    `expression` evaluates arbitrary JS in the page, which needs the same
    network/storage marker scan section 9.3 requires (formerly wired to the
    nonexistent browser_evaluate tool -- findings #1/#8)."""
    expression = args.get("expression")
    clear = bool(args.get("clear"))
    if not expression and not clear:
        return _low("browser_console with no expression/clear is a read-only log fetch")
    if not isinstance(expression, str) or not expression:
        return _medium("browser_console(clear=True) has a side effect (clears console log)")
    hit = [m for m in _JS_NETWORK_OR_STORAGE_MARKERS if m in expression]
    if hit:
        return _high(f"browser_console expression touches network/storage: {', '.join(hit)}")
    return _medium(
        "browser_console expression evaluation with no detected network/storage access"
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
    path: Any,
    *,
    cwd: str | None,
    verb: str,
    escalate_too_wide_workspace: bool = True,
    check_credential_filename: bool = False,
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
    escalation on).

    Controller ruling R-I1 (round 3, 2026-09-19): `check_credential_filename`
    — `read_file` only (see `classify()`) — adds one more tier BELOW the
    sensitive-directory/escapes-workspace `high` checks and ABOVE the plain
    `low` default: a path whose bare filename matches `permissions/
    defaults.py::is_credential_filename` (`id_rsa`, `.env`, `*.pem`, …) is
    `medium`, not `low`, even though it isn't under any denylisted
    directory — the filename alone is a real, if weaker, credential signal
    (ruling text: "凭据类文件名模式...→ medium（走审查闸→用户闸）；其余读
    → low"). Checked last (after the workspace-boundary checks above), since
    a path that already escapes the workspace is `high` regardless of its
    name, and a path inside the workspace has nothing else left to check."""
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
    except (OSError, RuntimeError):
        # Round 2 review finding #6: `Path.expanduser()` raises `RuntimeError`
        # (not `OSError`) for an unresolvable `~user` form (Python 3.12,
        # `Path('~nosuchuser/x').expanduser()` -> "Could not determine home
        # directory"). This branch pre-dates that finding for write_file/patch
        # (main already had it); Issue #13 routing read_file/search_files
        # through this same function widened its trigger surface without
        # widening the `except` to match — an uncaught `RuntimeError` here
        # used to escape `classify()` entirely and turn a permission request
        # into a JSON-RPC `-32603` error instead of a risk verdict.
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
    if not _is_relative_to(resolved, root):
        return _high(
            f"path {path!r} escapes the project workspace ({cwd!r}) — FR07's 越界路径走权限闸"
        )
    if check_credential_filename and defaults.is_credential_filename(resolved.name):
        return _medium(
            f"path {path!r} has a credential-shaped filename (id_*, *.pem, .env, "
            "*token*, *secret*, known_hosts, …) — controller ruling R-I1; stop and "
            "confirm even though it isn't under a denylisted directory"
        )
    return _low(f"path {path!r} is inside the project workspace")


def _classify_write(path: Any, *, cwd: str | None) -> Risk:
    return _classify_path_access(path, cwd=cwd, verb="write-family")


def _classify_read(
    path: Any, *, cwd: str | None, directory_scope: bool, check_credential_filename: bool = False
) -> Risk:
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
        path,
        cwd=cwd,
        verb="read-family",
        escalate_too_wide_workspace=directory_scope,
        check_credential_filename=check_credential_filename,
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
    # Round 2 review findings #2/#5 (important): `_terminal_token_sensitive_
    # root` above only catches a command that names a sensitive root
    # DIRECTLY (`cat ~/.ssh/id_rsa`) — it has no way to see the REVERSE case,
    # a command that recursively walks a directory which merely CONTAINS a
    # sensitive root (`grep -rn PRIVATE ~`, `find ~ -name 'id_*'`, `tar cf
    # x.tar .` from the default $HOME-cwd Project). `_classify_read`'s
    # `directory_scope` already applies the identical reasoning to
    # `search_files`; this mirrors it for `terminal`, floored at `medium`
    # (not `high`) because — same as that floor's own reasoning — this is a
    # structural "the scope is too broad to bound" signal, not proof any
    # sensitive file was actually read.
    ancestor_root = _terminal_recursive_ancestor_of_sensitive_root(
        tokens, programs=programs, cwd=cwd
    )
    if ancestor_root is not None:
        return _medium(
            f"command recursively traverses a directory that contains a default-deny "
            f"sensitive location ({ancestor_root}) — PRD 12.1 G15"
        )
    mut = programs & _MUTATING_PROGRAMS
    if mut:
        return _medium(f"command mutates state: {', '.join(sorted(mut))}")
    if "git" in programs and programs & _GIT_MUTATING_SUBCOMMANDS:
        return _medium("git command rewrites history or remote state")
    return _low(
        "plain terminal command with no network-egress or state-mutation signal "
        "(controller ruling R10, round 6)"
    )


_HOME_VAR_MARKERS = ("$HOME", "${HOME}")


def _substitute_home_env_var(tok: str) -> str:
    """Round 2 review finding #4 (critical): an UNQUOTED `$HOME`/`${HOME}` in
    a terminal command is not one of `_transparency._OPAQUE_SUBSTRINGS` (only
    a `$`/backtick seen INSIDE a quote trips that check, via
    `_has_quoted_dollar_or_backtick` — a bare `$HOME` outside any quote isn't
    a quoted dollar), so `cat $HOME/.ssh/id_rsa` tokenizes as `plain`. Without
    this substitution the literal token text `'$HOME/.ssh/id_rsa'` looks
    relative (no leading `/`) and gets joined onto `cwd`
    (`$HOME/$HOME/.ssh/id_rsa`), which never resolves under a real sensitive
    root — the exact silent bypass the review's repro demonstrated. This
    substitutes only the two spellings the repro used, deliberately NOT a
    general shell-variable expander (review's own suggested fix: "不要实现
    通用变量展开"); any other `$VAR` is left untouched and simply isn't
    recognized as a sensitive-path reference by this function, same as
    before this fix."""
    if not any(marker in tok for marker in _HOME_VAR_MARKERS):
        return tok
    try:
        home = str(Path.home())
    except RuntimeError:
        return tok
    return tok.replace("${HOME}", home).replace("$HOME", home)


def _could_be_path_token(tok: str) -> bool:
    """Round 2 review finding #7's "先做廉价筛选" — every default-deny root
    is either home-relative or an absolute filesystem path, so a token can
    only ever resolve under (or contain) one if it has a path separator,
    starts with `.`/`~`, or references `$HOME`/`${HOME}` (the one variable
    `_substitute_home_env_var` handles). Skips flags (`-r`, `-rn`), program
    names, and bare search patterns/arguments (`PRIVATE`, `ssh-rsa`) before
    they ever reach a `Path`/`resolve()` call — the dominant cost `_terminal_
    token_sensitive_root`'s per-token loop used to pay unconditionally."""
    return "/" in tok or tok.startswith((".", "~")) or any(m in tok for m in _HOME_VAR_MARKERS)


def _resolve_terminal_path_token(tok: str, *, cwd: str | None) -> Path | None:
    """`$HOME`-substitute, `~`-expand, and (for a still-relative token)
    resolve against `cwd` — the one path-resolution routine shared by the
    direct sensitive-root check (`_terminal_token_sensitive_root`) and the
    reverse "does this token's directory CONTAIN a sensitive root" check
    (`_terminal_recursive_ancestor_of_sensitive_root`), mirroring the same
    three-step resolution `_classify_path_access` uses for the file-tool
    equivalent of this same access. `None` means "can't resolve" — a still-
    relative token with no `cwd` available, or an unresolvable `~user` form
    (round 2 review finding #6: `Path.expanduser()` raises `RuntimeError`,
    not `OSError`, for that — see `_classify_path_access`'s matching fix) —
    left unresolved rather than raised, same "解析不了就 continue" contract
    this function's callers already documented."""
    tok = _substitute_home_env_var(tok)
    try:
        candidate = Path(tok).expanduser()
    except (OSError, RuntimeError, ValueError):
        return None
    if not candidate.is_absolute():
        if cwd is None:
            return None
        try:
            candidate = Path(cwd).expanduser() / candidate
        except (OSError, RuntimeError, ValueError):
            return None
    try:
        return candidate.resolve(strict=False)
    except OSError:
        return None


def _terminal_token_sensitive_root(tokens: list[str], *, cwd: str | None) -> Path | None:
    """Does any token in a `terminal` command's flat token stream, once
    `~`/`$HOME`-expanded (and, for a still-relative token, resolved against
    `cwd`), resolve under a `permissions/defaults.py` default-deny root?

    Round 1 fix (2026-09-19, review findings #1/#6): this used to `continue`
    past any token that was still relative after `~`-expansion, reasoning
    that the realistic G15 shape always uses `~` or an already-absolute
    path — that was wrong on the exact case that matters most: the DEFAULT
    Project's `cwd` IS `$HOME` (01-w2-interfaces.md §2.2's documented
    placeholder), so `cat .ssh/id_rsa` run from it targets the identical
    file `cat ~/.ssh/id_rsa` does, with neither a `~` nor a leading `/` in
    the command text to catch. A token that's still relative with no `cwd`
    available is left unresolved (skipped, as before) — with no workspace
    root to resolve against there's nothing more specific to check here than
    the plain-command `low` default already gives it.

    Round 2 fix (2026-09-19, review finding #4): also `$HOME`/`${HOME}`-
    substitutes before resolving (see `_substitute_home_env_var`) — an
    unquoted `$HOME` doesn't trip `_transparency`'s opaque check, so
    `cat $HOME/.ssh/id_rsa` used to resolve as a nonsense relative path and
    never match."""
    for tok in tokens:
        if not _could_be_path_token(tok):
            continue
        resolved = _resolve_terminal_path_token(tok, cwd=cwd)
        if resolved is None:
            continue
        root = defaults.matches(resolved)
        if root is not None:
            return root
    return None


def _terminal_recursive_ancestor_of_sensitive_root(
    tokens: list[str], *, programs: set[str], cwd: str | None
) -> Path | None:
    """Round 2 review findings #2/#5 (important): the REVERSE of
    `_terminal_token_sensitive_root` — does any token resolve to a directory
    that CONTAINS a default-deny sensitive root, for a command whose
    recursive/traversal semantics mean it would actually walk into it?
    Mirrors `_classify_read`'s `directory_scope` reasoning for
    `search_files` (see that function's docstring for why a directory
    argument needs this check and a single-file `read_file` path doesn't).

    "Recursive/traversal semantics" is either an explicit `-r`/`-R`/
    `--recursive`-shaped flag anywhere in the token stream (any program —
    `grep -rn`, `cp -r`, ...; reuses `_hard_deny._is_recursive_flag`, the
    same detector `rm -rf`'s hard-deny check uses) OR the command's first
    program being one that recurses into a directory argument with NO flag
    needed at all (`find`, `tar`, `zip` — `defaults.RECURSIVE_TRAVERSAL_
    PROGRAMS`). A command with neither signal skips the (slightly more
    expensive, since it can't reuse the cheap direct-match prefilter's
    program-name exclusion) resolve loop entirely."""
    if not (programs & defaults.RECURSIVE_TRAVERSAL_PROGRAMS) and not any(
        _hard_deny._is_recursive_flag(t) for t in tokens
    ):
        return None
    for tok in tokens:
        if not _could_be_path_token(tok):
            continue
        resolved = _resolve_terminal_path_token(tok, cwd=cwd)
        if resolved is None:
            continue
        root = defaults.ancestor_root_under(resolved)
        if root is not None:
            return root
    return None


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
        return _classify_read(
            path,
            cwd=cwd,
            directory_scope=tool_name == "search_files",
            # Controller ruling R-I1 (round 3): the credential-filename tier
            # is `read_file`-only — `search_files`'s `path` names a directory
            # ROOT being recursively walked, not the single file being
            # disclosed, so a filename-pattern match on it wouldn't mean
            # what it means for `read_file`.
            check_credential_filename=tool_name == "read_file",
        )
    if tool_name in ("write_file", "patch"):
        return _classify_write(args.get("path"), cwd=cwd)
    if tool_name == "terminal":
        return _classify_terminal(args, cwd=cwd)
    if tool_name == "browser_navigate":
        return _classify_browser_navigate(args)
    if tool_name == "browser_console":
        return _classify_browser_console(args)
    if tool_name in _BROWSER_ALWAYS_HIGH:
        return _high(f"{tool_name} is a constant user-gate tool (00-foundation.md §9.3)")
    if tool_name in _BROWSER_REVIEW_TOOLS:
        return _medium(f"{tool_name} has side effects and needs review")
    # 00-foundation.md §9.3's fail-closed catch-all ("上面几档没有点名的任何工具
    # ...一律用户闸") for anything this function doesn't specifically recognize —
    # `medium` (not `low`) so it never auto-bypasses in auto mode.
    return _medium(f"no specific risk rule for tool {tool_name!r}; defaulting to reviewed")
