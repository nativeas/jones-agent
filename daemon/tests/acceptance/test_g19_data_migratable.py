"""G19 — PRD 12.1 发布门禁 (同时是 N17):

"数据可迁移：`~/.jones/` 整目录拷到另一台机，重录 Key 后会话历史、Agent、Skill、
记忆完整可用" —— 验证方式："跨机迁移"。

N17（12.2 负面清单）："升级后会话历史、记忆、Agent 定义丢失或不可读"——升级场景
下的同一约束。

复用（不重复造）：`test_migrator_upgrade_e2e.py::
test_current_build_opens_a_database_created_by_the_previous_build`——用上一
个真实 git 版本（`v0.9-pre` 或该分支与 main 的 merge-base）起一个真实 daemon 建
出真实 `JONES_HOME`（默认 Project/Agent、主 Session），再用当前版本的 daemon
打开同一个 `JONES_HOME`，断言会话/Agent 完整存活——这正是"整目录拷到另一处
（这里是另一个版本）后数据完整可用"的字面证明。慢（真实 `git worktree` +
`uv sync`），按该模块自己的约定用 `JONES_E2E=1` 门控，不在默认 `pytest -q` /
`make check-daemon` 下跑。

**round-1 review fix (评审 #1/#4)**: 之前这里直接把 `run_existing` 透传给目标
测试，默认情况下（`JONES_E2E` 未设）目标测试被跳过、`returncode == 0`，这条
acceptance 用例本身也就"通过"了——CI 里 G19/N17 因此结构上不可能失败，跳过即
绿。现在改为这条 acceptance 测试自己显式 `skipif`：`JONES_E2E` 未设时它自己
呈现为"skipped"（未验证），而不是借 `run_existing` 把内层的 skip 洗成外层的
"passed"。`JONES_E2E=1` 时才真正调用 `run_existing`——它现在拒绝"零 passed /
含 skipped"的结果（见 `_reuse.py`），所以即使设了 `JONES_E2E=1` 但本机没有
`v0.9-pre` tag（目标测试内部 fixture 会再跳过一次，见该文件模块 docstring），
这里也会如实失败，而不是继续悄悄地通过。手工/E2E 记录见
`docs/acceptance/v1.0/G19.md`。
"""

from __future__ import annotations

import os

import pytest

from ._reuse import run_existing


@pytest.mark.skipif(
    not os.environ.get("JONES_E2E"),
    reason="G19/N17 需要真实 daemon 起停验证跨版本数据迁移（真实 git worktree + "
    "uv sync，慢且需要本地 v0.9-pre tag），未在默认 CI/`make check-daemon` 跑："
    "设 JONES_E2E=1 本地手动验证，见 docs/DEV.md、docs/acceptance/v1.0/G19.md",
)
def test_g19_n17_data_migratable_acceptance() -> None:
    # `run_existing` already inherits the parent process's full environment
    # (including `JONES_E2E`, if the caller set it) into the subprocess.
    run_existing(
        "tests/integration/test_migrator_upgrade_e2e.py::"
        "test_current_build_opens_a_database_created_by_the_previous_build",
        timeout=600,
    )
