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
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

RiskLevel = Literal["low", "medium", "high"]

# Tools whose read-only-ness is knowable from the name alone (no args needed) —
# 00-foundation.md §9.2's rule-gate-eligible browser tools plus Hermes's two
# read-only file tools. NOTE: these are classified `low` here for cases where a
# caller routes them through the review gate anyway (e.g. review runs before a
# rule-gate allow-rule would have short-circuited it) — the rule gate
# (`kernel/plugin/jones_gate`) already allows the browser subset of these
# outright per 00-foundation.md §9.2, so in practice only `read_file`/
# `search_files` are likely to actually reach this function via that name set.
_READ_ONLY_LOW = frozenset(
    {"read_file", "search_files", "browser_navigate", "browser_snapshot",
     "browser_take_screenshot", "browser_wait_for"}
)

# 02-w3-interfaces.md §1.1: "终端命令是否含网络外发 curl|wget|ssh|scp".
_NETWORK_EGRESS_PROGRAMS = frozenset({"curl", "wget", "ssh", "scp"})

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


def _classify_write(path: Any, *, cwd: str | None) -> Risk:
    if not isinstance(path, str) or not path:
        return _medium("write-family tool call with no resolvable path")
    if cwd is None:
        return _medium(f"path {path!r} not classified: this session's workspace root is unknown")
    try:
        resolved = Path(path).expanduser()
        if not resolved.is_absolute():
            resolved = Path(cwd).expanduser() / resolved
        resolved = resolved.resolve(strict=False)
        root = Path(cwd).expanduser().resolve(strict=False)
    except OSError:
        return _high(f"could not resolve path {path!r} to classify it")
    if _is_relative_to(resolved, root):
        return _low(f"path {path!r} is inside the project workspace")
    return _high(f"path {path!r} escapes the project workspace ({cwd!r})")


def _classify_terminal(args: dict[str, Any]) -> Risk:
    command = args.get("command")
    if not isinstance(command, str) or not command:
        return _high("terminal call with no command text to analyze")
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return _high("could not parse this command for risk analysis")
    programs = {Path(t).name for t in tokens if t and not t.startswith("-")}
    hit = programs & _NETWORK_EGRESS_PROGRAMS
    if hit:
        return _high(f"command includes a network-egress tool: {', '.join(sorted(hit))}")
    return _medium("terminal command, no specific risk signal matched")


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
    if tool_name in ("write_file", "patch"):
        return _classify_write(args.get("path"), cwd=cwd)
    if tool_name == "terminal":
        return _classify_terminal(args)
    if tool_name == "browser_evaluate":
        return _classify_browser_evaluate(args)
    if tool_name in _BROWSER_REVIEW_TOOLS:
        return _medium(f"{tool_name} has side effects and needs review")
    # 00-foundation.md §9.2's fail-closed catch-all ("上面三档没有点名的任何工具
    # ...一律用户闸") for anything this function doesn't specifically recognize —
    # `medium` (not `low`) so it never auto-bypasses in auto mode.
    return _medium(f"no specific risk rule for tool {tool_name!r}; defaulting to reviewed")
