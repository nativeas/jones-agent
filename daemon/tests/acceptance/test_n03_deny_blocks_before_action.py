"""N03 — PRD 12.2 负面清单:

"用户拒绝的动作仍被执行，或用户闸弹出前动作已发生" —— 对应原则 5.4，绝对不能
发生。

复用（不重复造）：
- `test_gates_sessions_integration.py::
  test_auto_mode_high_risk_still_goes_through_the_user_gate`——高危动作先广播
  `permission.requested`（等待用户）后才可能有 `permission.decided`，用户
  `deny` 后 `decided_by == "user"`、`decision == "deny"`——弹窗必须先于动作
  （ACP `request_permission` 协议本身就是在 daemon 应答前工具不会执行）。
- `test_gates_sessions_integration.py::
  test_approval_timeout_denies_and_terminates_the_run`——拒绝（含超时自动
  deny）之后 Run 以 error 收尾，不存在"先执行后拒绝"的路径。
"""

from __future__ import annotations

from ._reuse import run_existing


def test_n03_deny_blocks_before_action_acceptance() -> None:
    run_existing(
        "tests/test_gates_sessions_integration.py::"
        "test_auto_mode_high_risk_still_goes_through_the_user_gate",
        "tests/test_gates_sessions_integration.py::"
        "test_approval_timeout_denies_and_terminates_the_run",
    )
