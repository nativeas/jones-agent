# 03 · 多模态向量库选型

对应 Issue #3、PRD 13.2 风险 2、6.4、10.1、10.2、10.3、11.1、11.2、12.1（G17/G20）、12.3（FR18）。

> **修订说明**：本版是评审后的修复版本，见文末「修复记录」。核心结论变化：v1 推荐从
> `sqlite-blob` 改为 `sqlite-vec`（§6），原因是 PRD 11.1 对记忆检索延迟确有定量约束、
> `sqlite-blob` 的内存与延迟实测数字都不满足它（§5）。Store 接口（§8）补了枚举/读取入口、
> 多维度支持方案、真删的具体实现要求。

## 1. 本质约束（第一性原理）

先问这个问题真正要的是什么，再挑库：

1. **纯本地、可嵌入 Python 进程**——daemon 是单进程常驻服务，不能再拉起一个数据库服务端、不能开端口（PRD N14：守护进程不得监听 TCP）。所以任何需要独立 server 进程的方案（Milvus、Qdrant server 模式、Weaviate）直接出局，候选只能是"进程内库"。
2. **规模目标是 10 万条向量，不是千万级**（PRD 10.1：≥10 万条记忆向量不劣化交互延迟）。这个规模下暴力检索（矩阵乘法）本身就是毫秒级，近似索引（HNSW/IVF）带来的收益在这个数量级上不明显，但会引入额外依赖体积、索引构建时间、以及"结果不精确"的复杂度。
3. **多模态只是"向量维度和来源标签"，不是数据库特性**——文本/图像/音频向量化后都是定长 float 数组，库层面根本不关心内容模态；真正的多模态差异在 embedding 模型层（CLIP/ImageBind），不在向量库层。所以"向量库要不要支持多模态"是伪命题，只要库能存 float 向量 + 任意 metadata（来源、模态标签）即可。但这个论断成立的前提是"不同模态允许不同维度"这件事在接口/存储层被显式处理，不能只停留在论断层面——见 §7/§8 的多维度设计。
4. **打包体积是真实约束**（spike #2 关注点）：daemon 要靠 PyInstaller 打包分发，多一个依赖就多几十 MB 体积、多一份供应链风险、多一处"这台机器装不上这个 wheel"的失败面。
5. **检索延迟不是"没有约束"，而是被显式算进了首 Turn 预算**——PRD 11.1 启动性能表：「首个 Turn 首 token ≤ 模型 RTT + 800 ms，800 ms 是运行时 + worker 拉起 + **记忆检索** 的总开销上限」。这与 G17（"11.1/11.2 的全部指标在参考机上通过"）直接挂钩。上一版把这里读成"PRD 没有单独定量要求"是误读——见 §5、§6、文末修复记录。
6. **v1 只需要接口稳定，不需要现在就做对**（Issue 原文：「v1 只需定 Store 层接口与目录结构，文本版可先顶」）。这意味着选型的首要标准是"依赖轻、可替换、且不违反已有的性能/内存预算"——不是在预算内随便选一个更慢的方案只因为它零依赖。

结论方向：**在满足 PRD 11.1/11.2 预算的候选里，优先选没有额外原生依赖负担、失败模式可控的方案**，把"以后要不要上 ANN 索引"留给真实数据增长后再决定（YAGNI，不为"以后可能" 10 倍增长的场景现在就扛 LanceDB/Chroma 的依赖重量）。

## 2. 候选

| 候选 | 形态 | 索引 | 备注 |
|---|---|---|---|
| **numpy-brute** | 不引库，进程内 numpy 数组 | 无（暴力矩阵乘法） | 不落盘持久化，仅作延迟/召回基线参考，不是可用方案（重启即丢） |
| sqlite-blob | 不引库，SQLite BLOB 存向量原文 + 每次查询整表读出后 numpy 暴力检索 | 无 | 复用 daemon 已有的 SQLite 依赖（DEV.md：标准库 `sqlite3`），零新增依赖；**查询时把全表物化到 Python 侧，见 §5** |
| sqlite-persist | 不引库，SQLite BLOB 只做落盘持久化，查询用进程内常驻 numpy 矩阵缓存 | 无 | 评审要求补测的"显而易见方案"（见文末修复记录 #2/#8）：延迟等同 numpy-brute，代价是矩阵**永久**常驻内存，见 §5 |
| **sqlite-vec** | SQLite 扩展（`vec0` 虚拟表） | 精确 KNN（当前版本不做 ANN） | 单文件 `.so`/`.dylib` 扩展，纯 C，无 Python 原生依赖链 |
| LanceDB | 内嵌模式，本地列式存储（Lance 格式） | IVF_PQ 可选，**默认不建**，未显式 `create_index()` 时是 flat scan | Rust 核心 + pyarrow，磁盘格式支持增量/版本化 |
| Chroma | 内嵌模式，`PersistentClient` | HNSW（hnswlib，默认开启） | 依赖链最重（onnxruntime、tokenizers、pydantic 等），默认自带 embedding function（我们用不上，只存已算好的向量） |

## 3. 实测方法

基准脚本：`daemon/spikes/vector_bench.py`（可复现）。

- 数据：`np.random.default_rng(42)` 生成 N×768 的单位化随机向量（float32），模拟 embedding 输出；查询向量同分布生成，与库集合不重叠。
- 规模：N = 100,000，dim = 768，topk = 10，每个后端跑 30～100 次查询取 p50/p95/mean。
- 环境：macOS，Apple Silicon（arm64），Python 3.12（uv 管理，daemon 的运行时版本），各后端依赖用 `uv run --with <pkg>` 按需注入，不写进 `daemon/pyproject.toml`（避免污染 daemon 主依赖树，这只是 spike）。
- **召回率 / 一致性断言**：脚本对每个查询同时算出 numpy 精确 top-k 作为 ground truth，其余后端返回的 top-k 与它取交集算 `recall_at_k`（评审 #10）。对精确检索后端（`sqlite-blob`/`sqlite-persist`/`sqlite-vec`）来说 `recall_at_k=1.0` 是数学恒等式，不是"召回率指标"——评审第二轮 #3 指出这一点，脚本已改为把它变成硬断言：`recall_at_k < 1.0` 时脚本以非零退出码结束并把不一致的后端打到 stderr（`main()` 里的 `consistency_failures` 检查），这样"sqlite-vec 的结果集和 numpy 精确结果不一致"会让 CI/复现命令直接失败，而不是只在 JSON 里留一个安静的偏低数字等人工去读。LanceDB/Chroma 是近似检索，不纳入这个断言（预期 recall 可以 < 1.0）。
- **内存**：脚本自身用 `resource.getrusage().ru_maxrss` 记录进程峰值 RSS（`rss_max_mb` 字段），但同一进程里跑多个后端时这个数字是"跑到该行为止"的累计峰值，不是单后端独立值；本报告 §4/§5 里逐后端独立的内存数字来自 `/usr/bin/time -l`（每个后端单独一次 `uv run` 调用），方法与数字见 §5。
- 复现命令见脚本头部注释；每个后端一次独立 `uv run` 调用，互不污染环境。

**方法论边界（评审 #10）**：`gen_data` 生成的是 i.i.d. 标准正态后单位化的随机向量，768 维下两两接近正交、没有真实 embedding 常见的簇结构——这恰好是 HNSW/IVF 一类 ANN 索引最不具代表性的输入分布。本报告的 `recall_at_k` 列只用来确认"索引确实被使用、且没有把结果搞错"这一底线，**不代表** LanceDB/Chroma 在真实 embedding 分布上的真实召回表现；同理，§4 表里 LanceDB/Chroma 的延迟数字不能拿来和 sqlite-blob/sqlite-vec（精确 KNN）的延迟直接比"谁更快"——精确检索没有"用召回率换延迟"这个自由度，ANN 的意义要在有簇结构的真实数据上另测才成立。本次选型不依赖这个比较：v1 排除 LanceDB/Chroma 的理由始终是依赖体积（§5），不是延迟数字。

## 4. 结果

实测环境：本机 macOS，Python 3.12.12（uv 管理），单进程单线程，无 GPU。N=100,000，dim=768，topk=10。

| 后端 | 插入 10 万条耗时 | 检索 p50 | 检索 p95 | 检索 mean | recall@10 | 独立进程峰值 RSS | 落盘体积 |
|---|---|---|---|---|---|---|---|
| numpy-brute（无持久化，仅基线） | 0.007–0.010 s | 4.9 ms | 5.2–5.7 ms | 4.9 ms | 1.0 | 622.9 MB | 292.97 MB（`.npy`） |
| sqlite-blob | 1.01–1.06 s | 121.6–133.0 ms | 140.3–267.3 ms | 122.5–143.7 ms | 1.0 | **1731.4 MB** | 391.6 MB |
| sqlite-persist | 1.03–1.05 s | 4.9–5.0 ms | 5.2–5.8 ms | 4.9–5.0 ms | 1.0 | 627.2 MB（**且是永久常驻**，见 §5） | 391.6 MB |
| **sqlite-vec** | 4.2–4.7 s | 39.6–40.2 ms | 41.0–41.5 ms | 39.6–40.3 ms | 1.0 | 628.0–630.2 MB | 296.5 MB |
| LanceDB | 见下 | 见下 | 见下 | 见下 | 见下 | 见下 | 见下 |
| Chroma | 见下 | 见下 | 见下 | 见下 | 见下 | 见下 | 见下 |

延迟数字是多次独立运行（`n-queries` 30/100，跨若干次 `uv run` 调用）的区间，不是单次读数——**sqlite-blob 的 p95 在同一台机器上跨运行从 140ms 跳到 267ms**（约 1.9 倍），印证了评审指出的"133ms 不可稳定复现、系统有负载时会显著更差"；sqlite-vec 和 sqlite-persist 的区间窄得多，量级更可预测。独立进程峰值 RSS 用 `/usr/bin/time -l`、`--n-queries 5`、每个后端单独一次 `uv run` 测得（见 §5 方法与原始数字）。

LanceDB / Chroma：本次修复重新尝试联网安装（评审前一版因带宽问题未测得），结果见 §5 末尾——已更新为实测数字，或在仍未测得时明确保留"未测得"标注（不编造数字）。

## 5. 分析

### 5.1 sqlite-blob 与 sqlite-persist 的内存问题是本次修复的核心发现

`sqlite-blob` 每次查询都执行 `SELECT id, v FROM vecs` 整表 `fetchall()`，再 `b"".join(...)` 拼接后 `np.frombuffer(...).reshape(...)`。用 `/usr/bin/time -l uv run … --backends sqlite-blob --n-queries 5` 单独测得：

```
1815953408  maximum resident set size   # sqlite-blob ≈ 1.82 GB
```

对照同一 harness 下的基线：

```
 653148160  maximum resident set size   # numpy-brute ≈ 622.9 MB（与 §4 表一致，不是 0.65GB——653148160 字节按二进制单位是 622.9 MiB / 0.608 GiB，上一版把十进制字节数直接读成"0.65 GB"是单位换算错误；构成：base 数组 100000×768×4B ≈ 293.0MiB + bench_numpy_brute 里 `matrix = base.copy()` 的第二份拷贝 ≈ 293.0MiB，合计 586.0MiB，其余约 37MiB 是 Python/numpy 解释器自身常驻开销——上一版说"0.61GB 是 base 数组 + copy"也是错的，base+copy 只有 0.57GB）
 658522112  maximum resident set size   # sqlite-vec  ≈ 0.66 GB（几乎不比基线高）
 657768448  maximum resident set size   # sqlite-persist ≈ 0.66 GB（单次查询峰值不高，但见下）
```

`sqlite-blob` 单次查询的瞬时内存峰值比基线高出约 **1.1–1.2 GB**（10 万条 × 3072 字节 BLOB 的 `fetchall()` 元组/字节对象 + `b"".join` 的第二份连续拷贝 + `frombuffer` 的第三份视图），发生在**常驻 daemon 进程**里。这直接撞上 PRD 11.2 的两条硬上限：「守护进程常驻内存（空闲）≤150MB」「全部合计（空闲）≤500MB」——即便这个峰值只在检索的瞬间出现，一次检索就能把 daemon 进程的内存占用顶到那两个上限的 3.5～12 倍，且随记忆量线性变差（O(N)）。**§6 上一版推荐 `sqlite-blob` 是自相矛盾的**：本节（§5）已经用"不常驻大对象"的原则论证 `sqlite-vec` 更优，上一版 §6 却推荐了被这条原则否定的方案。

`sqlite-persist`（评审 #2/#8 要求补测的"进程内缓存矩阵"方案）解决了 `sqlite-blob` 的**瞬时**峰值问题——查询延迟降到 4.9ms，和 numpy-brute 一样快——但代价是那份 293MB（10 万×768×float32）的矩阵**永久常驻**在 daemon 进程里，只要这个 `VectorStore` 分片被打开就不释放。这同样撞 PRD 11.2：一个 daemon 进程可能同时打开多个分片（项目级 + 全局），每个分片一份常驻矩阵，「守护进程常驻内存（空闲）≤150MB」在只有一个 10 万条分片时就已经超了将近 2 倍，多分片场景下更糟。**这正是本次修复新增测试的方案，结论是：它比 `sqlite-blob` 好（延迟稳定、无瞬时峰值），但仍然不满足 11.2，不能作为 v1 选择**——评审 #2 要求把它"写进权衡"而不是跳过，这里补上。

`sqlite-vec` 是两个问题都没有的方案：`vec0` 虚拟表的 KNN 检索在 SQLite/C 层完成，不需要把候选向量搬进 Python 堆，查询前后 RSS 几乎不变（628–630MB，和空跑基线的 623–658MB 同一量级，波动即测量噪声，不是查询引入的常驻或瞬时开销）。

### 5.2 检索延迟对照 PRD 11.1 的 800ms 首 Turn 预算

PRD 11.1：「首个 Turn 首 token ≤ 模型 RTT + 800 ms，800 ms 是运行时 + worker 拉起 + **记忆检索** 的总开销上限」——记忆检索被显式点名分享这 800ms，不是"不在同步等待路径里"（上一版 §6 第 2 条的说法与此直接冲突，DEV.md：冲突时 PRD > design > Issue）。按本次实测：

- `sqlite-blob`：p50 121.6–133.0ms（占 800ms 预算 15–17%），**p95 在有系统负载时观测到 267ms、极端情况下可能更高**（占比 33%+），且是 O(N)，条数涨到 30 万～50 万时 p50 会逼近甚至超过 400–650ms（线性外推），届时记忆检索一项就可能吃光整个 800ms 预算，不需要等到"以后"才成为问题。
- `sqlite-vec`：p50 39.6–40.2ms、p95 41.0–41.5ms，稳定占预算的 5%，且不随负载显著波动（对照实验里 p50/p95 区间宽度远小于 sqlite-blob）。10 万条时留给"运行时 + worker 拉起"的预算还有 760ms 左右；即使记忆量涨到百万级、检索延迟粗略等比例增长到 400ms，也还没有单独吃光预算（虽然那时应该重新评估，但不是"v1 一上线就没有安全边际"）。

结论：**v1 选 `sqlite-vec`，不是"以后可能需要就先做"，而是当前实测数据下 `sqlite-blob` 已经不满足 PRD 11.1/11.2 的量化门槛，`sqlite-vec` 满足**。164KB 的纯 C 扩展这个代价，相对于"守住两条 PRD 硬指标"是划算的交换。

### 5.3 LanceDB / Chroma：依赖体积仍是排除理由

- LanceDB 拉 `lancedb` + `pyarrow` + `pydantic-core` 等 **16 个包**，仅 lancedb 60.8MB + pyarrow 34.2MB + pydantic-core 1.8MB 三项下载体积就 ≥96.8MB。
- Chroma 解析出 **79 个依赖包**，含 onnxruntime 20.5MB、grpcio 11.8MB、kubernetes client 4.4MB 等与"存向量"毫不相关的传递依赖——Chroma 默认带了一整套面向分布式部署的依赖链，即使只用它的嵌入模式。
- 这直接对应 spike #2（打包）关心的问题：每多一个依赖包，PyInstaller 打包体积多几十 MB，签名公证链条上多一个可能在洁净机器上装不上/加载失败的原生扩展（onnxruntime 尤其是各平台都要单独的预编译二进制）。

本次修复重新尝试了联网安装（见 §4 表格与下方补充），但**即使拿到了运行时数字，也不改变这条结论**——排除理由是依赖体积，不是延迟；这也是 §3 强调"LanceDB/Chroma 的延迟数字不能和精确检索直接比较"的原因：即使它们跑出比 sqlite-vec 更低的延迟，也换不回 16/79 个包的依赖树代价。

**LanceDB / Chroma 本次修复仍未测得运行时数字**——比上一版更充分地重试过：联网安装本身可达（`curl https://pypi.org/simple/lancedb/` 1.1s 内返回 200），但两个后台 `uv run --with lancedb` / `--with chromadb` 进程各自跑了 13 分钟以上，用 `nettop -p <pid>` 抓到的实际传输速率在 30～50 KB/s 量级、且 `re-tx`（TCP 重传）计数持续走高（lancedb 那条连接 5.6MB 重传 / 27MB 已收，chroma 那条 4.9MB 重传 / 24MB 已收，重传占比均超过 20%）——是实际的链路质量问题，不是"没重试"。按这个速率，lancedb 还需要的 ~70MB、chroma 还需要的更大依赖量级，都要数十分钟以上，超出本次修复的时间预算，中途 kill 了两个进程。依赖体积事实（§5.3 已有的 16/79 个包）本身已经支撑"不选"的结论，不影响推荐；如果评审需要运行时数字核实，命令仍是：

```
uv run --python 3.12 --with numpy --with lancedb daemon/spikes/vector_bench.py \
  --backends lancedb --n 100000 --dim 768 --lancedb-index
uv run --python 3.12 --with numpy --with chromadb daemon/spikes/vector_bench.py \
  --backends chroma --n 100000 --dim 768
```

## 6. 推荐

**v1 用 `sqlite-vec`（新增一个 164KB 的纯 C 扩展依赖），不用 `sqlite-blob`：**

1. 10 万条规模下，`sqlite-vec` 检索 p50 ≈ 40ms / p95 ≈ 41ms，稳定占 PRD 11.1「记忆检索」800ms 共享预算的 ~5%；`sqlite-blob` 的 121–267ms（占 15–33%+，且波动大、O(N) 随条数线性变差）不满足这条预算留有的安全边际，`sqlite-persist`（进程内缓存矩阵）虽然延迟和 `sqlite-vec` 一样好，但常驻内存直接撞 PRD 11.2（§5.1）。这三个候选里只有 `sqlite-vec` 同时满足延迟和内存两条硬指标。
2. `sqlite-vec` 查询时不把候选向量物化到 Python 侧，daemon 进程 RSS 在查询前后几乎不变（§5.1）；`sqlite-blob` 单次查询瞬时峰值比基线高 1.1–1.2GB，`sqlite-persist` 让 293MB 矩阵永久常驻——两者都会让「守护进程常驻内存 ≤150MB / 全部合计 ≤500MB」（PRD 11.2）在个位数分片规模下就顶到或超过上限。
3. 代价是 164KB 的原生扩展（`sqlite-vec` wheel 自带，随 numpy 一起打进 PyInstaller 产物，量级远小于 LanceDB/Chroma），以及比 `sqlite-blob` 慢的插入耗时（4.2–4.7s vs 1.0–1.1s，插入 10 万条一次性发生，不在任何用户可感知的路径上，可接受）。
4. **不选 LanceDB / Chroma**：79 个包 / 16 个包的依赖树，对一个要 PyInstaller 打包成单文件分发、要在用户全新 Mac 上"全程无账号、离线可用"（PRD G01/10.4 兼容性表）的桌面应用来说是不成比例的重量级选择；它们的 ANN 索引优势在 10 万条这个规模上体现不出来（§1 第 2 条），属于"以后可能用到"的过度设计（DEV.md 不做的事：不写以后可能用到的代码）。如果记忆量未来涨到百万级以上、`sqlite-vec` 也扛不住了，再重新评估 LanceDB（它的磁盘格式和版本化能力更适合那个规模），而不是现在预先引入。
5. **不选 `sqlite-persist`**：它是"先简单"里最诱人的选项（延迟和 `sqlite-vec` 一样好，代码比 `sqlite-vec` 更简单），但违反 PRD 11.2 是硬约束，不是可以先欠着以后再还的技术债——一旦 v1 按这个方案落地，后续要把"每个打开的分片常驻一份矩阵"这个假设从调用方代码里摘出去，改动面比直接用 `sqlite-vec` 大得多，属于"打补丁再打补丁"。

## 7. 多模态 embedding 模型候选（简述，v1 只需接口）

向量库选型与 embedding 模型选型是正交决策——库只管存取定长向量，不关心向量怎么来的。v1 先做文本，接口按模态分表（见 §8），图像/音频后续接入不改表结构。

| 模型 | 模态 | 本地可跑 | 典型维度 | 备注 |
|---|---|---|---|---|
| **CLIP**（OpenAI / open_clip） | 文本 + 图像 | 是（CPU 可跑，小模型几十 MB～几百 MB） | 512 | 文本图像共享同一向量空间，最成熟、生态最广，v1 图像检索首选 |
| **ImageBind**（Meta） | 文本 + 图像 + 音频 + 视频 + IMU 等 6 模态 | 是，但模型更大（~4.5GB），CPU 推理慢 | 1024 | 唯一原生统一音频的多模态模型；体积和延迟代价高，适合"以后要做"而非 v1 |
| **CLAP**（LAION） | 文本 + 音频 | 是（小模型量级） | 512 | 音频专用，若只需要"文本↔音频"检索、不需要跟图像共享空间，比 ImageBind 轻得多 |
| 本地文本 embedding（如 `bge-small` / `nomic-embed-text`，经 Ollama 或 `sentence-transformers` 本地跑） | 纯文本 | 是 | 384（bge-small）/ 768（多数本地文本模型） | v1 文本记忆先顶时用这个，几十 MB，CPU 秒级 |

v1 建议：文本记忆用本地小型文本 embedding 模型（经 Ollama 或轻量 `sentence-transformers`，与 PRD 6.5 的"支持本地 Ollama"呼应）；图像/音频留空。**候选模型维度并不一致**（CLIP 512 / bge-small 384 / 本地文本模型常见 768），这不是可以忽略的细节——接口层必须明确"同一个 store 里不同维度的向量怎么放"，见 §8 的按模态分表设计（不是靠 Protocol 上加一个 `dim` 字段，见文末修复记录 #3）。

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
    （<项目目录>/.jones/memory/ 或 ~/.jones/memory/global/），
    底层是该目录下的单个 SQLite 文件 `vectors.db`（见"目录结构"一节）。
    """

    def upsert(
        self, id: str, vector: list[float], modality: Modality,
        source_ref: str, metadata: dict | None = None,
    ) -> None:
        """插入或替换一条记忆。`len(vector)` 即该向量的维度——同一 modality 下
        所有向量必须同维度，首次为某 modality 写入时该维度被记录并锁定
        （见"多模态维度"一节）；维度不一致时直接抛错，不做静默补零/截断
        （DEV.md 诚实失败）。"""
        ...

    def get(self, id: str) -> MemoryHit | None:
        """按 id 读取单条记忆（含向量与 metadata），用于"编辑"——
        编辑 = get() 取出、改字段、upsert() 写回同一个 id。"""
        ...

    def list(
        self, modality: Modality | None = None,
        limit: int = 100, offset: int = 0,
    ) -> list[MemoryHit]:
        """枚举记忆，不需要先有一个查询向量。是记忆列表页 / 导出功能的直接入口
        （PRD FR18「用户可查、可删」、10.3「查看/编辑/删除/导出」——上一版接口
        只有 search(query_vector) 一个读入口，做不出这些功能，见文末修复记录 #4）。
        按插入/更新时间倒序，调用方做分页。"""
        ...

    def delete(self, id: str) -> None:
        """真删——契约定义（评审第二轮裁定，对应 PRD 10.4）：**真删 = SQLite 行 +
        payload 文件 + 向量分片一起删**。磁盘级残留（页面/WAL frame 里的原始字节
        还能被读出来）由下面两条一起满足，不要求"文件系统底层扇区被物理擦除"
        （那是操作系统/磁盘层的事，任何用户态数据库都做不到，也不是 PRD 10.4 的定义）：

        1. 打开连接时执行 `PRAGMA secure_delete=ON`——让 DELETE 就地把被删内容覆写
           为 0，代价只摊在被删的页上（不像 VACUUM 要重写整个文件，不适合作为高频
           delete 路径的常规操作）。
        2. **每次 DELETE 提交后执行 `PRAGMA wal_checkpoint(TRUNCATE)`**——生产推荐
           配置是 WAL 模式（见"目录结构"一节），而 WAL 是 append-only 日志：
           secure_delete 只保证"新写入的页"内容干净，不保证 -wal 文件里更早的、
           包含原始被删数据的旧 frame 被抹掉；只有 wal_checkpoint(TRUNCATE) 把
           -wal 文件截断到 0 字节，才会物理清除那些旧 frame。只做 1 不做 2，
           在一个长驻连接的 daemon 进程里，删除后到下次自然 checkpoint 之间的
           窗口期，-wal 文件上仍能读到被删向量的原始字节。

        两条都做到才是真删；只做其中一条都不够——`daemon/spikes/delete_verify.py`
        用真实文件字节检查（不是理论推导）验证了这个矩阵，对 sqlite-blob 和 vec0
        两种存储各测了"两条都做/只做一条/都不做"的组合：
          - sqlite-blob：只开 secure_delete 不 checkpoint → -wal 残留；只 checkpoint
            不开 secure_delete → 合并进主库的页仍残留；两条都做 → 真删。
          - sqlite-vec (vec0)：DELETE 在应用层已经把被删行的向量槽位覆写为 0（不依赖
            secure_delete PRAGMA，实测验证见该脚本"非 WAL"用例），所以 vec0 只需要
            checkpoint(TRUNCATE) 就够真删；但 secure_delete=ON 仍然按统一实现要求打开
            （对 `_meta`/`_collections` 等普通表有效，不是无用功）。
        实测方法与逐条数字见该脚本注释与运行输出（`uv run --python 3.12 --with numpy
        --with sqlite-vec daemon/spikes/delete_verify.py`）。"""
        ...

    def search(
        self, query_vector: list[float], modality: Modality, top_k: int = 10,
    ) -> list[MemoryHit]:
        """`modality` 是必填参数，不是可选过滤条件（评审第二轮裁定 #2：上一版
        `modality: Modality | None = None` 与"按 modality 分表 + 每 modality 锁定
        dim"自相矛盾——search 内部要靠 modality 才知道去查 `vecs_text` 还是
        `vecs_image`，不同表维度不同，向量空间也不可比，modality=None 时"跨所有
        模态比较相似度"做不出来）。调用方发起检索时天然知道 query_vector 来自哪个
        modality 的 embedding 模型，这不是一个可以省略的参数。真正的跨模态检索
        （例如"用一句文字搜图片"）要等文本和图像共享同一个 embedding 空间（如 CLIP
        联合空间）接入之后才有意义，属于未来 Issue，不是这个 Store 接口该解决的。"""
        ...

    def close(self) -> None: ...
```

**多模态维度**（评审 #3：上一版 §7 声称接口有 `dim` 字段保证多模态不改表结构，§8 实际没有这个字段，两处自相矛盾；且两个候选后端的建表语句都在 `CREATE TABLE` 时把维度钉死——问题本身没被回答）：

- `VectorStore` 不需要一个显式 `dim` 参数——`len(vector)` 就是维度，Protocol 层面不用多一个容易和 `vector` 参数打架的字段。
- 真正回答"不同维度怎么放"的是**存储层按 modality 分表**：`vectors.db` 里每个 modality 对应一张独立的 `vec0` 虚拟表（`vecs_text` / `vecs_image` / `vecs_audio`，首次对该 modality 调用 `upsert()` 时按当次向量的维度惰性建表），外加一张 `_collections(modality TEXT PRIMARY KEY, dim INTEGER NOT NULL)` 记录每个 modality 已锁定的维度，供后续 `upsert()` 校验。这样 CLIP（512）、bge-small（384）、某个 768 维文本模型可以在同一个 `vectors.db` 文件、同一个 `VectorStore` 实例里共存，互不冲突，且确实做到"接入新 embedding 模型不改表结构、不改协议"——只是"表结构"的正确理解是"每 modality 一张表"，不是"全库一张表、一个 dim 字段"。
- `MemoryHit`/`metadata` 之类非向量字段存在单独的 `_meta(id, modality, source_ref, metadata_json, updated_at)` 表里，按 id 和各 `vecs_*` 表关联，`search()`/`get()`/`list()` 内部做这个 join，调用方看到的还是扁平的 `MemoryHit`。

**目录结构 / 多分片路由**（评审 #4：Issue 明确要求的交付物之一，上一版只在 docstring 带了一句路径，没说清楚里面是什么、多分片怎么合并）：

```
<shard-dir>/                 # <项目目录>/.jones/memory/ 或 ~/.jones/memory/global/
└── vectors.db                # 唯一文件；WAL 模式，PRAGMA secure_delete=ON；
                               # delete() 提交后执行 wal_checkpoint(TRUNCATE)（真删契约，见 delete()）
    ├── vecs_text (vec0)      # 按 modality 惰性建表
    ├── vecs_image (vec0)
    ├── vecs_audio (vec0)
    ├── _collections           # (modality, dim)
    └── _meta                  # (id, modality, source_ref, metadata_json, updated_at)
```

- 一个 `VectorStore` 实例只管**一个**分片（一个 `vectors.db`），`search()`/`list()` 的返回结果不跨分片。
- "项目记忆 + 全局记忆一起检索"是调用方（agent 运行时的记忆检索步骤，不是 `VectorStore` 自己）的职责：分别对项目分片和全局分片的 `VectorStore` 调 `search()`，按 `score` 合并去重、按需做"项目优先"之类的加权——这是一个策略决策，不属于单分片 Store 实现该管的范围，所以没有放进 Protocol。

## 9. 对 PRD / design 的影响

- 未触碰 `docs/design/00-foundation.md` 既有契约；本 spike 的 `VectorStore` Protocol 是新增内容，供后续实现记忆功能（P1，FR18）的 Issue 引用，不改动已定稿的 RPC v0 / schema v1。
- 若后续 Issue 采纳本文推荐，需要在 `docs/design/00-foundation.md` §7 的"待 spike 决定的开放点"里把向量库这一项标记为已决定，并补一行到 §2 技术栈表（新增依赖：`sqlite-vec`，164KB 原生扩展）。本 spike 不代为修改，留给实现该功能的 Issue 在同一 PR 内更新（DEV.md：改接口先改文档，同一 PR 内）。

## 10. 给评审者的关注点

- `sqlite-vec` 是否需要额外处理扩展加载失败（不同平台/架构的 wheel 是否都带正确的 `.so`/`.dylib`）——这个 spike 未验证打包后行为，留给 spike #2 或后续集成 Issue 验证。
- §8 的"按 modality 分表 + secure_delete + wal_checkpoint(TRUNCATE)"设计的**正确性**（会不会真删、WAL 模式下会不会残留）已经用 `daemon/spikes/delete_verify.py` 在 WAL 模式下逐条组合验证过（评审第二轮 #1，见 delete() docstring 与该脚本），不再只是小规模手工检查。**没测的是性能**：10 万条规模下 `secure_delete=ON` 对写入延迟的额外开销、以及"每次 delete 都做一次 wal_checkpoint(TRUNCATE)"在高频删除场景下的开销（TRUNCATE 要求没有其它连接持有旧快照，理论上比 PASSIVE checkpoint 更贵）都没有量化（评审如果认为这值得在选型阶段量化，可以再补一版基准；本次修复的重点是先把"会不会真删"这个正确性问题锁死，性能数字留给实现该功能的 Issue，那时会有真实的 upsert/delete 频率参考）。
- LanceDB/Chroma 的实测数字（如果本次修复联网成功拿到）只是佐证，不是排除它们的理由——排除理由自始至终是依赖体积（§5.3）。

---

## 修复记录（评审后）

本节记录针对评审 10 条意见的逐条处理，评审原文见分支外的报告文件，此处只写处理结果。

1. **[critical] "PRD 没有定量要求"是误读，132ms/147ms 的 p95 不该被判定为可接受** — 采纳。§1 第 5 条、§5.2、§6 已重写：明确引用 PRD 11.1「记忆检索」被算进 800ms 首 Turn 预算，删除"不在同步等待路径里"的错误论证。重新实测（n=100000, dim=768）：sqlite-blob p50 121.6–133.0ms / p95 140.3–267.3ms（区间来自多次独立运行，量级与评审复现的 147.7/402.5ms 一致，且同样观察到明显的运行间波动），sqlite-vec p50 39.6–40.2ms / p95 41.0–41.5ms（与评审复现的 39.3/41.5ms 高度吻合）。**结论已改为 v1 选 sqlite-vec**（代价 164KB 原生扩展），不是维持 sqlite-blob 再补预算分解——按实测数字，sqlite-blob 确实不该继续作为 v1 推荐。
2. **[important] 推荐方案每次查询物化 293MB，未对照 PRD 11.2** — 采纳。§5.1 补上了逐后端独立进程 `/usr/bin/time -l` 实测：sqlite-blob 单次查询峰值 RSS ≈1.82GB（比基线高 1.1–1.2GB 瞬时分配），sqlite-vec ≈0.63GB（几乎不比空跑基线高）。同时按评审要求补测了"进程内缓存矩阵"方案（新增 `sqlite-persist` 后端于 `vector_bench.py`）：延迟等同 numpy-brute（~5ms），但 293MB 矩阵永久常驻，同样撞 PRD 11.2，结论是"比 sqlite-blob 好但仍不满足预算"，写进 §5.1 与 §6 第 5 条，不再是被跳过的选项。
3. **[important] §7 声称有 dim 字段、§8 没有；多模态多维度未被接口覆盖** — 采纳。不通过给 Protocol 加 `dim` 参数解决（会和 `vector` 参数的长度打架），而是在 §8 新增"多模态维度"一节：存储层按 modality 惰性建表（`vecs_text`/`vecs_image`/`vecs_audio`），外加 `_collections(modality, dim)` 记录并校验每个 modality 锁定的维度。§7 的措辞已改为不再声称"Protocol 有 dim 字段"。
4. **[important] VectorStore 没有枚举/读取入口，做不出 FR18/10.3 的查看编辑导出；目录结构未回答** — 采纳。Protocol 新增 `get(id)` 与 `list(modality=None, limit, offset)`。§8 新增"目录结构 / 多分片路由"一节，回答了 db 文件名（`vectors.db`）、内部表结构、多分片如何路由、project/global 结果如何合并（调用方合并，Store 只管单分片）。
5. **[important] delete() 承诺真删但 SQLite DELETE 不满足** — 采纳。新增 `daemon/spikes/delete_verify.py`，实测验证：普通 DELETE 后 hexdump 能在库文件里找到被删向量的原始字节（真的残留）；`PRAGMA secure_delete=ON` 后 DELETE 能可靠清除（不需要额外 VACUUM）。§8 delete() 的 docstring 已改为写明这个 PRAGMA 是实现的硬要求，`vector_bench.py` 里 sqlite-blob/sqlite-vec 两个 bench 函数的建连接语句也加上了这一行，作为参考实现。
6. **[important] bench_lancedb 从未建索引但被标为 IVF_PQ** — 采纳。`vector_bench.py` 的 `bench_lancedb` 新增 `--lancedb-index` 开关：不传时明确跑 flat scan；传了就调用 `create_index()`，失败时在结果里老实标 `indexed: false` 并附错误信息，不冒充索引数字。模块 docstring 与 §2 候选表的措辞已改为"默认不建索引"。本次修复重新尝试联网获取实测数字（比上一版更充分：两个后端各自后台跑了 13 分钟以上），联网本身可达但传输速率只有 30～50 KB/s 且重传率超过 20%，仍未在预算内跑完，`nettop` 证据与复现命令见 §5.3，保留"未测得"标注，不编造数字——但脚本层面 `--lancedb-index` 开关与索引失败时的诚实回退已经就位，供评审在更好的网络环境下复现。
7. **[critical] v1 推荐与 PRD 11.1/11.2 冲突，基准未测内存** — 采纳，与 #1/#2 是同一组修复：结论改为 sqlite-vec；`vector_bench.py` 新增 `rss_max_mb` 字段与本报告 §5.1 的独立进程内存实测，补齐了此前完全缺失的内存维度。§6 的数字错引（132ms 标成 p95）已随整节重写一并修正。
8. **[important] 未覆盖"SQLite 持久化 + 进程内 numpy 矩阵"这个零依赖方案** — 采纳。`vector_bench.py` 新增 `sqlite-persist` 后端并实测（§4/§5.1）：延迟 ~5ms（接近 numpy-brute），但矩阵永久常驻违反 PRD 11.2，明确写入 §6 第 5 条作为"评估过但不选"的方案，不是被跳过。
9. **[important] bench_lancedb 从不建索引却被标 IVF_PQ；insert_s 计入 pyarrow 转换开销** — 采纳，与 #6 是同一处代码。同时把 `list(base)` 逐个构造 numpy 标量数组的写法换成 `pa.FixedSizeListArray.from_arrays(pa.array(base.reshape(-1)), dim)` 零拷贝构造，避免 Python 对象转换开销被计入插入耗时。
10. **[important] 未测召回率，i.i.d. 随机向量下 ANN 与精确检索不可比** — 部分采纳，采取评审给出的第二种方案（"在 §3 里写死本基准只比较精确检索后端，ANN 行不可用同表比较"），而不是重建有簇结构的数据生成器：`vector_bench.py` 新增 `ground_truth_topk()` + 每个后端的 `recall_at_k` 字段（对本次测到的 sqlite-blob/sqlite-persist/sqlite-vec 均为 1.0，符合"都是精确检索"的预期，起到正确性回归检查的作用）；§3 新增"方法论边界"一节，明确声明 recall 列不代表真实分布下的 ANN 召回表现、LanceDB/Chroma 的延迟不能和精确检索直接比较。未重建聚簇数据集的原因：v1 推荐已收敛到 sqlite-vec（精确检索），LanceDB/Chroma 是否要上 ANN 是"以后规模涨到百万级"的问题，届时应该用真实 embedding 分布重新测，而不是现在为一个不选的方案造一份合成聚簇数据集。

### 相关测试

- `uv run --python 3.12 --with numpy daemon/spikes/vector_bench.py --backends numpy-brute,sqlite-blob,sqlite-persist --n 100000 --dim 768 --n-queries 100` — 通过，recall_at_k 均为 1.0。
- `uv run --python 3.12 --with numpy --with sqlite-vec daemon/spikes/vector_bench.py --backends sqlite-vec --n 100000 --dim 768 --n-queries 100` — 通过，recall_at_k 为 1.0。
- `uv run --python 3.12 --with numpy --with sqlite-vec daemon/spikes/delete_verify.py` — 通过（exit 0），secure_delete=ON 的两个用例均验证为"真删"，未开 secure_delete 的 sqlite-blob 用例验证为"残留"（符合预期）。
- `uv run --with ruff ruff check daemon/spikes/vector_bench.py daemon/spikes/delete_verify.py` — 见下方"lint"结果。
- LanceDB / Chroma 端到端复现命令见文末 §4/§5.3 结果或"未测得"标注。

---

## 第二轮修复记录（评审后）

控制者裁定 5 条意见的处理结果（详细过程、探测脚本、逐条证据在分支报告文件末尾的同构小节里，此处只写结论）：

1. **[important] WAL 残留，真删定义按 PRD 10.4 裁定** — 采纳。§8 `delete()` 契约重写为
   「secure_delete=ON + delete 提交后 `wal_checkpoint(TRUNCATE)`」两者缺一不可；
   `daemon/spikes/delete_verify.py` 新增 6 条 WAL 用例直接读 -wal 文件字节验证，
   11 条用例（5 非 WAL + 6 WAL）全部通过。未出现"实测仍残留"的情况，"推荐配置"
   在实测下确实做到真删。
2. **[important] search(modality=None) 接口自相矛盾** — 采纳。§8 `search()` 的
   `modality` 改为必填参数，去掉 `| None = None`，docstring 写明原因与"跨模态检索
   留给未来统一 embedding 空间"这句裁定要求的话。`list()` 未改（裁定只针对
   search，list 是纯枚举，`modality=None` 语义上不矛盾）。
3. **[minor] recall_at_k 恒等、delete_verify 的 vec0 用例无区分力** — 部分采纳。
   `vector_bench.py` 把 `recall_at_k<1.0`（对精确后端）变成硬断言，脚本会
   `SystemExit(1)`，是真正的结果集一致性回归检查。`delete_verify.py` 的 vec0
   用例：实测发现 vec0 的 DELETE 在应用层已经零化向量槽位、不依赖 secure_delete
   （直接探测 `vecs_vector_chunks00` 影子表验证），"secure_delete 开关"这个维度
   对 vec0 天然没有区分力，不是测试设计问题；已改为在真正有区分力的维度（WAL +
   是否 checkpoint）上给 vec0 用例区分力（一条 LEAK、一条 SCRUBBED）。**未按字面
   做出"secure_delete=OFF 时 vec0 应 LEAK"这条用例**，因为实测证明这个前提是假的，
   强行做会是一条已知错误的断言——这一点记在分支报告的 open 小节。
4. **[minor] §5.1 内存基线数字自相矛盾（0.65GB vs 0.61GB）；pyarrow 零拷贝未验证**
   — 采纳。§5.1 改成统一的二进制 MiB 口径：653148160 字节 = 622.9MiB（与 §4 表
   一致），构成拆解为 base 293.0MiB + copy 293.0MiB + 解释器开销 ~37MiB，不再是
   两个对不上的数字。pyarrow 零拷贝改动本轮重新尝试联网验证（先试完整 lancedb、
   超时后改试单独的 pyarrow 包），`nettop` 证据显示网络仍然降速（约 4 分钟仅
   收到 ~9.6MB/95MB，`rx_ooo`≈240 万包），未能在预算内拿到新的 `insert_s` 数字。
   按裁定的"标注未验证"分支处理：代码保留（它修的是一个独立于计时结果的测量
   方法论 bug），但明确标注这个改进的幅度本轮仍未验证，不冒充已验证。
5. **bench 脚本目录归属确认** — 已确认，无需改代码。只读检查了 `daemon/` 目录
   owner（`w1/0-foundation` 分支）的 `daemon/pyproject.toml`：打包目标是
   `packages = ["src/jones_daemon"]`（不含 `daemon/spikes/`），pytest
   `testpaths = ["tests"]`（不含 `daemon/spikes/`）。`daemon/spikes/` 下两个脚本
   位置不动。

### 相关测试

- `uv run --with ruff ruff check daemon/spikes/` — 通过。
- `uv run --python 3.12 --with numpy --with sqlite-vec daemon/spikes/delete_verify.py` — 通过（exit 0），11 条用例全部符合预期。
- `uv run --python 3.12 --with numpy daemon/spikes/vector_bench.py --backends numpy-brute,sqlite-blob,sqlite-persist --n 100000 --dim 768 --n-queries 30` — 通过，一致性断言未触发。
- `uv run --python 3.12 --with numpy --with sqlite-vec daemon/spikes/vector_bench.py --backends sqlite-vec --n 100000 --dim 768 --n-queries 30` — 通过，recall_at_k=1.0，p50 39.7ms / p95 41.3ms，与上一版同量级。
