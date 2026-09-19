"""N10 — PRD 12.2 负面清单:

"agent 修改或删除了自己的审批记录、回放记录、权限规则文件" —— 对应原则 10.4，
绝对不能发生。

复用（不重复造）：硬禁止清单把 `~/.jones/`、`$HOME/.jones`、
`<project>/.jones/permissions.json` 列为只读保护路径（02-w3-interfaces.md §1.1
"保护路径...一律硬拒，除非...只读命令集合"），`permission_decisions`/回放数据
都落在 `~/.jones/jones.db` 与 `runs/<id>/` 下，同一保护范围覆盖：
- `test_gates_hard_deny.py::
  test_write_verb_touching_user_root_is_denied`。
- `test_gates_hard_deny.py::
  test_protected_user_root_path_write_is_denied`。
- `test_gates_hard_deny.py::
  test_protected_project_permissions_path_is_denied`。
- `test_gates_rule_gate.py::
  test_write_file_to_protected_jones_path_is_hard_denied`——`write_file`/
  `patch`（走 edit_approval 通道，不经过规则闸的 approve 分支）对同一保护路径
  同样只输出 block，不放行。
"""

from __future__ import annotations

from ._reuse import run_existing


def test_n10_no_self_tamper_acceptance() -> None:
    run_existing(
        "tests/test_gates_hard_deny.py::"
        "test_write_verb_touching_user_root_is_denied",
        "tests/test_gates_hard_deny.py::"
        "test_protected_user_root_path_write_is_denied",
        "tests/test_gates_hard_deny.py::"
        "test_protected_project_permissions_path_is_denied",
        "tests/test_gates_rule_gate.py::"
        "test_write_file_to_protected_jones_path_is_hard_denied",
    )
