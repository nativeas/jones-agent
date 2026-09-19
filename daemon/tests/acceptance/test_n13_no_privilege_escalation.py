"""N13 — PRD 12.2 负面清单:

"子会话权限超出父会话，或任务模式的父派生出自动模式的子" —— 对应原则 9.6，
绝对不能发生。（不在任务给出的"至少"清单里，但已有充分自动化覆盖，一并纳入
CI 验收，不留空。）

复用（不重复造）：
- `test_gates_modes.py::test_task_parent_forbids_auto_child_N13`——模式收窄
  校验本身：task 父不能派生 auto 子。
- `test_gates_gate_config.py::
  test_child_allowlist_is_narrowed_by_parent_agent_N13`——工具白名单收窄：子
  会话的白名单 ⊆ 父会话。
- `test_gates_rule_gate.py::
  test_allow_rule_does_not_bypass_the_agent_tool_whitelist_N13`——即使有匹配
  的 allow 规则，也不能绕过白名单收窄。
- `test_sessions_service.py::
  test_create_without_system_dispatch_still_rejects_auto_child_of_task_parent_N13`
  ——`system_dispatch` 例外（Issue #20 裁定，Cron 系统派发）不会连带放宽普通
  agent 自主派生的这条校验。
"""

from __future__ import annotations

from ._reuse import run_existing


def test_n13_no_privilege_escalation_acceptance() -> None:
    run_existing(
        "tests/test_gates_modes.py::test_task_parent_forbids_auto_child_N13",
        "tests/test_gates_gate_config.py::"
        "test_child_allowlist_is_narrowed_by_parent_agent_N13",
        "tests/test_gates_rule_gate.py::"
        "test_allow_rule_does_not_bypass_the_agent_tool_whitelist_N13",
        "tests/test_sessions_service.py::"
        "test_create_without_system_dispatch_still_rejects_auto_child_of_task_parent_N13",
    )
