"""G15 — PRD 12.1 发布门禁:

"敏感目录默认不可见：agent 尝试读 `~/.ssh`、`~/.aws`、`~/.jones/secrets/`，均被
拒且走权限闸提示" —— 验证方式："三个路径各一次"。

复用（不重复造），三个路径各一（`permissions/defaults.py` 的默认拒绝根 +
`permissions/review.py::classify()` 把命中路径的 `read_file` 标为 high，确保
真的走到用户闸而不是被自动放行）：
- `~/.ssh`：`test_cap_files_g15.py::test_ssh_dir_is_a_default_deny_root`、
  `test_read_file_under_ssh_is_high_risk`。
- `~/.aws`：`test_cap_files_g15.py::
  test_aws_and_jones_secrets_are_default_deny_roots`、
  `test_read_file_under_aws_is_high_risk`。
- `~/.jones/secrets/`：同一条 `test_aws_and_jones_secrets_are_default_deny_roots`
  （两个路径同一断言）、`test_read_file_under_jones_secrets_is_high_risk`。

端到端（`SessionService` 级，证明 high 真的落到用户闸而不是被规则闸自动放行）：
`test_read_file_under_ssh_goes_to_user_gate_not_auto_allowed`。
"""

from __future__ import annotations

from ._reuse import run_existing


def test_g15_sensitive_dirs_hidden_acceptance() -> None:
    run_existing(
        "tests/test_cap_files_g15.py::test_ssh_dir_is_a_default_deny_root",
        "tests/test_cap_files_g15.py::"
        "test_aws_and_jones_secrets_are_default_deny_roots",
        "tests/test_cap_files_g15.py::test_read_file_under_ssh_is_high_risk",
        "tests/test_cap_files_g15.py::test_read_file_under_aws_is_high_risk",
        "tests/test_cap_files_g15.py::"
        "test_read_file_under_jones_secrets_is_high_risk",
        "tests/test_cap_files_g15.py::"
        "test_read_file_under_ssh_goes_to_user_gate_not_auto_allowed",
    )
