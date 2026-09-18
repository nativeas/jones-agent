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

## 8. FR09 浏览器能力：daemon 内部契约（spike #4 结论落地，第二轮已重写）

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

### 8.1 选型：为什么是 Playwright MCP，不是 chrome-devtools-mcp

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
是「懒加载启动、空闲后可能随时关闭」（见下 8.2），如果关闭时机恰好落在
Chromium 那个不受控的 ~30s 内部 flush 窗口之前，chrome-devtools-mcp 方案
会**真实丢失刚建立的登录态**，这不是理论风险，是本轮用同一套本地测试站
+ 同样的「杀进程模拟重启」手法实测复现的（见「验证方法」的输出）。工具
粒度、维护活跃度、依赖体积三项两者大体相当，不构成决定性差异。**v1 选
Playwright MCP**；chrome-devtools-mcp 的 DevTools 级能力（performance
trace、lighthouse、heap snapshot）留作以后如果需要更深的页面诊断能力时
的候选，不是这次 FR09 的取舍点。

### 8.2 契约

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
    （8.1 表格里的实测结论）。这个「先 close 再 kill」的顺序是本节新增的、
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
