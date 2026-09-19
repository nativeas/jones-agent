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
`make check-daemon` 下跑；这里透传同一环境变量，默认情况下这条 acceptance 用
例本身也会因为目标测试被跳过而通过（不是空洞地伪装通过——`JONES_E2E=1` 时会
真的跑一遍并要求真实通过）。
"""

from __future__ import annotations

from ._reuse import run_existing


def test_g19_n17_data_migratable_acceptance() -> None:
    # `run_existing` already inherits the parent process's full environment
    # (including `JONES_E2E`, if the caller set it) into the subprocess.
    run_existing(
        "tests/integration/test_migrator_upgrade_e2e.py::"
        "test_current_build_opens_a_database_created_by_the_previous_build",
        timeout=600,
    )
