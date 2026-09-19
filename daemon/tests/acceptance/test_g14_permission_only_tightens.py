"""G14 — PRD 12.1 发布门禁 (同时是 N11):

"权限只能收紧：项目级 `permissions.json` 写「允许」用户级禁止项，实际仍被拦" ——
验证方式："按描述配置并触发"。

N11（12.2 负面清单）："项目级配置放宽了用户级权限限制"——同一约束的反面表述。

复用（不重复造）：`test_config_resolver.py::
test_permissions_project_cannot_loosen_a_user_level_deny`——用户级 deny 一条
规则，项目级对同一 match 写 allow，合并结果里 deny 仍然生效（"允许只能收紧，不
能放宽"）。
"""

from __future__ import annotations

from ._reuse import run_existing


def test_g14_n11_permissions_only_tighten_acceptance() -> None:
    run_existing(
        "tests/test_config_resolver.py::"
        "test_permissions_project_cannot_loosen_a_user_level_deny",
    )
