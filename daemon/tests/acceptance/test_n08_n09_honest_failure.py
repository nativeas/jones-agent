"""N08 + N09 — PRD 12.2 负面清单:

- N08: "agent 假装成功：工具报错但 agent 向用户宣称已完成"
- N09: "错误被静默吞掉：日志有异常但 UI 无任何呈现"

两条都对应原则 5.5（DEV.md 工程原则 #4 "诚实失败"）：任何 `except` 必须要么
处理要么向上抛带上下文，禁止 `except: pass`。

复用（不重复造）：`sessions/service.py::_run_turn` 的三层异常处理（ACP prompt
失败 / worker 启动失败 / 兜底未知异常）无一例外都走 `_terminate_run(kind="error", ...)`
或 `daemon.error` 广播，从不静默吞掉、也不会让 Turn 以"成功"收尾：
- `test_sessions_service.py::
  test_streamed_assistant_text_is_kept_when_the_worker_errors_mid_turn`——worker
  中途报错时，Run 以 error 收尾，不是假装完成。
- `test_sessions_service.py::
  test_unexpected_exception_in_run_turn_still_terminates_the_run`——未预料的
  异常同样必须变成 `run.terminated(kind="error")`，不是裸吞掉。
"""

from __future__ import annotations

from ._reuse import run_existing


def test_n08_n09_honest_failure_acceptance() -> None:
    run_existing(
        "tests/test_sessions_service.py::"
        "test_streamed_assistant_text_is_kept_when_the_worker_errors_mid_turn",
        "tests/test_sessions_service.py::"
        "test_unexpected_exception_in_run_turn_still_terminates_the_run",
    )
