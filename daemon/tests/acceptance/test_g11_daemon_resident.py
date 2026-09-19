"""G11 — PRD 12.1 发布门禁:

"守护进程独立常驻：关闭 Electron 后 Cron 仍按时触发；重开 Electron 能看到结果"
—— 验证方式："设 1 分钟 Cron，关窗等待，重开验证"。

自动化的一半：架构上 Cron 触发（`SchedulerService` → `SessionService.create`/
`send`）完全在守护进程内部完成，不依赖任何已连接的 RPC 客户端（Electron 是否
在运行、是否有客户端订阅了这个 Session，对 `_dispatch`/`_run_turn` 都不是前置
条件）——复用（不重复造）`test_scheduler_service.py::
test_dispatched_task_row_is_created_with_source_cron` 与
`test_default_mode_is_auto_and_dispatches_against_real_session_service`：两者
都用真实 `SessionService` 触发一次完整 Cron 派发，全程没有构造任何 RPC 连接或
订阅者，证明"触发结果落库"这一半不依赖 Electron 是否打开。

不能自动化的一半（需要真实 launchd 常驻 + 真实关闭/重开 Electron，会在开发机
上安装持久 LaunchAgent，不适合在沙箱/CI 里跑）：见
`docs/acceptance/v1.0/G11.md`（手工记录模板，待执行）。
"""

from __future__ import annotations

from ._reuse import run_existing


def test_g11_cron_fires_independent_of_any_rpc_client_acceptance() -> None:
    run_existing(
        "tests/test_scheduler_service.py::"
        "test_dispatched_task_row_is_created_with_source_cron",
        "tests/test_scheduler_service.py::"
        "test_default_mode_is_auto_and_dispatches_against_real_session_service",
    )
