# 00 · 基础设计：仓库布局、技术栈、进程与协议

对应 PRD 第 6 / 7 / 10 节。这是所有并行工作的共同契约；改这里要同 PR 改文档。

## 1. 仓库布局（monorepo）

```
jones-agent/
├── daemon/                  # Python 3.12 守护进程（PRD 6.1 ②③）
│   ├── pyproject.toml       # uv 管理；包名 jones_daemon
│   ├── src/jones_daemon/
│   │   ├── __main__.py      # `python -m jones_daemon` 启动
│   │   ├── paths.py         # ~/.jones/ 与 <project>/.jones/ 路径解析（PRD 10.2）
│   │   ├── rpc/             # NDJSON JSON-RPC 2.0 服务端（本文件 §3）
│   │   ├── store/           # SQLite 访问层 + migrations/（PRD 7、10.3）
│   │   ├── sessions/        # Session / Turn / 队列（PRD 9.2）
│   │   ├── workers/         # 按会话拉起的内核 worker 子进程管理，stdio
│   │   ├── kernel/          # 对 Hermes 的适配（由 spike #1 决定形态）
│   │   ├── permissions/     # 三道闸（PRD FR05）
│   │   ├── scheduler/       # Cron（FR11）
│   │   └── logging.py       # JSON lines 结构化日志
│   └── tests/
├── apps/desktop/            # Electron + React（PRD 6.1 ①）
│   ├── package.json         # pnpm；electron-vite + React 18 + TypeScript
│   ├── src/main/            # Electron main：拉起/发现守护进程、socket 客户端、contextBridge 白名单
│   ├── src/preload/         # 仅暴露 `window.jones` 的白名单 API
│   ├── src/renderer/        # React 三栏 UI（无 Node 权限）
│   └── tests/
├── packaging/               # launchd plist、python-build-standalone 打包脚本、electron-builder 配置、签名脚本
├── docs/                    # PRD、design、spikes
├── .github/workflows/ci.yml # pytest + ruff + pnpm test + tsc
└── Makefile                 # make check = 全部 lint + test
```

## 2. 技术栈与版本

| 层 | 选择 | 理由 |
|---|---|---|
| 守护进程语言 | Python 3.12（uv 管理解释器；hermes-agent 要求 >=3.11,<3.14） | 与 Hermes 同语言，库形式引入 |
| 内核 | `hermes-agent`（NousResearch，PyPI/Git 依赖，版本锁定） | PRD 6.2：不自研循环引擎 |
| SQLite | 标准库 `sqlite3`，WAL，手写迁移 SQL（`store/migrations/NNN_*.sql`） | 不引 ORM |
| RPC | JSON-RPC 2.0，NDJSON，Unix domain socket | 流式、简单、无 TCP（PRD 11.3） |
| 桌面 | Electron 33+，electron-vite，React 18，TypeScript strict，Zustand | 轻、快、够用 |
| 测试 | pytest / vitest；ruff；tsc --noEmit | |
| 打包（**已定案**，见 spike #2 与控制者裁定） | python-build-standalone：各架构分别用 `uv python install <target-triple>` 拿到该架构的预编译解释器，直接拷贝成 daemon 单目录（arm64 一份、x86_64 一份，不是 universal2 fat 二进制）；electron-builder 按目标架构选对应目录作为 `extraResources`（`${arch}` 宏，一份配置覆盖两次构建） | spike #2 验证：跟本仓库解释器管理工具 `uv` 一致（`uv python install <target-triple>` 零额外步骤拿到对应架构解释器），不需要装 uv 管理之外的 universal2 安装器；PyInstaller 并非做不到跨架构（给 universal2 基础解释器同样可行），只是需要多一条脱离 uv 的安装路径，见 `docs/spikes/02-packaging.md` §1 |

## 3. 进程模型

```
launchd (用户级 LaunchAgent, KeepAlive)
   └── jones-daemon (Python)  ── 监听 ~/.jones/runtime/daemon.sock
          ├── worker[session A] (Python 子进程, stdio)
          ├── worker[session B]
          └── ...
Electron main ──(socket client)──> daemon
   └── renderer (React) ──(contextBridge IPC)──> main
```

- 守护进程启动：写 `~/.jones/runtime/daemon.pid` 与 `daemon.sock`；已有活实例则退出（PID + socket 探活）。
- Electron main 启动：先连 socket；连不上则尝试 `launchctl kickstart`（已安装）或直接 spawn daemon（开发模式），最多重试 3 次后向 renderer 报错（PRD 11.3）。
- worker：由 daemon 按 Session 拉起，`python -m jones_daemon.workers.entry --session <id>`，stdio 上跑 Hermes 自带的 ACP（Agent Client Protocol）server（`acp_adapter/`，本身就是 JSON-RPC 2.0，不是自定义协议）；daemon 是 ACP client。worker 进程内启动时额外加载一个 Jones 自研的 `pre_tool_call` 插件，做 Step 级权限拦截（见 §7 spike #1 结论、PRD 6.3）。

## 4. RPC 契约 v0（daemon ⇄ 前端）

传输：NDJSON，每行一个 JSON-RPC 2.0 对象。请求/响应/通知三种。JSON-RPC 信封的 `id` 字段（请求/响应关联用）只要求是字符串，由前端自行生成（如客户端自增计数器加前缀 `c-<n>`），不要求是 ULID——ULID 是 §5 领域对象（`session`/`project`/... 的主键）的 id 格式，是另一套 id 空间，两者共用「id」这个字段名但含义不同。所有时间为 ISO-8601 UTC。

### 4.1 方法（前端 → daemon）

| 方法 | 参数 | 返回 |
|---|---|---|
| `daemon.ping` | — | `{version, pid, uptime_s}` |
| `daemon.status` | — | `{sessions_active, workers, memory_mb}` |
| `project.list` / `project.create` / `project.delete` | `{path}` | `Project` |
| `agent.list` / `agent.get` / `agent.upsert` / `agent.delete` | `Agent` | `Agent` |
| `session.list` | `{project_id?}` | `Session[]` |
| `session.create` | `{project_id, agent_id, parent_id?, mode?, title?}` | `Session` |
| `session.get` | `{id}` | `Session` + 最近 Turn |
| `session.set_mode` | `{id, mode: "chat"\|"task"\|"auto"}` | `Session` |
| `session.send` | `{id, text, attachments?}` | `{turn_id, queued: bool}` — 运行中则入队（PRD 9.2） |
| `session.queue` / `session.queue_remove` / `session.queue_reorder` | `{id, ...}` | `QueueItem[]` |
| `session.stop` | `{id}` | `{stopped: bool}` — 用户终止（PRD 9.3） |
| `turn.messages` | `{session_id, before?, limit}` | `Message[]` |
| `run.get` / `run.steps` | `{run_id}` | `Run` / `Step[]`（回放数据源） |
| `permission.pending` | `{session_id?}` | `PermissionRequest[]` |
| `permission.decide` | `{request_id, decision: "allow"\|"deny", remember?: "session"\|"project"}` | `PermissionDecision` |
| `provider.list` / `provider.set_key` / `provider.delete_key` | `{provider, key?}` | `{provider, has_key, key_hint}`（hint 只给末 4 位，PRD FR04） |
| `model.list` | `{provider?}` | `Model[]` |
| `capability.list` | `{session_id}` | `{tools: [{name, source, enabled, hidden_reason?}]}`（FR16） |
| `cron.list` / `cron.upsert` / `cron.delete` / `cron.run_now` | `Cron` | `Cron` |
| `settings.get` / `settings.set` | `{scope: "user"\|"project", project_id?, patch}` | `Settings` |

### 4.2 通知（daemon → 前端，按 session 订阅：`session.subscribe {id}` / `session.unsubscribe`）

| 通知 | 载荷 |
|---|---|
| `turn.started` | `{session_id, turn_id, run_id}` |
| `message.delta` | `{session_id, turn_id, message_id, delta}` — 流式文本 |
| `message.completed` | `Message` |
| `step.started` / `step.completed` | `Step`（含 tool 名、参数摘要、结果摘要、耗时） |
| `permission.requested` | `PermissionRequest`（含 gate: "rule"\|"review"\|"user"，风险等级，动作描述） |
| `permission.decided` | `PermissionDecision` |
| `run.terminated` | `{run_id, kind: "user"\|"error"\|"budget", reason, card}` — PRD 9.3 |
| `queue.changed` | `{session_id, items: QueueItem[]}` |
| `daemon.error` | `{code, message, detail?}` — 永不静默（PRD 5.5） |

### 4.3 错误码

JSON-RPC 标准码 + 应用码：`1001 not_found`、`1002 invalid_state`（如对纯对话模式发工具调用）、`1003 permission_denied`、`1004 provider_error`、`1005 budget_exceeded`、`1006 kernel_error`、`1007 too_many_requests`（单连接在途请求数超过上限，见 foundation 实现的每连接并发闸）。`data` 里带人可读 `message` 与结构化 `detail`。

## 5. 领域模型 → SQLite schema v1（PRD 7）

表名即对象名（蛇形复数）。所有表有 `id TEXT PRIMARY KEY`（ULID）、`created_at`、`updated_at`。

| 表 | 关键列 |
|---|---|
| `projects` | `path UNIQUE, name, settings_json` |
| `agents` | `project_id NULL(用户级), name, persona, tone, principles, tool_allowlist_json, skills_json, model_pref_json` |
| `sessions` | `project_id, agent_id, parent_id NULL, is_main BOOL, mode, title, status` |
| `turns` | `session_id, user_message_id, run_id NULL, status` |
| `messages` | `session_id, turn_id, role(user/assistant/system/tool), content_json, seq` |
| `tasks` | `session_id, goal_id NULL, cron_id NULL, source, status, title` |
| `runs` | `task_id NULL, turn_id NULL, session_id, status, started_at, ended_at, terminated_kind, terminated_reason, prompt_snapshot_ref` |
| `steps` | `run_id, seq, tool, args_json, result_summary, payload_ref, duration_ms, permission_id NULL, status` |
| `permission_decisions` | `step_id, gate, risk, decision, decided_by(rule/model/user), request_json, decided_at` |
| `queue_items` | `session_id, text, attachments_json, position, state(pending/sent)` |
| `goals` | `session_id NULL, project_id NULL, title, budget_tokens, spent_tokens, status` |
| `crons` | `project_id, agent_id, expr, prompt, mode, enabled, last_run_at, next_run_at, fail_count` |
| `providers` | `name, has_key BOOL, key_hint, default_model`（Key 本体在 vault，不在库） |
| `schema_version` | `version INT` |

主会话：`sessions.is_main = 1` 全局唯一（部分唯一索引，索引键仅 `is_main`、不带 `project_id`，跨所有 project 只允许一条），随首次启动创建，不可删除。

**追加（W3/#11，2026-09-19）**：`permission_decisions.decided_by` 的取值追加第四个：`timeout`——
PRD 9.4 审批超时自动拒绝时如实记录，不借用 `rule`（规则闸未参与）或 `user`（无真人）。该列本身是
无约束 TEXT，非枚举，这条只是补全文档描述，不涉及迁移。详见 `docs/design/02-w3-interfaces.md` §1.2。

## 6. 路径（PRD 10.2）

`paths.py` 提供 `user_root()`（默认 `~/.jones`，可用 `JONES_HOME` 覆盖，测试用）、`project_root(project_path)` → `<project>/.jones`，以及各子目录访问器；所有目录首次访问时创建。

## 7. 待 spike 决定的开放点

- **Hermes 接入形态**（spike #1，已完成，2026-09-18 评审后修正，结论详见 [docs/spikes/01-hermes-hook.md](../spikes/01-hermes-hook.md)）：**A + B 都要，不是二选一**。
  - **A（`pre_tool_call` hook）实测可行**：`hermes_cli.plugins` 在 `agent/agent_runtime_helpers.py::invoke_tool()` 里、任何工具真正派发前同步调用注册的 `pre_tool_call` 回调；回调可以真的阻塞调用线程等外部裁决，返回 `block` 时工具从不执行、`{"error": message}` 原样成为该工具调用的结果回到 agent——已驱动真实 `invoke_tool()`（用一个 ~5 行的 stub agent，不需要完整 `AIAgent`/任何 provider SDK）逐条断言通过，`docs/spikes/hermes_hook_demo.py` 可独立重跑。这是唯一覆盖**任意**工具调用（不只是 Hermes 自己认的"危险命令"）的拦截点，FR05 的 Step 级权限闸必须靠它。
  - **B（`acp_adapter` 作为 worker 协议）也实测可行，且比自定义协议成熟得多**：stdio 上的标准 ACP JSON-RPC，`session/update` 推工具调用 start/complete 事件与流式文本/思考 delta，`cancel()` 真的设置 agent 会检查的 `cancel_event`，`session/request_permission` 有现成实现（`acp_adapter/permissions.py::make_approval_callback`）。**且 ACP 的 `request_permission` 已经接在 `pre_tool_call` 的 `approve` 分支上**（见下），不是只接危险 shell 命令；它同时是 A 的传输层，也是 A 的一部分，不是并列的备选方案。
  - **组合结论（已修正：Jones 的插件不自己讲 ACP）**：worker 跑 ACP server（daemon 是 ACP client，直接复用 `acp_adapter/` 的会话生命周期、流式事件、cancel，不重造 stdio 协议）；Jones 自研一个 `pre_tool_call` 插件做 Step 级拦截。规则闸能本地决出的直接在插件里返回 `{"action":"block",...}`（不打 IPC）；审查闸/用户闸需要人工裁决的，插件**立即**返回 `{"action":"approve", "message", "rule_key"}`——真正的人工等待由 Hermes 自己的 `tools.approval.request_tool_approval()` 完成：它经 `tools/approval_context.py::_resolve_cli_approval_callback(None)` 落到 `tools/terminal_tool.py` 的按线程审批回调槽，而 worker 以 ACP agent 身份运行时，这个槽已经被 `acp_adapter/server.py::_run_agent_turn()` 绑定为 `make_approval_callback(conn.request_permission, ...)`——approve 因此自动变成一次真实的 ACP `session/request_permission` 往返。**Jones 的插件不需要、也不应该自己再发起或等待一次 ACP 往返**；DEV.md 原则 1（能复用 Hermes 的就不要重写）在这里的落地就是"只返回 approve，剩下的交给 Hermes"。
  - **一个必须记住的坑**：`pre_tool_call` 回调本身受 `plugins.hook_callback_timeout` 限制（config 项，默认 30s，超时直接 fail-closed 拒绝）。这 30s 只卡"这次回调本身要跑多久"——插件立即返回 `approve` 时几乎不占用这个窗口；`request_tool_approval()` 随后在调用者线程上做的真人等待发生在 hook 派发**已经返回之后**，因此不计入这 30s（**已实测**：把 `hook_callback_timeout` 压到 0.5s、模拟人工审批耗时 1.5s，往返仍完整跑完，见 spike #1 demo 第 4 节）。**反过来，如果插件自己在回调里阻塞等待（自建 ACP 往返、`queue.get()` 等）**，这段阻塞就计入这 30s，超时后不仅这一次调用被 fail-closed，**同一个已注册回调**在后续 60s 抑制窗口内的**每一次** `pre_tool_call`（含完全无关的工具调用）都会被直接跳过判定为 block——不是单次失败，是整个会话的工具调用被连续拒绝到孤儿线程自己跑完 + 60s 抑制窗口过完为止（`hermes_cli/plugins_dispatch.py::_HOOK_TIMEOUT_SUPPRESSION_SECONDS`，已实测见 spike #1 demo 第 6/7 节）。这是权限闸可用性的硬约束，daemon 骨架/权限闸实现必须把"决定阶段快速返回"当成正确性要求，不是性能优化项。
  - **待定，留给 W3 权限闸 Issue**：ACP 还有第二个 `session/request_permission` 接入点，专管 `write_file`/`patch` 的编辑审批（`acp_adapter/edit_approval.py::maybe_require_edit_approval`），走独立策略（ask/workspace_session/session，另有 `.env`/`id_rsa` 等敏感文件自动放行名单）。daemon 作为 ACP client 时，这一路和 Jones 自己 `pre_tool_call` 闸的 `write_file`/`patch` 拦截会同时命中同一次文件写入——要么关掉 Hermes 这一路、要么复用它、要么让 Jones 的闸接管，本 spike 不定案，权限闸实现前必须先决定，否则要么用户被问两遍要么两套审批逻辑打架。
  - **复用范围**：`hermes_state_*.py`（Session/Message/Turn 的 SQLite facade）建议直接作为 worker 内的会话存储，Jones 的 `sessions`/`messages` 表退化成引用 Hermes session_id 的外键，不重复造；`tools/`、`skills/`、MCP client 原样复用（PRD 6.2 本来的意思）；`cron/` 和 `gateway/`（Hermes 自己的 IM 网关，注意和 PRD 里 Jones 的 Channel Gateway 撞名，不是一个东西）功能对得上但没有在本 spike 验证，留给后续 spike。Jones 独有、Hermes 没有对应物的：`runs`/`steps`/`permission_decisions` 回放表——**daemon 侧写**，不是 Jones 的 `pre_tool_call` 插件里写（插件对 approve 分支立即返回，从不知道最终结果）：规则闸的决定 daemon 只能从 ACP `session/update` 事件流异步得知，审查闸/用户闸的决定 daemon 自己就是拍板者、写库要排在回 ACP 响应之前；完整三条时序图、`steps`↔`permission_decisions` 关联方式的已知缺口、写库失败时的诚实失败要求，见 [docs/spikes/01-hermes-hook.md](../spikes/01-hermes-hook.md) §"审计写入时序"（2026-09-19 评审新增，取代了本节此前"由 pre_tool_call 插件 + ACP 工具调用事件拼出来"这句不够精确的旧表述）。
  - **Hermes 内置的批准绕过路径必须在 worker 启动时禁用**（2026-09-19 评审新增；第三轮评审订正了归属并补了第 4 条，源码证据见 [docs/spikes/01-hermes-hook.md](../spikes/01-hermes-hook.md) §"Hermes 内置的批准绕过路径"）：Jones 把用户闸"委托"给 Hermes 自己的 `request_tool_approval()` 走 ACP，这条路径实际只受两道短路影响——进程级 `HERMES_YOLO_MODE` 环境变量（冻结于 `tools.approval` 模块 import 时）、命中持久化 `command_allowlist`（`tools/approval.py` 模块级代码在 import 时无条件从活动 profile 的 `config.yaml` 加载）；`config.yaml` 里的 `approvals.mode: off` 短路的是另一个独立机制——`terminal_tool`/`code_execution_tool` 自己内建的 Tier-1/2 危险命令/代码扫描，不影响 Jones 转发的 `approve` 请求，但同样必须禁用（否则少一层跟 Jones 插件并行的防线）。第四条性质完全不同：`HERMES_SAFE_MODE=1` 会让 `PluginManager.discover_and_load()` 直接跳过扫描（`hermes_cli/plugins.py:1221-1222`），Jones 的插件根本不会被加载——规则闸/审查闸/用户闸三道闸静默全部消失，不是某一道被短路。daemon 拉起 worker 子进程时必须：不传 `HERMES_YOLO_MODE`、不传 `HERMES_SAFE_MODE`、worker 的 `config.yaml` 不写 `approvals.mode: off`、worker 的 `HERMES_HOME` 与用户默认的 `~/.hermes` 相互隔离（Hermes 自己按 `HERMES_HOME` 分 profile 隔离永久 allowlist，直接复用这个机制即可）。已建议列为 FR05 验收项（见 `docs/PRD.md` §12.3、§6.3）。
  - **未验证**：并发工具调用（`tool_executor.py` 的 8 线程池）下审批回调这个按线程槽会不会跨线程丢失——`agent/tool_executor.py` 有 `propagate_context_to_thread` 把 thread-local 的审批回调带进 worker 线程，源码看设计上没问题，但本 spike 没有并发场景的实测，留给 daemon 骨架落地时的集成测试覆盖（见下方"下一步"）。
- 向量库（spike #3，已完成，选 sqlite-vec，见 [docs/spikes/03-vector-store.md](../spikes/03-vector-store.md)）、浏览器登录态（spike #4，已完成，契约见 §9）。

## 8. 给 W2/W3 实现者的接入清单（2026-09-19 评审新增，spike #1 源码核对；对应 issue #1 评审第 4 条）

本节只写"接口在哪、怎么接上"，不重复 §7 已经写过的设计结论（worker↔daemon 走 ACP、Jones 自研
`pre_tool_call`/`post_tool_call` 插件做 Step 级拦截 + 审计）；两节冲突时以 §7 结论为准，本节是它的落地
坐标。全部条目均为源码核对结果（`ee4452991d17534aa561f31ee55596d082aa94e7`），不是设计推测；标"待验证"
的例外单独说明。

### 8.1 worker 进程如何启动 Hermes ACP server

- **命令行**：`python -m acp_adapter.entry`（`acp_adapter/entry.py`，模块 docstring 自带
  `python -m acp_adapter.entry   # or: hermes acp / hermes-acp`）——stdio 上起标准 ACP JSON-RPC server，
  `stdout` 只用于 ACP 协议，日志全部走 `stderr`（`entry.py::_setup_logging()`，混进 stdout 会污染
  JSON-RPC 流，daemon 侧的 ACP client 实现不能假设 stderr 也是 JSON）。
- **必须的环境变量**：
  - `HERMES_HOME`：daemon 为每个 worker 显式设置的隔离路径（不是用户默认 `~/.hermes`），原因见 §7
    "Hermes 内置的批准绕过路径必须在 worker 启动时禁用"一条；`hermes_constants.get_hermes_home()`
    读取顺序是"context-local override → `HERMES_HOME` 环境变量 → 平台默认"，daemon 子进程 spawn 时
    传这个环境变量即可，不需要更底层的 override API。
  - **禁止**设置 `HERMES_YOLO_MODE`（不能从 daemon 自身环境继承，需显式构造子进程 env 并排除这个键，
    见 §7）。
  - **禁止**设置 `HERMES_SAFE_MODE`（同上；设了这个会让 `PluginManager.discover_and_load()` 整个跳过
    扫描，Jones 的插件永远不会被加载，见 §7）。
- **该 `HERMES_HOME` 下的 `config.yaml` 必须包含**：`plugins.enabled` 列表里加上 Jones 自研插件的
  `name`（`config.yaml` 的 `plugins.enabled` 是 opt-in 白名单，见"实测证据"一节末尾，未列入的插件即便
  发现也不加载——注意这是 `config.yaml` 的键，不是 `plugin.yaml` 的键，`plugin.yaml` 只声明插件自身的
  `name`/`version`/`hooks`）；**不得**写 `approvals.mode: off`（默认 `manual` 即可，见 §7）；
  `command_allowlist` 初始为空或由 Jones 自己管理写入，不要复制用户默认 profile 的现存内容。
- **启动自检（fail-closed，2026-09-19 第三轮评审新增）**：daemon 每次拉起 worker 子进程后，在把它接入
  正式会话前必须显式验证两件事，任一失败就拒绝启动该 worker（不能带着"权限闸可能不存在"这个状态继续跑）：
  1. 子进程 env 里确实没有 `HERMES_YOLO_MODE`/`HERMES_SAFE_MODE`（daemon 自己构造 env 时就该保证，这里
     是双重确认，防止未来有人在别处不小心把它们带回来）；
  2. Jones 自研插件确实被加载——`pre_tool_call`/`post_tool_call` 已注册。ACP 协议本身没有"列出已加载
     插件"的标准方法，建议做法：worker 启动后，daemon 主动发一次已知会命中规则闸的合成工具调用（例如一
     个 Jones 保留、必定被规则闸拒绝的工具名/参数组合），断言收到的是插件产生的拒绝结果而不是工具直接
     执行的结果；断言失败视为"插件未加载或 `HERMES_SAFE_MODE` 生效"，daemon 拒绝把这个 worker 交付给
     真实会话使用。这条自检解决的正是 `HERMES_SAFE_MODE` 的风险——它不产生任何显式报错，只是让 Jones 的
     三道闸静默消失，事后从单次工具调用结果上可能无法可靠区分"插件正常放行"和"插件根本不存在"，所以必须
     在启动阶段主动探测，不能被动等第一次真实工具调用去发现。
- **worker↔daemon 的进程关系**：daemon 是这个子进程的父进程和 ACP **client**（stdio 两端），worker/
  Hermes 是 ACP **agent**（server）——方向和"谁发 RPC 请求给谁"因此是：daemon 发 `initialize`/
  `new_session`/`prompt`/`cancel` 等方法调用给 worker；worker 反过来向 daemon 发
  `session/request_permission`、`session/update` 等（`acp/interfaces.py` 里 `Agent`/`Client` 两个
  Protocol 分别对应这两个方向，见 8.3）。

### 8.2 Jones 插件如何被加载

- **文件位置**：`<worker 的 HERMES_HOME>/plugins/<plugin_name>/`，至少两个文件：`plugin.yaml`（只声明
  插件自身的 `name`/`version`/`hooks: [pre_tool_call, post_tool_call]`）+ `__init__.py`（定义
  `register(ctx)`，内部 `ctx.register_hook("pre_tool_call", fn)` / `ctx.register_hook("post_tool_call",
  fn)`）。是否真的加载由 `plugins.enabled` 决定——**这是 `config.yaml` 的键，不是 `plugin.yaml` 的键**
  （见 §8.1），`plugin.yaml` 本身不含 `enabled` 字段。`docs/spikes/hermes_hook_demo.py::_materialize_plugin()`
  是这个落盘形状的可运行参照（demo 用临时目录，daemon 骨架落地时换成 daemon 管理的固定路径）。
- **注册方式与触发时机（已静态核实，非待验证）**：Hermes 自己的
  `hermes_cli.plugins.get_plugin_manager().discover_and_load()` 扫描 `<HERMES_HOME>/plugins/*/plugin.yaml`，
  对 `config.yaml` 的 `plugins.enabled` 里列出的名字调用其 `register(ctx)`；daemon/worker 侧不需要（也
  不应该）自己重新实现一遍插件发现逻辑。**触发点不在 `acp_adapter/entry.py`**（读过 `entry.py` 全文，
  它只做 stdio 日志路由和拉起 server，不含插件相关代码），而是每次 ACP `new_session`/`load_session`/
  `resume_session` 创建 `AIAgent` 实例时自动触发，调用链（源码逐跳核对）：
  `acp_adapter/session.py:421`（`AIAgent(**kwargs)`）→ `run_agent.py::AIAgent.__init__`（第 280 行调
  `init_agent(self, **init_kwargs)`）→ `agent/agent_init.py::init_agent()`（第 2281 行调
  `_load_tools(agent, ...)`）→ `agent/agent_init.py::_load_tools()`（第 1050-1051 行
  `from hermes_cli.plugins import discover_plugins; discover_plugins()`）→
  `hermes_cli/plugins.py::discover_plugins()` → `get_plugin_manager().discover_and_load()`。即：daemon
  不需要在 `initialize()` 之前手动调用插件发现——**每次真正创建会话时都会自动、幂等地触发一次**
  （`discover_and_load()` 内部用 `_discovered` 标记短路重复扫描）。**需要注意的风险**（不是待验证，是
  已确认的行为）：`_load_tools()` 里这次调用包在 `try/except Exception: logger.warning(...)` 里——发现
  失败（含 `HERMES_SAFE_MODE=1` 短路、或插件目录里的 `__init__.py` 抛异常）只打一行 warning，不会让
  `AIAgent` 初始化失败、不会让 ACP 会话创建失败，会话会"正常"起来但 Jones 的插件没有被加载。这正是
  §8.1"启动自检"一条要求 daemon 主动探测而非只看 Hermes 有没有报错的原因。

### 8.3 daemon 需要实现的 ACP client 方法最小集合

`acp/interfaces.py::Client`（`typing.Protocol`）声明的全部方法：`request_permission`、`session_update`、
`write_text_file`、`read_text_file`、`create_terminal`、`terminal_output`、`release_terminal`、
`wait_for_terminal_exit`、`kill_terminal`、`ext_method`、`ext_notification`、`on_connect`。**归属订正**：
`acp/` 不是 hermes-agent 自己的代码，是第三方依赖包 `agent-client-protocol==0.9.0`（PyPI 包名
`agent_client_protocol`，import 名 `acp`；本机 hermes-agent venv 里可见
`venv/lib/python3.11/site-packages/acp/interfaces.py` 与对应的
`agent_client_protocol-0.9.0.dist-info`）——daemon 侧实现这个 `Client` Protocol 时，需要的是这个 PyPI
包本身（pin 住同一个 `0.9.0` 版本，接口才对得上），不是去 hermes-agent 仓库里找这份源码。

- **必须真正实现**：`request_permission`（FR05 用户闸/审查闸的接线点，见 §7、spike doc"审计写入时序"）、
  `session_update`（daemon 据此维护 `steps`/`messages`/流式文本增量，是 RPC v0 §4.2 通知层的上游数据源）。
- **其余方法（`write_text_file`/`read_text_file`/`create_terminal`/`terminal_output`/`release_terminal`/
  `wait_for_terminal_exit`/`kill_terminal`）是否要实现，取决于 daemon 在 `initialize()` 握手时怎么声明
  `ClientCapabilities`**（`acp_adapter/server.py:502` 的 `initialize()` 接受 `client_capabilities` 参数）：
  Jones 的文件读写、终端执行已经是 Hermes 自己 `tools/file_tools.py`/`tools/terminal_tool.py` 里的工具
  （worker 进程内直接执行，不需要回调 daemon 来读写文件或起终端），推荐 daemon 在 `initialize()` 里如实
  声明不支持这些编辑器式能力，让 Hermes 走自己的工具而不是回调 daemon。**待验证**：本 spike 只核对了
  `Client` Protocol 的方法签名和 `initialize()` 接受 capabilities 参数这两点，没有逐条跟踪"声明不支持
  某能力后 Hermes 是否真的永远不会调用对应方法"——留给 W2 daemon 骨架落地时用一次真实握手核实，若发现
  某个方法即使声明不支持仍被调用，至少要给一个不崩溃的兜底实现（返回协议允许的"不支持"错误，不能让
  daemon 直接抛未处理异常打断 ACP 连接）。
- `Agent` Protocol（worker/Hermes 侧要实现、daemon 侧要调用的方法）已经由 `acp_adapter/server.py` 实现，
  daemon 只需要作为 client 去调，最常用的是 `initialize`、`new_session`/`load_session`/`resume_session`/
  `fork_session`、`prompt`、`cancel`——不需要 daemon 自己重新实现这一侧。

### 8.4 Hermes 自身 session/state（`hermes_state_*`）与 Jones SQLite 的边界

延续 §7"复用范围"一条，落到具体表级别：

- **复用 Hermes 的**：`hermes_state_*.py`（`hermes_state_sessions.py` 等）管理的 Session/Message/Turn
  facade 作为 worker 内的会话存储真源；Jones 的 `sessions`/`messages` 表（00-foundation.md §5）**引用**
  Hermes 的 session_id，不重复存一份消息内容。
- **Jones 自己记的**（Hermes 没有对应概念）：`runs`、`steps`、`permission_decisions`、`goals`、
  `queue_items`、`crons`、`providers` ——这些是 Jones 的回放/权限/多 Provider 模型特有的，daemon 侧
  `store/` 层写，见 §5 schema 与本节 §7 的审计写入时序。
- **明确不能复用、需要自己隔离的一条**：`hermes_state_sessions.py:730` 的 `model_config.yolo_mode`
  是 Hermes 自己的会话级 YOLO 状态字段——即使确认了 ACP 路径当前不会自动 `_restore_session_yolo()`
  （见 §7"批准绕过路径"一节最后一段），Jones 也不应该把这个字段当成自己的状态来源或去写它；Jones 的
  "会话是否处于宽松审批模式"应该是 Jones 自己 `sessions` 表的 `mode`/`settings_json`，不要跟 Hermes
  这个字段混用，避免未来 Hermes 给 ACP 补上 `/yolo` 等价物时，两套状态互相踩踏。

## 9. FR09 浏览器能力：daemon 内部契约（spike #4 结论落地，第二轮已重写）

评审要求（jones-agent#4 修复记录）：这份接口草案原来写在 `docs/spikes/04-browser-login-state.md`
里——按第 1 节的目录所有权表，`docs/spikes/` 只承载「技术验证报告」，不是契约的家；
现移到这里，spike 报告改为只引用本节。修 PR 见该 spike 报告末尾的「修复记录」。

> **第二轮修订**：本节第一版否决「复用现成浏览器 MCP Server」的核心论证——
> 「权限闸没有天然介入点，除非再插一层代理」——是**事实错误**：daemon 本身
> 就是这个 MCP Server 的 **客户端**，`tools/call` 请求是 daemon 自己代码
> 发出去的，权限闸只要长在 daemon 发起这次调用之前，天然就卡在了「工具调用
> 前」，不需要在 daemon 和 MCP Server 之间再插任何代理。控制者据此按工程
> 原则 1（能复用就复用，不重写已有能力）裁定：FR09 改为**复用现成浏览器
> MCP Server + Jones 专属常驻 Chrome profile**，Jones 不再自研 `browser.*`
> 工具集。下面是重新调研 + 实测后的新契约；旧的五个自研工具定义与旧论证
> 整段删除，不保留占位。

### 9.1 选型：为什么是 Playwright MCP，不是 chrome-devtools-mcp

两个候选都支持「以指定 `--user-data-dir` 启动/接管 Chrome」，都已用一段
独立探针脚本（`daemon/spikes/browser_probe.py` 之外，探针代码见本节末尾
「验证方法」）实测过：冷启动 → 通过 `tools/call` 导航到一个本机测试站的
`/login`（签发 persistent cookie）→ 读页确认登录成功 → **完整杀掉 MCP
server 子进程（模拟 daemon 重启，不是只断开连接）** → 重新起一个新的
MCP server 进程，同一个 profile 目录 → 直接访问受保护页，看登录态是否还在。

| 维度 | Playwright MCP（`@playwright/mcp@0.0.81`） | chrome-devtools-mcp（`chrome-devtools-mcp@1.9.0`） |
|---|---|---|
| 跨重启保持登录 | **实测通过**——但有前提，见下 | **实测不稳定**，见下 |
| 工具粒度 | 26 个工具，语义化到位（`browser_navigate`/`browser_click`/`browser_fill_form`/`browser_snapshot` 直接给可读的 a11y 树），单页场景不需要显式传 pageId | 29 个工具，更偏底层 DevTools 能力（`performance_start_trace`、`lighthouse_audit`、`take_heapsnapshot`），几乎每个工具都要求传 `pageId`（先 `list_pages` 拿 id），对 FR09「导航/读页/点击/填表」这个子集来说是多余的心智负担 |
| 登录态持久化机制 | 暴露 `browser_close` 工具，调用后立即 checkpoint 当前 context（实测：调用后 cookie 立刻落盘；不调用、直接杀进程，cookie 不落盘） | **没有**等价的「主动 flush 并进入可安全终止状态」工具——`close_page` 明确拒绝关闭最后一个页面（"The last open page cannot be closed"），实测对已有页面 `navigate` 到 `about:blank` 再杀进程、或开新页面后 `close_page` 关掉登录页，cookie 均**未**落盘；cookie 最终能落盘依赖 Chromium 内部的周期性 flush（实测约 30s 后才会自发落盘，不受 MCP 工具调用控制） |
| 维护活跃度 | Microsoft 官方（Playwright 团队）维护；首发 2025-03，最近一次发布 2026-09-14（4 天前），434 个已发布版本，发布节奏密集 | Google 官方（Chrome DevTools 团队）维护；首发 2025-05，最近一次发布 2026-09-08（10 天前），61 个已发布版本；同样活跃，起步比 Playwright MCP 晚约两个月 |
| 依赖体积（实测安装后 `node_modules`） | ~18MB（`@playwright/mcp` 108KB + `playwright` 4.9MB + `playwright-core` 13MB，三个包） | ~14MB（单一包，自带打包依赖） |

**结论**：`browser_close` 这个「主动落盘再关闭」的原语，是 Playwright MCP
相对 chrome-devtools-mcp 唯一但**决定性**的优势——FR09 的 daemon 生命周期
是「懒加载启动、空闲后可能随时关闭」（见下 9.2），如果关闭时机恰好落在
Chromium 那个不受控的 ~30s 内部 flush 窗口之前，chrome-devtools-mcp 方案
会**真实丢失刚建立的登录态**，这不是理论风险，是本轮用同一套本地测试站
+ 同样的「杀进程模拟重启」手法实测复现的（见「验证方法」的输出）。工具
粒度、维护活跃度、依赖体积三项两者大体相当，不构成决定性差异。**v1 选
Playwright MCP**；chrome-devtools-mcp 的 DevTools 级能力（performance
trace、lighthouse、heap snapshot）留作以后如果需要更深的页面诊断能力时
的候选，不是这次 FR09 的取舍点。

### 9.2 契约

```
BrowserMcpSession（daemon 内部状态，非 SQLite 表，进程重启即丢——与 Step/Run
                    的持久化记录不冲突，回放读的是 Step 里记录的动作参数/
                    结果摘要，不依赖这个活对象）：
  - profile_dir: <user_root()>/browser/profile   # Jones 专属，非用户 Default profile，路径不变
  - proc: daemon 用 stdio 子进程方式直接拉起的 MCP server 进程：
      npx -y @playwright/mcp@<锁定版本> --browser chrome
          --user-data-dir <profile_dir>
    **第三轮修复**：`--headless` 在 Playwright MCP 里是一个不接值的布尔开关
    （出现即代表启用 headless，不出现就是有头——旧版本这里写的
    `--headless=false` 是把值传给一个不吃值的 flag，Commander 直接报
    `error: unknown option '--headless=false'` 并以退出码 1 秒退，daemon 会
    拿到一个刚起来就挂掉的子进程）。有头是 Playwright MCP 的默认行为，daemon
    生产配置要有头，因此**正确做法是完全不传 `--headless`**，不是传某个
    「等于 false」的值。实际跑通的完整命令行（`@playwright/mcp@0.0.81`，本机
    macOS + 系统 Chrome，2026-09-19 实测：进程正常常驻、`initialize`/
    `browser_navigate`/`browser_close` 全部成功返回，退出码 0）：
      npx -y @playwright/mcp@0.0.81 --browser chrome --user-data-dir <profile_dir>
    浏览器可执行文件由 Playwright 按 --browser chrome 这个 channel 名自动发现
    系统已安装的 Chrome，不需要 daemon 自己维护一份「自动发现 Chrome/Edge
    路径」的逻辑（旧版本自己发现二进制路径的代码不再需要）。
  - 有头：用户需要在这个 Jones 专属窗口里手动登录，必须可见；本节的探针脚本
    （`daemon/spikes/mcp_reuse_probe/test_persist.py`）为了自动化跑得快显式加了
    `--headless`，daemon 的生产配置必须**不加**这个 flag（保持默认有头），不要
    照抄探针脚本。
  - 生命周期：daemon 直接持有并管理这个 MCP server 子进程（stdio pipe，和 FR13
    里任何一个 stdio MCP Server 的接入方式完全一致），第一次浏览器工具调用时
    懒加载启动；daemon 决定关闭它（空闲超时或 daemon 自己退出）前，必须先调用
    一次 `browser_close` 工具（等价于优雅关闭当前 page/context，触发 cookie
    落盘），再终止子进程——直接 SIGTERM/kill 子进程会有丢失刚登录状态的风险
    （9.1 表格里的实测结论）。这个「先 close 再 kill」的顺序是本节新增的、
    不能省略的实现约束。
  - MCP server 进程的生命周期完全由 daemon 的子进程收拢机制管理（懒加载、
    优雅关闭、崩溃重启都是 daemon 对自己拉起的这一个 stdio 子进程做的事），
    不存在「进程管理分裂成两套」的问题——这正是第一版论证搞错的地方。

工具（不再是 Jones 自研，是 Playwright MCP 的原生工具，经 daemon 的权限闸后
透传给 kernel/worker；工具名与 schema 由 Playwright MCP 定义，daemon 不重新
包一层同名的 browser.* 壳）：
  browser_navigate                    # 导航
  browser_snapshot / browser_evaluate  # 读页（accessibility 树 / JS 求值只读表达式）
  browser_click / browser_fill_form / browser_type / browser_press_key
  browser_drag / browser_file_upload / browser_select_option / browser_hover
  browser_tabs / browser_wait_for / browser_take_screenshot / browser_close
  （完整列表以接入时锁定的 Playwright MCP 版本的 tools/list 实际返回为准）

约束：
  - 权限闸的拦截点：daemon 是这个 MCP server 的 tools/call 发起方（不是把
    MCP server 直接暴露给 worker），闸的代码就长在 daemon 组装/发出这次
    tools/call 请求之前——这是「进程边界」意义上天然存在的介入点，不需要
    额外代理层，第一版认为「没有天然介入点」是错的。
  - 权限分级（**第三轮重写**，按控制者裁定）：上一版把 `browser_evaluate`
    （任意 JS 求值）塞进「规则闸默认放行」档，前提是「仅当求值表达式本身
    不含有副作用调用时」——这自相矛盾：规则闸是按 PRD FR05 定义的机械
    工具名/规则匹配层，没有能力判断一段任意 JS 字符串有没有副作用，把这个
    语义判断压给规则闸等于让它做它做不到的事。按 PRD FR05 的三道闸
    （规则闸 → 审查闸 → 用户闸）重新分级，用「能不能靠工具名机械判定」
    区分规则闸与审查闸，用「需要审查闸对参数/页面上下文做语义判断」区分
    审查闸与用户闸：
    - **规则闸放行**（只读、按工具名机械匹配即可判定，不经过审查闸）：
      `browser_navigate`（导航）、`browser_snapshot`（读页 a11y 树）、
      `browser_take_screenshot`（截图）、`browser_wait_for`（只是等待某个
      条件出现，不产生任何页面/文件副作用）。
    - **审查闸**（有副作用或工具名本身不足以判断风险，需要模型看这次调用
      的参数与页面上下文；这正是审查闸存在的目的——语义判断，不是硬塞进
      规则闸）：`browser_click`、`browser_fill_form`、`browser_type`、
      `browser_press_key`、`browser_drag`、`browser_select_option`、
      `browser_hover`、`browser_file_upload`、`browser_evaluate`、
      `browser_tabs`（新建/切换/关闭标签页）。下载没有独立工具，是某次
      `browser_navigate`/`browser_click` 的副作用，按触发它的那次调用定级，
      不低于审查闸。`browser_close` 由 daemon 生命周期管理自己在关闭子进程
      前调用（见上），不经过这里的分级；若被 agent 当普通工具主动调用，按
      审查闸处理。
    - **用户闸**（审查闸判断满足以下任一条件即标红升级；这个信号只能来自
      模型对参数/页面上下文的语义判断，不是单独的工具名规则，因为
      Playwright MCP 没有语义化的「表单提交」工具）：① 表单提交（这次
      点击/按键实质是提交表单，即将触发导航或向服务器发起写请求）；
      ② 任何触发外发的动作（把本地内容/文件发送到外部，如上传、发布）；
      ③ `browser_evaluate` 求值的表达式里含网络请求（`fetch`/
      `XMLHttpRequest` 等）或存储写入（`localStorage`/`sessionStorage`/
      `indexedDB`/写 cookie 等）。
    - **兜底档（控制者裁定，第三轮复审补）**：上面三档没有点名的任何工具
      ——包括但不限于 `browser_run_code_unsafe`、`browser_network_request`、
      以及未来新版本 Playwright MCP 新增的工具——**一律用户闸**（fail-closed）。
      规则闸的默认映射表在 daemon 启动时与 `tools/list` 实际返回比对，出现
      未映射工具名即记 warning 日志并按用户闸处理，不允许「未知即放行」。
    - **下载**：v1 不支持浏览器下载。导航或点击若触发下载，daemon 侧对该
      次调用按用户闸处理（能否在 MCP 启动参数层面直接禁用下载，由 FR09
      实现 Issue 核实锁定版本的参数后决定），而不是让 `browser_navigate`
      的规则闸放行与「下载不低于审查闸」互相打架。
    - 分级由 Jones 规则闸按 MCP 工具名做默认映射（上面前两档是固定映射，
      第三档由审查闸在放行到它手上的调用里逐次判断触发，不是独立的工具名
      规则）；用户可以在 `permissions.json` 里针对具体工具名进一步收紧
      （例如强制把某个工具整体钉死在用户闸，不管审查闸怎么判断），但不能
      放宽——与 PRD 规则闸「只能收紧」的既有原则一致（对齐 G14）。
    - PRD 12.3 FR09 验收口径同 PR 更新为这三档。
  - Step 记录 args_json 时对 `browser_fill_form`/`browser_type` 的输入内容做
    脱敏（若字段名/上下文疑似密码则不落明文），对齐 N02，这条约束不变。
  - 不做「自动发现并接管用户当前浏览器窗口」的路径（spike #4 实测：单实例锁会把
    补开调试端口的第二次启动直接转发并秒退，flag 被忽略，100% 复现，不存在不重启
    偷偷挂调试口这条路——这条实测结论不受本轮 MCP 选型变化影响，继续成立）；
    浏览器能力首次使用时，若 Jones 专属 profile 里未登录目标网站，工具应
    返回明确错误/提示（而不是静默失败），提示用户到 Jones 浏览器窗口里手动
    登录一次——对齐诚实失败原则。

### 验证方法（本轮新增实测，可复现）

不复用 `daemon/spikes/browser_probe.py`（那是 CDP/profile-copy 路径的探针，
协议不同）；新增 `daemon/spikes/mcp_reuse_probe/`，纯 stdlib 的独立 MCP stdio
JSON-RPC 客户端脚本，跑法：

```bash
python3 daemon/spikes/mcp_reuse_probe/test_persist.py playwright /tmp/jones_pw_profile
python3 daemon/spikes/mcp_reuse_probe/test_persist.py devtools   /tmp/jones_dt_profile
```

需要 `node`/`npx`（联网拉取 `@playwright/mcp`/`chrome-devtools-mcp`，首次运行
会有 npm 下载耗时）。脚本自带一个只监听 127.0.0.1、随机端口的本地登录态测试站
（不发外部请求，不涉及真实账号），两个候选各跑一次「登录 → 完整杀进程 → 重启 →
免登录读受保护页」，Playwright MCP 一支额外验证了「调 `browser_close` 后落盘、
不调则不落盘」这个关键差异点。详见 `daemon/spikes/mcp_reuse_probe/README.md`。
