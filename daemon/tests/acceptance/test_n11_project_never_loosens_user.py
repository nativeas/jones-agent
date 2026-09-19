"""N11 — PRD 12.2 负面清单:

"项目级配置放宽了用户级权限限制" —— 对应原则 10.1，绝对不能发生。与 G14 是同
一份证据。

复用（不重复造）：见 `test_g14_permission_only_tightens.py`。
"""

from __future__ import annotations

from ._reuse import run_existing


def test_n11_project_never_loosens_user_acceptance() -> None:
    run_existing(
        "tests/test_config_resolver.py::"
        "test_permissions_project_cannot_loosen_a_user_level_deny",
    )
