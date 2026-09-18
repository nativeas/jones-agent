"""验证 SQLite 后端的 delete() 能否满足 PRD G20（真删：磁盘上不留残留）。

背景（评审 #5）：普通 SQLite `DELETE` 只把页面挂回 freelist，BLOB 原文原样留在库文件里
直到 `VACUUM`；`PRAGMA secure_delete` 默认关闭。本脚本往一张表里插入一条带
可识别标记的向量，删除它，然后直接 hexdump/搜索库文件字节，确认标记是否还在磁盘上——
分别验证 sqlite-blob（普通 BLOB 表）和 sqlite-vec（vec0 虚拟表）两条路径，
在「什么都不做」「只开 secure_delete」「只 VACUUM」三种策略下的行为。

结论写进 docs/spikes/03-vector-store.md §8：VectorStore 的 delete() 实现必须在
建连接时执行 `PRAGMA secure_delete=ON`（写路径按页面就地覆写，开销只摊在被删的页上，
不像 VACUUM 要重写整个文件），不能只满足于"调用了 SQLite 的 DELETE"。

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
    """True 表示磁盘上任何文件都找不到 needle 的前缀了（已被真删）。"""
    prefix = needle[:64]
    for f in workdir.rglob("*"):
        if f.is_file() and prefix in f.read_bytes():
            return False
    return True


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


def main() -> None:
    cases = [
        ("sqlite-blob DELETE only (no secure_delete, no vacuum)", lambda: check_blob(secure_delete=False, vacuum=False), False),
        ("sqlite-blob DELETE + secure_delete=ON", lambda: check_blob(secure_delete=True, vacuum=False), True),
        ("sqlite-blob DELETE + VACUUM (no secure_delete)", lambda: check_blob(secure_delete=False, vacuum=True), True),
        ("sqlite-vec (vec0) DELETE only (no secure_delete, no vacuum)", lambda: check_vec0(secure_delete=False, vacuum=False), None),
        ("sqlite-vec (vec0) DELETE + secure_delete=ON", lambda: check_vec0(secure_delete=True, vacuum=False), True),
    ]
    failed = False
    for label, fn, expect_scrubbed in cases:
        scrubbed = fn()
        status = "SCRUBBED (真删)" if scrubbed else "残留 (LEAK)"
        note = ""
        if expect_scrubbed is not None and scrubbed != expect_scrubbed:
            note = "  <-- 与预期不符！"
            failed = True
        print(f"{label}: {status}{note}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
