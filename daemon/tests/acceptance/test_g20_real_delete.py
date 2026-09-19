"""G20 — PRD 12.1 发布门禁:

"真删：删除 Session / Project / 记忆后，SQLite 行、payload 文件、向量分片均不
存在" —— 验证方式："删除后检查磁盘"。

记忆（长期记忆向量库）是 FR18（PRD 8.2 P1，"v1 尽力、可顺延"），v1 未实现，见
`docs/acceptance/v1.0/FR-checklist.md`——没有向量分片可删，这一分句在 v1 下是
空真（vacuously true），不是缺口。

复用（不重复造），Session/Project（含级联的 Run）各一，`store/maintenance.py`
（04-w5-interfaces.md §「真删（G20）」/ Issue #23）：
- `test_storage_maintenance.py::
  test_delete_session_cascades_every_row_and_purges_payload`——SQLite 行级联
  删除 + payload 目录真删。
- `test_storage_maintenance.py::
  test_delete_run_cascades_and_purges_payload_and_nulls_the_turn_reference`。
- `test_storage_maintenance.py::
  test_delete_project_removes_row_and_attachments_and_checkpoints`——含
  `wal_checkpoint(TRUNCATE)`，删除在 WAL 模式下也真的落盘、不是逻辑删除还留在
  WAL 里可恢复。
"""

from __future__ import annotations

from ._reuse import run_existing


def test_g20_real_delete_acceptance() -> None:
    run_existing(
        "tests/test_storage_maintenance.py::"
        "test_delete_session_cascades_every_row_and_purges_payload",
        "tests/test_storage_maintenance.py::"
        "test_delete_run_cascades_and_purges_payload_and_nulls_the_turn_reference",
        "tests/test_storage_maintenance.py::"
        "test_delete_project_removes_row_and_attachments_and_checkpoints",
    )
