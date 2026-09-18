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
├── packaging/               # launchd plist、PyInstaller spec、electron-builder 配置、签名脚本
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
| 打包 | PyInstaller（daemon 单目录）+ electron-builder（把 daemon 作为 extraResources） | spike #2 验证 |

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

传输：NDJSON，每行一个 JSON-RPC 2.0 对象。请求/响应/通知三种。id 为字符串。所有时间为 ISO-8601 UTC。所有 id 为 ULID 字符串。

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

JSON-RPC 标准码 + 应用码：`1001 not_found`、`1002 invalid_state`（如对纯对话模式发工具调用）、`1003 permission_denied`、`1004 provider_error`、`1005 budget_exceeded`、`1006 kernel_error`。`data` 里带人可读 `message` 与结构化 `detail`。

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

主会话：`sessions.is_main = 1` 唯一（部分唯一索引），随首次启动创建，不可删除。

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
  - **Hermes 内置的批准绕过路径必须在 worker 启动时禁用**（2026-09-19 评审新增，源码证据见 [docs/spikes/01-hermes-hook.md](../spikes/01-hermes-hook.md) §"Hermes 内置的批准绕过路径"）：Jones 把用户闸"委托"给 Hermes 自己的 `request_tool_approval()` 走 ACP，但这个函数前面有三道能让它零等待直接放行、`session/request_permission` 根本不会被发出的短路——进程级 `HERMES_YOLO_MODE` 环境变量（冻结于 `tools.approval` 模块 import 时）、`config.yaml` 里的 `approvals.mode: off`、以及命中持久化 `command_allowlist`（`tools/approval.py` 模块级代码在 import 时无条件从活动 profile 的 `config.yaml` 加载）。daemon 拉起 worker 子进程时必须：不传 `HERMES_YOLO_MODE`、worker 的 `config.yaml` 不写 `approvals.mode: off`、worker 的 `HERMES_HOME` 与用户默认的 `~/.hermes` 相互隔离（Hermes 自己按 `HERMES_HOME` 分 profile 隔离永久 allowlist，直接复用这个机制即可）。已建议列为 FR05 验收项（见 `docs/PRD.md` §12.3、§6.3）。
  - **未验证**：并发工具调用（`tool_executor.py` 的 8 线程池）下审批回调这个按线程槽会不会跨线程丢失——`agent/tool_executor.py` 有 `propagate_context_to_thread` 把 thread-local 的审批回调带进 worker 线程，源码看设计上没问题，但本 spike 没有并发场景的实测，留给 daemon 骨架落地时的集成测试覆盖（见下方"下一步"）。
- 向量库（spike #3）、浏览器登录态（spike #4）、打包（spike #2）。

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
- **该 `HERMES_HOME` 下的 `config.yaml` 必须包含**：`plugins.enabled` 列表里加上 Jones 自研插件的
  `name`（`plugin.yaml` 的 `plugins.enabled` 是 opt-in 白名单，见"实测证据"一节末尾，未列入的插件即便
  发现也不加载）；**不得**写 `approvals.mode: off`（默认 `manual` 即可，见 §7）；`command_allowlist`
  初始为空或由 Jones 自己管理写入，不要复制用户默认 profile 的现存内容。
- **worker↔daemon 的进程关系**：daemon 是这个子进程的父进程和 ACP **client**（stdio 两端），worker/
  Hermes 是 ACP **agent**（server）——方向和"谁发 RPC 请求给谁"因此是：daemon 发 `initialize`/
  `new_session`/`prompt`/`cancel` 等方法调用给 worker；worker 反过来向 daemon 发
  `session/request_permission`、`session/update` 等（`acp/interfaces.py` 里 `Agent`/`Client` 两个
  Protocol 分别对应这两个方向，见 8.3）。

### 8.2 Jones 插件如何被加载

- **文件位置**：`<worker 的 HERMES_HOME>/plugins/<plugin_name>/`，至少两个文件：`plugin.yaml`（声明
  `name`/`version`/`hooks: [pre_tool_call, post_tool_call]`，与 §7 的 `plugins.enabled` 配合决定是否
  真的加载）+ `__init__.py`（定义 `register(ctx)`，内部 `ctx.register_hook("pre_tool_call", fn)` /
  `ctx.register_hook("post_tool_call", fn)`）。`docs/spikes/hermes_hook_demo.py::_materialize_plugin()`
  是这个落盘形状的可运行参照（demo 用临时目录，daemon 骨架落地时换成 daemon 管理的固定路径）。
- **注册方式**：Hermes 自己的 `hermes_cli.plugins.get_plugin_manager().discover_and_load()` 在进程
  启动时扫描 `<HERMES_HOME>/plugins/*/plugin.yaml`，对 `plugins.enabled` 里列出的名字调用其
  `register(ctx)`；daemon/worker 侧不需要（也不应该）自己重新实现一遍插件发现逻辑，`acp_adapter/entry.py`
  拉起进程的过程本身会触发这一步（**待验证**：本 spike 没有跑通 `python -m acp_adapter.entry` 的完整
  启动路径去确认 `discover_and_load()` 具体在哪一行被调用，只确认了它是 Hermes 自己在正常 CLI 启动路径
  上会做的事——留给 daemon 骨架 Issue 第一次真正启动 `acp_adapter.entry` 时核实调用时机，若发现 ACP
  入口没有自动触发插件发现，需要 daemon 侧在 `initialize` 之前手动调用一次）。

### 8.3 daemon 需要实现的 ACP client 方法最小集合

`acp/interfaces.py::Client`（`typing.Protocol`）声明的全部方法：`request_permission`、`session_update`、
`write_text_file`、`read_text_file`、`create_terminal`、`terminal_output`、`release_terminal`、
`wait_for_terminal_exit`、`kill_terminal`、`ext_method`、`ext_notification`、`on_connect`。

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
