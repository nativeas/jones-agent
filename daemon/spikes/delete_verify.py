"""验证 SQLite 后端的 delete() 能否满足"真删"（第二轮评审裁定，PRD 10.4）：

    真删 = SQLite 行 + payload 文件 + 向量分片一起删。磁盘级残留（页面/WAL frame
    里的原始字节还在文件里）由「删除后 wal_checkpoint(TRUNCATE) + secure_delete」满足，
    不要求"文件系统底层扇区物理擦除"（那是操作系统/磁盘层的事，任何用户态数据库都做不到）。

本脚本验证三层递进的问题：

1. **非 WAL 模式（旧版已验证）**：普通 `DELETE` 只把页面挂回 freelist，BLOB 原文
   原样留在库文件里直到 `VACUUM`；`PRAGMA secure_delete=ON` 能让 DELETE 就地
   把被删内容覆写为 0，不需要 VACUUM。sqlite-blob 表验证了这条。

2. **vec0 的 DELETE 在应用层已经零化向量数据，与 secure_delete 无关**（本轮新发现，
   见下方"vec0 结果"）：手动探测 `vecs_vector_chunks00` 影子表发现，vec0 的
   DELETE 实现会主动把被删行的向量槽位覆写掉，不依赖 SQLite 的 secure_delete
   PRAGMA——所以"非 WAL + secure_delete=OFF/ON"这两个 vec0 用例天然都会
   SCRUBBED，不是测试设计有问题，而是 vec0 本身在这个维度上没有可区分的行为。
   这两个用例保留，但期望值改为都是 SCRUBBED，并在下面解释原因（评审第二轮 #3）。

3. **WAL 模式下的残留是评审第二轮 #1 指出的真实问题，本脚本新增验证**：WAL 是
   append-only 日志，secure_delete 只保证"新写入的页"内容干净（包括 DELETE
   产生的"已清零"页），不保证 -wal 文件里更早的、包含原始 marker 字节的旧 frame
   被抹掉——那些旧 frame 只有在 `wal_checkpoint(TRUNCATE)` 把 -wal 文件截断到
   0 字节时才会被物理清除。这一层对 sqlite-blob 和 vec0 都适用（vec0 虽然在
   应用层零化了向量槽位，但那次覆写本身在 WAL 模式下也只是追加一个新 frame，
   不会抹掉更早的、包含原始 INSERT 数据的旧 frame）。

    "推荐配置"（WAL + secure_delete）下的真删契约 = 删除后执行
    `PRAGMA wal_checkpoint(TRUNCATE)`。WAL 场景的用例直接读 -wal 文件的字节
    做检查（不只是整体扫描 workdir），并且验证"checkpoint 后 -wal 不含残留"这个
    具体断言。

结论写进 docs/spikes/03-vector-store.md §8：VectorStore 的 delete() 实现必须
（a）建连接时执行 `PRAGMA secure_delete=ON`，且（b）每次 DELETE 提交后执行
`PRAGMA wal_checkpoint(TRUNCATE)`——两者缺一都会在 WAL 模式下留下残留（本脚本
用例逐一验证了缺哪一个会导致残留）。

用法：
  uv run --python 3.12 --with numpy --with sqlite-vec daemon/spikes/delete_verify.py
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

DIM = 768


def _marker(n: int = 3072) -> bytes:
    return bytes([0xAB, 0xCD, 0xEF, 0x12] * (n // 4))


def _scrubbed(workdir: Path, needle: bytes) -> bool:
    """True 表示 workdir 下任何文件（主库、-wal、-shm 全部含在内）都找不到
    needle 的前缀了（已被真删）。"""
    prefix = needle[:64]
    for f in workdir.rglob("*"):
        if f.is_file() and prefix in f.read_bytes():
            return False
    return True


def _wal_path(workdir: Path) -> Path | None:
    matches = list(workdir.glob("*-wal"))
    return matches[0] if matches else None


def _wal_scrubbed(workdir: Path, needle: bytes) -> bool:
    """专门检查 -wal 文件本身的字节（评审第二轮 #1 明确要求：读 -wal 文件字节做检查，
    不能只看整体 workdir 扫描）。文件不存在或已被 TRUNCATE 到 0 字节都算"干净"。"""
    p = _wal_path(workdir)
    if p is None or not p.exists():
        return True
    data = p.read_bytes()
    if not data:
        return True
    return needle[:64] not in data


# ---------------------------------------------------------------------------
# 非 WAL 模式（旧版用例，保留）
# ---------------------------------------------------------------------------
def check_blob(*, secure_delete: bool, vacuum: bool) -> bool:
    d = Path(tempfile.mkdtemp())
    con = sqlite3.connect(d / "t.db")
    if secure_delete:
        con.execute("PRAGMA secure_delete=ON")
    con.execute("CREATE TABLE vecs (id INTEGER PRIMARY KEY, v BLOB NOT NULL)")
    marker = _marker()
    con.execute("INSERT INTO vecs (id, v) VALUES (1, ?)", (marker,))
    for i in range(2, 200):  # 填充其它行，避免删除的页恰好是文件末尾这种平凡情况
        con.execute("INSERT INTO vecs (id, v) VALUES (?, ?)", (i, os.urandom(3072)))
    con.commit()
    con.execute("DELETE FROM vecs WHERE id=1")
    con.commit()
    if vacuum:
        con.execute("VACUUM")
    con.close()
    return _scrubbed(d, marker)


def check_vec0(*, secure_delete: bool, vacuum: bool) -> bool:
    import numpy as np
    import sqlite_vec

    d = Path(tempfile.mkdtemp())
    con = sqlite3.connect(d / "t.db")
    if secure_delete:
        con.execute("PRAGMA secure_delete=ON")
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    con.execute(f"CREATE VIRTUAL TABLE vecs USING vec0(id INTEGER PRIMARY KEY, embedding float[{DIM}])")
    marker_vec = np.full(DIM, 1.2345e-30, dtype=np.float32)
    marker = marker_vec.tobytes()
    con.execute("INSERT INTO vecs (id, embedding) VALUES (1, ?)", (marker,))
    rng = np.random.default_rng(0)
    for i in range(2, 500):
        con.execute("INSERT INTO vecs (id, embedding) VALUES (?, ?)", (i, rng.standard_normal(DIM, dtype=np.float32).tobytes()))
    con.commit()
    con.execute("DELETE FROM vecs WHERE id=1")
    con.commit()
    if vacuum:
        con.execute("VACUUM")
    con.close()
    return _scrubbed(d, marker)


# ---------------------------------------------------------------------------
# WAL 模式（新增，评审第二轮 #1）：验证"推荐配置"（WAL + secure_delete）实际
# 满不满足真删，以及 wal_checkpoint(TRUNCATE) 是否能补齐它。
# ---------------------------------------------------------------------------
def check_blob_wal(*, secure_delete: bool, checkpoint_truncate: bool) -> dict:
    """关键：检查点在 `con.close()` 之前做——daemon 是常驻进程，一次 delete 不会
    触发连接关闭。SQLite 在"最后一个连接 close()"时会自动做一次隐式 checkpoint
    并删掉 -wal/-shm 文件，这个"关闭时的兜底清理"掩盖了 WAL 残留的真实窗口期：
    daemon 进程的连接可能几小时都不关闭，中间任何时刻读这个 -wal 文件都可能读到
    刚删除的向量原文。所以本函数在 close() 之前、也在 close() 之后各测一次，
    两个数字都返回，报告里要看的是"关闭之前"这个数字（模拟常驻连接的真实状态）。
    """
    d = Path(tempfile.mkdtemp())
    con = sqlite3.connect(d / "t.db")
    con.execute("PRAGMA journal_mode=WAL")
    if secure_delete:
        con.execute("PRAGMA secure_delete=ON")
    con.execute("CREATE TABLE vecs (id INTEGER PRIMARY KEY, v BLOB NOT NULL)")
    marker = _marker()
    con.execute("INSERT INTO vecs (id, v) VALUES (1, ?)", (marker,))
    for i in range(2, 200):
        con.execute("INSERT INTO vecs (id, v) VALUES (?, ?)", (i, os.urandom(3072)))
    con.commit()
    con.execute("DELETE FROM vecs WHERE id=1")
    con.commit()
    if checkpoint_truncate:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    wal_scrubbed_before_close = _wal_scrubbed(d, marker)
    scrubbed_overall_before_close = _scrubbed(d, marker)
    con.close()  # 隐式 checkpoint 会在这里把残留"善后"掉——不代表显式契约生效了
    return {
        "wal_scrubbed": wal_scrubbed_before_close,
        "scrubbed_overall": scrubbed_overall_before_close,
        "scrubbed_after_close": _scrubbed(d, marker),
    }


def check_vec0_wal(*, secure_delete: bool, checkpoint_truncate: bool) -> dict:
    import numpy as np
    import sqlite_vec

    d = Path(tempfile.mkdtemp())
    con = sqlite3.connect(d / "t.db")
    con.execute("PRAGMA journal_mode=WAL")
    if secure_delete:
        con.execute("PRAGMA secure_delete=ON")
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    con.execute(f"CREATE VIRTUAL TABLE vecs USING vec0(id INTEGER PRIMARY KEY, embedding float[{DIM}])")
    marker_vec = np.full(DIM, 1.2345e-30, dtype=np.float32)
    marker = marker_vec.tobytes()
    con.execute("INSERT INTO vecs (id, embedding) VALUES (1, ?)", (marker,))
    rng = np.random.default_rng(0)
    for i in range(2, 500):
        con.execute("INSERT INTO vecs (id, embedding) VALUES (?, ?)", (i, rng.standard_normal(DIM, dtype=np.float32).tobytes()))
    con.commit()
    con.execute("DELETE FROM vecs WHERE id=1")
    con.commit()
    if checkpoint_truncate:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    wal_scrubbed_before_close = _wal_scrubbed(d, marker)
    scrubbed_overall_before_close = _scrubbed(d, marker)
    con.close()
    return {
        "wal_scrubbed": wal_scrubbed_before_close,
        "scrubbed_overall": scrubbed_overall_before_close,
        "scrubbed_after_close": _scrubbed(d, marker),
    }


def main() -> None:
    failed = False

    # --- 非 WAL 用例（旧版保留） ---
    simple_cases = [
        ("sqlite-blob DELETE only (no secure_delete, no vacuum)", lambda: check_blob(secure_delete=False, vacuum=False), False),
        ("sqlite-blob DELETE + secure_delete=ON", lambda: check_blob(secure_delete=True, vacuum=False), True),
        ("sqlite-blob DELETE + VACUUM (no secure_delete)", lambda: check_blob(secure_delete=False, vacuum=True), True),
        # vec0 的两条：DELETE 在应用层已经零化向量槽位，不依赖 secure_delete——
        # 两条期望值都是 True（SCRUBBED），这不是测试没有区分力，而是 vec0 在
        # "非 WAL + secure_delete 开关"这个维度上真实地没有区分（见模块 docstring
        # 第 2 点、评审第二轮 #3 的处理说明）。有区分力的维度移到下面的 WAL 用例。
        ("sqlite-vec (vec0) DELETE only (no secure_delete, no vacuum) — vec0 自身零化，预期仍 SCRUBBED", lambda: check_vec0(secure_delete=False, vacuum=False), True),
        ("sqlite-vec (vec0) DELETE + secure_delete=ON", lambda: check_vec0(secure_delete=True, vacuum=False), True),
    ]
    for label, fn, expect_scrubbed in simple_cases:
        scrubbed = fn()
        status = "SCRUBBED (真删)" if scrubbed else "残留 (LEAK)"
        note = ""
        if scrubbed != expect_scrubbed:
            note = "  <-- 与预期不符！"
            failed = True
        print(f"{label}: {status}{note}")

    # --- WAL 用例（新增，评审第二轮 #1）：三选一组合，验证"checkpoint(TRUNCATE) +
    # secure_delete 两者都要"这个契约，对 blob 和 vec0 各测一遍。 ---
    wal_cases = [
        (
            "sqlite-blob WAL + secure_delete=ON + 不 checkpoint",
            lambda: check_blob_wal(secure_delete=True, checkpoint_truncate=False),
            False,  # 预期残留：WAL 是 append-only，不 checkpoint 就抹不掉旧 frame
        ),
        (
            "sqlite-blob WAL + secure_delete=OFF + checkpoint(TRUNCATE)",
            lambda: check_blob_wal(secure_delete=False, checkpoint_truncate=True),
            False,  # 预期残留：checkpoint 只搬运/截断 WAL，不负责零化合并进主库的页
        ),
        (
            "sqlite-blob WAL + secure_delete=ON + checkpoint(TRUNCATE)  [推荐配置]",
            lambda: check_blob_wal(secure_delete=True, checkpoint_truncate=True),
            True,  # 两者都要才真删
        ),
        (
            "sqlite-vec (vec0) WAL + secure_delete=ON + 不 checkpoint",
            lambda: check_vec0_wal(secure_delete=True, checkpoint_truncate=False),
            False,
        ),
        (
            # 与 sqlite-blob 那条不同：vec0 的 DELETE 在应用层已经把向量槽位覆写为
            # 干净数据（见上方"非 WAL"用例的发现），不依赖 SQLite 的 secure_delete
            # 零化"合并进主库的页"这一步；所以对 vec0 来说 checkpoint(TRUNCATE) 单独
            # 就够用，secure_delete=OFF 不影响结果——这条预期是 SCRUBBED，不是残留。
            # （实测过程中先假设"两者都要"套用了 blob 的结论，实测发现对 vec0 不成立，
            # 已按实测改期望值，不是把测试改到凑预期。）
            "sqlite-vec (vec0) WAL + secure_delete=OFF + checkpoint(TRUNCATE)",
            lambda: check_vec0_wal(secure_delete=False, checkpoint_truncate=True),
            True,
        ),
        (
            "sqlite-vec (vec0) WAL + secure_delete=ON + checkpoint(TRUNCATE)  [推荐配置]",
            lambda: check_vec0_wal(secure_delete=True, checkpoint_truncate=True),
            True,
        ),
    ]
    for label, fn, expect_scrubbed in wal_cases:
        result = fn()
        scrubbed = result["scrubbed_overall"]
        status = "SCRUBBED (真删)" if scrubbed else "残留 (LEAK)"
        note = ""
        if scrubbed != expect_scrubbed:
            note = "  <-- 与预期不符！"
            failed = True
        print(
            f"{label}: {status}  "
            f"(连接关闭前 wal_scrubbed={result['wal_scrubbed']}, "
            f"连接关闭后 scrubbed={result['scrubbed_after_close']}){note}"
        )

    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
