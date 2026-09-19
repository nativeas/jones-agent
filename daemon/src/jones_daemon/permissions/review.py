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

# Tools whose read-only-ness is knowable from the name alone (no args needed) —
# 00-foundation.md §9.2's rule-gate-eligible browser tools plus Hermes's two
# read-only file tools. NOTE: these are classified `low` here for cases where a
# caller routes them through the review gate anyway (e.g. review runs before a
# rule-gate allow-rule would have short-circuited it) — the rule gate
# (`kernel/plugin/jones_gate`) already allows the browser subset of these
# outright per 00-foundation.md §9.2, so in practice only `read_file`/
# `search_files` are likely to actually reach this function via that name set.
_READ_ONLY_LOW = frozenset(
    {"read_file", "search_files", "browser_snapshot", "browser_get_images", "browser_vision"}
)

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
        return False
    for rel in _SENSITIVE_HOME_RELATIVE_PATHS:
        sensitive_root = home / rel
        if resolved == sensitive_root or _is_relative_to(resolved, sensitive_root):
            return True
    return False


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


def _classify_write(path: Any, *, cwd: str | None) -> Risk:
    if not isinstance(path, str) or not path:
        return _medium("write-family tool call with no resolvable path")
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
            "a shell rc file, ~/Library/LaunchAgents, ~/.hermes, ~/.claude, …) — never low "
            "risk regardless of the workspace boundary (review finding #9)"
        )

    if cwd is None:
        return _medium(f"path {path!r} not classified: this session's workspace root is unknown")
    try:
        root = Path(cwd).expanduser().resolve(strict=False)
    except OSError:
        return _high(f"could not resolve workspace root {cwd!r} to classify {path!r}")

    if _workspace_root_too_wide(root):
        return _medium(
            f"workspace root {cwd!r} is the user's home directory (or an ancestor of it) — "
            "'inside the workspace' can't be treated as a low-risk boundary here (the "
            "default Project's cwd is $HOME until real Project paths land, review "
            "findings #7/#9)"
        )
    if _is_relative_to(resolved, root):
        return _low(f"path {path!r} is inside the project workspace")
    return _high(f"path {path!r} escapes the project workspace ({cwd!r})")


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


def _classify_terminal(args: dict[str, Any]) -> Risk:
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
    mut = programs & _MUTATING_PROGRAMS
    if mut:
        return _medium(f"command mutates state: {', '.join(sorted(mut))}")
    if "git" in programs and programs & _GIT_MUTATING_SUBCOMMANDS:
        return _medium("git command rewrites history or remote state")
    return _low(
        "plain terminal command with no network-egress or state-mutation signal "
        "(controller ruling R10, round 6)"
    )


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
    if tool_name in ("write_file", "patch"):
        return _classify_write(args.get("path"), cwd=cwd)
    if tool_name == "terminal":
        return _classify_terminal(args)
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
