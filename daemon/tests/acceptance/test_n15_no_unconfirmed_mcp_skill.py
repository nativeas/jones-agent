"""N15 — PRD 12.2 负面清单:

"未经用户确认，第三方 Skill / MCP 工具进入 Agent 白名单" —— 对应原则 5.3、
11.3，绝对不能发生：MCP/第三方来源的工具不因为"白名单为空=不限"而被当成默认
启用，必须显式在 `tool_allowlist` 里点名。

复用（不重复造）：
- `test_capabilities_registry.py::
  test_unrestricted_allowlist_enables_every_builtin_but_no_mcp_or_skill`——空
  白名单只解锁内置工具，不会连带放行任何 MCP/Skill 来源的工具。
- `test_capabilities_registry.py::
  test_explicit_allowlist_entry_enables_third_party_tool`——第三方工具必须被
  显式点名才会出现在装配结果里。
- `test_capabilities_policy_gate_agreement.py::
  test_registry_enabled_matches_real_gate_verdict`——透明页"是否启用"的判定
  与规则闸的真实放行判定一致，不存在透明页说启用、规则闸其实还没经过用户确认
  就放行的分裂口径。
"""

from __future__ import annotations

from ._reuse import run_existing


def test_n15_no_unconfirmed_mcp_skill_acceptance() -> None:
    run_existing(
        "tests/test_capabilities_registry.py::"
        "test_unrestricted_allowlist_enables_every_builtin_but_no_mcp_or_skill",
        "tests/test_capabilities_registry.py::"
        "test_explicit_allowlist_entry_enables_third_party_tool",
        "tests/test_capabilities_policy_gate_agreement.py::"
        "test_registry_enabled_matches_real_gate_verdict",
    )
