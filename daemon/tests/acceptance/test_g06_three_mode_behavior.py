"""G06 — PRD 12.1 发布门禁:

"三种工作模式行为符合 9.1：纯对话模式零工具调用；任务模式写动作逐条弹闸；自动
模式规则闸内不弹" —— 验证方式："同一任务三种模式各跑一遍，比对回放"。

复用（不重复造），三种模式各一（`SessionService` 级、真 ACP 往返，
`fake_acp_agent.py` 驱动）：
- chat：`test_gates_sessions_integration.py::
  test_chat_mode_send_completes_with_zero_tool_calls_N12`——零工具调用完成。
- task：`test_gates_sessions_integration.py::
  test_task_mode_write_action_still_goes_through_the_user_gate_G06`——写动作
  逐条弹用户闸；`test_task_mode_low_risk_auto_allows_with_no_pending_broadcast_G06`
  ——只读工具直接放行，不弹。
- auto：`test_gates_sessions_integration.py::
  test_auto_mode_low_risk_auto_allows_with_no_pending_broadcast`——规则闸放行
  范围内不弹。
"""

from __future__ import annotations

from ._reuse import run_existing


def test_g06_three_modes_acceptance() -> None:
    run_existing(
        "tests/test_gates_sessions_integration.py::"
        "test_chat_mode_send_completes_with_zero_tool_calls_N12",
        "tests/test_gates_sessions_integration.py::"
        "test_task_mode_write_action_still_goes_through_the_user_gate_G06",
        "tests/test_gates_sessions_integration.py::"
        "test_task_mode_low_risk_auto_allows_with_no_pending_broadcast_G06",
        "tests/test_gates_sessions_integration.py::"
        "test_auto_mode_low_risk_auto_allows_with_no_pending_broadcast",
    )
