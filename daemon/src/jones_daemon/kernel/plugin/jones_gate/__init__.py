"""Jones `pre_tool_call` gate — W2 skeleton (docs/design/00-foundation.md §7/§8.2).

This file is distributed BY THE DAEMON, not imported by it: `workers/manager.py`
copies this whole directory into each worker's isolated `HERMES_HOME/plugins/
jones_gate/` before starting the worker subprocess, and Hermes's own plugin
manager (`hermes_cli.plugins`, running *inside the worker process*) loads it. It
must therefore have zero dependency on `jones_daemon` — the worker's Python
environment is Hermes's, not the daemon's.

Two verdicts only, per the W2 scope in 01-w2-interfaces.md §2:

1. The reserved startup-probe tool name is always blocked (fail-closed) — this is
   what `WorkerManager`'s startup self-check asserts against to prove this plugin
   is actually loaded (00-foundation.md §8.1's "启动自检"): a real tool result
   coming back unblocked would be indistinguishable from `HERMES_SAFE_MODE`
   silently having skipped plugin discovery altogether.
2. Everything else is approved immediately. This is not "no gate" — it hands the
   decision to Hermes's own `tools.approval.request_tool_approval()`, which (when
   running under `acp_adapter/`) turns into a real ACP `session/request_permission`
   round trip back to the daemon (see docs/spikes/01-hermes-hook.md and
   00-foundation.md §7 "组合结论"). The plugin callback returns immediately and
   never itself waits — blocking in here would count against
   `plugins.hook_callback_timeout` and fail the whole callback closed for every
   later tool call too (00-foundation.md §7 "一个必须记住的坑").

Rule-gate / review-gate logic (locally-decided blocks, risk scoring) is W3's FR05
work, not this file's.
"""
from __future__ import annotations

from typing import Any

# Reserved tool name the daemon uses to prove this plugin is loaded (never a real
# tool a worker would otherwise dispatch). Kept in sync by hand with
# `jones_daemon.workers.manager.PROBE_TOOL_NAME` — this file has no import on the
# daemon package to copy it from (see module docstring).
PROBE_TOOL_NAME = "jones.__probe__"


def _on_pre_tool_call(
    tool_name: str = "", args: dict | None = None, tool_call_id: str = "", **kwargs: Any
) -> dict | None:
    if tool_name == PROBE_TOOL_NAME:
        return {
            "action": "block",
            "message": "jones_gate startup self-check: this tool is reserved and never runs.",
        }
    return {
        "action": "approve",
        "message": "jones_gate: forwarded to Hermes's own approval wait",
        "rule_key": tool_name,
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
