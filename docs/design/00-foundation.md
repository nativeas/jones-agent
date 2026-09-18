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
  - **复用范围**：`hermes_state_*.py`（Session/Message/Turn 的 SQLite facade）建议直接作为 worker 内的会话存储，Jones 的 `sessions`/`messages` 表退化成引用 Hermes session_id 的外键，不重复造；`tools/`、`skills/`、MCP client 原样复用（PRD 6.2 本来的意思）；`cron/` 和 `gateway/`（Hermes 自己的 IM 网关，注意和 PRD 里 Jones 的 Channel Gateway 撞名，不是一个东西）功能对得上但没有在本 spike 验证，留给后续 spike。Jones 独有、Hermes 没有对应物的：`runs`/`steps`/`permission_decisions` 回放表——这条审计链只能由 Jones 自己的 `pre_tool_call` 插件 + ACP 工具调用事件拼出来。
  - **未验证**：并发工具调用（`tool_executor.py` 的 8 线程池）下审批回调这个按线程槽会不会跨线程丢失——`agent/tool_executor.py` 有 `propagate_context_to_thread` 把 thread-local 的审批回调带进 worker 线程，源码看设计上没问题，但本 spike 没有并发场景的实测，留给 daemon 骨架落地时的集成测试覆盖（见下方"下一步"）。
- 向量库（spike #3）、浏览器登录态（spike #4）、打包（spike #2）。
