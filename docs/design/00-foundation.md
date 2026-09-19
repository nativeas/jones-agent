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
| `capability.list` | `{session_id}` | `{tools: [{name, source, enabled, hidden_reason?, actually_loaded: bool}], drift: string[]}`（FR16、G21——评审第 3 轮 #7：此行原缺 `drift`/`actually_loaded`，与 03-w4 §2 的定义不一致，renderer 若信了这张表会对未定义字段解引用；H 落地时按这行实现） |
| `skill.list` | `{project_id?}` | `{skills: [{name, description, tier: "project"\|"user"\|"builtin", source_path, valid, error?}]}`（FR12，issue #18/#19；`project_id` 缺省时只看用户级+内置两层） |
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

JSON-RPC 标准码 + 应用码：`1001 not_found`、`1002 invalid_state`（如对纯对话模式发工具调用）、`1003 permission_denied`、`1004 provider_error`、`1005 budget_exceeded`、`1006 kernel_error`、`1007 too_many_requests`（单连接在途请求数超过上限，见 foundation 实现的每连接并发闸）、`1008 mcp_server_down`、`1009 capability_drift`（H/#17 §2、§8 新增：G21「诚实失败」信号，`capability.list` 的对账结果与 worker 启动/mcp.json 解析失败路径都会广播，走 `RpcServer.broadcast_all`——见 03-w4-interfaces.md §8）。`data` 里带人可读 `message` 与结构化 `detail`。

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

## 9. FR09 浏览器能力：daemon 内部契约（W4/#15-16 第四轮修订：R-J1/R-J2 控制者裁定收口）

**本节第四轮修订提醒（2026-09-19，第二轮评审后的控制者裁定 R-J1/R-J2，不可推翻）**：
第三轮文本（α 取代 β）本身不变，但 §9.2 的 `allow_private_urls: true` 强制项和
§9.3 的 `browser_navigate` 分级描述都需要订正——第二轮评审 finding #6/#8/#9
发现：(1) 这个开关同时关掉了 Hermes 对**跳转后**目标的 SSRF 复查，一次公网 URL
→ 302 跳转到内网页面即可绕过；(2) 这个开关的作用域不止浏览器，`web_extract`/
`vision`/`skills_hub` 的 SSRF 防护也被一并关掉。控制者裁定 R-J1：**本分支绝不
再设置这个开关**。§9.2/§9.3 下面已按这个裁定改写；`browser_worker_config` 的
`config_yaml` 现在是空字典，`permissions/review.py::_classify_browser_navigate`
的用户闸升级从「唯一剩下的闸」降级为「防御性冗余层」（Hermes 自己的
`_url_policy_error` 现在才是主闸，见 §9.2 新增段落）。

**本节第三轮修订**：上一版（W3 之前）裁定「复用现成浏览器 MCP Server（Playwright
MCP）+ Jones 专属常驻 Chrome profile」（β）。`03-w4-interfaces.md` §4 把这份裁定重新
开放为二选一并要求实测：α「用 **Hermes 原生 `browser_*` 工具集**，配置其 backend 走
CDP，attach 到 **Jones 拉起的专属 Chrome**」，或维持 β。**本轮裁定：α 胜出，β 整段
弃用**——理由与实测证据见下；PRD FR09 现有措辞「复用现成浏览器 MCP Server 承载具体
工具，Jones 不自研 browser.\* 工具集」因此过期，需要控制者同意后由 PRD 的所有者更新
（本分支不越权改 `docs/PRD.md`，写在这里留痕，见报告「契约变更」）。下面的 §9.1-§9.5
整段替换旧文；旧的 Playwright MCP 契约、进程模型、分级表全部删除，不保留占位——按
`03-w4-interfaces.md` §4「不允许两套同时开」，H（#17）的 MCP 接入不得注册任何
Playwright/浏览器类 MCP Server，Hermes worker 的 `browser` toolset 也不得被禁用
（它就是本节说的工具，禁用它等于关掉浏览器能力）。

### 9.1 为什么是 α：三条独立证据，不是单一论证

**证据 1——真的少一层（第一性原理）**：源码核对
（`ee4452991d17534aa561f31ee55596d082aa94e7`）确认 Hermes 自己的
`toolsets.py::_HERMES_CORE_TOOLS` 已经内置一组浏览器工具（`browser_navigate` /
`browser_snapshot` / `browser_click` / `browser_type` / `browser_scroll` /
`browser_back` / `browser_press` / `browser_get_images` / `browser_vision` /
`browser_console`，schema 见 `tools/browser_tool.py:437` 的 `BROWSER_TOOL_SCHEMAS`；
外加 `browser_cdp`——原始 CDP 透传逃生舱，`tools/browser_cdp_tool.py`——和
`browser_dialog`/`browser_vault_*`），跟 `read_file`/`terminal` 等其它核心工具**地位
完全相同**，随 worker 启动即注册，不需要任何 MCP 接入、不需要 H 的 `mcp_servers`
config.yaml 写入、不需要额外进程。`tools/browser_tool_cdp.py::_get_cdp_override_raw()`
读 `BROWSER_CDP_URL` 环境变量（优先）或 `browser.cdp_url` config 项，
`tools/browser_tool_session.py::_create_session_for_key()` 的优先级是
「CDP override > hybrid 本地 sidecar > cloud > local」——即 daemon 只需要在 worker 进程
的 env 里放一个 `BROWSER_CDP_URL`，Hermes 自己的浏览器工具就会 attach 到那个地址，不
会再走它自己默认的「local Chromium」路径（`_create_local_session`）。这就是 α 比 β
「更省一层」的字面意思：β 需要 daemon 额外管理一个 `npx @playwright/mcp` 子进程、把它
接成 MCP Server、再让 Hermes 通过 MCP 客户端调用；α 只需要 daemon 管理 Chrome 本身，
Hermes 已经知道怎么跟它说话。

**证据 2——闸的接入点更简单、更统一**：β 的旧文本依赖一个特殊论证（daemon 是 MCP
Server 的 `tools/call` 发起方，闸长在这次调用之前）。α 不需要这个论证：
`browser_navigate` 等工具是 `_HERMES_CORE_TOOLS` 的普通成员，走的是跟 `read_file`/
`terminal` 完全相同的 `pre_tool_call` 插件拦截点（00-foundation.md §7/§8 已经确立、F/#11
已经落地的三道闸机制）——**本轮用真实代码验证过这一点**，见下「实测」。这意味着 H
（#17）的能力注册表、F（#11）的规则闸/审查闸、G（#12）的 Step 记录，对浏览器工具完全
不需要特殊分支；唯一需要的分支是 §9.3 的风险分级表（工具名 → 三道闸），这跟 `terminal`
工具按参数分级是同一种机制，不是新机制。

**证据 3——登录态持久化更稳健，但有一个必须诚实写下的前提**：Jones 自己启动、自己
持有的 Chrome 进程，登录态落在这个 profile 目录的 Cookie SQLite 存储上，不依赖进程
连续性——**但这个磁盘写入不是 `Set-Cookie` 的同步效果**。本轮实测（见「验证方法」）：
对同一个 profile 反复「设置 persistent cookie → 立即 `SIGKILL`（无优雅关闭）→ 用同一
profile 重新拉起 Chrome → 访问受保护页」，8 次独立重复里只有 1 次登录态存活——
Chromium 的 `SQLitePersistentCookieStore` 会攒批写入，`Set-Cookie` 落盘有一个不可忽略
的窗口，这一点上 α 和 β 面对的是**同一个** Chromium 行为，不是 α 独有的弱点（β 旧文本
记录的「~30s 自发落盘」窗口就是同一机制的另一次观测）。真正让登录态可靠的是**优雅关闭
——发 `SIGTERM` 让 Chrome 走自己的正常退出流程，5 次重复全部存活**；`capabilities/
browser.py::BrowserManager.shutdown()` 因此总是先 `SIGTERM` 再等待，只有超时才回退
`SIGKILL`（详细数字见报告「判据实测」）。这比 β 依赖「daemon 记得在 kill 前调用一次
`browser_close` 工具」更简单：α 的优雅关闭是 daemon 自己代码里的一步，不依赖 worker/
Hermes 配合，也不占用任何工具调用的闸时延。

### 9.2 契约

```
BrowserManager（daemon 内部状态，capabilities/browser.py，非 SQLite 表，
                与 Step/Run 的持久化记录不冲突——回放读的是 Step 里记录的
                动作参数/结果摘要，不依赖这个活对象）：
  - profile_dir: <user_root()>/browser/profile   # Jones 专属，非用户 Default profile
  - proc: daemon 用 subprocess.Popen 直接拉起的 **真实 Chrome/Chromium 进程**（不是
      Node/npm 包装层）：
        <chrome_binary> --user-data-dir=<profile_dir> --remote-debugging-port=0
            --no-first-run --no-default-browser-check
      生产配置**不加 --headless**（用户需要在这个 Jones 专属窗口里手动登录，必须
      可见）；测试用 headless=True，见 `BrowserManager.__init__` 的 `headless` 参数,
      生产代码路径绝不翻这个默认值。
  - 端口发现：`--remote-debugging-port=0` 让 Chrome 自己选随机端口，写
      `<profile_dir>/DevToolsActivePort`（`<port>\n<ws_path>\n`）；
      `ensure_started()` 轮询这个文件（默认 10s 超时，`DEFAULT_LAUNCH_TIMEOUT_S`），
      找不到就是 `BrowserLaunchError`（诚实失败，不静默）。
  - 生命周期与崩溃/重启模型（`ensure_started()` 每次都做，不需要 daemon 自己记
      「上次是怎么关的」）：
      1. 先看 `<profile_dir>/DevToolsActivePort` 是否存在且端口真的对 CDP 应答
         （`GET /json/version`）——是则**复用这个活进程**（覆盖「daemon 被杀但
         Chrome 作为孤儿存活」这种情况：Unix 不会因为父进程死了就杀子进程）。这一步
         同时避开了 `docs/spikes/04-browser-login-state.md` 记录的单实例锁问题——
         永远不会对着一个已经被某个活 Chrome 占用的 profile 再拉起第二个进程。
      2. 端口不在/不应答 → 删除陈旧的 `DevToolsActivePort`、用同一个 `profile_dir`
         冷启动一个全新 Chrome（覆盖「Chrome 跟 daemon 一起被杀」的情况——登录态
         取决于 §9.1 证据 3 的优雅关闭前提，不取决于进程是否存活）。
  - 关闭：`shutdown()` 发 `SIGTERM`、轮询等待退出（默认 5s，`DEFAULT_SHUTDOWN_TIMEOUT_S`），
      超时才 `SIGKILL`。daemon 正常退出、空闲超时都走这条路径，绝不直接
      `SIGKILL` 一个还在运行的 Chrome（§9.1 证据 3 的实测结论）。
  - 单例：`get_browser_manager(user_root)` 按 `<user_root>/browser/profile` 缓存
      `BrowserManager` 实例——整个 Jones 安装只有一个 Chrome，所有 Session 共享同一个
      登录态（这是 FR09「登录一次、持续复用」的字面要求，不是 per-session 隔离）。

worker 侧接线（`capabilities/browser.py::browser_worker_config(ctx, session) -> dict`，
`async def`——见下方「为什么是 async」，03-w4-interfaces.md §1 授权 H 的
`_prepare_hermes_home` 调用这个函数）：
  - 惰性启动 Jones Chrome（`ensure_started()`，经 `asyncio.to_thread` 跑在 daemon
      event loop 之外），返回 `{"env": {"BROWSER_CDP_URL": "<http://127.0.0.1:port>"},
      "config_yaml": {}}`。
  - **控制者裁定 R-J1（第二轮评审 finding #6/#8/#9 后，2026-09-19，不可推翻）：
      `config_yaml` 绝不再写 `browser.allow_private_urls: true`。** 第三轮文本
      曾要求这个开关，理由是 `tools/browser_tool_cloud.py::_is_local_backend()`
      的 SSRF 防护把「CDP override」一律当成「可能在别的主机上」（文档字符串原话：
      "A CDP override is never trusted as local"），默认拒绝 `browser_navigate`
      打到 `127.0.0.1`/私网地址——这个技术判断没变，但第二轮评审核实到这个开关的
      真实作用域比文本暗示的大得多，且有一个第三轮文本没写的绕过口子：
      1. **跳转绕过（finding #8，critical）**：`tools/browser_tool.py::
         _post_redirect_block` 的私网复查同样看这个开关——开着它，一次公网
         `browser_navigate('http://attacker.example/x')` 跳转到
         `http://127.0.0.1:<daemon 自己的端口>/` 或内网管理页，跳转后的内容照样
         回到模型上下文，`_classify_browser_navigate` 只看模型传入的原始 URL，
         对这条跳转链路完全无感。
      2. **作用域外溢（finding #9）**：`tools/url_safety.py::
         _resolve_allow_private_urls` 把 `browser.allow_private_urls` 当作
         legacy 别名读取（`HERMES_ALLOW_PRIVATE_URLS` → `security.
         allow_private_urls` → 这个键），进而影响 `tools/web_tools.py`
         （`web_extract`）、`tools/vision_tools.py`（图片下载）、
         `tools/skills_hub.py`、`tools/image_source.py`、
         `tools/kanban_tools.py` 的 SSRF 检查——不是「浏览器专属」开关。
      源码核对（`tools/browser_tool_cloud.py::_is_local_backend`/`tools/
      browser_tool.py::_url_policy_error`/`_post_redirect_block`）确认：这个
      hermes-agent 版本里，「CDP attach 本身」（daemon 塞进 worker env 的
      `BROWSER_CDP_URL`，`_get_cdp_override_raw()` 读取）和「导航目标 SSRF/
      scheme 检查」共用同一个开关，没有更窄的、只放行 attach 控制通道的变体
      ——attach 这条控制通道本身完全不需要这个开关（它走 env var，跟
      `allow_private_urls` 无关），需要它的只是「导航到私网/`file://`目标」这
      一件事，而这正是评审要收紧的那一件事。**后果（诚实写下，不是回避）**：
      不设置这个开关意味着 `browser_navigate` 到 `file://`/`localhost`/私网地址
      现在被 Hermes 自己的 `_url_policy_error` 直接拒绝（返回错误，不是「转交
      用户闸后可以被批准放行」）——比「转用户闸」更严格，直到 hermes-agent 上游
      给这个开关拆出一个只作用于 CDP attach 的窄变体，或 Jones 自己给这份依赖
      打一个范围更窄的本地 patch（都不在本分支范围内：`hermes-agent` 不是这个
      仓库拥有的目录）。`permissions/review.py::_classify_browser_navigate` 对
      这些 URL 仍然分级 `high`（§9.3 新增条款）——现在是防御性冗余层（等那天真
      的拆出窄变体了，用户闸的可见批准仍然生效），不再是唯一的闸。
  - **为什么是 `async`（控制者裁定 R-J4）**：第二轮评审 finding #12 指出
      `ensure_started()` 是阻塞调用（`subprocess.Popen` + HTTP 轮询，~0.76s 常见、
      10s 最坏），调用方必须自己记得包一层 `asyncio.to_thread` 才不卡住 daemon
      event loop——`browser_worker_config` 现在直接是 `async def`，内部自己做
      `asyncio.to_thread(manager.ensure_started)`，调用方不需要（也不应该）自己
      再包一层。
  - **并发串行化（第三轮评审 finding #12，round-2 只做完了上一条，没做这一条）**：
      `asyncio.to_thread` 派发到真实线程池，不是协作式调度——两个 Session 并发
      走 `browser_worker_config` 时，会有两个真实 OS 线程同时进入
      `get_browser_manager`/`ensure_started()`。round-2 的版本在这里留了一句
      过期的文档「daemon 单事件循环天然串行，RPC handler 不会在不同 OS 线程上并发
      跑同一个 manager」——这句话被同一次改动（把这个函数改成 `async` + `to_thread`）
      自己证伪了，却一直留到 round-3 评审指出才删掉。`capabilities/browser.py`
      现在用 `_MANAGERS_LOCK`（保护 `get_browser_manager` 的建表 check-then-set）
      + `BrowserManager._lock`（保护 `ensure_started()` 整个方法体，含 `_launch()`）
      两把锁把这条并发路径重新变回串行：两个线程谁先拿到锁谁先跑完
      `ensure_started()`，另一个要么复用它刚启动的活 Chrome，要么（Chrome 还没起来时）
      排队等它，不会再出现「各自造一个 manager」「后一个 `_launch()` 删掉前一个的
      `DevToolsActivePort`」「两个真 Chrome 撞同一个 `--user-data-dir` 单实例锁」
      这三种竞态（见 `capabilities/browser.py` 两处锁的 docstring）。
  - **懒启动的触发时机（控制者裁定 R-J4）**：不允许在 worker spawn 时无条件调用
      这个函数（会给每个 Session 弹一个可见 Chrome 窗口，不管这个 Session 的
      Agent 有没有启用任何 `browser_*` 工具）。推荐的触发点：`sessions/
      service.py::_on_request_permission` 已经是「daemon 看到这个 Session 第一次
      真的要调 `browser_*` 工具」这件事天然发生的地方（规则闸/审查闸都要经过它才
      能决定放行），H（#17）落地实际接线时应该挂在这里，而不是 worker 启动路径；
      没有 Chrome 的机器上，`BrowserLaunchError` 必须映射成只针对浏览器工具集的
      `hidden_reason=browser_unavailable` + 错误卡片，不能让整个 worker/Session
      失败（本条是 `capabilities/browser.py` 对调用方的契约承诺，实际接线仍然是
      H 的工作，见 03-w4-interfaces.md §1；本分支没有落地这条接线，见报告
      「没做什么」）。
  - `session` 参数目前不参与决策（收到但不使用）——FR09 是「一个 Jones 安装一个
    Chrome」，不是 per-session 隔离；如果未来产品要求「每个 Session 独立浏览器身份」，
    需要重新设计这个签名，不能在今天的实现里悄悄按 session 分支。

工具（不再是外部 MCP Server 的工具，是 Hermes 自己 `_HERMES_CORE_TOOLS` 里的
`browser_*`；工具名与 schema 由接入时锁定的 hermes-agent 版本
`ee4452991d17534aa561f31ee55596d082aa94e7` 的 `tools/browser_tool.py` 定义，daemon 不
重新包一层同名壳）：
  browser_navigate / browser_snapshot / browser_click / browser_type /
  browser_scroll / browser_back / browser_press / browser_get_images /
  browser_vision / browser_console
  逃生舱与扩展面（风险显著更高，见 §9.3）：
  browser_cdp（原始 CDP 透传）/ browser_dialog / browser_vault_list /
  browser_vault_unlock / browser_vault_fill / browser_vault_save_login /
  browser_vault_enter_code
```

**已知功能缺口（诚实写下，不是 α 的隐藏代价）**：Hermes 原生工具集里**没有**
Playwright MCP 曾提供的 `browser_fill_form`（原子化表单填写）、`browser_select_option`
（下拉选择）、`browser_drag`（拖拽）、`browser_file_upload`（文件上传）、
`browser_hover`、`browser_tabs`（多标签页管理）、`browser_take_screenshot`（有
`browser_get_images`，语义不完全等价）、`browser_wait_for`。多数场景可以用
`browser_snapshot` 拿到 accessibility 树的 ref，再逐元素 `browser_click`/`browser_type`
组合出「填表」的效果（PRD FR09「填表」验收用这种组合覆盖，见报告验收对照）；但**没有
原生的文件上传工具**——`browser_vault_*`（密码库自动填充）不能替代这个缺口。这是一个
真实的功能缩水，留给 #15 之后的迭代：要么等 Hermes 上游补齐，要么在 Jones 侧对
`browser_cdp` 逃生舱包一层「文件上传」的语义化审查（不是重写整个工具集，只补这一个
缺口）——本分支不做这一步（不在 §1 授权范围内新增工具语义，只做浏览器进程管理），
写进报告「没做什么」。

### 9.3 权限分级（按 Hermes 真实工具名重写；第四轮修订同步 `permissions/
review.py::classify()` 的真实实现——控制者裁定 R-J2：这份表和那份代码必须
互相校验，见 `daemon/tests/test_gates_review.py::
test_section_9_3_table_matches_the_real_classifier`，改一边不改另一边这条测试
会红）

分级原则不变（PRD FR05 三道闸：机械工具名判定 → 规则闸；需要语义判断 → 审查闸；
高风险信号 → 用户闸）：

- **规则闸放行**（只读、按工具名机械匹配即可判定）：`browser_snapshot`、
  `browser_get_images`、`browser_vision`（只读页面理解，不产生副作用）、
  `browser_console`（`expression=None`/`clear=False` 时是只读读取；带
  `expression` 求值时降级到审查闸——工具函数签名本身允许两种用法，机械判定按「是否传
  了 `expression`」区分，不需要语义判断）、**`browser_navigate` 仅在目标 URL 通过
  下面④的机械安全检查时**（scheme ∈ `{http, https}` 且 host 不是字面
  private/loopback/link-local/CGNAT 地址，含十进制/八进制/十六进制/短点分等解析
  等价形式与 IPv4-mapped IPv6——`permissions/review.py::
  _looks_private_or_loopback`/`_parse_loose_ipv4`，round-2 finding #2/#8）——不
  满足则见④，不是「browser_navigate 无条件规则闸放行」（第三轮文本这里的表述不
  完整，第四轮改正）。
- **审查闸**（有副作用或工具名本身不足以判断风险，需要模型看参数/页面上下文）：
  `browser_click`、`browser_type`、`browser_scroll`、`browser_back`、`browser_press`、
  `browser_console`（带 `expression`，且④的网络/存储标记扫描未命中）、`browser_dialog`。
- **用户闸**（满足以下任一条件即标红升级）：①这次点击/按键实质
  是提交表单（即将触发导航或向服务器发起写请求）——语义判断来自模型，不是单独的
  工具名规则，v1 未实现（Hermes 原生工具集里没有语义化的「提交表单」工具，见
  `classify()` 文档字符串「v1 用规则先满足可测性，模型判断是 W4+ 增强」）；②任何
  触发外发的动作——同①，语义判断，v1 未实现；③`browser_console` 求值的表达式含
  网络请求（`fetch`/`XMLHttpRequest`）或存储写入（`localStorage`/`sessionStorage`/
  `indexedDB`/写 cookie）——**已实现**，`_classify_browser_console` 的字符串标记
  扫描；④`browser_navigate` 的目标 URL scheme 不是 `http(s)`，或 host 是字面
  private/loopback/link-local/CGNAT 地址（见上「规则闸放行」的判定细节）——**已
  实现**，`_classify_browser_navigate`；机械判定，不是语义判断。**控制者裁定
  R-J1（第二轮评审 finding #6/#8/#9）**：④现在是防御性冗余层，不是唯一的闸——
  §9.2 已改为不再设置 `browser.allow_private_urls`，这类 URL 首先被 Hermes 自己
  的 `_url_policy_error` 直接拒绝（比「转用户闸后可批准」更严格），④确保这个拒绝
  在 auto/task 模式下对用户仍然可见（`permission.requested` 广播），不是静默失败。
- **恒定用户闸**（工具名本身即代表高风险操作，不经过审查闸的语义判断，直接钉死）：
  `browser_cdp`（原始 CDP 透传——可以执行 `Network.setCookie`、`Input.dispatchMouseEvent`
  等绕过所有语义分级的原语，本轮实测用真实 `invoke_tool()` 验证过插件层能够拦住它，
  见「验证方法」）、`browser_vault_unlock`/`browser_vault_fill`/`browser_vault_save_login`/
  `browser_vault_enter_code`（触碰密码库）。
- **兜底档**（上面没点名的任何工具——包括未来新版本 hermes-agent 新增的
  `browser_*` 工具）：一律用户闸（fail-closed，`classify()` 的 catch-all 返回
  `medium`——见本文件顶部/`review.py` 文档字符串：「非 `low` 在 `sessions/
  service.py::_on_request_permission` 的分支里行为等同 `high`」，`medium` 从不
  自动放行，语义上就是「用户闸」）。**规则闸的默认映射表在 daemon 启动时与
  worker 实际暴露的工具名比对，出现未映射的 `browser_*` 工具名即记 warning 日志
  并按用户闸处理，不允许「未知即放行」**（同 §8.1 的启动自检精神）——这一条
  在代码里尚未实现（没有启动时的工具名比对/warning 逻辑），只有「未知工具名
  → `classify()` 兜底 medium」这一半；本分支没有补上启动自检那一半，见报告
  「没做什么」。
- **下载**：v1 不支持浏览器下载，与旧 β 文本结论一致；`browser_navigate`/`browser_click`
  触发的下载按触发它的那次调用定级，不低于审查闸。
- 用户可以在 `permissions.json` 里针对具体 `browser_*` 工具名进一步收紧（例如把
  `browser_navigate` 也钉死到用户闸），但不能放宽规则闸/恒定用户闸这两档——与 PRD
  规则闸「只能收紧」的既有原则一致（对齐 G14）。
- PRD 12.3 FR09 验收口径需要同 PR 更新为这份新分级（本分支不改 PRD，见报告）。

### 9.4 Step 记录与脱敏

不变于旧文本：Step 记录 `args_json` 时对 `browser_type` 的输入内容做脱敏（字段名/
页面上下文疑似密码则不落明文），对齐 N02。`browser_vault_*` 系列的参数本身可能就是
凭据引用（不是明文密码），仍按同样规则脱敏处理，不假设工具名本身就是安全的。

### 9.5 不做「接管用户当前浏览器窗口」——结论不变

`docs/spikes/04-browser-login-state.md` 的实测结论（单实例锁会把补开调试端口的第二次
启动直接转发并秒退）不受本轮 MCP→原生工具集的变化影响，继续成立：浏览器能力首次使用
时若 Jones 专属 profile 里未登录目标网站，工具应返回明确错误/提示，引导用户到 Jones
浏览器窗口里手动登录一次——对齐诚实失败原则。

### 验证方法（本轮实测，可复现）

真实源码、真实子进程、真实 Chrome，均可在有 `hermes-agent` checkout（`uv sync --group
worker`，见 docs/DEV.md）和真实 Chrome/Chromium 的机器上重跑：

- **单元测试**（不需要真 Chrome，CI 可跑）：`daemon/tests/test_cap_browser.py`，用
  `daemon/tests/fake_chrome.py`（stdlib-only，模拟 `DevToolsActivePort` 写入 + CDP
  liveness 探针）驱动 `BrowserManager` 的启动/幂等/重连孤儿/陈旧端口文件/SIGTERM→SIGKILL
  回退/`browser_worker_config` 的完整生命周期。
- **真实浏览器/真实 Hermes 集成测试**（`JONES_E2E=1` 门控，需要真 Chrome；其中网关测试
  还需要 `hermes-agent` 可 import）：`daemon/tests/integration/test_cap_browser_e2e.py`：
  1. `test_login_state_survives_a_graceful_shutdown_and_restart`——登录一次 → 优雅
     `shutdown()` → 用同一 profile 重新拉起 → 免登录访问受保护页成功（Issue #15 判据）。
  2. `test_agent_browser_close_does_not_kill_the_shared_chrome`——验证 Hermes 侧
     `browser_*` 工具（经 `agent-browser` CLI）调用等价于「关闭」的操作不会杀掉 Jones
     持有的共享 Chrome 进程（α 设计依赖的安全性质）。
  3. `test_real_hermes_browser_navigate_goes_through_the_gate`——**真实**
     `hermes_cli.plugins`/`agent.agent_runtime_helpers.invoke_tool()`：一个真实注册的
     `pre_tool_call` 插件对 `browser_cdp` 返回 `block`（真的在执行前拦截，从未连接
     CDP）、对 `browser_navigate` 返回 `approve`（真的落到 `model_tools.
     handle_function_call` → 真实 Hermes 浏览器工具 → 真实 agent-browser 子进程 → 真实
     CDP → 真实 Chrome 导航），与 `docs/spikes/hermes_hook_demo.py`（spike #1）同一套
     手法，但把验证范围从「block 生效」扩到「approve 之后真的执行到底」。
- 报告「判据实测」一节给出 8 次 SIGKILL / 5 次优雅关闭的完整重复实验数据与耗时。
