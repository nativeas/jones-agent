# 04 · W5：Cron、内置 Skill、错误面板、存储收口

对应 Issue #20 #21 #22 #23。上游：PRD FR11/FR12（内置）/FR14、9.3、10、11.3、12.1 G03/G08/G19/G20，12.2 N07–N09/N16/N17；`00-foundation.md` §4/§5；`02-w3` §1；`03-w4` §2（注册表）。

## 0. 原则

- Cron 由 **Jones daemon 自己调度**（`crons` 表已在 schema v1；daemon 是常驻进程，PRD 11.2「空闲不轮询、用系统定时器唤醒」）。Hermes 自带的 `cronjob_manage` 工具与 `cron/` 依赖 Hermes gateway 进程，与 Jones 的进程模型不符——在 worker toolsets 里**禁用** `cronjob_manage`（H 的 toolsets 下发已排除 `kanban_*`/`ha_*`/`computer_use`/`delegate_task`，这里加一项），不重复造两套 cron。
- 内置 Skill 尽量**复用 Hermes 已有工具集**：媒体生成 = Hermes `image_gen`/`video_gen`/`tts` toolsets + 一个 Jones 内置 Skill 做编排说明；办公文档 = 一个 Jones 内置 Skill（说明 + 随 Skill 分发的 Python 脚本，依赖固定）。不在 daemon 里写文档渲染代码。
- 错误面板是「诚实失败」的落地：daemon 负责**分类**并给结构化卡片，renderer 只渲染，不猜。

## 1. 分工与文件所有权

| 分支 | Issue | 独占 | 允许的共享改动 |
|---|---|---|---|
| L `w5/20-cron` | #20 | `daemon/src/jones_daemon/scheduler/`（新）、`daemon/tests/test_scheduler*.py` | 只通过 `SessionService` 公开方法（`create`/`send`/`get`）派发，不改 sessions 内部；`__main__` 加一行 |
| M `w5/21-bundled-skills` | #21 | `daemon/src/jones_daemon/skills/bundled/**`（新）、`daemon/tests/test_bundled_skills*.py`、`tests/integration/` 对应用例 | `skills/service.py`：只把 bundled 目录接进第三层（K 已留位）；toolsets 下发：在 H 的清单里**允许** `image_gen`/`video_gen`/`tts`（改 `capabilities/` 的 toolsets 常量一处，报告标明） |
| N `w5/22-error-panel` | #22 | `daemon/src/jones_daemon/errors/`（新：分类器 + 卡片构造）、`apps/desktop/src/renderer/**` 中的错误/终止卡片与重试动作、`daemon/tests/test_errors*.py` | `sessions/service.py`：只改 `_terminate_run`、`_on_worker_crash`、新增 `retry`（公开方法）；`sessions/methods.py` 加 `session.retry`；`kernel/acp_client.py`：只加错误分类所需的异常类型信息，不改协议 |
| O `w5/23-storage` | #23 | `daemon/src/jones_daemon/store/maintenance.py`（新：真删、备份轮转、迁移校验）、`daemon/tests/test_storage*.py`、`docs/design/00-foundation.md` §6 追加 | `sessions/methods.py` 加 `session.delete`；`replay/store.py::purge_run` 调用；`secrets/vault.py` 原子写 fsync；`paths.py` 只加访问器 |

## 2. L：Cron（FR11）

- `scheduler/cron_expr.py`：五段 cron 解析 + `next_after(dt)`（自己写，≤150 行，有边界测试；不引第三方）。
- `scheduler/service.py`：启动时加载 `crons` 表 `enabled=1` 的项，计算 `next_run_at`；**单个** `asyncio` 定时器等待最近一项（不轮询）；到点 → 若已有该 cron 的 Run 在跑则跳过并记 `skipped_overlap`；否则 `SessionService.create(project_id, agent_id, parent_id=<主会话>, mode=cron.mode 默认 task, title=cron 名)` → `send(prompt)`；`tasks` 行 `source=cron`。
- **结果推回主会话**：Run 结束（成功/终止）后向主会话插入一条 `role=system` 消息（摘要 + 子会话链接 + 终止卡片若有），并广播 `message.completed`。
- **失败计数**：连续失败 3 次 → `enabled=0` + 主会话系统消息「已自动停用」（PRD 12.3 FR11）。
- **时钟与恢复**：daemon 启动时对错过的触发**不补跑**（PRD 5.8 精神；记录一条 `missed` 日志与主会话提示）；`runtime/` 里记 `next_run_at` 快照（`runtime/cron_schedule.json`，best-effort 镜像——`crons.next_run_at` 才是 `_loop` 实际调度依据的事实源，这份文件写失败只记日志，不影响派发）。
- RPC `cron.list/upsert/delete/run_now` 已在 v0；`run_now` 走同一派发路径。
- 测试：可注入时钟；分钟精度；重叠跳过；三次失败停用；Electron 无关（纯 daemon 测试即可证明）。

### 2.1 第 1 轮评审后追加的契约决策（round-1，2026-09-19）

- **`cron.upsert` 的 `mode` 默认值改为 `task`，不是 PRD 9.1 字面的 `auto`**：`ensure_main_session()`（`sessions/service.py`，不在 L 范围）恒把主会话建成 `mode=task`；`SessionService.create()` 的 PRD 9.6/N13 闸拒绝 `mode=task` 父会话下的 `mode=auto` 子会话。这两条都是既有、正确的 PRD 落地，但同时成立时，PRD 9.1「Cron 触发的 Run 默认以自动模式运行」这句话在当前系统里**无法达成**——默认配置下第一次 `create()` 就会被拒。这是 PRD 内部两条要求的真实冲突，不是本分支能单方面通过设计文档改写 PRD 的地方；在跨分支裁定（`sessions/service.py` 是否要为系统触发开一道豁免，或主会话默认改 `auto`）之前，L 在自己独占的 `scheduler/` 范围内选择**不让默认配置开箱即挂**：`cron.upsert` 不传 `mode` 时用 `task`，能正常派发、正常走审查闸（PRD 9.1 对 `task` 模式的定义——只读放行、改变外部世界的动作逐条走三道闸——本来就适用）。用户仍可显式把某个 cron 设成 `mode=auto`；那种情况下若主会话仍是 `task`，仍会被 9.6 闸拒绝并诚实记为失败，这是已知的、未解决的跨分支缺口（见分支报告），需要控制者在 `sessions/service.py` 侧裁定后再收口，**不是本条决策想掩盖的问题**。
- **cron 表达式按本机系统时区解释，存储仍是 UTC ISO**：`scheduler/cron_expr.py` 的 `next_after` 本身不关心时区（按调用者传入的 `datetime` 的 tzinfo 计算）；`scheduler/service.py::_next_after_local` 是唯一做 UTC↔本地转换的地方——把 `Clock.now()`（UTC）转成本机时区再求下一次匹配，再转回 UTC 存库。理由：这是单机桌面调度器，用户写 `"0 9 * * *"`的直觉预期是"本机每天早 9 点"，不是 UTC 9 点；`00-foundation.md` §4.1 规定的是**存储格式**（UTC ISO），不等于"表达式按 UTC 解释"，此前的实现把这两件事混为一谈。已知限制：本机时区偏移若不是整小时（极少数地区，如 UTC+5:30），跨时区部署会有分钟级别的边界效应；v1 不为此加 `tz` 列，桌面单机场景下按需再收口。

## 3. M：内置 Skill（FR12 内置）

- 目录：`jones_daemon/skills/bundled/<name>/`，格式与 Hermes 原生 Skill 一致（K 已验证外部目录接法）。
- `office-docs`：SKILL 说明 + `scripts/`（Markdown → Docx（python-docx）、→ PDF（用系统可用路径：优先 `pandoc`/LibreOffice 若存在，否则 reportlab 简版；报告写清选择）、→ 表格（openpyxl）；输出到当前 Project 目录，路径由 agent 传入）。依赖以 `uv run --with` 内联脚本元数据（PEP 723）声明，**不加进 daemon 的 pyproject**。
- `media-gen`：说明如何用 Hermes `image_generate` / `text_to_speech` / video 工具；需要哪个厂商 Key 走 B 的 provider（报告列清变量名）；没有 Key → provider_error 卡片。
- 验收（12.3 FR12）：办公文档四种格式各产出一份可打开（用 python 打开校验：docx 用 python-docx 读回、pdf 检查 %PDF 头 + 页数、xlsx 用 openpyxl 读回）；媒体三种各一份可播放（检查文件头/时长）——需要模型 Key 的部分 `JONES_E2E` 门控，报告如实写。

## 4. N：错误面板（FR14）

- `errors/classify.py`：把 daemon 能遇到的失败归成固定枚举 `ErrorKind = network | provider_auth | provider_quota | provider_error | tool_exception | worker_crash | approval_timeout | budget | internal`；输入是异常对象 / ACP 错误 / 子进程退出码，输出 `ErrorCard {kind, title, message, step_seq?, raw_excerpt(≤2KB, 已脱敏), actions:[retry|switch_model|abandon], retryable: bool}`。Key 脱敏走 B 的现有工具函数。
- `_terminate_run(kind="error"|"budget", card)`：`run.terminated` 的 `card` 字段统一用 ErrorCard；预算终止（token / API 额度 / 11.2 的 Step、时长上限）也走它。
- **N07**：worker 存活由 `WorkerManager._watch_exit` 已知；再加 daemon 侧「运行中 Run 超过 5s 没有任何 ACP update 且 worker 进程已退出」→ 立即 `worker_crash` 终止；UI 的「运行中」指示器只信 `turn.started`/`run.terminated`，不自己猜。
- RPC `session.retry {id, turn_id?, model_override?}`：重新发起该 Turn（新 Turn，用户消息复用），`model_override` 写入本 Turn 的 provider 解析；`abandon` = 清掉队列中该 Turn 的后续（`queue_items`）并标记 Run。
- renderer：错误卡片三个动作接 RPC；网络/配额/认证/工具/崩溃/超时/预算 7 类卡片样式区分（颜色 + 图标），每张卡片有「查看原始错误」折叠。
- 测试（G08 四种故障注入，全用假 ACP agent / provider stub，不联网）：断网（provider 解析抛 network）、Key 失效（provider_auth）、工具抛异常（ACP tool_call 返回 error）、kill worker（`worker_crash` ≤ 5s 被发现）。每种断言：出卡片、进程不崩、`run.terminated` 载荷形状正确、UI 无白屏（vitest 渲染卡片）。

## 5. O：存储收口（第 10 节）

- **目录审计**：写一个测试对照 PRD 10.2 的用户级与项目级目录树，`paths.py` 每个访问器都有对应项，多余/缺失即失败。
- **真删（G20）**：`maintenance.delete_session(id)` / `delete_project(id)` / `delete_run(id)`：事务内删 SQLite 行（级联 turns/messages/runs/steps/permission_decisions/queue_items），再删 `runs/<id>/` payload、`projects/<id>/attachments`，最后 `wal_checkpoint(TRUNCATE)`（busy → 重试 → 抛错，按 spike 03 契约）；记忆向量分片留钩子（FR18 P1）。RPC `session.delete`、`project.delete`（已有，改为调 maintenance）、`run.delete`（新增到 v0 表）。「导出后删除」：`session.export {id} -> 路径`（JSON），删除前可选。
- **凭据**：`vault.py` 原子写补 `fsync`；启动时 Key 脱敏自检——跑一遍所有日志文件与最近 100 条 RPC 响应样本，grep 已配置 Key 的任意 8 字节子串，命中即 `daemon.error`（G03 的运行时守卫，不只靠测试）。
- **备份轮转**：迁移备份 `jones.db.bak-*` 保留最近 5 份；`logs/` 滚动 7 天；`cache/` 一键清空 RPC `daemon.clear_cache`。
- **迁移（G19）**：测试：起 daemon 于 `JONES_HOME=A`，写入会话/Agent/Skill/记忆占位，停；整目录复制到 `B`，以 `JONES_HOME=B` 启动，断言会话历史/Agent/Skill 可读，`secrets/` 因绑定密钥链而需要重录（用 `JONES_VAULT_KEY` 模拟不同机器：不同 key 解密失败必须是**显式** `vault_key_mismatch` 错误 + 提示重录，不是崩溃）。
- **重启不重放**再验一次：`queue_items` pending 在重启后仍 pending（A 已有测试，这里做端到端：真实起停 daemon）。

## 6. 全体

- 性能：L 单定时器无轮询（断言空闲 CPU 无唤醒）；N 分类器纯计算；O 真删在 DB 线程、payload 删除不阻塞事件循环。
- 诚实失败：任何删除半途失败 → 记录部分完成状态并 `daemon.error`，不假装删完。
