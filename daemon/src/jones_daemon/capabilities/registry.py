"""CapabilityRegistry — pure computation of the *expected* tool-assembly set and
reconciliation against the worker's *actual* one (Issue #17 §2, #19 daemon 侧;
docs/design/03-w4-interfaces.md §2; G21, N15).

## Builtin candidate set — source-verified, corrects the design doc's original plan

03-w4-interfaces.md §2 originally assumed the daemon selects a *named* Hermes
toolset bundle per Agent/Session by writing a `toolsets:` key into the worker's
`config.yaml`. Source check against the installed `hermes-agent` checkout
(`acp_adapter/session.py::SessionManager._make_agent`) shows this isn't how an
ACP-launched Hermes worker picks its tools: `enabled_toolsets` is hard-coded to
`["hermes-acp"] + [f"mcp-{name}" for name in <config.yaml's mcp_servers keys>]`
— there is no `config.yaml` field, and no ACP protocol field (`NewSessionRequest`
carries only `cwd`/`mcpServers`), that lets an ACP client pick a *different*
built-in toolset bundle. `BUILTIN_TOOLS` below is therefore the literal, hand-
verified expansion of the `"hermes-acp"` toolset (`toolsets.py::TOOLSETS`,
`_CODING_TOOLS` minus `clarify`) — the entire universe of builtin tool names an
ACP-launched worker can ever expose, independent of anything Jones configures.
See the PR report's "契约变更" section for what this corrects.

Because there is no config-level lever to drop specific `hermes-acp` tools from
the model's schema, "disable kanban_*/ha_*/computer_use/delegate_task for v1"
(03-w4-interfaces.md §2) is NOT achievable by omission or by a `disabled_toolsets`
field for ACP sessions — those tool names aren't even in `BUILTIN_TOOLS` (kanban_*/
ha_* never appear in `hermes-acp`'s expansion to begin with; `computer_use` is
gated by its own `check_fn` and is not part of `hermes-acp` either — verified
against `toolsets.py`). `delegate_task` IS part of `hermes-acp` and DOES have an
always-true `check_fn` (`tools/delegate_tool.py::check_delegate_requirements`) —
the only lever this registry has over it is the SAME `tool_allowlist` mechanism
`kernel/plugin/jones_gate` already enforces (Agent-level, `agents.tool_allowlist_
json`). This module does not silently bake in a Jones-specific exclusion for it:
whether v1's default Agent excludes `delegate_task` is a product/seeding decision
outside this Issue's file ownership (`agents/store.py` isn't in 03-w4-interfaces.md
§1's H row) — see the PR report's "评审关注点".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from jones_daemon.kernel.plugin.jones_gate import _rules

Source = Literal["builtin", "mcp", "skill"]
HiddenReason = Literal[
    "not_in_allowlist", "denied_by_rule", "mode_chat", "mcp_server_down", "unknown_tool"
]

# The `"hermes-acp"` toolset's real expansion (`toolsets.py::TOOLSETS["hermes-acp"]`,
# `_CODING_TOOLS` minus `"clarify"` — `_CODING_TOOLS` itself is `_core_without(
# "image_generate", "text_to_speech", "cronjob_manage", "computer_use", *_HA_TOOLS,
# kanban=False)`), hand-copied from the installed `hermes-agent` checkout's
# `toolsets.py` (see module docstring — order matches `_HERMES_CORE_TOOLS` there).
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


@dataclass(frozen=True)
class CapabilityEntry:
    name: str
    source: Source
    enabled: bool
    hidden_reason: str | None = None
    actually_loaded: bool = False
    mcp_server: str | None = None
    skill: str | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "source": self.source,
            "enabled": self.enabled,
            "actually_loaded": self.actually_loaded,
        }
        if self.hidden_reason is not None:
            out["hidden_reason"] = self.hidden_reason
        if self.mcp_server is not None:
            out["mcp_server"] = self.mcp_server
        if self.skill is not None:
            out["skill"] = self.skill
        return out


@dataclass(frozen=True)
class McpServerState:
    """One configured MCP server's state at registry-compute time.

    `tools=None` means "not connected / discovery hasn't run yet or failed" —
    the honest default (a server the daemon has never heard back from), distinct
    from `tools=[]` (connected, genuinely zero tools advertised)."""

    name: str
    enabled: bool = True
    # `None` = per-tool schema not known ahead of a live worker (the daemon has
    # no independent MCP client of its own — see `capabilities/methods.py`'s
    # docstring); a non-`None` list is only ever available to a caller who
    # separately learned it (a test, or a future live-discovery enhancement).
    tools: list[str] | None = None
    # Confirmed-down is a STRONGER claim than merely "tools unknown" — only
    # assert `hidden_reason="mcp_server_down"` when a caller has real evidence
    # (methods.py cross-references `<HERMES_HOME>/jones_tools.json`'s connected-
    # server list against this server's name). `None` = not checked either way
    # ("this session hasn't run a Turn yet" is not the same claim as "down").
    reachable: bool | None = None


def _hidden_reason_for_builtin(
    name: str, *, tool_allowlist: list[str], rules: list[dict[str, Any]]
) -> str | None:
    """Builtin tools follow `kernel/plugin/jones_gate/__init__.py::_decide`'s own
    convention exactly: `tool_allowlist` empty = unrestricted (every builtin
    candidate is a candidate); non-empty = only listed names pass."""
    if tool_allowlist and name not in tool_allowlist:
        return "not_in_allowlist"
    if _rules.decide(rules, name, {}) == "deny":
        return "denied_by_rule"
    return None


def _hidden_reason_for_third_party(
    name: str, *, tool_allowlist: list[str], rules: list[dict[str, Any]]
) -> str | None:
    """N15: MCP/Skill tools are third-party by default and must be EXPLICITLY
    present in the Agent's `tool_allowlist` — unlike builtins, an *empty*
    (="unrestricted") allowlist does NOT enable them. This is what makes N15's
    "第三方工具默认不进白名单，需用户显式启用" true even for an Agent with no
    other tool restrictions configured."""
    if name not in tool_allowlist:
        return "not_in_allowlist"
    if _rules.decide(rules, name, {}) == "deny":
        return "denied_by_rule"
    return None


def _mcp_entry_names(server: McpServerState) -> list[str]:
    """Real per-tool names when known, else one `mcp:<server>` placeholder so
    a server with an unenumerated tool list still gets exactly one visible
    row (see `McpServerState.tools`'s docstring). The placeholder name is also
    the `tool_allowlist` convention for "enable this whole (not yet
    individually enumerated) MCP server" — N15 still requires it to be listed
    explicitly, same as any real per-tool name would be."""
    if server.tools is not None:
        return list(server.tools)
    return [f"mcp:{server.name}"]


def expected_capabilities(
    *,
    mode: str,
    tool_allowlist: list[str] | None = None,
    rules: list[dict[str, Any]] | None = None,
    mcp_servers: list[McpServerState] | None = None,
    skill_tools: dict[str, list[str]] | None = None,
) -> list[CapabilityEntry]:
    """The Agent-whitelist ∩ mode ∩ rule-gate "expected assembled set" (03-w4-
    interfaces.md §2's "期望装配集合") — independent of whether a worker has
    actually started; combine with `reconcile()` and a real `jones_tools.json`
    read to get `capability.list`'s `actually_loaded`/`drift`.

    `tool_allowlist` follows the SAME convention `jones_gate.json`'s
    `tool_allowlist` field / `kernel/plugin/jones_gate/__init__.py::_decide` use:
    empty list = unrestricted for builtins (never for MCP/Skill — see N15 in
    `_hidden_reason_for_third_party`). Pass the exact same list
    `permissions/gate_config.py::build()` computed for this session so
    `capability.list` and the rule gate agree on what "the Agent's whitelist"
    means (same "两个 gate 必须对同一份快照达成一致" principle
    `sessions/service.py::_on_request_permission` already documents elsewhere)."""
    tool_allowlist = tool_allowlist or []
    rules = rules or []
    entries: list[CapabilityEntry] = []

    if mode == "chat":
        # N12: chat mode blocks every tool call, no exceptions — jones_gate's own
        # `_decide` enforces this identically; nothing here is a candidate.
        for name in BUILTIN_TOOLS:
            entries.append(
                CapabilityEntry(
                    name=name, source="builtin", enabled=False, hidden_reason="mode_chat"
                )
            )
        for server in mcp_servers or []:
            for name in _mcp_entry_names(server):
                entries.append(
                    CapabilityEntry(
                        name=name, source="mcp", enabled=False, hidden_reason="mode_chat",
                        mcp_server=server.name,
                    )
                )
        for skill_name, tools in (skill_tools or {}).items():
            for name in tools:
                entries.append(
                    CapabilityEntry(
                        name=name, source="skill", enabled=False, hidden_reason="mode_chat",
                        skill=skill_name,
                    )
                )
        return entries

    for name in BUILTIN_TOOLS:
        reason = _hidden_reason_for_builtin(name, tool_allowlist=tool_allowlist, rules=rules)
        entries.append(
            CapabilityEntry(
                name=name, source="builtin", enabled=reason is None, hidden_reason=reason
            )
        )

    for server in mcp_servers or []:
        if not server.enabled or server.reachable is False:
            # Config-disabled, or CONFIRMED down (a caller only sets
            # `reachable=False` on real evidence — see `McpServerState`'s
            # docstring): one placeholder entry so the transparency page has
            # something to show even without a real tool list (diagnostic
            # honesty — DEV.md 工程原则 #4 — rather than silently omitting it).
            entries.append(
                CapabilityEntry(
                    name=f"mcp:{server.name}", source="mcp", enabled=False,
                    hidden_reason="mcp_server_down", mcp_server=server.name,
                )
            )
            continue
        for name in _mcp_entry_names(server):
            reason = _hidden_reason_for_third_party(
                name, tool_allowlist=tool_allowlist, rules=rules
            )
            entries.append(
                CapabilityEntry(
                    name=name, source="mcp", enabled=reason is None, hidden_reason=reason,
                    mcp_server=server.name,
                )
            )

    for skill_name, tools in (skill_tools or {}).items():
        for name in tools:
            reason = _hidden_reason_for_third_party(
                name, tool_allowlist=tool_allowlist, rules=rules
            )
            entries.append(
                CapabilityEntry(
                    name=name, source="skill", enabled=reason is None, hidden_reason=reason,
                    skill=skill_name,
                )
            )

    return entries


@dataclass(frozen=True)
class ReconcileResult:
    tools: list[CapabilityEntry]
    drift: list[str]
    # None when `<HERMES_HOME>/jones_tools.json` hasn't been written yet (worker
    # never started a Turn) — distinct from "checked, no drift found". A caller
    # (methods.py) must not report `drift: []` as a clean bill of health when this
    # is False (DEV.md 工程原则 #4: 诚实失败).
    actual_available: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "tools": [t.to_json() for t in self.tools],
            "drift": list(self.drift),
            "actual_available": self.actual_available,
        }


_MCP_PLACEHOLDER_PREFIX = "mcp:"
# `tools/mcp_tool_schema.py::MCP_TOOL_NAME_PREFIX` + its `build_mcp_tool_name()`
# format (`mcp__<server>__<tool>`), source-verified against the installed
# `hermes-agent` checkout — used to attribute a real loaded MCP tool name back
# to its server without daemon-side MCP introspection (see `McpServerState`'s
# docstring for why the daemon doesn't have one).
_REAL_MCP_PREFIX = "mcp__"


def _mcp_prefix_for(server_name: str) -> str:
    return f"{_REAL_MCP_PREFIX}{server_name}__"


def reconcile(
    expected: list[CapabilityEntry], actual_loaded_names: list[str] | None
) -> ReconcileResult:
    """G21: the transparency page's tool set must equal what the Turn actually
    assembled. `actual_loaded_names` is `<HERMES_HOME>/jones_tools.json`'s `tools`
    list (jones_gate's `on_session_start` hook — see `kernel/plugin/jones_gate/
    _tools_snapshot.py`), or `None` if that file doesn't exist yet.

    An expected `mcp:<server>` placeholder (see `_mcp_entry_names` — emitted
    when no per-tool schema was known ahead of a live worker) is matched
    against real loaded names by the `mcp__<server>__` prefix instead of an
    exact-name match, and expanded into the real per-tool rows once actual
    data makes them known — a placeholder is a stand-in for "we don't know the
    tool names yet", not a tool name Hermes would ever really register.

    `drift` = every tool name where "expected enabled" and "actually loaded"
    disagree, in either direction: an enabled-and-expected tool the worker never
    actually registered (a stale allowlist entry, a Hermes version mismatch), or
    a tool the worker registered that this registry didn't expect at all
    (`hidden_reason` was never computed for it, or it was expected hidden but
    loaded anyway — either is a G21 violation and gets `"unknown_tool"` in the
    per-tool listing when it wasn't already a known candidate)."""
    if actual_loaded_names is None:
        return ReconcileResult(tools=list(expected), drift=[], actual_available=False)

    actual = set(actual_loaded_names)
    remaining = set(actual)
    tools: list[CapabilityEntry] = []
    drift: list[str] = []
    exact_names = {e.name for e in expected if not e.name.startswith(_MCP_PLACEHOLDER_PREFIX)}

    for entry in expected:
        if entry.source == "mcp" and entry.name.startswith(_MCP_PLACEHOLDER_PREFIX):
            server = entry.mcp_server or entry.name[len(_MCP_PLACEHOLDER_PREFIX):]
            prefix = _mcp_prefix_for(server)
            matches = sorted(n for n in actual if n.startswith(prefix))
            if matches:
                for name in matches:
                    remaining.discard(name)
                    tools.append(
                        CapabilityEntry(
                            name=name, source="mcp", enabled=entry.enabled,
                            hidden_reason=entry.hidden_reason, actually_loaded=True,
                            mcp_server=server,
                        )
                    )
                if not entry.enabled:
                    # Expected hidden, but the worker loaded real tools for it
                    # anyway — a G21 violation, flagged per real tool name.
                    drift.extend(matches)
                continue
            # No real tools observed for this server at all.
            tools.append(
                CapabilityEntry(
                    name=entry.name, source=entry.source, enabled=entry.enabled,
                    hidden_reason=entry.hidden_reason, actually_loaded=False,
                    mcp_server=entry.mcp_server, skill=entry.skill,
                )
            )
            if entry.enabled:
                drift.append(entry.name)
            continue

        loaded = entry.name in actual
        remaining.discard(entry.name)
        tools.append(
            CapabilityEntry(
                name=entry.name, source=entry.source, enabled=entry.enabled,
                hidden_reason=entry.hidden_reason, actually_loaded=loaded,
                mcp_server=entry.mcp_server, skill=entry.skill,
            )
        )
        if entry.enabled != loaded:
            drift.append(entry.name)

    for name in sorted(remaining - exact_names):
        # Loaded for real, but not a name this registry's builtin/mcp/skill
        # candidate computation produced at all — `hidden_reason` is repurposed
        # here as a classification note rather than strictly "why hidden" (the
        # tool IS enabled/loaded); the transparency page still needs to flag it.
        # Best-effort MCP attribution by the `mcp__<server>__<tool>` naming
        # convention (source-verified, see `_REAL_MCP_PREFIX`) — a name that
        # doesn't match it is reported as builtin-shaped (Hermes has no other
        # namespaced tool-name convention today).
        if name.startswith(_REAL_MCP_PREFIX):
            rest = name[len(_REAL_MCP_PREFIX):]
            server = rest.split("__", 1)[0] if "__" in rest else None
            tools.append(
                CapabilityEntry(
                    name=name, source="mcp", enabled=True, hidden_reason="unknown_tool",
                    actually_loaded=True, mcp_server=server,
                )
            )
        else:
            tools.append(
                CapabilityEntry(
                    name=name, source="builtin", enabled=True, hidden_reason="unknown_tool",
                    actually_loaded=True,
                )
            )
        drift.append(name)

    return ReconcileResult(tools=tools, drift=drift, actual_available=True)
