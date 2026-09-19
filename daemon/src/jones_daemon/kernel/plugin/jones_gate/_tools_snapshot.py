"""`on_session_start` hook — writes `<HERMES_HOME>/jones_tools.json`, the real,
actually-assembled tool set for this session (Issue #17 §2, #19 daemon 侧;
03-w4-interfaces.md §2's "实际装配集合"; G21).

Stdlib-only at import time (this package's zero-`jones_daemon`-dependency rule,
see `__init__.py`'s module docstring) — everything Hermes-specific is imported
lazily, inside the hook function, and any failure is swallowed (never raised
into Hermes's `invoke_hook`, which would only log a warning anyway — see below).

## Why `on_session_start`'s own kwargs aren't enough

Source check (`agent/conversation_loop.py`, the only real call site for this
hook on the ACP path — `acp_adapter/` itself never calls it directly, it's
reached through `AIAgent`'s normal turn-processing code shared by every
platform) shows the hook fires with exactly `session_id`, `model`, `platform`
— no tool list. The plugin has no reference to the `AIAgent` instance handling
this session either (the hook dispatch doesn't pass it).

## What this does instead: a fresh, source-verified recompute

`agent.tools`/`agent.valid_tool_names` were already computed once, at agent
construction time, by `model_tools.get_tool_definitions(enabled_toolsets=...)`
(`agent/agent_init.py::_load_tools`) — a PURE function of `enabled_toolsets`/
`disabled_toolsets`, not agent-instance state. This hook reproduces that exact
computation instead of trying to reach the agent object:
  - `tools.mcp_tool_discovery.get_registered_mcp_server_names()` — which MCP
    servers are ACTUALLY connected right now (a live, public, non-underscore
    API — more accurate than what `_make_agent` saw at construction time, since
    MCP discovery is asynchronous and can still be catching up; this hook fires
    at the START of the first real turn, later than agent construction).
  - the ACP session's `enabled_toolsets` formula, `["hermes-acp"] + [f"mcp-{n}"
    for n in <connected mcp server names>]` — mirrored inline rather than
    imported from `acp_adapter/session.py::_expand_acp_enabled_toolsets`
    (leading-underscore, private) to avoid depending on a Hermes-internal name
    that isn't part of any documented API; the formula itself is simple and
    stable (verified against the installed checkout, and against
    `acp_adapter/commands.py::_cmd_tools`'s independent use of the same
    private helper for its own `/tools` slash command, so this isn't a
    one-off reading).
  - `model_tools.get_tool_definitions(enabled_toolsets=...)` — Hermes's own
    public toolset-resolution entrypoint, run fresh, in-process, so it reflects
    real `check_fn` results (HA/computer_use/... availability) for this exact
    worker environment.

Best-effort by design: if `model_tools`/`tools.mcp_tool_discovery` aren't
importable (the daemon's own test suite drives this package against a FAKE ACP
agent that never loads real Hermes plugins at all — see `docs/DEV.md`'s
`hermes-agent` optional-dependency note), this hook simply doesn't write the
file. `capabilities/registry.py::reconcile()` treats a missing `jones_tools.
json` as "actual assembly not yet known" (not as "zero tools", and not as an
error) — see that function's docstring.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

_FILE_NAME = "jones_tools.json"


def _hermes_home() -> Path | None:
    home = os.environ.get("HERMES_HOME")
    return Path(home) if home else None


def _compute_tool_names() -> list[str] | None:
    try:
        from tools.mcp_tool_discovery import get_registered_mcp_server_names

        mcp_names = sorted(get_registered_mcp_server_names())
    except Exception:
        mcp_names = []

    enabled_toolsets = list(
        dict.fromkeys(["hermes-acp", *(f"mcp-{name}" for name in mcp_names if name)])
    )

    try:
        import model_tools

        tools = model_tools.get_tool_definitions(
            enabled_toolsets=enabled_toolsets, quiet_mode=True
        )
    except Exception:
        return None

    names = {
        t.get("function", {}).get("name")
        for t in (tools or [])
        if isinstance(t, dict) and isinstance(t.get("function"), dict)
    }
    return sorted(n for n in names if isinstance(n, str) and n)


def on_session_start(session_id: str = "", **_kwargs: Any) -> None:
    """Public hook entry point registered as `on_session_start` — see module
    docstring. Never raises: any failure here must not affect the real Turn
    (Hermes's own `invoke_hook` already swallows a per-callback exception to a
    warning log, per `hermes_cli/plugins_dispatch.py`, but this package's own
    convention — see `__init__.py`'s `_on_pre_tool_call` — is to never depend
    on the host's safety net for something this non-critical)."""
    try:
        home = _hermes_home()
        if home is None:
            return
        names = _compute_tool_names()
        if names is None:
            return
        try:
            from tools.mcp_tool_discovery import get_registered_mcp_server_names

            mcp_names = sorted(get_registered_mcp_server_names())
        except Exception:
            mcp_names = []
        payload = {
            "session_id": session_id,
            "tools": names,
            "mcp_servers": mcp_names,
            "written_at": time.time(),
        }
        target = home / _FILE_NAME
        tmp = home / f"{_FILE_NAME}.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)
    except Exception:
        return
