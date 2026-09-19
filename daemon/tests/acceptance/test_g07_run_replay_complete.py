"""G07 — PRD 12.1 发布门禁:

"Run 回放完整：任一 Run 可回放全部 Step 的参数、结果、耗时、审批结果、发送给
模型的完整 prompt" —— 验证方式："随机抽 5 个 Run 回放，与实时记录比对"。

复用（不重复造）：
- `test_sessions_service.py::test_run_steps_includes_the_steps_permission_decision`
  ——`run.steps` 回放里带上了该 Step 的审批结果（回放数据以 SQLite 为事实源，
  02-w3-interfaces.md §2）。
- `test_replay_store.py`：Step 参数/结果 payload 与发给模型的完整 prompt 快照
  （`write_prompt_snapshot`/`read_payload`）的落盘与读回往返。

桌面端回放 UI（逐 Step 前进/后退、不触发真实动作）见
`apps/desktop/tests/acceptance/test_g07_replay_ui.test.ts`。
"""

from __future__ import annotations

from ._reuse import run_existing


def test_g07_run_replay_acceptance() -> None:
    run_existing(
        "tests/test_sessions_service.py::"
        "test_run_steps_includes_the_steps_permission_decision",
        "tests/test_replay_store.py::test_write_then_read_payload_roundtrips",
        "tests/test_replay_store.py::test_write_prompt_snapshot_roundtrips",
    )
