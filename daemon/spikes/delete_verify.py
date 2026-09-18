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

4. **checkpoint 遇到 SQLITE_BUSY 不能静默留残留（评审第三轮裁定，本轮新增）**：
   `wal_checkpoint(TRUNCATE)` 需要没有其它连接持有会挡住它的读事务/锁；如果
   daemon 进程里同时有别的连接正在读，TRUNCATE 会做不完整（返回的三元组
   `(busy, log, checkpointed)` 里 `busy!=0`），且不会自动重试或报错——调用方
   如果对这个返回值毫无处理，就会在 -wal 里留下残留而不自知。`checkpoint_
   truncate_or_raise()` 把这个返回值当成契约的一部分：busy 时按指数退避有限
   重试，仍然 busy 就抛 `CheckpointBusyError`（不吞），调用方（未来的记忆模块）
   负责把该 Project 标记为"待清理"、下次空闲重试。本脚本用第二个连接开一个
   不提交的读事务人为制造 busy，验证这条路径真的抛异常而不是静默通过。

结论写进 docs/spikes/03-vector-store.md §8：VectorStore 的 delete() 实现必须
（a）建连接时执行 `PRAGMA secure_delete=ON`，（b）每次 DELETE 提交后执行
`PRAGMA wal_checkpoint(TRUNCATE)` 且检查返回三元组的 busy 位，busy 时有限
重试、仍失败则向上抛异常——三者缺一都会在 WAL 模式或高并发场景下留下残留或
静默失败（本脚本用例逐一验证了缺哪一个会导致什么后果）。

用法：
  uv run --python 3.12 --with numpy --with sqlite-vec daemon/spikes/delete_verify.py
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
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


class CheckpointBusyError(RuntimeError):
    """`wal_checkpoint(TRUNCATE)` 在有限重试后仍处于 busy 状态（PRAGMA 返回三元组
    `(busy, log, checkpointed)` 的 busy 位非 0）——delete() 契约要求把这个状态
    向上抛出，不静默吞掉。调用方（未来的记忆模块）负责把该 Project 标记为
    "待清理"，在下次空闲时重试 wal_checkpoint(TRUNCATE)（控制者裁定，见
    docs/spikes/03-vector-store.md §8 delete() docstring）。"""


def checkpoint_truncate_or_raise(
    con: sqlite3.Connection,
    *,
    max_retries: int = 5,
    max_backoff_s: float = 1.0,
) -> None:
    """delete() 提交后调用：执行 `PRAGMA wal_checkpoint(TRUNCATE)`，检查返回的
    `(busy, log, checkpointed)` 三元组里的 busy 位。busy!=0 表示有其它连接的
    读事务/锁挡住了 TRUNCATE（SQLite 行本身已经在 DELETE 里删了，只是 -wal 的
    磁盘级清理这一步暂时做不到）——按指数退避重试最多 `max_retries` 次（每次
    等待翻倍，封顶 `max_backoff_s` 秒），仍然 busy 就抛出 `CheckpointBusyError`，
    不静默返回：调用方必须处理（标记 Project 待清理 + 下次空闲重试），不能假装
    真删已经完成。"""
    busy = log = checkpointed = None
    delay = 0.0
    for _ in range(max_retries):
        if delay:
            time.sleep(delay)
        busy, log, checkpointed = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if not busy:
            return
        delay = min(max_backoff_s, (delay or 0.01) * 2)
    raise CheckpointBusyError(
        f"wal_checkpoint(TRUNCATE) 重试 {max_retries} 次后仍 busy "
        f"(busy={busy}, log={log}, checkpointed={checkpointed})"
    )


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


# ---------------------------------------------------------------------------
# checkpoint busy → 重试 → 抛异常（评审第三轮裁定）：单独验证
# checkpoint_truncate_or_raise() 本身的契约，不是 WAL 残留矩阵的一部分。
# ---------------------------------------------------------------------------
def check_checkpoint_busy_raises() -> bool:
    """用第二个连接开一个不提交的读事务，人为制造 `wal_checkpoint(TRUNCATE)`
    的 busy 状态（读事务挡住 TRUNCATE 需要的排它访问），验证
    `checkpoint_truncate_or_raise()` 确实在有限重试后抛出 `CheckpointBusyError`，
    而不是静默返回、留下残留却不报错。"""
    d = Path(tempfile.mkdtemp())
    con = sqlite3.connect(d / "t.db")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA secure_delete=ON")
    con.execute("CREATE TABLE vecs (id INTEGER PRIMARY KEY, v BLOB NOT NULL)")
    con.execute("INSERT INTO vecs (id, v) VALUES (1, ?)", (_marker(),))
    for i in range(2, 200):
        con.execute("INSERT INTO vecs (id, v) VALUES (?, ?)", (i, os.urandom(3072)))
    con.commit()
    con.execute("DELETE FROM vecs WHERE id=1")
    con.commit()

    reader = sqlite3.connect(d / "t.db")
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM vecs").fetchone()  # 持有读事务，挡住 TRUNCATE

    raised = False
    try:
        checkpoint_truncate_or_raise(con, max_retries=3, max_backoff_s=0.1)
    except CheckpointBusyError:
        raised = True
    finally:
        reader.commit()
        reader.close()
        con.close()
    return raised


def check_checkpoint_succeeds_without_blocker() -> bool:
    """对照用例：没有其它连接挡着时，`checkpoint_truncate_or_raise()` 应该一次
    成功、不抛异常——证明上一条用例的"抛异常"确实是 busy 触发的，不是函数本身
    坏了、逢查就抛。"""
    d = Path(tempfile.mkdtemp())
    con = sqlite3.connect(d / "t.db")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA secure_delete=ON")
    con.execute("CREATE TABLE vecs (id INTEGER PRIMARY KEY, v BLOB NOT NULL)")
    con.execute("INSERT INTO vecs (id, v) VALUES (1, ?)", (_marker(),))
    con.commit()
    con.execute("DELETE FROM vecs WHERE id=1")
    con.commit()
    ok = False
    try:
        checkpoint_truncate_or_raise(con, max_retries=3, max_backoff_s=0.1)
        ok = True
    except CheckpointBusyError:
        ok = False
    finally:
        con.close()
    return ok


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
    # 每条用例现在带两个独立的期望值：expect_scrubbed（整体 workdir 扫描，含主库
    # 文件）和 expect_wal_scrubbed（只看 -wal 文件本身的字节）。两者**不总是相等**
    # ——第 2 条（blob + secure_delete=OFF + checkpoint）就是反例：checkpoint(TRUNCATE)
    # 会把 -wal 截空（wal_scrubbed=True），但它搬进主库文件的那一页没有被
    # secure_delete 零化，marker 字节留在主库文件里（scrubbed_overall=False）。
    # 之前的版本只打印 wal_scrubbed、不断言，这个反例被悄悄放过了；现在两个字段
    # 都按各自的期望值断言。
    wal_cases = [
        (
            "sqlite-blob WAL + secure_delete=ON + 不 checkpoint",
            lambda: check_blob_wal(secure_delete=True, checkpoint_truncate=False),
            False,  # 预期整体残留：WAL 是 append-only，不 checkpoint 就抹不掉旧 frame
            False,  # -wal 本身也残留：secure_delete 的零化写入只是追加新 frame，不动旧 frame
        ),
        (
            "sqlite-blob WAL + secure_delete=OFF + checkpoint(TRUNCATE)",
            lambda: check_blob_wal(secure_delete=False, checkpoint_truncate=True),
            False,  # 预期整体残留：checkpoint 只搬运/截断 WAL，不负责零化合并进主库的页
            True,  # 但 -wal 本身是干净的——TRUNCATE 已经把它截到 0 字节，残留搬进了主库文件
        ),
        (
            "sqlite-blob WAL + secure_delete=ON + checkpoint(TRUNCATE)  [推荐配置]",
            lambda: check_blob_wal(secure_delete=True, checkpoint_truncate=True),
            True,  # 两者都要才真删
            True,
        ),
        (
            "sqlite-vec (vec0) WAL + secure_delete=ON + 不 checkpoint",
            lambda: check_vec0_wal(secure_delete=True, checkpoint_truncate=False),
            False,
            False,
        ),
        (
            # 与 sqlite-blob 那条不同：vec0 的 DELETE 在应用层已经把向量槽位覆写为
            # 干净数据（见上方"非 WAL"用例的发现），不依赖 SQLite 的 secure_delete
            # 零化"合并进主库的页"这一步；所以对 vec0 来说 checkpoint(TRUNCATE) 单独
            # 就够用，secure_delete=OFF 不影响结果——这条预期是 SCRUBBED，不是残留。
            # （实测过程中先假设"两者都要"套用了 blob 的结论，实测发现对 vec0 不成立，
            # 已按实测改期望值，不是把测试改到凑预期。这条结论目前只在单个 vec0
            # chunk（499 行，未跨 chunk 边界）下验证过，见 §8/文末"证据边界"说明。）
            "sqlite-vec (vec0) WAL + secure_delete=OFF + checkpoint(TRUNCATE)",
            lambda: check_vec0_wal(secure_delete=False, checkpoint_truncate=True),
            True,
            True,
        ),
        (
            "sqlite-vec (vec0) WAL + secure_delete=ON + checkpoint(TRUNCATE)  [推荐配置]",
            lambda: check_vec0_wal(secure_delete=True, checkpoint_truncate=True),
            True,
            True,
        ),
    ]
    for label, fn, expect_scrubbed, expect_wal_scrubbed in wal_cases:
        result = fn()
        scrubbed = result["scrubbed_overall"]
        wal_scrubbed = result["wal_scrubbed"]
        status = "SCRUBBED (真删)" if scrubbed else "残留 (LEAK)"
        note = ""
        if scrubbed != expect_scrubbed or wal_scrubbed != expect_wal_scrubbed:
            note = "  <-- 与预期不符！"
            failed = True
        print(
            f"{label}: {status}  "
            f"(连接关闭前 wal_scrubbed={wal_scrubbed}, "
            f"连接关闭后 scrubbed={result['scrubbed_after_close']}){note}"
        )

    # --- checkpoint busy → 重试 → 抛异常（评审第三轮裁定）---
    busy_cases = [
        (
            "wal_checkpoint(TRUNCATE) 被其它连接的读事务挡住 → 有限重试后抛 CheckpointBusyError",
            check_checkpoint_busy_raises,
            True,
        ),
        (
            "wal_checkpoint(TRUNCATE) 无阻塞 → 一次成功、不抛异常",
            check_checkpoint_succeeds_without_blocker,
            True,
        ),
    ]
    for label, fn, expect_ok in busy_cases:
        ok = fn()
        note = ""
        if ok != expect_ok:
            note = "  <-- 与预期不符！"
            failed = True
        print(f"{label}: {'符合预期' if ok == expect_ok else '不符合预期'}{note}")

    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
