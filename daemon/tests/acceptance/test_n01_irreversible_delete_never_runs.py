"""N01 — PRD 12.2 负面清单:

"agent 执行了任何不可逆删除操作——无论何种模式、何种授权" —— 对应原则 5.7，
绝对不能发生，任一出现即阻断发布/回滚。与 G05 是同一份证据的负面表述。

复用（不重复造）：见 `test_g05_irreversible_delete_hard_denied.py` 引用的
`test_gates_rule_gate.py::test_hard_deny_applies_in_every_mode_G05` 与
`test_gates_hard_deny.py` 的硬禁止分类器用例——硬禁止清单在插件侧本地同步判定，
不接受任何 `permissions.json`/`remember` 规则放宽（00-w3-interfaces.md §1.1:
"任何配置不可放宽"）。
"""

from __future__ import annotations

from ._reuse import run_existing


def test_n01_irreversible_delete_never_runs_acceptance() -> None:
    run_existing(
        "tests/test_gates_rule_gate.py::test_hard_deny_applies_in_every_mode_G05",
        "tests/test_gates_rule_gate.py::"
        "test_hard_deny_wins_even_with_an_allow_rule_for_the_same_tool",
        "tests/test_gates_hard_deny.py::test_rm_rf_is_denied",
    )
