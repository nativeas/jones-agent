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
- worker：由 daemon 按 Session 拉起，`python -m jones_daemon.workers.entry --session <id>`，stdio 上跑同一套 NDJSON JSON-RPC（daemon 是客户端）。worker 内加载 Hermes。

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

- **Hermes 接入形态**（spike #1）：A) 库形式 `from run_agent import AIAgent` + 工具调用前 hook；B) Hermes 自带 `acp_adapter`（Agent Client Protocol，stdio，内建 `session/request_permission`）作为 worker 协议。**倾向 B**——权限请求、流式事件、会话是 ACP 原生概念，与 PRD 的 stdio worker 决定完全吻合；若 B 可用，`workers/` 的 stdio 协议直接采用 ACP 而非自定义 JSON-RPC。
- 向量库（spike #3）、打包（spike #2）。
- ~~浏览器登录态（spike #4）~~ 已决定，见第 8 节。

## 8. FR09 浏览器能力：daemon 内部契约（spike #4 结论落地）

评审要求（jones-agent#4 修复记录）：这份接口草案原来写在 `docs/spikes/04-browser-login-state.md`
里——按第 1 节的目录所有权表，`docs/spikes/` 只承载「技术验证报告」，不是契约的家；
现移到这里，spike 报告改为只引用本节。修 PR 见该 spike 报告末尾的「修复记录」。

浏览器能力作为 worker 侧的一组工具（类比 FR07 文件五件套），不在第 4 节 RPC v0 的
前端方法表里新增顶层方法，走既有的 `session.send` → Step 机制；这里定义的是
daemon 内部 `kernel/` 与「浏览器子系统」之间的契约，供 daemon 骨架 Issue 与浏览器
能力落地 Issue 实现时遵循：

```
BrowserSession（daemon 内部状态，非 SQLite 表，进程重启即丢——与 Step/Run 的持久化记录不冲突，
                回放读的是 Step 里记录的动作参数/结果摘要，不依赖这个活对象）：
  - profile_dir: <user_root()>/browser/profile   # Jones 专属，非用户 Default profile
  - browser_path: 自动发现（macOS: /Applications/Google Chrome.app/... 或 Edge 对应路径）
  - proc: 冷启动的 Chrome/Edge 子进程句柄，--remote-debugging-port=0（随机端口，
    spike #4 实测过 port=0 + 轮询 <profile_dir>/DevToolsActivePort 这条路径，
    不是只在文档里推荐、从没跑过的配置——见 daemon/spikes/browser_probe.py
    的 _read_devtools_active_port）
  - cdp_endpoint: 从 DevToolsActivePort 文件读到的实际 ws endpoint；attach 前
    必须校验监听该端口的进程 pid 就是本进程拉起的那个（lsof 核对），不能假设
    端口没被别的进程占用
  - 首个 CDP 客户端连上一个全新 profile 时，若前台还残留浏览器自带的初始 tab
    （例如 Edge 冷启动自带的 edge://sync-confirmation-dialog），应复用/接管
    已有 tab 而不是开新 tab——spike #4 实测：新开的后台 tab 在这种情况下
    会被节流，表单提交静默失效，复现 100%（daemon/spikes/browser_probe.py
    step_cdp_attach 的注释）
  - 生命周期：daemon 启动后懒加载（第一次浏览器工具调用才拉起）；daemon 退出/崩溃后此进程
    独立存活或一并退出待定（倾向：daemon 退出时优雅关闭，避免孤儿进程——需要 daemon 骨架
    Issue 里统一子进程收拢策略，此处只声明约束，不重复实现）

工具（暴露给 kernel/worker，经权限闸）：
  browser.navigate   {url}                          -> {title, url}
  browser.read       {selector?}                    -> {text | html}       # 只读，规则闸可默认放行
  browser.click       {selector}                     -> {ok}
  browser.fill        {selector, value}               -> {ok}
  browser.submit      {selector}                      -> {ok}               # 表单提交，必须过用户闸（PRD 12.3 FR09 口径）

约束：
  - navigate/read 为只读动作，可被规则闸放行；click/fill 视目标风险可能触发审查闸；
    submit 一律用户闸（表单提交视为有副作用的外发动作，对齐 12.3 FR09「表单提交走用户闸」）。
  - Step 记录 args_json 时对 fill 的 value 做脱敏（若字段名/上下文疑似密码则不落明文），
    对齐 N02。
  - 不做「自动发现并接管用户当前浏览器窗口」的路径（spike #4 实测：单实例锁会把
    补开调试端口的第二次启动直接转发并秒退，flag 被忽略，100% 复现，不存在不重启
    偷偷挂调试口这条路）；浏览器能力首次使用时，若 Jones 专属 profile 里未登录
    目标网站，工具应返回明确错误/提示（而不是静默失败），提示用户到 Jones 浏览器
    窗口里手动登录一次——对齐诚实失败原则。
  - 为什么不直接接一个现成的浏览器 MCP Server 顶替这一整套自研工具（工程原则 1
    「能复用就复用」）：调研过（见 spike 报告「c) 是否该用 MCP 顶替自研」一节），
    结论是不成立——权限闸要求在 Step 落库、拦截点在「工具调用前」而不是「进程边界」，
    经 stdio/HTTP 的 MCP Server 是黑盒子进程，闸没有一个天然的介入点能卡在它的
    工具调用和真正执行之间（除非在 daemon 和 MCP Server 之间再插一层代理，那样
    等于又实现了一遍这里的工具面，复用等于没复用）；且 CDP 进程生命周期
    （随 daemon 懒加载、随 daemon 优雅关闭）需要跟 daemon 自己的子进程收拢
    机制统一管理，交给外部 MCP Server 进程管理会分裂成两套生命周期。这不代表
    以后不能把 `browser.*` 这五个工具本身包成一个 Jones 自带的 MCP Server（对
    FR13 MCP 接入是同一形态），只是「谁持有 CDP 进程、闸在哪里介入」这两点决定了
    它不能是一个外部现成的、不受 daemon 控制的 MCP Server。
