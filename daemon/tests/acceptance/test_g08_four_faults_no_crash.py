"""G08 — PRD 12.1 发布门禁:

"异常显式报错不崩溃：断网、Key 失效、工具抛异常、worker 被 kill 四种故障下，UI
出错误卡片，进程不崩、不白屏、不静默" —— 验证方式："四种故障注入"。

复用（不重复造），四种故障各一（`SessionService` 级）：
- 断网（Provider/ACP 层连不上）：`test_sessions_service.py::
  test_unexpected_exception_in_run_turn_still_terminates_the_run`——`_run_turn`
  的兜底 `except Exception` 把任何未命名异常（含连接失败）转成
  `run.terminated(kind="error")`，不是裸异常/裸崩溃。
- Key 失效：`test_sessions_service.py::
  test_provider_error_terminates_the_run_before_spawning_a_worker`。
- 工具抛异常（ACP `prompt()` 报错）：`test_sessions_service.py::
  test_streamed_assistant_text_is_kept_when_the_worker_errors_mid_turn`。
- worker 被 kill：`test_sessions_service.py::
  test_four_parallel_sessions_survive_one_worker_crash`——同时验证了 G12 的
  隔离性。

桌面端"不白屏"的另一半（渲染层 N16）见
`apps/desktop/src/renderer/src/components/__tests__/ErrorBoundary.test.tsx`
（已在常规 `pnpm test` 中运行，见 `test_n16_no_white_screen.py` 的引用）。
"""

from __future__ import annotations

from ._reuse import run_existing


def test_g08_four_fault_injections_acceptance() -> None:
    run_existing(
        "tests/test_sessions_service.py::"
        "test_unexpected_exception_in_run_turn_still_terminates_the_run",
        "tests/test_sessions_service.py::"
        "test_provider_error_terminates_the_run_before_spawning_a_worker",
        "tests/test_sessions_service.py::"
        "test_streamed_assistant_text_is_kept_when_the_worker_errors_mid_turn",
        "tests/test_sessions_service.py::"
        "test_four_parallel_sessions_survive_one_worker_crash",
    )
