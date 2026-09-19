"""N07 — PRD 12.2 负面清单:

"UI 显示「运行中」但底层 Run 已死亡 / worker 已退出超过 5 秒" —— 对应原则
5.5、9.3，绝对不能发生。

复用（不重复造）：`workers/manager.py` 用 `asyncio` 对每个 worker 子进程
`await process.wait()`（事件驱动，不是轮询），退出即刻回调 `_on_worker_crash`
→ `_terminate_run`，不存在"死了但 UI 还显示运行中"的滞后窗口，更不用说 5 秒：
- `test_workers_manager.py::
  test_worker_crash_after_startup_invokes_the_crash_callback`——进程退出立刻
  触发崩溃回调，不是轮询发现。
- `test_sessions_service.py::
  test_four_parallel_sessions_survive_one_worker_crash`——崩溃后该 Session 的
  Run 状态翻转为 terminated，同一断言里验证。

`test_cap_terminal_stop_cancel.py` 的相关注释（"UI shows a Run as dead" 的
`_resolve_pending_permissions`分支）是这条门禁的另一个切面：worker 崩溃时任何
待审的权限请求也一并按 deny 收尾，不会悬空成"看起来还在等用户"。
"""

from __future__ import annotations

from ._reuse import run_existing


def test_n07_no_stale_running_status_acceptance() -> None:
    run_existing(
        "tests/test_workers_manager.py::"
        "test_worker_crash_after_startup_invokes_the_crash_callback",
        "tests/test_sessions_service.py::"
        "test_four_parallel_sessions_survive_one_worker_crash",
    )
