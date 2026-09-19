"""Jones `pre_tool_call` gate — the rule gate half of FR05's three gates
(docs/design/02-w3-interfaces.md §1.1, docs/design/00-foundation.md §7/§8.2).

This file (and its `_hard_deny`/`_config`/`_rules`/`_review_payload`
sibling modules) is distributed BY THE DAEMON, not imported by it:
`workers/manager.py` copies this whole directory into each worker's isolated
`HERMES_HOME/plugins/jones_gate/` before starting the worker subprocess, and
Hermes's own plugin manager (`hermes_cli.plugins`, running *inside the
worker process*) loads it. It must therefore have zero dependency on
`jones_daemon` — the worker's Python environment is Hermes's, not the
daemon's. (Its logic IS still exercised by `uv run pytest` in the daemon's
own venv, though — nothing stops importing `jones_daemon.kernel.plugin.
jones_gate` directly as an ordinary package for testing; "zero dependency"
only means this code never imports *other* `jones_daemon` modules from
inside itself.)

## What runs here vs. what doesn't (contract §1.1's "①")

Every verdict below is either `block` (a real pre-execution veto — the
message becomes the tool's own result, the tool never runs) or `None`
("no objection from us", tool proceeds — either straight to execution, or to
whatever OTHER gate Hermes itself has wired up for this specific tool, see
"the write_file/patch special case" below) or `approve` (escalate to a
*human*, via Hermes's own `tools.approval.request_tool_approval()`, which —
when running under `acp_adapter/` — turns into a real ACP
`session/request_permission` round trip to the daemon; see
docs/spikes/01-hermes-hook.md). This callback never itself waits on
anything: `approve`/`block` return immediately, `plugins.hook_callback_timeout`
never sees the human-approval wait (00-foundation.md §7's "一个必须记住的坑").

## The `write_file`/`patch` special case

`acp_adapter/edit_approval.py` binds a SECOND, independent ACP
`session/request_permission` path for exactly these two tools — one that
carries the real structured `{"tool", "arguments"}` the generic escalation
above (`tools/approval.py::request_tool_approval`) does NOT (that path only
ever forwards a synthetic `"<tool_name> (plugin approval rule)"` label — see
`_review_payload.py`'s docstring for how the generic path's message is
therefore encoded to carry real data instead). This second path is bound
unconditionally by `acp_adapter/server.py` whenever an ACP connection exists,
which is always true for our workers — there is no way to opt out of it from
inside a plugin. Escalating `write_file`/`patch` via `approve` here as well
would ask the human TWICE (00-foundation.md §7's still-open "两套审批逻辑打架"
question, resolved here): once the rule gate has no objection, this plugin
returns `None` for these two tools specifically, letting `edit_approval.py`'s
own gate run instead — same daemon-side `_on_request_permission` handler
(`sessions/service.py`), a different (and strictly more informative)
`rawInput` shape. **Known gap**: because that gate's own auto-approve policy
(`should_auto_approve_edit`) is a separate Hermes session setting this PR
does not wire to Jones's mode/risk, an auto-mode low-risk `write_file` may
still show the user a prompt under real Hermes even though the daemon
side answers it instantly without waiting (see the PR report).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from . import _config, _hard_deny, _review_payload, _rules

# Reserved tool name the daemon uses to prove this plugin is loaded (never a real
# tool a worker would otherwise dispatch). Kept in sync by hand with
# `jones_daemon.workers.manager.PROBE_TOOL_NAME` — this file has no import on the
# daemon package to copy it from (see module docstring).
PROBE_TOOL_NAME = "jones.__probe__"

# `workers/manager.py::_GATE_BLOCK_MARKER` requires this exact string verbatim
# in a blocked probe call's result before calling it "verified blocked" — see
# that module's docstring for why (round-1 W2 review fix, unchanged by W3).
_PROBE_BLOCK_MESSAGE = "jones_gate startup self-check: this tool is reserved and never runs."

# Prefix `sessions/service.py::_handle_tool_call_update` (owned by G/#12, Run
# replay) parses to recognize a rule-gate block and write it into
# `permission_decisions(gate="rule", ...)` — see docs/spikes/01-hermes-hook.md
# "审计写入时序" ②. Matches the convention already established by
# `docs/spikes/hermes_hook_demo.py`. A stable, documented string, not an
# implementation detail: changing it is a contract change (02-w3-interfaces.md).
RULE_GATE_BLOCK_PREFIX = "JONES RULE GATE: "

# `write_file`/`patch` defer to `acp_adapter/edit_approval.py`'s own
# independent ACP approval path instead of being escalated here — see the
# module docstring's "write_file/patch special case".
_EDIT_APPROVAL_TOOLS = frozenset({"write_file", "patch"})

_VALID_MODES = frozenset({"chat", "task", "auto"})


def _block(reason: str) -> dict[str, Any]:
    return {"action": "block", "message": f"{RULE_GATE_BLOCK_PREFIX}{reason}"}


def _hard_deny_verdict(
    tool_name: str, args: dict[str, Any], config: dict[str, Any]
) -> _hard_deny.Verdict:
    user_root = config.get("user_root")
    project_permissions_path = config.get("project_permissions_path")
    if tool_name == "terminal":
        command = args.get("command")
        if isinstance(command, str) and command:
            verdict = _hard_deny.classify_command(command, cwd=config.get("cwd"))
            if verdict.denied:
                return verdict
            touches_protected = bool(user_root) and isinstance(
                user_root, str
            ) and _hard_deny.command_touches_protected_path(
                command, user_root=user_root, project_permissions_path=project_permissions_path,
                cwd=config.get("cwd"),
            )
            if touches_protected:
                return _hard_deny.Verdict(
                    True, "this command touches Jones's own data under ~/.jones/ (PRD 10.4, N10)"
                )
        return _hard_deny.Verdict(False)
    if tool_name in _EDIT_APPROVAL_TOOLS:
        path = args.get("path")
        if isinstance(path, str) and path and isinstance(user_root, str) and user_root:
            if _hard_deny.is_protected_path(
                path, user_root=user_root, project_permissions_path=project_permissions_path
            ):
                return _hard_deny.Verdict(
                    True, "writes/deletes to Jones's own data are never allowed (PRD 10.4, N10)"
                )
        return _hard_deny.Verdict(False)
    return _hard_deny.Verdict(False)


def _on_pre_tool_call(
    tool_name: str = "", args: dict | None = None, tool_call_id: str = "", **kwargs: Any
) -> dict | None:
    args = args if isinstance(args, dict) else {}

    if tool_name == PROBE_TOOL_NAME:
        return {"action": "block", "message": _PROBE_BLOCK_MESSAGE}

    config = _config.load()
    if config is _config.FAIL_CLOSED:
        return _block("gate config unavailable or unreadable; failing closed (PRD 5.5)")

    mode = config.get("mode")
    if mode not in _VALID_MODES:
        return _block("gate config has no valid session mode; failing closed (PRD 5.5)")

    # ① 硬禁止清单 — code constants, never influenced by config content beyond
    # the paths it's checked against (see module/`_hard_deny.py` docstrings).
    hard = _hard_deny_verdict(tool_name, args, config)
    if hard.denied:
        return _block(hard.reason)

    # ① permissions.json 命中
    rule_verdict = _rules.decide(config.get("rules") or [], tool_name, args)
    if rule_verdict == "deny":
        return _block(f"tool call denied by a permissions.json rule (tool={tool_name!r})")

    # 纯对话模式：block 一切工具（N12）— checked after hard-deny/rule-deny (both
    # of which would block anyway) so the block MESSAGE names the strongest
    # reason a caller could act on, and before the "allow 命中" branch so a
    # chat-mode session can never be argued into direct passthrough by a
    # stale/misconfigured allow rule (PRD 9.1's mode boundary is absolute).
    if mode == "chat":
        return _block("chat mode forbids all tool calls, no exceptions (PRD 9.1, N12)")

    if rule_verdict == "allow" and not config.get("rules_degraded"):
        # A degraded permissions.json (existed but failed to parse
        # somewhere — see `_config.py`'s schema comment) can't be trusted
        # for a direct-allow bypass: the merge that produced `rules` may be
        # silently missing a `deny` the unreadable file would have
        # contributed. Falling through to escalation (rather than treating
        # `rule_verdict` as if it were `None`, i.e. still skipping the
        # `deny` branch above) is the fail-closed choice — never widen,
        # only ever narrow what escalates.
        return None  # 直接放行，零 IPC

    allowlist = config.get("tool_allowlist") or []
    if allowlist and tool_name not in allowlist:
        return _block(f"tool {tool_name!r} is not in this session's Agent tool whitelist")

    if tool_name in _EDIT_APPROVAL_TOOLS:
        # See module docstring's "write_file/patch special case" — defer to
        # Hermes's own ACP edit-approval path instead of escalating here.
        return None

    # ② 交给 Hermes 自己的人工等待（真正的 ACP session/request_permission 往返）—
    # this callback returns immediately either way, see module docstring.
    return {
        "action": "approve",
        "message": _review_payload.encode(tool_name, args, mode=mode),
        # Unique per call (never just `tool_name`): Hermes's own session/
        # permanent allowlist cache is keyed on `plugin_rule:{rule_key}`
        # (`tools/approval.py::request_tool_approval`'s `pattern_key`) — a
        # constant `rule_key` would make ANY "allow for session"/"allow
        # always" answer for ONE call to this tool silently auto-approve
        # every FUTURE call to it too, regardless of args (a different
        # command, a different file) — Hermes has no finer-grained cache key
        # to offer here. Jones's own `remember` semantics
        # (02-w3-interfaces.md §1.1) are deliberately NOT built on this cache:
        # `permission.decide(remember=...)` narrows `permissions.json`/the
        # session's rules instead, re-checked by ① on every future call —
        # so making this cache a permanent no-op (fresh key every time) is
        # what keeps the two mechanisms from fighting, not a missing feature.
        "rule_key": f"{tool_name}:{tool_call_id or uuid4().hex}",
    }


def _probe_handler(**_kwargs: Any) -> str:
    """Never actually reached — `_on_pre_tool_call` blocks `PROBE_TOOL_NAME` before
    Hermes ever dispatches to a handler. Defined only because
    `PluginContext.register_tool()` requires one; a probe tool that isn't in the
    model's exposed tool schema at all can't be reliably asked to be called, which
    is the whole point of registering it for real instead of just reserving a name.
    """
    return "jones.__probe__ should never execute"


def register(ctx: Any) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_tool(
        name=PROBE_TOOL_NAME,
        toolset="jones",
        schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_probe_handler,
        description=(
            "Jones daemon startup self-check probe. Always blocked by jones_gate "
            "before it can run."
        ),
    )
