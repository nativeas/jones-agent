"""Single source of truth for "is `tool_name` allowed under this Agent's
`tool_allowlist`" — N15 (PRD 12.2) + the `mcp:<server>` whole-server
placeholder convention (Issue #17 §2).

## Why this lives here, not in `daemon/src/jones_daemon/capabilities/`

Controller ruling R-H2 (round-2 review, 2026-09-19) requires ONE function
both `capabilities/registry.py` (the transparency page's "expected enabled"
computation, runs in the daemon's own process) and `kernel/plugin/jones_gate/
__init__.py::_decide` (the real enforcement gate, runs INSIDE the worker
subprocess, in Hermes's Python environment) call — so the page can never again
show `enabled` for a tool the gate would actually block, or vice versa (round-1
review findings #1/#7: before this fix, `registry.py` re-implemented this
logic from scratch in a function whose docstring CLAIMED to mirror a
`_decide`-side helper — `_tool_permitted` — that never existed anywhere in
this package; `grep -rn _tool_permitted kernel/plugin/jones_gate/` had zero
hits. The claim was aspirational, not true, and the two sides had already
drifted: an Agent whitelisted with `mcp:<server>` read as `enabled: true` on
the transparency page while the gate, which only ever did a plain `tool_name
not in allowlist` check, still blocked every real call).

`kernel/plugin/jones_gate/__init__.py`'s own module docstring establishes the
constraint this module must honor: that package is distributed BY THE DAEMON
(`workers/manager.py::_prepare_hermes_home` `shutil.copytree`s the whole
directory into each worker's isolated `HERMES_HOME/plugins/jones_gate/`) and
loaded BY HERMES'S plugin manager running inside the worker process — it must
have zero import-time dependency on `jones_daemon` itself, because the
worker's Python environment is Hermes's, not the daemon's. This module (like
its `_rules.py`/`_hard_deny.py`/`_config.py`/`_review_payload.py` siblings)
is therefore stdlib-only and lives inside the `jones_gate` package so it ships
with every copy. `capabilities/policy.py` (daemon-side) re-exports this
module's `tool_allowed`/`BUILTIN_TOOLS` for callers that only ever run in the
daemon's own process and would rather not spell out the `kernel.plugin.
jones_gate` import path — same precedent `capabilities/registry.py` already
set by importing `_rules` the same way (`from jones_daemon.kernel.plugin.
jones_gate import _rules`), which works because the DAEMON's own venv can
import `jones_gate` as an ordinary package for testing/reuse even though the
WORKER's copy of it can never import back out to `jones_daemon`.

## The policy itself (03-w4-interfaces.md's N15 + controller ruling R-H2)

- `tool_name in allowlist` (exact match) always enables it, for any source.
- A **builtin** tool (present in `BUILTIN_TOOLS`, the `"hermes-acp"` toolset's
  real expansion — see that constant's own docstring) additionally reads as
  allowed when `allowlist` is EMPTY ("unrestricted" — `kernel/plugin/
  jones_gate/__init__.py`'s own long-standing convention, unchanged by this
  round).
- A **third-party** tool (an MCP tool — recognized by the `mcp__<server>__`
  name prefix Hermes itself uses, `tools/mcp_tool_schema.py::
  MCP_TOOL_NAME_PREFIX`, source-verified — or a Skill tool, i.e. anything NOT
  in `BUILTIN_TOOLS`) is NEVER auto-allowed by an empty allowlist — N15's
  "第三方工具默认不进白名单，需用户显式启用" holds even for an Agent with no
  other tool restrictions configured. An MCP tool additionally reads as
  allowed when the allowlist contains the whole-server placeholder
  `mcp:<server>` (`_mcp_entry_names`'s convention in `capabilities/
  registry.py` — used both before a live worker has ever reported real
  per-tool names, and as a deliberately coarser "allow everything this server
  ever exposes" grant once it has).
"""

from __future__ import annotations

MCP_TOOL_NAME_PREFIX = "mcp__"
MCP_PLACEHOLDER_PREFIX = "mcp:"

# The `"hermes-acp"` toolset's real expansion (`toolsets.py::TOOLSETS["hermes-acp"]`,
# `_CODING_TOOLS` minus `"clarify"` — `_CODING_TOOLS` itself is `_core_without(
# "image_generate", "text_to_speech", "cronjob_manage", "computer_use", *_HA_TOOLS,
# kanban=False)`), hand-copied from the installed `hermes-agent` checkout's
# `toolsets.py` (source-verified against `ee4452991d17534aa561f31ee55596d082aa94e7`;
# see `capabilities/registry.py`'s module docstring for the full source-check
# writeup — order matches `_HERMES_CORE_TOOLS` there). The entire universe of
# builtin tool names an ACP-launched Hermes worker can ever expose, independent
# of anything Jones configures — this is what makes "is this name a builtin"
# decidable from the name alone, with no daemon-side state.
BUILTIN_TOOLS: tuple[str, ...] = (
    "web_search", "web_extract",
    "terminal", "process_manage",
    "read_file", "write_file", "patch", "search_files",
    "vision_analyze",
    "skills_list", "skill_view", "skill_manage",
    "browser_navigate", "browser_snapshot", "browser_click",
    "browser_type", "browser_scroll", "browser_back",
    "browser_press", "browser_get_images",
    "browser_vision", "browser_console", "browser_cdp", "browser_dialog",
    "browser_vault_list", "browser_vault_unlock", "browser_vault_fill",
    "browser_vault_save_login", "browser_vault_enter_code",
    "browser_exec",
    "todo_list", "memory",
    "session_search",
    "execute_code", "delegate_task",
    "manage_connections",
)

# Source-verified against `ee4452991d17534aa561f31ee55596d082aa94e7`'s
# `check_fn` for each tool below (grep `check_fn=` under `tools/*.py`, then
# read each function): these are the `BUILTIN_TOOLS` names whose presence in
# the model's REAL assembled schema depends on a Hermes subsystem Jones does
# not (yet, or ever, for v1) configure — a browser profile (`browser_cdp`/
# `browser_dialog`/the five `browser_vault_*` tools all gate on
# `check_browser_requirements()`, which is J/#15-16's browser capability, not
# yet wired on this branch), the connector gateway (`manage_connections` ->
# `tools/connections_tool.py::_connectors_available()`), a configured web
# search backend/API key (`web_search`/`web_extract` -> `check_web_api_key()`
# — see 03-w4-interfaces.md §4's "深度调研" note that this needs a Key via B's
# vault/provider mechanism), or a vision-capable model/aux client
# (`vision_analyze` -> `check_vision_requirements()`). Every other
# `BUILTIN_TOOLS` name's `check_fn` (file/todo/skills/session_search/
# terminal/process_manage/memory/execute_code/delegate_task) is either
# unconditional or defaults true with nothing Jones-specific to configure
# (`memory`'s `get_builtin_memory_store_flags()` defaults both flags true;
# `execute_code`'s `SANDBOX_AVAILABLE = True` constant plus a non-container
# `env_type` short-circuits its check to true; `terminal`'s default
# `TERMINAL_ENV=local` checker) — their absence from a real worker's assembled
# schema would be a genuine anomaly, not routine.
#
# Used by `capabilities/registry.py::reconcile()` (controller ruling R-H1) so
# a completely ordinary Jones deployment — no browser profile, no connectors,
# no search key configured yet — doesn't manufacture G21 `daemon.error` noise
# for tools that were never realistically going to be in the schema to begin
# with; the transparency page still shows them `actually_loaded: false`
# (diagnostic honesty preserved), they just don't count toward `drift`.
CONDITIONAL_BUILTIN_TOOLS: frozenset[str] = frozenset(
    {
        "web_search", "web_extract", "vision_analyze", "manage_connections",
        "browser_cdp", "browser_dialog",
        "browser_vault_list", "browser_vault_unlock", "browser_vault_fill",
        "browser_vault_save_login", "browser_vault_enter_code",
    }
)

# Hermes's own Tool Search bridge (`tools/tool_search.py::assemble_tool_defs`,
# activated by `should_activate()` whenever ANY "deferrable" — i.e. non-core —
# tool is enabled, which is true for EVERY session with even one MCP server
# configured): once active, the model's real schema swaps every deferrable
# tool's own definition out for exactly these three bridge tools. Registered
# here as a known-explainable name set (controller ruling R-H1) as a defense-
# in-depth measure — `_tools_snapshot.py`'s `on_session_start` hook calls
# `get_tool_definitions(..., skip_tool_search_assembly=True)` precisely so
# these names should never actually show up in a real `jones_tools.json`
# snapshot at all (review round-2 finding #6's real fix) — but a snapshot
# written by a differently-configured or older worker must not manufacture
# three rows of `unknown_tool` drift for names this registry can, in fact,
# explain on sight.
TOOL_SEARCH_BRIDGE_NAMES: frozenset[str] = frozenset({"tool_search", "tool_describe", "tool_call"})


def mcp_server_for(tool_name: str) -> str | None:
    """The server name a real `mcp__<server>__<tool>` name belongs to, or
    `None` if `tool_name` isn't MCP-shaped. Shared by both the policy check
    below and `capabilities/registry.py`'s reconciliation so the two never
    disagree about what a given loaded tool name means."""
    if not tool_name.startswith(MCP_TOOL_NAME_PREFIX):
        return None
    rest = tool_name[len(MCP_TOOL_NAME_PREFIX):]
    return rest.split("__", 1)[0] if rest else None


def tool_allowed(tool_name: str, allowlist: list[str] | None) -> bool:
    """N15 + `mcp:<server>` membership test — see module docstring. The ONE
    function both `capabilities/registry.py::expected_capabilities` (what the
    transparency page calls "enabled") and `kernel/plugin/jones_gate/
    __init__.py::_decide` (what actually gates a real tool call) call, so
    those two answers can never again independently drift apart."""
    allowlist = allowlist or []
    if tool_name in allowlist:
        return True
    if tool_name in BUILTIN_TOOLS:
        # Builtins: empty allowlist = unrestricted. A non-empty one already
        # failed the exact-match check above, so there's nothing further to
        # try for a builtin name.
        return not allowlist
    server = mcp_server_for(tool_name)
    if server is not None:
        return f"{MCP_PLACEHOLDER_PREFIX}{server}" in allowlist
    # Skill tool (or any other unrecognized name): third-party, exact-name
    # membership only — no server-level placeholder convention exists for
    # Skills (each Skill is not "a server" the way an MCP connection is).
    return False
