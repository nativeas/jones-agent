"""G21 — PRD 12.1 发布门禁:

"能力透明一致：透明页列出的工具集合与该 Turn 实际装配给模型的工具集合完全一
致，含 MCP 与 Skill 来源的工具" —— 验证方式："接入 1 个 MCP、1 个 Skill 后比
对透明页与回放中的 tool 列表"。

复用（不重复造）：
- `test_capabilities_registry.py::
  test_reconcile_flags_drift_when_expected_enabled_tool_never_loaded` /
  `test_reconcile_no_drift_when_actual_matches_expected`——注册表的"期望装配"
  与真实 `jones_tools.json`（worker 侧实际加载快照）对账，drift 检测本身。
- `test_capabilities_methods.py::
  test_capability_list_reads_real_jones_tools_json_and_reports_real_drift`——
  `capability.list` RPC（透明页的数据源）真的读真实快照文件而不是纸面配置。
- `test_capabilities_policy_gate_agreement.py::
  test_registry_enabled_matches_real_gate_verdict`——透明页判定"是否启用"与
  规则闸真实放行判定一致（MCP/Skill 来源的工具同样受白名单与权限闸约束，不是
  透明页单独维护一份口径）。

桌面端「装配对账不一致」告警 UI 见
`apps/desktop/src/renderer/src/components/__tests__/CapabilitySettings.test.tsx`
（已在常规 `pnpm test` 中运行）。
"""

from __future__ import annotations

from ._reuse import run_existing


def test_g21_capability_transparency_acceptance() -> None:
    run_existing(
        "tests/test_capabilities_registry.py::"
        "test_reconcile_flags_drift_when_expected_enabled_tool_never_loaded",
        "tests/test_capabilities_registry.py::"
        "test_reconcile_no_drift_when_actual_matches_expected",
        "tests/test_capabilities_methods.py::"
        "test_capability_list_reads_real_jones_tools_json_and_reports_real_drift",
        "tests/test_capabilities_policy_gate_agreement.py::"
        "test_registry_enabled_matches_real_gate_verdict",
    )
