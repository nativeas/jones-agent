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
built-in toolset bundle. `BUILTIN_TOOLS` (re-exported below from `kernel/plugin/
jones_gate/_policy.py` — see "Single source of truth" below for why it lives
there now) is therefore the literal, hand-verified expansion of the `"hermes-
acp"` toolset (`toolsets.py::TOOLSETS`, `_CODING_TOOLS` minus `clarify`) — the
entire universe of builtin tool names an ACP-launched worker can ever expose,
independent of anything Jones configures. See the PR report's "契约变更"
section for what this corrects.

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

## Single source of truth for "is this tool allowed" (controller ruling R-H2)

N15's "third-party tools need explicit allow-listing" and the `mcp:<server>`
whole-server placeholder convention used to be re-implemented HERE, with a
docstring claiming (falsely — round-1 review findings #1/#7) to mirror a
function of the same shape on the enforcement side
(`kernel/plugin/jones_gate/__init__.py::_decide`) that never actually existed.
Both sides now call the exact same function, `kernel.plugin.jones_gate._policy.
tool_allowed` (re-exported here as `capabilities.policy.tool_allowed` — see
that module's docstring for why the real implementation has to live inside the
`jones_gate` package, which ships to the worker, rather than in
`jones_daemon.capabilities`) — this module can never again show `enabled` for
a tool the real gate would actually block, or vice versa.

## Drift semantics (controller ruling R-H1)

`reconcile()` no longer treats "expected hidden but Hermes assembled it into
the model's schema anyway" as drift. That direction is the NORMAL case for
every `tool_allowlist`-restricted session and every MCP server not yet
explicitly allow-listed (N15's whole point): `tool_allowlist` is an
EXECUTION-time gate (`_policy.tool_allowed`, enforced by `_decide` before a
call runs), not a schema-time one — nothing in Hermes lets Jones drop a tool
from what the model can SEE, only from what it can successfully CALL (see this
module's first section above). Comparing "gate-enabled" against
"schema-present" as if they were the same boolean, the way the previous round
did, meant `drift` was non-empty in EVERY ordinary restricted-Agent or
MCP-configured session — a G21 "honest failure" signal that's always red
carries no information (round-2 review findings #2/#5).

`drift` now holds only two kinds of real anomaly:
1. A tool the registry expected ENABLED (gate would allow it) that the worker
   never actually loaded at all — a stale allowlist entry, a Hermes version
   mismatch, or a genuinely broken MCP connection masquerading as configured.
2. A tool name the worker loaded that this registry's candidate computation
   cannot explain at all — not a known builtin/MCP/skill candidate, and not
   one of the two "known-explainable, not this session's fault" name classes
   `kernel.plugin.jones_gate._policy` documents: Hermes's own Tool Search
   bridge (`tool_search`/`tool_describe`/`tool_call` — `TOOL_SEARCH_BRIDGE_
   NAMES`, review finding #6) and the `CONDITIONAL_BUILTIN_TOOLS` subset whose
   real availability depends on a Hermes subsystem Jones doesn't configure
   (browser profile, connector gateway, search API key, vision model) — see
   that module's docstring for the full source-verified list. `enabled` (gate
   policy) and `actually_loaded` (schema presence) are independently reported
   on every entry regardless; a caller that wants "what would the model
   literally see" reads `actually_loaded`, and "what could it successfully
   call" reads `enabled` — conflating them into one `drift` signal was the
   bug.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from jones_daemon.kernel.plugin.jones_gate import _policy, _rules

Source = Literal["builtin", "mcp", "skill"]
HiddenReason = Literal[
    "not_in_allowlist", "denied_by_rule", "mode_chat", "mcp_server_down", "unknown_tool",
]

# Re-exported for backwards-compatible/discoverable access from this module —
# `_policy.py`'s module docstring explains why the real definition lives in
# `kernel.plugin.jones_gate` now (controller ruling R-H2).
BUILTIN_TOOLS = _policy.BUILTIN_TOOLS


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
    # server list against this server's name, AND that snapshot's MCP discovery
    # had actually finished — review round-2 finding #3). `None` = not checked
    # either way ("this session hasn't run a Turn yet, or discovery was still
    # in flight when it wrote its snapshot" is not the same claim as "down").
    reachable: bool | None = None


def _hidden_reason(
    name: str, *, tool_allowlist: list[str], rules: list[dict[str, Any]]
) -> str | None:
    """`enabled = hidden_reason is None`, for any source. `_policy.tool_allowed`
    (controller ruling R-H2) is the single allowlist/N15 check shared with the
    real enforcement gate — see this module's docstring."""
    if not _policy.tool_allowed(name, tool_allowlist):
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
    return [f"{_policy.MCP_PLACEHOLDER_PREFIX}{server.name}"]


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
    `tool_allowlist` field / `kernel/plugin/jones_gate/__init__.py::_decide` use
    (`_policy.tool_allowed`, controller ruling R-H2). Pass the exact same list
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
        reason = _hidden_reason(name, tool_allowlist=tool_allowlist, rules=rules)
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
                    name=f"{_policy.MCP_PLACEHOLDER_PREFIX}{server.name}",
                    source="mcp", enabled=False,
                    hidden_reason="mcp_server_down", mcp_server=server.name,
                )
            )
            continue
        for name in _mcp_entry_names(server):
            reason = _hidden_reason(name, tool_allowlist=tool_allowlist, rules=rules)
            entries.append(
                CapabilityEntry(
                    name=name, source="mcp", enabled=reason is None, hidden_reason=reason,
                    mcp_server=server.name,
                )
            )

    for skill_name, tools in (skill_tools or {}).items():
        for name in tools:
            reason = _hidden_reason(name, tool_allowlist=tool_allowlist, rules=rules)
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


def _mcp_prefix_for(server_name: str) -> str:
    return f"{_policy.MCP_TOOL_NAME_PREFIX}{server_name}__"


def reconcile(
    expected: list[CapabilityEntry],
    actual_loaded_names: list[str] | None,
    *,
    tool_allowlist: list[str] | None = None,
    rules: list[dict[str, Any]] | None = None,
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

    `tool_allowlist`/`rules` (same snapshot `expected_capabilities` was called
    with — pass them here too): once a placeholder expands into real per-tool
    names, `enabled` for EACH real name is RECOMPUTED against those real names
    via `_hidden_reason`/`_policy.tool_allowed`, rather than inherited from the
    placeholder's own precomputed value. This matters whenever the allowlist
    names an exact real tool (`mcp__<server>__<tool>`) rather than the
    whole-server `mcp:<server>` placeholder: `_policy.tool_allowed("mcp:
    <server>", allowlist)` and `_policy.tool_allowed("mcp__<server>__<tool>",
    allowlist)` are NOT the same question, and conflating them (this
    function's behavior before this fix, caught by the R-H2 end-to-end
    gate-agreement test) could show `enabled: false` on the transparency page
    for a tool the real gate would actually allow. Omitted (`None`, the
    default): falls back to the placeholder's own precomputed `enabled` —
    correct whenever the allowlist only ever names whole servers, wrong
    otherwise, kept only for callers that genuinely don't have the allowlist
    at hand.

    `drift` (controller ruling R-H1 — see this module's docstring for the full
    "why"): ONLY a tool expected enabled that the worker never actually loaded
    (`enabled and not actually_loaded`), or a loaded name this registry's
    candidate computation cannot explain at all. "Expected hidden but loaded
    anyway" is NOT drift — `tool_allowlist` is an execution-time gate, not a
    schema-time one (see module docstring); `enabled`/`actually_loaded` are
    still both reported per-entry so the transparency page can show it."""
    if actual_loaded_names is None:
        return ReconcileResult(tools=list(expected), drift=[], actual_available=False)

    actual = set(actual_loaded_names)
    remaining = set(actual)
    tools: list[CapabilityEntry] = []
    drift: list[str] = []
    exact_names = {
        e.name for e in expected if not e.name.startswith(_policy.MCP_PLACEHOLDER_PREFIX)
    }

    for entry in expected:
        if entry.source == "mcp" and entry.name.startswith(_policy.MCP_PLACEHOLDER_PREFIX):
            server = entry.mcp_server or entry.name[len(_policy.MCP_PLACEHOLDER_PREFIX):]
            prefix = _mcp_prefix_for(server)
            matches = sorted(n for n in actual if n.startswith(prefix))
            if matches:
                for name in matches:
                    remaining.discard(name)
                    if tool_allowlist is not None:
                        # Recompute against the REAL per-tool name — see this
                        # function's docstring for why inheriting the
                        # placeholder's own `enabled` is wrong whenever the
                        # allowlist names an exact tool rather than the
                        # whole-server placeholder.
                        reason = _hidden_reason(
                            name, tool_allowlist=tool_allowlist, rules=rules or []
                        )
                        enabled, hidden_reason = reason is None, reason
                    else:
                        enabled, hidden_reason = entry.enabled, entry.hidden_reason
                    tools.append(
                        CapabilityEntry(
                            name=name, source="mcp", enabled=enabled,
                            hidden_reason=hidden_reason, actually_loaded=True,
                            mcp_server=server,
                        )
                    )
                # Expected hidden but loaded anyway is NOT drift (R-H1) — the
                # gate still blocks the call, this is just the ordinary "schema
                # visibility ≠ execution permission" shape N15 relies on.
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
        if entry.enabled and not loaded and entry.name not in _policy.CONDITIONAL_BUILTIN_TOOLS:
            # R-H1: only the "expected enabled, never actually loaded" direction
            # is drift, and only for names whose absence isn't already explained
            # by a Hermes subsystem Jones doesn't configure (browser profile,
            # connectors, search key, vision model — see `_policy.py`'s
            # `CONDITIONAL_BUILTIN_TOOLS` docstring for the source-verified
            # list). A conditional tool's absence is still visible via
            # `actually_loaded: false` above, just not counted as a G21
            # anomaly a normal, minimally-configured deployment would trip on
            # every single `capability.list` call.
            drift.append(entry.name)

    for name in sorted(remaining - exact_names):
        # Loaded for real, but not a name this registry's builtin/mcp/skill
        # candidate computation produced at all. Two name shapes are already
        # explainable and must NOT be reported as `unknown_tool`/drift (R-H1):
        if name in _policy.TOOL_SEARCH_BRIDGE_NAMES:
            tools.append(
                CapabilityEntry(
                    name=name, source="builtin", enabled=True, actually_loaded=True,
                )
            )
            continue
        server = _policy.mcp_server_for(name)
        # Best-effort MCP attribution by the `mcp__<server>__<tool>` naming
        # convention (source-verified, see `_policy.MCP_TOOL_NAME_PREFIX`) — a
        # name that doesn't match it is reported as builtin-shaped (Hermes has
        # no other namespaced tool-name convention today). Neither shape is a
        # known-explainable name, so both are real G21 anomalies: a tool this
        # registry never expected under ANY candidate source, source-attributed
        # only best-effort.
        if server is not None:
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
