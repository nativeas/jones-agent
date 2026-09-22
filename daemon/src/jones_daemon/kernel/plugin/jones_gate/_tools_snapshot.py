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

## The file's write is NOT best-effort — the *tool list inside it* is (round-1
## review finding #4)

`workers/manager.py::_wait_for_tools_snapshot` (Issue #38 ruling 1) fail-closed
gates worker delivery purely on this file's EXISTENCE — proof this hook ran at
all, i.e. that `jones_gate` loaded. That's a different claim from "the tool
list this hook wanted to report is known", and conflating the two used to mean
any unrelated Hermes-side failure (`model_tools`'s import breaking, a
`check_fn` raising, a `get_tool_definitions` signature change) silently
produced the exact same symptom as `jones_gate` never having loaded at all —
every session on the machine refusing to start, with a startup-self-check
error message pointing at the wrong cause. So: once `HERMES_HOME` is known,
this hook always writes the file — `tools` is `null` (with
`tools_unavailable_reason` explaining why) only when `model_tools`/`tools.
mcp_tool_discovery` aren't importable or `get_tool_definitions` itself raises
(the daemon's own test suite drives this package against a FAKE ACP agent that
never loads real Hermes plugins at all — see `docs/DEV.md`'s `hermes-agent`
optional-dependency note). `capabilities/methods.py::_read_jones_tools`'s
existing `isinstance(raw_tools, list)` guard already treats a `null` `tools`
field exactly like a missing file (falls back to `actual_tools = None`), so
`capabilities/registry.py::reconcile()` still reports "actual assembly not yet
known" — see that function's docstring — unchanged by this. Only a missing
`HERMES_HOME`, or the write itself failing (disk full, permissions), skips
writing the file at all now.

## `skip_tool_search_assembly=True` (review round-2 finding #6)

Hermes's own Tool Search bridge (`tools/tool_search.py::assemble_tool_defs`,
wired into `model_tools.get_tool_definitions` by default) activates whenever
ANY "deferrable" (non-core) tool is enabled — true for every session with even
one MCP server configured, i.e. this Issue's entire point. Once active, the
model's REAL schema swaps every deferrable tool's own definition out for three
generic bridge tools (`tool_search`/`tool_describe`/`tool_call`) — so calling
`get_tool_definitions` the naive way here would make a snapshot that NEVER
contains a real `mcp__<server>__<tool>` name for any session with an MCP
server attached, which is exactly backwards for a hook whose entire purpose is
proving what got REALLY assembled (G21). `get_tool_definitions` takes a
`skip_tool_search_assembly: bool` parameter for precisely this
(`model_tools.py`, source-verified against `ee4452991d17534aa561f31ee55596d082aa94e7`
— Hermes's own MCP bridge-dispatch code path uses the same flag internally,
`model_tools.py:709`, to get the real per-tool catalog for its own purposes)
— passed `True` below to get the raw, unfolded tool list instead.

## `mcp_discovery_complete` (review round-2 finding #3, and its round-2
## follow-up: join BEFORE reading names, not after)

MCP discovery is asynchronous (`acp_adapter/entry.py` starts it on a
background thread at worker startup; `ensure_mcp_discovery_before_agent_build`
only bounds the FIRST turn's wait to ~1.5s before giving up and letting the
turn proceed). This hook's own snapshot write happens once, at the start of
the very first real Turn (the startup self-check probe,
`workers/manager.py::_startup_self_check`) — a slow MCP server can easily
still be mid-handshake at that exact moment, in which case
`get_registered_mcp_server_names()` legitimately, correctly returns a set that
doesn't (yet) include it. Without a signal distinguishing "discovery hadn't
finished when this was written" from "discovery finished and genuinely found
nothing", `capabilities/methods.py` would have no way to tell "still starting
up" apart from "confirmed dead" — and `McpServerState`'s own docstring is
explicit that only the latter may assert `hidden_reason="mcp_server_down"`.
`hermes_cli.mcp_startup.mcp_discovery_in_flight()` (a live, public,
non-underscore query Hermes's own late-refresh scheduler,
`acp_adapter/server.py::_schedule_mcp_late_refresh`, already calls for the
same reason) answers exactly this — recorded here as `mcp_discovery_complete:
bool` in the written payload. Best-effort, same as everything else in this
hook: unimportable or erroring `hermes_cli.mcp_startup` records `False` (never
`True`) — "can't tell" must never be read downstream as "confirmed done",
since that's the one claim `McpServerState` requires real evidence for.

**Ordering** (round-2 review finding #2): `join_mcp_discovery(timeout=1.0)`
runs FIRST, before either `mcp_names` or `names` is read even once. The
original code computed `names` (which internally read
`get_registered_mcp_server_names()` to build `enabled_toolsets`), THEN read
`mcp_names` a second time for the payload, and only THEN joined — so a probe
Turn that lands while discovery is still in flight but finishes inside that
same 1-second join produced the worst combination: `mcp_servers=[]`/`tools`
missing the late server's tools (both computed from the stale pre-join read)
paired with `mcp_discovery_complete=True` (computed from the post-join state)
— a snapshot that looks complete but isn't, which is exactly the false
"confirmed down" `capabilities/methods.py` §"mcp_discovery_complete" section
above says must never happen. `get_registered_mcp_server_names()` is now
called exactly ONCE, after the join, and that one result is reused for both
`enabled_toolsets`/`_compute_tool_names` and the `mcp_servers` field — all
three payload fields (`tools`, `mcp_servers`, `mcp_discovery_complete`) are
now evaluated from the same point in time, after discovery has had its one
chance to finish.
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


def _registered_mcp_server_names() -> list[str]:
    """The single read point for `get_registered_mcp_server_names()` — see
    module docstring's "Ordering" section: callers must call this exactly
    once, AFTER `_mcp_discovery_complete()`'s join, and reuse the one result
    for every field of the payload so `tools`/`mcp_servers`/
    `mcp_discovery_complete` all describe the same point in time."""
    try:
        from tools.mcp_tool_discovery import get_registered_mcp_server_names

        return sorted(get_registered_mcp_server_names())
    except Exception:
        return []


def _compute_tool_names(mcp_names: list[str]) -> tuple[list[str] | None, str | None]:
    """Returns `(names, unavailable_reason)` — exactly one is non-`None`. Round-1
    review finding #4: the reason travels with the result (instead of being
    discarded the way a bare `except: return None` used to) so the payload can
    say *why* the tool list is missing rather than looking identical to
    `jones_gate` never having loaded at all — see module docstring."""
    enabled_toolsets = list(
        dict.fromkeys(["hermes-acp", *(f"mcp-{name}" for name in mcp_names if name)])
    )

    try:
        import model_tools

        tools = model_tools.get_tool_definitions(
            enabled_toolsets=enabled_toolsets, quiet_mode=True,
            # Review round-2 finding #6 — see module docstring's
            # "skip_tool_search_assembly=True" section: without this, every
            # MCP/deferrable tool's real name is folded away behind the
            # `tool_search`/`tool_describe`/`tool_call` bridge the moment any
            # MCP server is configured, defeating this hook's entire purpose.
            skip_tool_search_assembly=True,
        )
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"

    names = {
        t.get("function", {}).get("name")
        for t in (tools or [])
        if isinstance(t, dict) and isinstance(t.get("function"), dict)
    }
    return sorted(n for n in names if isinstance(n, str) and n), None


def _mcp_discovery_complete() -> bool:
    """`True` only when we have real evidence MCP discovery has actually
    finished — see module docstring's "mcp_discovery_complete" section.
    Never raises; `False` (never a guessed `True`) on any failure to check."""
    try:
        from hermes_cli.mcp_startup import join_mcp_discovery, mcp_discovery_in_flight
    except Exception:
        return False
    try:
        # A short, bounded wait: this hook already runs at the start of the
        # first real Turn (after `ensure_mcp_discovery_before_agent_build`'s
        # own ~1.5s bound has already had its chance), so a slow server is
        # more likely done than not by now — this just closes a small race
        # rather than committing to a long block on a hook Hermes's own
        # `invoke_hook` never awaits anyway (fire-and-forget from the caller's
        # perspective; a slow join here only delays THIS hook's own return).
        join_mcp_discovery(timeout=1.0)
    except Exception:
        pass
    try:
        return not mcp_discovery_in_flight()
    except Exception:
        return False


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
        # Round-2 review finding #2: join FIRST, read names exactly once,
        # AFTER the join — see module docstring's "Ordering" section. Reusing
        # one `mcp_names` read for both `_compute_tool_names` and the
        # `mcp_servers` field keeps all three payload fields consistent with
        # each other (no field computed from a pre-join snapshot next to one
        # computed post-join).
        discovery_complete = _mcp_discovery_complete()
        mcp_names = _registered_mcp_server_names()
        names, unavailable_reason = _compute_tool_names(mcp_names)
        payload = {
            "session_id": session_id,
            # round-1 review finding #4: `tools` is `null` (never a raised
            # exception, never a skipped write) when it couldn't be computed —
            # see module docstring's "The file's write is NOT best-effort"
            # section for why this file must still be written in that case.
            "tools": names,
            "tools_unavailable_reason": unavailable_reason,
            "mcp_servers": mcp_names,
            "mcp_discovery_complete": discovery_complete,
            "written_at": time.time(),
        }
        target = home / _FILE_NAME
        tmp = home / f"{_FILE_NAME}.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)
    except Exception:
        return
