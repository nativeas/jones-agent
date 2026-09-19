"""G09 — PRD 12.1 发布门禁:

"用户终止收尾干净：Run 中途停止后，正在执行的工具调用完整结束，无半截文件、无
孤儿进程" —— 验证方式："在文件写入与终端命令中途停止"。

复用（不重复造）：
- `test_cap_terminal_stop_cancel.py::
  test_stop_sends_acp_cancel_while_a_terminal_call_is_pending_approval`——
  daemon 侧的停止半程：`SessionService.stop()` 把待审批的终端调用取消掉。
- `test_cap_terminal_stop_cancel.py::
  test_cancel_reaps_a_real_subprocess_spawned_by_the_worker_and_reports_the_pid`
  ——真实子进程场景：取消后 10 秒内验证子进程确实被回收，不是孤儿进程（这正是
  PRD 12.1 "无孤儿进程"的字面证明，用真实 subprocess，不是 mock）。

该模块自己的 docstring 说明了取消路径的另一半（真实 Hermes worker 收到 ACP
cancel 后真正杀掉子进程）不可在沙箱里复现，由
`test_real_hermes_e2e_files_terminal.py::`（`JONES_E2E=1`）覆盖，此处不重复引用
（未在默认 `pytest -q`/CI 下运行）。
"""

from __future__ import annotations

from ._reuse import run_existing


def test_g09_clean_termination_acceptance() -> None:
    run_existing(
        "tests/test_cap_terminal_stop_cancel.py::"
        "test_stop_sends_acp_cancel_while_a_terminal_call_is_pending_approval",
        "tests/test_cap_terminal_stop_cancel.py::"
        "test_cancel_reaps_a_real_subprocess_spawned_by_the_worker_and_reports_the_pid",
    )
