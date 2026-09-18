"""向量库选型基准测试（spike #3）。

对四个候选做同一套测量：插入 N 条 dim 维向量的耗时、top-K 检索延迟（p50/p95）、
落盘体积。候选：
  - numpy-brute   : 不引库，纯 numpy 内存暴力检索（不落盘，仅做延迟基线参考）
  - sqlite-blob   : 不引库，SQLite BLOB 存向量 + 读出后 numpy 暴力检索
  - sqlite-vec    : sqlite-vec 扩展（vec0 虚拟表，KNN）
  - lancedb       : LanceDB 嵌入模式（本地目录，IVF_PQ 索引）
  - chroma        : Chroma 嵌入模式（PersistentClient）

用法（各后端依赖用 uv --with 按需注入，不常驻在 daemon/pyproject.toml 里）：

  uv run --python 3.12 --with numpy \
      daemon/spikes/vector_bench.py --backends numpy-brute,sqlite-blob --n 100000 --dim 768

  uv run --python 3.12 --with numpy --with sqlite-vec \
      daemon/spikes/vector_bench.py --backends sqlite-vec --n 100000 --dim 768

  uv run --python 3.12 --with numpy --with lancedb \
      daemon/spikes/vector_bench.py --backends lancedb --n 100000 --dim 768

  uv run --python 3.12 --with numpy --with chromadb \
      daemon/spikes/vector_bench.py --backends chroma --n 20000 --dim 768   # chroma 见报告里的规模说明

结果以 JSON Lines 打印到 stdout，一行一个后端，便于拼进报告表格。
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

import numpy as np


def gen_data(n: int, dim: int, n_queries: int, seed: int = 42) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    base = rng.standard_normal((n, dim), dtype=np.float32)
    base /= np.linalg.norm(base, axis=1, keepdims=True)
    queries = rng.standard_normal((n_queries, dim), dtype=np.float32)
    queries /= np.linalg.norm(queries, axis=1, keepdims=True)
    return base, queries


def dir_size_mb(path: Path) -> float:
    if path.is_file():
        return path.stat().st_size / 1024 / 1024
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return total / 1024 / 1024


def latency_stats(samples_ms: list[float]) -> dict:
    s = sorted(samples_ms)
    n = len(s)
    return {
        "p50_ms": round(s[n // 2], 3),
        "p95_ms": round(s[min(n - 1, int(n * 0.95))], 3),
        "mean_ms": round(statistics.fmean(s), 3),
    }


# ---------------------------------------------------------------------------
# numpy-brute：内存里直接暴力检索，不落盘，作为延迟基线（无索引开销）
# ---------------------------------------------------------------------------
def bench_numpy_brute(base: np.ndarray, queries: np.ndarray, topk: int, workdir: Path) -> dict:
    t0 = time.perf_counter()
    matrix = base.copy()  # 模拟“加载进内存”这一步
    insert_s = time.perf_counter() - t0

    lat = []
    for q in queries:
        t0 = time.perf_counter()
        scores = matrix @ q
        idx = np.argpartition(-scores, topk)[:topk]
        idx[np.argsort(-scores[idx])]  # 排出最终 top-k 顺序（计入计时，结果本身不需要）
        lat.append((time.perf_counter() - t0) * 1000)

    npy_path = workdir / "brute.npy"
    np.save(npy_path, base)
    return {
        "backend": "numpy-brute",
        "insert_s": round(insert_s, 4),
        "disk_mb": round(dir_size_mb(npy_path), 2),
        **latency_stats(lat),
    }


# ---------------------------------------------------------------------------
# sqlite-blob：SQLite 存 BLOB，查询时整表读出到 numpy 再暴力检索
# ---------------------------------------------------------------------------
def bench_sqlite_blob(base: np.ndarray, queries: np.ndarray, topk: int, workdir: Path) -> dict:
    import sqlite3

    db_path = workdir / "blob.db"
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE vecs (id INTEGER PRIMARY KEY, v BLOB NOT NULL)")

    t0 = time.perf_counter()
    con.executemany(
        "INSERT INTO vecs (id, v) VALUES (?, ?)",
        ((i, base[i].tobytes()) for i in range(len(base))),
    )
    con.commit()
    insert_s = time.perf_counter() - t0

    lat = []
    dim = base.shape[1]
    for q in queries:
        t0 = time.perf_counter()
        rows = con.execute("SELECT id, v FROM vecs").fetchall()
        mat = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(len(rows), dim)
        scores = mat @ q
        idx = np.argpartition(-scores, topk)[:topk]
        idx[np.argsort(-scores[idx])]  # 排出最终 top-k 顺序（计入计时，结果本身不需要）
        lat.append((time.perf_counter() - t0) * 1000)

    con.close()
    return {
        "backend": "sqlite-blob",
        "insert_s": round(insert_s, 4),
        "disk_mb": round(dir_size_mb(db_path), 2),
        **latency_stats(lat),
    }


# ---------------------------------------------------------------------------
# sqlite-vec：vec0 虚拟表原生 KNN
# ---------------------------------------------------------------------------
def bench_sqlite_vec(base: np.ndarray, queries: np.ndarray, topk: int, workdir: Path) -> dict:
    import sqlite3

    import sqlite_vec

    dim = base.shape[1]
    db_path = workdir / "sqlite_vec.db"
    con = sqlite3.connect(db_path)
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    con.execute(f"CREATE VIRTUAL TABLE vecs USING vec0(id INTEGER PRIMARY KEY, embedding float[{dim}])")

    t0 = time.perf_counter()
    con.executemany(
        "INSERT INTO vecs (id, embedding) VALUES (?, ?)",
        ((i, base[i].tobytes()) for i in range(len(base))),
    )
    con.commit()
    insert_s = time.perf_counter() - t0

    lat = []
    for q in queries:
        t0 = time.perf_counter()
        con.execute(
            "SELECT id FROM vecs WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (q.tobytes(), topk),
        ).fetchall()
        lat.append((time.perf_counter() - t0) * 1000)

    con.close()
    return {
        "backend": "sqlite-vec",
        "insert_s": round(insert_s, 4),
        "disk_mb": round(dir_size_mb(db_path), 2),
        **latency_stats(lat),
    }


# ---------------------------------------------------------------------------
# LanceDB：本地目录存储，嵌入模式
# ---------------------------------------------------------------------------
def bench_lancedb(base: np.ndarray, queries: np.ndarray, topk: int, workdir: Path) -> dict:
    import lancedb
    import pyarrow as pa

    dim = base.shape[1]
    db_path = workdir / "lancedb"
    db = lancedb.connect(db_path)

    schema = pa.schema([("id", pa.int64()), ("vector", pa.list_(pa.float32(), dim))])

    t0 = time.perf_counter()
    table = db.create_table(
        "vecs",
        data=pa.table({"id": np.arange(len(base)), "vector": list(base)}, schema=schema),
    )
    insert_s = time.perf_counter() - t0

    lat = []
    for q in queries:
        t0 = time.perf_counter()
        table.search(q).limit(topk).to_list()
        lat.append((time.perf_counter() - t0) * 1000)

    return {
        "backend": "lancedb",
        "insert_s": round(insert_s, 4),
        "disk_mb": round(dir_size_mb(db_path), 2),
        **latency_stats(lat),
    }


# ---------------------------------------------------------------------------
# Chroma：嵌入模式 PersistentClient
# ---------------------------------------------------------------------------
def bench_chroma(base: np.ndarray, queries: np.ndarray, topk: int, workdir: Path) -> dict:
    import chromadb

    db_path = workdir / "chroma"
    client = chromadb.PersistentClient(path=str(db_path))
    collection = client.create_collection("vecs", metadata={"hnsw:space": "cosine"})

    t0 = time.perf_counter()
    batch = 5000
    for start in range(0, len(base), batch):
        end = min(start + batch, len(base))
        collection.add(
            ids=[str(i) for i in range(start, end)],
            embeddings=base[start:end].tolist(),
        )
    insert_s = time.perf_counter() - t0

    lat = []
    for q in queries:
        t0 = time.perf_counter()
        collection.query(query_embeddings=[q.tolist()], n_results=topk)
        lat.append((time.perf_counter() - t0) * 1000)

    return {
        "backend": "chroma",
        "insert_s": round(insert_s, 4),
        "disk_mb": round(dir_size_mb(db_path), 2),
        **latency_stats(lat),
    }


BACKENDS = {
    "numpy-brute": bench_numpy_brute,
    "sqlite-blob": bench_sqlite_blob,
    "sqlite-vec": bench_sqlite_vec,
    "lancedb": bench_lancedb,
    "chroma": bench_chroma,
}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--backends", default="all", help="逗号分隔，或 all")
    p.add_argument("--n", type=int, default=100_000)
    p.add_argument("--dim", type=int, default=768)
    p.add_argument("--topk", type=int, default=10)
    p.add_argument("--n-queries", type=int, default=100)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--keep", action="store_true", help="保留临时目录（默认跑完即删）")
    args = p.parse_args()

    names = list(BACKENDS) if args.backends == "all" else args.backends.split(",")
    for name in names:
        if name not in BACKENDS:
            raise SystemExit(f"unknown backend: {name} (choices: {list(BACKENDS)})")

    base, queries = gen_data(args.n, args.dim, args.n_queries, args.seed)
    print(f"# n={args.n} dim={args.dim} topk={args.topk} n_queries={args.n_queries}", file=sys.stderr)

    for name in names:
        workdir = Path(tempfile.mkdtemp(prefix=f"vecbench_{name}_"))
        try:
            result = BACKENDS[name](base, queries, args.topk, workdir)
            result["n"] = args.n
            result["dim"] = args.dim
            print(json.dumps(result, ensure_ascii=False))
        finally:
            if not args.keep:
                shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
