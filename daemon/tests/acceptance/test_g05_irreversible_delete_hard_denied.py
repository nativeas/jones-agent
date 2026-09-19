"""G05 — PRD 12.1 发布门禁:

"不可逆删除永不执行：自动模式下 agent 尝试 `rm -rf` / 清空回收站 / 永久删除文件，
均被硬拒" —— 验证方式："三种模式各跑一遍"。

复用（不重复造）：
- `test_gates_rule_gate.py::test_hard_deny_applies_in_every_mode_G05` ——同一条
  `rm -rf` 在 chat/task/auto 三种模式下都被规则闸硬拒（字面就是这条门禁的三模式
  遍历）。
- `test_gates_hard_deny.py`：硬禁止分类器本身对 `rm -rf`、`trash`（清空回收站）、
  `shred`、`diskutil erase` 等的命中判定——门禁文字点名的几种操作各选一条。
"""

from __future__ import annotations

from ._reuse import run_existing


def test_g05_hard_deny_every_mode_acceptance() -> None:
    run_existing(
        "tests/test_gates_rule_gate.py::test_hard_deny_applies_in_every_mode_G05",
        "tests/test_gates_hard_deny.py::test_rm_rf_is_denied",
        "tests/test_gates_hard_deny.py::test_trash_command_is_denied",
        "tests/test_gates_hard_deny.py::test_shred_is_denied",
        "tests/test_gates_hard_deny.py::test_diskutil_erase_is_denied",
    )
