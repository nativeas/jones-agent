"""N12 — PRD 12.2 负面清单:

"纯对话模式下发生任何工具调用（含只读）" —— 对应原则 9.1，绝对不能发生。与
G06 的 chat 分支是同一份证据。

复用（不重复造）：
- `test_gates_rule_gate.py::test_chat_mode_blocks_every_tool_call_N12`——插件
  在 chat 模式下对任意工具调用直接 block。
- `test_gates_rule_gate.py::
  test_chat_mode_still_blocks_even_with_a_matching_allow_rule`——即使
  `permissions.json` 有匹配的 allow 规则，chat 模式的边界依然是绝对的，不能被
  任何规则突破。
- `test_gates_sessions_integration.py::
  test_chat_mode_send_completes_with_zero_tool_calls_N12`——`SessionService`
  级、真 ACP 往返，chat 模式下整个 Turn 零工具调用完成。
"""

from __future__ import annotations

from ._reuse import run_existing


def test_n12_chat_mode_zero_tools_acceptance() -> None:
    run_existing(
        "tests/test_gates_rule_gate.py::test_chat_mode_blocks_every_tool_call_N12",
        "tests/test_gates_rule_gate.py::"
        "test_chat_mode_still_blocks_even_with_a_matching_allow_rule",
        "tests/test_gates_sessions_integration.py::"
        "test_chat_mode_send_completes_with_zero_tool_calls_N12",
    )
