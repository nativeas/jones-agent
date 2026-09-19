"""G12 — PRD 12.1 发布门禁:

"多 Session 隔离：4 个 Session 并行，其中 1 个 worker 被 kill，其余 3 个不受
影响" —— 验证方式："并行跑 4 个长任务，kill 其一"。

复用（不重复造）：`test_sessions_service.py::
test_four_parallel_sessions_survive_one_worker_crash`——字面就是这条门禁：4 个
Session 并行运行，其中一个的 worker 崩溃，断言其余 3 个的 Run 不受影响、正常
完成。
"""

from __future__ import annotations

from ._reuse import run_existing


def test_g12_multi_session_isolation_acceptance() -> None:
    run_existing(
        "tests/test_sessions_service.py::"
        "test_four_parallel_sessions_survive_one_worker_crash",
    )
