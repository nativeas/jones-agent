"""G10 — PRD 12.1 发布门禁 (同时是 N04):

"重启不自动重放：队列中有 3 条未发送指令时 kill 守护进程再启动，3 条均为「待
发送」且 UI 有提醒" —— 验证方式："按描述操作"。

N04（12.2 负面清单）："重启后自动发送了排队中的消息或执行了排队中的动作"——
绝对不能发生，是 G10 的反面表述，同一份证据。

复用（不重复造）：`test_sessions_service.py::
test_restart_marks_stale_runs_terminated_and_never_auto_resends_the_queue`——
字面标题就是这条门禁：daemon 重启后，中断的 Run 标记为"中断"（PRD 11.3 崩溃
恢复），排队中的条目保持「待发送」，`_advance_queue` 不会在 worker 注册表为空
时把它们当成"可以继续"自动发出去。
"""

from __future__ import annotations

from ._reuse import run_existing


def test_g10_n04_restart_no_autoresend_acceptance() -> None:
    run_existing(
        "tests/test_sessions_service.py::"
        "test_restart_marks_stale_runs_terminated_and_never_auto_resends_the_queue",
    )
