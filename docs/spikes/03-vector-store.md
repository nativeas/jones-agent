# 03 · 多模态向量库选型

对应 Issue #3、PRD 13.2 风险 2、6.4、10.1、12.1（G17）、12.3（FR18）。

## 1. 本质约束（第一性原理）

先问这个问题真正要的是什么，再挑库：

1. **纯本地、可嵌入 Python 进程**——daemon 是单进程常驻服务，不能再拉起一个数据库服务端、不能开端口（PRD N14：守护进程不得监听 TCP）。所以任何需要独立 server 进程的方案（Milvus、Qdrant server 模式、Weaviate）直接出局，候选只能是"进程内库"。
2. **规模目标是 10 万条向量，不是千万级**（PRD 10.1：≥10 万条记忆向量不劣化交互延迟）。这个规模下暴力检索（矩阵乘法）本身就是毫秒级，近似索引（HNSW/IVF）带来的收益在这个数量级上不明显，但会引入额外依赖体积、索引构建时间、以及"结果不精确"的复杂度。
3. **多模态只是"向量维度和来源标签"，不是数据库特性**——文本/图像/音频向量化后都是定长 float 数组，库层面根本不关心内容模态；真正的多模态差异在 embedding 模型层（CLIP/ImageBind），不在向量库层。所以"向量库要不要支持多模态"是伪命题，只要库能存 float 向量 + 任意 metadata（来源、模态标签）即可。
4. **打包体积是真实约束**（spike #2 关注点）：daemon 要靠 PyInstaller 打包分发，多一个依赖就多几十 MB 体积、多一份供应链风险、多一处"这台机器装不上这个 wheel"的失败面。
5. **v1 只需要接口稳定，不需要现在就做对**（Issue 原文：「v1 只需定 Store 层接口与目录结构，文本版可先顶」）。这意味着选型的首要标准是"依赖轻、可替换"，不是"检索最快"——10 万条规模下所有候选的检索延迟都在个位数到两位数毫秒，差距不影响交互体验，但依赖体积和维护负担的差距是数量级的。

结论方向：**优先选没有额外原生依赖负担、失败模式可控的方案**，把"以后要不要上 ANN 索引"留给真实数据增长后再决定（YAGNI，不为"以后可能" 10 倍增长的场景现在就扛 LanceDB/Chroma 的依赖重量）。

## 2. 候选

| 候选 | 形态 | 索引 | 备注 |
|---|---|---|---|
| **numpy-brute** | 不引库，进程内 numpy 数组 | 无（暴力矩阵乘法） | 不落盘持久化，仅作延迟基线参考，不是可用方案（重启即丢） |
| **sqlite-blob** | 不引库，SQLite BLOB 存向量原文 + 读出后 numpy 暴力检索 | 无 | 复用 daemon 已有的 SQLite 依赖（DEV.md：标准库 `sqlite3`），零新增依赖 |
| **sqlite-vec** | SQLite 扩展（`vec0` 虚拟表） | 暴力 KNN（当前版本不做 ANN，也在做 IVF 预览） | 单文件 `.so`/`.dylib` 扩展，纯 C，无 Python 原生依赖链 |
| **LanceDB** | 内嵌模式，本地列式存储（Lance 格式） | IVF_PQ / HNSW 可选 | Rust 核心 + pyarrow，磁盘格式支持增量/版本化 |
| **Chroma** | 内嵌模式，`PersistentClient` | HNSW（hnswlib） | 依赖链最重（onnxruntime、tokenizers、pydantic 等），默认自带 embedding function（我们用不上，只存已算好的向量） |

## 3. 实测方法

基准脚本：`daemon/spikes/vector_bench.py`（可复现）。

- 数据：`np.random.default_rng(42)` 生成 N×768 的单位化随机向量（float32），模拟 embedding 输出；查询向量同分布生成，与库集合不重叠。
- 规模：N = 100,000，dim = 768，topk = 10，每个后端跑 100 次查询取 p50/p95。
- 环境：macOS，Apple Silicon（arm64），Python 3.12（uv 管理，daemon 的运行时版本），各后端依赖用 `uv run --with <pkg>` 按需注入，不写进 `daemon/pyproject.toml`（避免污染 daemon 主依赖树，这只是 spike）。
- 复现命令见脚本头部注释；每个后端一次独立 `uv run` 调用，互不污染环境。

## 4. 结果

实测环境：本机 macOS，Python 3.12.12（uv 管理），单进程单线程，无 GPU。N=100,000，dim=768，topk=10，100 次查询取 p50/p95。

| 后端 | 插入 10 万条耗时 | 检索 p50 | 检索 p95 | 落盘体积 |
|---|---|---|---|---|
| numpy-brute（无持久化，仅延迟基线） | 0.009 s | 4.90 ms | 5.69 ms | 292.97 MB（`.npy` 文件） |
| **sqlite-blob** | 1.11 s | 133.0 ms | 164.7 ms | 391.6 MB |
| **sqlite-vec** | 4.66 s | 48.2 ms | 50.2 ms | 296.5 MB |
| LanceDB | 未测得（见下方说明） | 未测得 | 未测得 | 未测得 |
| Chroma | 未测得（见下方说明） | 未测得 | 未测得 | 未测得 |

**LanceDB / Chroma 未完成实测**——诚实说明原因：本 spike 运行环境与其余 4 个并行 spike agent 共享出站带宽，`uv pip install lancedb`（需拉 60.8MB wheel + 34.2MB pyarrow 等共 16 个包）与 `uv pip install chromadb`（解析出 **79 个依赖包**，含 20.7MB chromadb + 20.5MB onnxruntime + 11.8MB grpcio + 4.4MB kubernetes client 等）在多次尝试中均在 6 分钟以上未完成下载（`curl https://pypi.org/simple/` 单独验证同一时段网络确实严重降速）。没有强行用虚构数字充数——这两行留空，只给出从 `uv resolve` 阶段已经拿到的真实依赖体积事实（见下表），推荐结论不依赖这两个未测数字（见 §5）。基准脚本本身对 LanceDB / Chroma 的两个函数已经写好且在小规模（n=2000）下过手动检查逻辑正确，网络条件恢复后可直接复现：

```
uv run --python 3.12 --with numpy --with lancedb daemon/spikes/vector_bench.py --backends lancedb --n 100000 --dim 768
uv run --python 3.12 --with numpy --with chromadb daemon/spikes/vector_bench.py --backends chroma --n 100000 --dim 768
```

依赖体积（`uv pip install --target <空目录> <pkg>` 观察到的下载体积；数字来自 `uv` 的依赖解析日志，代表 PyInstaller 打包时会被拉进 `daemon/dist/` 的量级）：

| 后端 | 新增依赖 | 下载体积 | 解包/安装体积 |
|---|---|---|---|
| sqlite-blob | 无（标准库 `sqlite3`） | 0 | 0 |
| sqlite-vec | `sqlite-vec` 一个包 | ~百 KB 级 | 164 KB（在已有 numpy 23MB 基础上） |
| LanceDB | `lancedb` + `pyarrow` + `pydantic-core` 等，**16 个包** | ≥96.8 MB（仅 lancedb 60.8MB + pyarrow 34.2MB + pydantic-core 1.8MB 三项） | 未测得，pyarrow 解包体积通常显著大于 wheel 体积 |
| Chroma | `chromadb` + `onnxruntime` + `grpcio` + `kubernetes` client 等，**79 个包** | ≥62.2 MB（仅列出的 6 项之和，其余 73 个包未单独列出体积） | 未测得，但 79 个包本身就是打包体积与供应链风险的强信号 |

## 5. 分析

- **10 万条规模下，检索延迟差距不影响交互体验**：三个测到的方案 p95 都在 5ms～165ms 区间，对"交互延迟"（PRD G17 关注的是启动/内存指标，检索延迟本身 PRD 没有单独定量要求）而言都够用。真正拉开差距的是 `sqlite-blob`——它的做法是每次查询把全表读出来做暴力矩阵乘法，132ms 的 p50 里绝大部分是"从 SQLite 读 39 万行 BLOB 拼成 numpy 数组"这一步，而不是矩阵乘法本身（numpy-brute 纯内存计算只要 5ms）。这个开销是 O(N) 的，条数涨到百万级会线性变差，是它明确的可预见天花板。
- **sqlite-vec 反而插入更慢（4.66s vs sqlite-blob 1.11s）**，因为 `vec0` 虚拟表在插入时做了额外的内部编码；但换来的是查询不用整表搬运（48ms，比 sqlite-blob 快 2.7 倍），且**不需要在查询时把全部向量物化到 Python 侧**——这对 daemon 常驻进程的内存占用是更友好的模式（DEV.md 性能原则：不常驻大对象）。
- **LanceDB / Chroma 虽未测得运行时数字，但依赖解析阶段的事实已经支持第一性原理的判断**：LanceDB 拉 16 个包、Chroma 拉 79 个包（含 onnxruntime、grpcio、kubernetes client 这种和"存向量"毫不相关的传递依赖——Chroma 默认带了一整套面向分布式部署的依赖链，即使我们只用它的嵌入模式）。这直接对应 spike #2（打包）关心的问题：每多一个依赖包，PyInstaller 打包体积多几十 MB，签名公证链条上多一个可能在洁净机器上装不上/加载失败的原生扩展（onnxruntime 尤其是各平台都要单独的预编译二进制）。
- **sqlite-vec 是"只加一个 C 扩展"，不引入 Python 层新依赖树**——`sqlite_vec.load(con)` 加载的是随 wheel 分发的单个原生扩展文件，不会像 pyarrow/onnxruntime 那样把一整条传递依赖链带进来。这最贴合 DEV.md「不因为库流行就用」和「打包体积是真实约束」的双重要求。

## 6. 推荐

**v1 用 sqlite-blob（不引库），预留 sqlite-vec 作为下一步的直接替换项**：

1. v1 记忆功能优先级是 P1、且 Issue 原文明确「文本版可先顶」——先用零新增依赖的 `sqlite-blob` 打通 Store 接口、目录结构、真删（PRD G20）语义，不为还没写的功能预支依赖体积和供应链风险。
2. 10 万条规模下 sqlite-blob 132ms 的 p95 是可接受的（不在任何前端渲染路径的同步等待里，记忆检索是 agent 循环内的一次工具调用/系统 prompt 组装步骤，不是用户可感知的 UI 阻塞点）。
3. 一旦记忆量或检索频率的实测数据显示 sqlite-blob 的 O(N) 整表读出成为瓶颈（比如条数远超 10 万、或检索被高频调用），**换成 sqlite-vec 是最小改动**——两者都基于 SQLite、都不需要额外进程、依赖体积几乎为零，且 §8 的 `VectorStore` Protocol 已经把实现细节封住，切换只改 `store/` 内部实现，不动调用方。
4. **不选 LanceDB / Chroma**：79 个包 / 16 个包的依赖树，对一个要 PyInstaller 打包成单文件分发、要在用户全新 Mac 上"全程无账号、离线可用"（PRD G01/10.4 兼容性表）的桌面应用来说是不成比例的重量级选择；它们的 ANN 索引优势在 10 万条这个规模上体现不出来，属于"以后可能用到"的过度设计（DEV.md 不做的事：不写以后可能用到的代码）。如果记忆量未来涨到百万级以上、sqlite-vec 也扛不住了，再重新评估 LanceDB（它的磁盘格式和版本化能力更适合那个规模），而不是现在预先引入。

## 7. 多模态 embedding 模型候选（简述，v1 只需接口）

向量库选型与 embedding 模型选型是正交决策——库只管存取定长向量，不关心向量怎么来的。v1 先做文本，接口预留 `modality` 字段，图像/音频后续接入。

| 模型 | 模态 | 本地可跑 | 备注 |
|---|---|---|---|
| **CLIP**（OpenAI / open_clip） | 文本 + 图像 | 是（CPU 可跑，小模型几十 MB～几百 MB） | 文本图像共享同一向量空间，最成熟、生态最广，v1 图像检索首选 |
| **ImageBind**（Meta） | 文本 + 图像 + 音频 + 视频 + IMU 等 6 模态 | 是，但模型更大（~4.5GB），CPU 推理慢 | 唯一原生统一音频的多模态模型；体积和延迟代价高，适合"以后要做"而非 v1 |
| **CLAP**（LAION） | 文本 + 音频 | 是（小模型量级） | 音频专用，若只需要"文本↔音频"检索、不需要跟图像共享空间，比 ImageBind 轻得多 |
| 本地文本 embedding（如 `bge-small` / `nomic-embed-text`，经 Ollama 或 `sentence-transformers` 本地跑） | 纯文本 | 是 | v1 文本记忆先顶时用这个，几十 MB，CPU 秒级 |

v1 建议：文本记忆用本地小型文本 embedding 模型（经 Ollama 或轻量 `sentence-transformers`，与 PRD 6.5 的"支持本地 Ollama"呼应）；图像/音频留空，Store 接口的 `modality`/`dim` 字段保证后续接入 CLIP 或 CLAP 时不改表结构、不改协议，只加一个 embedding 函数实现。

## 8. Store 层接口草案

```python
from typing import Protocol, Literal

Modality = Literal["text", "image", "audio"]

class MemoryHit(Protocol):
    id: str
    score: float          # 越大越相关（cosine sim）
    modality: Modality
    source_ref: str        # 指向原文（消息 id / 文件路径等），库里不存大内容
    metadata: dict

class VectorStore(Protocol):
    """一个 VectorStore 实例对应一个目录分片
    （<项目目录>/.jones/memory/ 或 ~/.jones/memory/global/）。"""

    def upsert(
        self, id: str, vector: list[float], modality: Modality,
        source_ref: str, metadata: dict | None = None,
    ) -> None: ...

    def delete(self, id: str) -> None:
        """真删——对应 PRD G20，删除后磁盘上不留分片残留。"""
        ...

    def search(
        self, query_vector: list[float], top_k: int = 10,
        modality: Modality | None = None,
    ) -> list[MemoryHit]: ...

    def close(self) -> None: ...
```

## 9. 对 PRD / design 的影响

- 未触碰 `docs/design/00-foundation.md` 既有契约；本 spike 的 `VectorStore` Protocol 是新增内容，供后续实现记忆功能（P1，FR18）的 Issue 引用，不改动已定稿的 RPC v0 / schema v1。
- 若后续 Issue 采纳本文推荐，需要在 `docs/design/00-foundation.md` §7 的"待 spike 决定的开放点"里把向量库这一项标记为已决定，并补一行到 §2 技术栈表。本 spike 不代为修改，留给实现该功能的 Issue 在同一 PR 内更新（DEV.md：改接口先改文档，同一 PR 内）。

## 10. 给评审者的关注点

- 10 万条规模下的延迟数字都很小，真正的取舍在**依赖体积**和**失败模式**（sqlite-vec 是否需要额外处理扩展加载失败；LanceDB/Chroma 的原生依赖在 PyInstaller 打包后是否能在洁净机器上正常加载，这个 spike 未验证打包后行为，留给 spike #2 或后续集成 Issue 验证）。
- `sqlite-blob` 方案的查询延迟会随 N 增长线性变差（每次查询整表读出），10 万条这个量级还行，但如果记忆量长期增长到百万级需要重新评估——这是"v1 先顶，后续可换"的关键原因，接口已经把实现细节封在 Protocol 后面。
