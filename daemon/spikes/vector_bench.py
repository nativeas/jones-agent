"""向量库选型基准测试（spike #3）。

对候选做同一套测量：插入 N 条 dim 维向量的耗时、top-K 检索延迟（p50/p95/mean）、
recall@k（相对 numpy-brute 精确结果的召回率）、落盘体积。候选：
  - numpy-brute    : 不引库，纯 numpy 内存暴力检索（不落盘，仅做延迟/召回基线参考）
  - sqlite-blob    : 不引库，SQLite BLOB 存向量 + 每次查询整表读出后 numpy 暴力检索
  - sqlite-persist : 不引库，SQLite BLOB 只做持久化，查询用进程内常驻 numpy 矩阵缓存
                      （评审 #8 要求补测的"显而易见的零依赖方案"：延迟应接近 numpy-brute，
                      代价是矩阵常驻内存，用于对照 sqlite-vec 的内存/延迟取舍）
  - sqlite-vec     : sqlite-vec 扩展（vec0 虚拟表，精确 KNN，当前版本不做 ANN）
  - lancedb        : LanceDB 嵌入模式（本地目录）。默认不建索引（flat scan）；
                      传 --lancedb-index 才尝试建 IVF_PQ（版本/数据量相关，可能建索引失败，
                      失败时结果里 indexed=false 并回退到 flat scan 的真实数字，不冒充索引数字）
  - chroma         : Chroma 嵌入模式（PersistentClient，默认 HNSW 索引）

⚠️ 方法论说明（评审 #10）：`gen_data` 生成的是 i.i.d. 标准正态后单位化的随机向量，
两两接近正交、没有真实 embedding 常见的簇结构，是 HNSW/IVF 一类 ANN 索引最不具代表性
的输入分布。recall@k 列在本数据集上不代表真实工作负载下的召回率，只用来确认"索引确实
被使用、且没有严重损坏正确性"这一底线；不要拿本脚本里 lancedb/chroma 的延迟数字和
sqlite-blob/sqlite-vec（精确 KNN）直接比较优劣——精确检索天然没有"用召回率换延迟"这个
自由度，ANN 的意义要在有簇结构的真实数据上另测才成立。

用法（各后端依赖用 uv --with 按需注入，不常驻在 daemon/pyproject.toml 里；
测内存占用时每个后端单独一次 uv run 调用，见报告 §4 的 /usr/bin/time -l 用法，
同一进程内跑多个后端时 rss_max_mb 是跑到该行为止的累计峰值，不是单后端独立值）：

  uv run --python 3.12 --with numpy \
      daemon/spikes/vector_bench.py --backends numpy-brute,sqlite-blob,sqlite-persist --n 100000 --dim 768

  uv run --python 3.12 --with numpy --with sqlite-vec \
      daemon/spikes/vector_bench.py --backends sqlite-vec --n 100000 --dim 768

  uv run --python 3.12 --with numpy --with lancedb \
      daemon/spikes/vector_bench.py --backends lancedb --n 100000 --dim 768 --lancedb-index

  uv run --python 3.12 --with numpy --with chromadb \
      daemon/spikes/vector_bench.py --backends chroma --n 100000 --dim 768

结果以 JSON Lines 打印到 stdout，一行一个后端，便于拼进报告表格。
"""

from __future__ import annotations

import argparse
import json
import resource
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


def ground_truth_topk(base: np.ndarray, queries: np.ndarray, topk: int) -> list[set[int]]:
    """numpy 精确暴力检索的 top-k id 集合，作为 recall@k 的基线（评审 #10）。"""
    out = []
    for q in queries:
        scores = base @ q
        idx = np.argpartition(-scores, topk)[:topk]
        out.append({int(i) for i in idx})
    return out


def recall_at_k(predicted_ids: list[list[int]], ground_truth: list[set[int]]) -> float:
    hits = 0
    total = 0
    for pred, gt in zip(predicted_ids, ground_truth):
        hits += len(set(pred) & gt)
        total += len(gt)
    return round(hits / total, 4) if total else 0.0


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


def rss_max_mb() -> float:
    """当前进程（及已回收子进程）的峰值常驻内存，跑到调用这行为止。
    macOS 上 ru_maxrss 单位是字节，Linux 上是 KB——脚本只在 macOS 上验证过（daemon 目标平台）。"""
    ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(ru / 1024 / 1024, 1) if sys.platform == "darwin" else round(ru / 1024, 1)


# ---------------------------------------------------------------------------
# numpy-brute：内存里直接暴力检索，不落盘，作为延迟/召回基线（无索引开销）
# ---------------------------------------------------------------------------
def bench_numpy_brute(
    base: np.ndarray, queries: np.ndarray, topk: int, workdir: Path,
    ground_truth: list[set[int]] | None = None,
) -> dict:
    t0 = time.perf_counter()
    matrix = base.copy()  # 模拟“加载进内存”这一步
    insert_s = time.perf_counter() - t0

    lat, preds = [], []
    for q in queries:
        t0 = time.perf_counter()
        scores = matrix @ q
        idx = np.argpartition(-scores, topk)[:topk]
        idx = idx[np.argsort(-scores[idx])]
        lat.append((time.perf_counter() - t0) * 1000)
        preds.append(idx.tolist())

    npy_path = workdir / "brute.npy"
    np.save(npy_path, base)
    result = {
        "backend": "numpy-brute",
        "insert_s": round(insert_s, 4),
        "disk_mb": round(dir_size_mb(npy_path), 2),
        "rss_max_mb": rss_max_mb(),
        **latency_stats(lat),
    }
    if ground_truth is not None:
        result["recall_at_k"] = recall_at_k(preds, ground_truth)
    return result


# ---------------------------------------------------------------------------
# sqlite-blob：SQLite 存 BLOB，查询时整表读出到 numpy 再暴力检索（O(N) 每次查询）
# ---------------------------------------------------------------------------
def bench_sqlite_blob(
    base: np.ndarray, queries: np.ndarray, topk: int, workdir: Path,
    ground_truth: list[set[int]] | None = None,
) -> dict:
    import sqlite3

    db_path = workdir / "blob.db"
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA secure_delete=ON")  # 真删要求，见 docs/spikes/03-vector-store.md §8 delete 讨论
    con.execute("CREATE TABLE vecs (id INTEGER PRIMARY KEY, v BLOB NOT NULL)")

    t0 = time.perf_counter()
    con.executemany(
        "INSERT INTO vecs (id, v) VALUES (?, ?)",
        ((i, base[i].tobytes()) for i in range(len(base))),
    )
    con.commit()
    insert_s = time.perf_counter() - t0

    lat, preds = [], []
    dim = base.shape[1]
    for q in queries:
        t0 = time.perf_counter()
        rows = con.execute("SELECT id, v FROM vecs").fetchall()
        mat = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(len(rows), dim)
        scores = mat @ q
        idx = np.argpartition(-scores, topk)[:topk]
        idx = idx[np.argsort(-scores[idx])]
        lat.append((time.perf_counter() - t0) * 1000)
        preds.append([int(i) for i in idx])

    con.close()
    result = {
        "backend": "sqlite-blob",
        "insert_s": round(insert_s, 4),
        "disk_mb": round(dir_size_mb(db_path), 2),
        "rss_max_mb": rss_max_mb(),
        **latency_stats(lat),
    }
    if ground_truth is not None:
        result["recall_at_k"] = recall_at_k(preds, ground_truth)
    return result


# ---------------------------------------------------------------------------
# sqlite-persist：SQLite 只做持久化落盘，查询用进程内常驻 numpy 矩阵缓存
# （评审 #8 要求补测的方案：insert/upsert 时同步写 SQLite + 更新内存矩阵，
#  查询不重读 SQLite。延迟应接近 numpy-brute，代价是矩阵常驻内存，见报告 §5）
# ---------------------------------------------------------------------------
def bench_sqlite_persist(
    base: np.ndarray, queries: np.ndarray, topk: int, workdir: Path,
    ground_truth: list[set[int]] | None = None,
) -> dict:
    import sqlite3

    db_path = workdir / "persist.db"
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA secure_delete=ON")
    con.execute("CREATE TABLE vecs (id INTEGER PRIMARY KEY, v BLOB NOT NULL)")

    dim = base.shape[1]
    n = len(base)
    cache = np.empty((n, dim), dtype=np.float32)  # 常驻内存缓存，模拟 daemon 进程生命周期内不释放

    t0 = time.perf_counter()
    con.executemany(
        "INSERT INTO vecs (id, v) VALUES (?, ?)",
        ((i, base[i].tobytes()) for i in range(n)),
    )
    con.commit()
    cache[:] = base  # upsert 路径同步更新缓存（这里等价于 gen_data 已有的数组，模拟拷贝成本）
    insert_s = time.perf_counter() - t0

    lat, preds = [], []
    for q in queries:
        t0 = time.perf_counter()
        scores = cache @ q
        idx = np.argpartition(-scores, topk)[:topk]
        idx = idx[np.argsort(-scores[idx])]
        lat.append((time.perf_counter() - t0) * 1000)
        preds.append(idx.tolist())

    con.close()
    result = {
        "backend": "sqlite-persist",
        "insert_s": round(insert_s, 4),
        "disk_mb": round(dir_size_mb(db_path), 2),
        "rss_max_mb": rss_max_mb(),
        **latency_stats(lat),
    }
    if ground_truth is not None:
        result["recall_at_k"] = recall_at_k(preds, ground_truth)
    return result


# ---------------------------------------------------------------------------
# sqlite-vec：vec0 虚拟表原生 KNN（精确检索，不整表物化到 Python）
# ---------------------------------------------------------------------------
def bench_sqlite_vec(
    base: np.ndarray, queries: np.ndarray, topk: int, workdir: Path,
    ground_truth: list[set[int]] | None = None,
) -> dict:
    import sqlite3

    import sqlite_vec

    dim = base.shape[1]
    db_path = workdir / "sqlite_vec.db"
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA secure_delete=ON")  # 真删要求，见 docs/spikes/03-vector-store.md §8 delete 讨论
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

    lat, preds = [], []
    for q in queries:
        t0 = time.perf_counter()
        rows = con.execute(
            "SELECT id FROM vecs WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (q.tobytes(), topk),
        ).fetchall()
        lat.append((time.perf_counter() - t0) * 1000)
        preds.append([r[0] for r in rows])

    con.close()
    result = {
        "backend": "sqlite-vec",
        "insert_s": round(insert_s, 4),
        "disk_mb": round(dir_size_mb(db_path), 2),
        "rss_max_mb": rss_max_mb(),
        **latency_stats(lat),
    }
    if ground_truth is not None:
        result["recall_at_k"] = recall_at_k(preds, ground_truth)
    return result


# ---------------------------------------------------------------------------
# LanceDB：本地目录存储，嵌入模式。默认不建索引（flat scan）；
# --lancedb-index 时尝试 create_index，失败则老实回退并标 indexed=false（评审 #6/#9）
# ---------------------------------------------------------------------------
def bench_lancedb(
    base: np.ndarray, queries: np.ndarray, topk: int, workdir: Path,
    ground_truth: list[set[int]] | None = None, build_index: bool = False,
) -> dict:
    import lancedb
    import pyarrow as pa

    dim = base.shape[1]
    db_path = workdir / "lancedb"
    db = lancedb.connect(db_path)

    schema = pa.schema([("id", pa.int64()), ("vector", pa.list_(pa.float32(), dim))])

    t0 = time.perf_counter()
    # 零拷贝构造 FixedSizeList，而不是 list(base)（后者会先把 base 拆成 n 个独立的
    # numpy 标量数组对象，这段 Python 对象转换开销此前被错误计入插入耗时，见评审 #9）
    vector_array = pa.FixedSizeListArray.from_arrays(pa.array(base.reshape(-1)), dim)
    table = db.create_table(
        "vecs",
        data=pa.table({"id": pa.array(np.arange(len(base))), "vector": vector_array}, schema=schema),
    )
    insert_s = time.perf_counter() - t0

    indexed = False
    index_error = None
    if build_index:
        try:
            table.create_index(metric="cosine", vector_column_name="vector")
            indexed = True
        except Exception as e:  # noqa: BLE001 — LanceDB 不同版本/后端抛的异常类型不固定，
            # 这里的诚实失败策略是"记录类型+消息、indexed 保持 false"而不是让脚本崩溃，
            # 不吞掉（向上体现在结果 JSON 的 index_error 字段里，不是 except: pass）
            index_error = f"{type(e).__name__}: {e}"

    lat, preds = [], []
    for q in queries:
        t0 = time.perf_counter()
        rows = table.search(q).limit(topk).to_list()
        lat.append((time.perf_counter() - t0) * 1000)
        preds.append([r["id"] for r in rows])

    result = {
        "backend": "lancedb",
        "indexed": indexed,  # false = flat scan 的真实数字，不是 IVF_PQ 的数字
        "insert_s": round(insert_s, 4),
        "disk_mb": round(dir_size_mb(db_path), 2),
        "rss_max_mb": rss_max_mb(),
        **latency_stats(lat),
    }
    if index_error:
        result["index_error"] = index_error
    if ground_truth is not None:
        result["recall_at_k"] = recall_at_k(preds, ground_truth)
    return result


# ---------------------------------------------------------------------------
# Chroma：嵌入模式 PersistentClient，默认 HNSW（近似检索）
# ---------------------------------------------------------------------------
def bench_chroma(
    base: np.ndarray, queries: np.ndarray, topk: int, workdir: Path,
    ground_truth: list[set[int]] | None = None,
) -> dict:
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

    lat, preds = [], []
    for q in queries:
        t0 = time.perf_counter()
        res = collection.query(query_embeddings=[q.tolist()], n_results=topk)
        lat.append((time.perf_counter() - t0) * 1000)
        preds.append([int(i) for i in res["ids"][0]])

    result = {
        "backend": "chroma",
        "indexed": True,  # Chroma 默认建 HNSW，没有 flat-scan 模式可关
        "insert_s": round(insert_s, 4),
        "disk_mb": round(dir_size_mb(db_path), 2),
        "rss_max_mb": rss_max_mb(),
        **latency_stats(lat),
    }
    if ground_truth is not None:
        result["recall_at_k"] = recall_at_k(preds, ground_truth)
    return result


BACKENDS = {
    "numpy-brute": bench_numpy_brute,
    "sqlite-blob": bench_sqlite_blob,
    "sqlite-persist": bench_sqlite_persist,
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
    p.add_argument(
        "--no-ground-truth", action="store_true",
        help="跳过 numpy 精确基线（recall@k 计算的前提），只测延迟——大 n 时基线本身很快，默认开启",
    )
    p.add_argument(
        "--lancedb-index", action="store_true",
        help="LanceDB 后端尝试 create_index()；不传则明确跑 flat scan，不冒充 IVF_PQ（评审 #6）",
    )
    args = p.parse_args()

    names = list(BACKENDS) if args.backends == "all" else args.backends.split(",")
    for name in names:
        if name not in BACKENDS:
            raise SystemExit(f"unknown backend: {name} (choices: {list(BACKENDS)})")

    base, queries = gen_data(args.n, args.dim, args.n_queries, args.seed)
    print(f"# n={args.n} dim={args.dim} topk={args.topk} n_queries={args.n_queries}", file=sys.stderr)

    ground_truth = None
    if not args.no_ground_truth:
        ground_truth = ground_truth_topk(base, queries, args.topk)

    for name in names:
        workdir = Path(tempfile.mkdtemp(prefix=f"vecbench_{name}_"))
        try:
            fn = BACKENDS[name]
            if name == "lancedb":
                result = fn(base, queries, args.topk, workdir, ground_truth, build_index=args.lancedb_index)
            else:
                result = fn(base, queries, args.topk, workdir, ground_truth)
            result["n"] = args.n
            result["dim"] = args.dim
            print(json.dumps(result, ensure_ascii=False))
        finally:
            if not args.keep:
                shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
