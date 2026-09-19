"""Agent tool-allowlist policy helpers.

`is_tool_allowlist_subset` exists for PRD 9.6 / N13 ("子会话工具白名单 ⊆ 父会话"),
checked when a sub-session is created. Sub-session creation is owned by branch A
(`sessions/`, issue #10) — this module doesn't call it, it's exposed here (agents/
is issue #9's owned directory, where `tool_allowlist` semantics are defined) for
A's session-creation code to import and call. See the branch C report for this
cross-branch dependency.
"""

from __future__ import annotations


def is_tool_allowlist_subset(child: list[str], parent: list[str]) -> bool:
    """Whether `child`'s tool allowlist is an allowed narrowing of `parent`'s.

    An empty allowlist means "no restriction — every tool goes through the other
    gates" (design §2: agent_default 的"白名单为空=全部工具经历闸"), i.e. empty is
    the *broadest* value, not the narrowest. So:
      - an empty `parent` (unrestricted) accepts any `child` — nothing to narrow
        against;
      - a non-empty `parent` requires a non-empty `child` that's a literal subset —
        an empty `child` would itself mean "unrestricted", which is broader than
        `parent`, i.e. NOT a valid narrowing.
    """
    if not parent:
        return True
    if not child:
        return False
    return set(child) <= set(parent)
