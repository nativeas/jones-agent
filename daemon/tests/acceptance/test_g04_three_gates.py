"""G04 — PRD 12.1 发布门禁:

"三道闸生效：规则闸禁止项被拦、审查闸对高危动作标红、用户闸拒绝即不执行" ——
验证方式："三条用例各一，分别验证拦截、标红、拒绝后无副作用"。

复用（不重复造），三闸各一：
- 规则闸拦截：`test_gates_rule_gate.py::test_chat_mode_blocks_every_tool_call_N12`
  ——插件在 chat 模式下对任意工具调用直接返回 block，零 IPC。
- 审查闸标红：`test_gates_review.py::test_terminal_with_curl_is_high_risk`
  ——`classify()` 把带网络外发的终端命令标为 high。
- 用户闸拒绝即不执行：`test_gates_sessions_integration.py::
  test_approval_timeout_denies_and_terminates_the_run`——用户闸的拒绝路径
  （含超时自动 deny）导致该 Turn 以 error 收尾，不存在"先执行后补票"的副作用。
"""

from __future__ import annotations

from ._reuse import run_existing


def test_g04_three_gates_acceptance() -> None:
    run_existing(
        "tests/test_gates_rule_gate.py::test_chat_mode_blocks_every_tool_call_N12",
        "tests/test_gates_review.py::test_terminal_with_curl_is_high_risk",
        "tests/test_gates_sessions_integration.py::"
        "test_approval_timeout_denies_and_terminates_the_run",
    )
