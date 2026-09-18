# 01 · W1 收尾 + W2：模块所有权与内部接口

对应 Issue #5 #6 #7 #8 #9 #10。五条分支并行，本文件是它们之间的契约；改接口先改这里。
上游依据：`00-foundation.md`（§3 进程模型、§4 RPC v0、§5 schema、§8 Hermes 接入清单、§9 FR09）。

## 0. 并行分工与文件所有权

| 分支 | Issue | 独占目录 / 文件 | 迁移号 |
|---|---|---|---|
| A `w2/10-sessions-workers` | #10 | `daemon/src/jones_daemon/{sessions,workers,kernel}/`、`daemon/tests/test_sessions*.py test_workers*.py test_acp*.py` | `002_*.sql` |
| B `w2/7-providers-vault` | #7 | `daemon/src/jones_daemon/{secrets,providers}/`、`daemon/tests/test_secrets*.py test_providers*.py` | `003_*.sql` |
| C `w2/8-9-projects-agents` | #8 #9 | `daemon/src/jones_daemon/{projects,agents,config}/`、对应 tests | `004_*.sql` |
| D `w2/5-desktop-ui` | #5 | `apps/desktop/src/renderer/**`、`apps/desktop/src/preload/**`、`apps/desktop/src/renderer` 的 tests | — |
| E `w2/6-daemon-lifecycle` | #6 | `apps/desktop/src/main/**`、`packaging/**`、`daemon/src/jones_daemon/service.py`、`daemon/tests/test_service*.py` | — |

共享但只允许「加一行」的文件：`daemon/src/jones_daemon/__main__.py`（每个模块在自己的包里提供 `register(server, ctx)`，`__main__` 只加一行调用）。`00-foundation.md` 只允许追加，不改已有段落。其它目录不碰。

每个 daemon 模块的 RPC 方法放在自己包内的 `methods.py`，签名同 `rpc/methods.py` 现有风格：`async def handler(params: dict, conn: Connection) -> Any`。

## 1. 共享运行时上下文

```python
# daemon/src/jones_daemon/context.py  —— 由 A 创建（A 最先需要），其它分支只 import、不改
@dataclass
class DaemonContext:
    db: Database            # store/db.py 现有连接封装（单专用线程串行化）
    paths: Paths            # paths.py
    server: RpcServer       # 用于 broadcast / 按 session 订阅推送
    providers: "ProviderResolver"   # B 实现；A 只依赖 Protocol（见 §3）
    config: "ConfigResolver"        # C 实现；A/B 只依赖 Protocol（见 §4）
```
在 B/C 落地前，A 用 `NullProviderResolver` / `NullConfigResolver`（返回默认值、明确抛 `not_configured` 错误）跑通自己的测试；`__main__` 组装时替换。

## 2. A：Session / Turn / 队列 / Worker（#10）

- **SessionService**（`sessions/service.py`）：`create/get/list/set_mode/send/stop/queue_*`，直接对应 RPC v0 `session.*`；`send` 在会话运行中时入队（`queue_items`），当前 Turn 结束后按序取下一条；`stop` = 用户终止（PRD 9.3：正在执行的工具调用收尾，不再发新调用）。
- **主会话**：启动时若无 `is_main=1` 则创建（project = 用户 home 目录的隐式 Project，agent = 内置默认 Agent；后两者由 C 提供，C 未到位前 A 用占位常量 id `proj_default` / `agent_default` 并在 002 迁移里 seed 这两行的最小占位——C 的 004 迁移负责补全字段，**不改 id**）。
- **WorkerManager**（`workers/manager.py`）：每 Session 一个 worker 子进程 `python -m acp_adapter.entry`（00-foundation §8.1：隔离 `HERMES_HOME=<user_root>/workers/<session_id>/hermes`，env 显式构造、剔除 `HERMES_YOLO_MODE`/`HERMES_SAFE_MODE`，`config.yaml` 写入 `plugins.enabled: [jones_gate]`）。空闲超时（默认 10 min）回收；崩溃 → 该 Run 按 `error` 终止并推 `run.terminated`；**启动自检**（§8.1）失败即拒绝交付。
- **ACP client**（`kernel/acp_client.py`）：stdio NDJSON JSON-RPC，实现 daemon 侧需要的最小集合：`initialize`、`session/new`、`session/prompt`、`session/cancel`；处理 agent→client 的 `session/update`（文本/思考 delta → `message.delta`；tool call start/complete → `step.started/completed` 并写 `steps`）、`session/request_permission`（W2 只做一件事：写 `permission_decisions` 一行 `gate=user, decision=pending` 并推 `permission.requested`；**裁决逻辑是 W3 FR05**，W2 里 `permission.decide` 只把用户答复原路回给 ACP）。依赖 `agent-client-protocol==0.9.0` 的类型定义可选，协议本身手写即可。
- **Jones 插件骨架**（`kernel/plugin/jones_gate/`，随 daemon 分发、由 WorkerManager 落到每个 worker 的 `HERMES_HOME/plugins/`）：`pre_tool_call` 目前只实现两条：① 保留探测工具名 `jones.__probe__` 必定 `block`（供启动自检）；② 其它一律 `approve`（交给 Hermes 审批槽 → ACP request_permission）。规则闸/审查闸在 W3 填进来。`post_tool_call` 写 step 结果摘要到 stdout 之外的通道？——不要：step 记录以 ACP `session/update` 的 tool call 事件为事实源，插件不另开通道。
- **Hermes 依赖**：`daemon/pyproject.toml` 加 `hermes-agent` 依赖，**锁到 commit `ee4452991d17534aa561f31ee55596d082aa94e7`**（git 依赖或 PyPI 同版本，先查 PyPI 是否有对应版本；本机 `/Users/nativeas/.hermes/hermes-agent` 是同一 commit 的完整 checkout，可作为 `uv` 的本地 path 依赖做开发验证，但提交的 pyproject 必须是可复现的远端引用）。
- **测试**：用一个假的 ACP agent（`tests/fake_acp_agent.py`，纯 stdlib，按脚本回放 update/request_permission）覆盖 client、WorkerManager 生命周期、队列串行、stop、崩溃恢复；真实 Hermes 的端到端放 `tests/integration/`，用 `JONES_E2E=1` 门控（需要模型 Key，CI 不跑）。
- **通知路由**：`session.subscribe/unsubscribe` 在 A 里实现，`Connection` 上挂订阅集合；`RpcServer.broadcast(session_id, method, params)` 由 A 加到 `rpc/server.py`（这是 A 唯一允许改的 rpc/ 文件，加法不改法）。

## 3. B：Provider / Key vault（#7）

```python
# daemon/src/jones_daemon/providers/resolver.py
class ProviderBinding(TypedDict):
    provider: str          # anthropic | openai | deepseek | qwen | gemini | ollama
    model: str
    env: dict[str, str]    # 传给 worker 子进程的环境变量（如 ANTHROPIC_API_KEY / OPENAI_BASE_URL），Key 只在这里出现一次
    hermes_config: dict    # 写进该 worker config.yaml 的 model/provider 段（按 Hermes 的 config 结构，源码核对 hermes_cli/config 相关）
class ProviderResolver(Protocol):
    def resolve(self, model_pref: dict | None) -> ProviderBinding: ...   # model_pref 来自 Agent.model_pref_json；None → 用户默认
    def list_models(self, provider: str | None) -> list[dict]: ...
```
- Vault：`secrets/vault.py`，文件 `<user_root>/secrets/vault.enc`，AES-256-GCM（`cryptography`），数据密钥由 macOS Keychain 项 `jones-agent/vault-key` 派生/存放（`keyring` 库；Linux CI 用 `keyrings.alt` 文件后端或环境变量 `JONES_VAULT_KEY` 门控，测试用后者）。**Key 永不写日志、永不完整回显**：RPC 返回只给 `key_hint` 末 4 位。
- `providers` 表（已有）记 `has_key/key_hint/default_model`；RPC `provider.*`、`model.list`（各厂商模型清单先用内置静态表 + Ollama 本地 `/api/tags` 动态）。
- 六家厂商映射到 Hermes 的 provider 配置方式要源码核对（Hermes 主要走 OpenAI-compatible + anthropic extra），写进 `docs/design/01-w2-interfaces.md` 本节末尾（B 允许追加本节）。

## 4. C：Project / Agent / 配置合并（#8 #9）

```python
# daemon/src/jones_daemon/config/resolver.py
class ConfigResolver(Protocol):
    def settings(self, project_id: str | None) -> dict: ...        # 用户级 settings.json ← 项目级 .jones/settings.json 覆盖
    def permissions(self, project_id: str | None) -> Permissions: ...  # 用户级 ← 项目级只能收紧（PRD 10.1；违反的项忽略并记 warning）
    def mcp_servers(self, project_id: str | None) -> list[dict]: ...
```
- `projects/`：以目录路径为锚点建 Project（`projects` 表已有）；`<path>/.jones/` 首次创建；RPC `project.*`。Project 删除 = SQLite 行 + `<user_root>/projects/<id>/`，**不删用户目录里的 `.jones/`**（那是用户数据）。
- `agents/`：Agent 定义落 `<user_root>/agents/<id>/agent.yaml`（项目级在 `<path>/.jones/agents/<id>/`），`agents` 表是索引（同步策略：文件为事实源，启动与 `agent.upsert` 时同步入表）；RPC `agent.*`。内置默认 Agent `agent_default`（人设最小、白名单为空=全部工具经闸、模型偏好 None）。
- 004 迁移：补全 A 在 002 里 seed 的 `proj_default`（path = 用户 home）/`agent_default` 的字段；`settings_json` 结构由 C 定义并写在本节。

## 5. D：桌面 UI（#5）

只依赖 RPC v0 与通知（§4 of 00-foundation）。在 renderer 内实现 `RpcTransport` 接口 + `MockTransport`（vitest 与 `pnpm dev:mock` 用），真实实现走 `window.jones.rpc`。
- 左栏：Project 分组 → Session 树（主会话固定顶部、不可删除；子会话缩进）；新建会话；模式切换（chat/task/auto）。
- 中栏：消息流（虚拟列表；`message.delta` 增量渲染；`step.started/completed` 折叠卡片；`run.terminated` 错误/预算/用户终止卡片按 PRD 9.3 三种样式）；输入框；运行中发送 → 入队提示；队列面板（查看/撤回/调序）；停止按钮。
- 右栏：动作流（Step 时间线）+ 审批面板（`permission.requested` 卡片：允许/拒绝/记住本会话）。
- 设置页：Provider（配 Key，只显示 hint；六家）、Agent（六项编辑）、Project（选目录）。
- 性能：列表虚拟化；渲染路径不做 IPC；delta 合并批量 flush（≤ 16ms 一帧）。
- 修一个已知问题：`pnpm smoke` 日志里 `No handler registered for 'rpc:call'`——renderer 首屏在 main 注册 IPC handler 前就发起了调用；renderer 侧要等 `window.jones` 就绪且 main 的 handler 注册（main 由 E 负责，D 与 E 约定：main 在 `createWindow()` 之前完成 `ipcMain.handle('rpc:call')` 注册；D 侧对首个调用失败要显式呈现而非吞掉）。

## 6. E：守护进程生命周期（#6）

- `daemon/src/jones_daemon/service.py` + CLI 子命令 `python -m jones_daemon service install|uninstall|status`：写/删 `~/Library/LaunchAgents/ai.jones.daemon.plist`（基于 `packaging/launchd/` 草案，KeepAlive、RunAtLoad、日志到 `<user_root>/logs/`），`launchctl bootstrap/bootout`。
- Electron main：启动时 connect → 失败则 `launchctl kickstart -k gui/<uid>/ai.jones.daemon` → 再失败且处于 dev 模式则直接 spawn `uv run python -m jones_daemon`（打包模式 spawn `process.resourcesPath/daemon/...`）→ 3 次失败向 renderer 推 `daemon.error`。健康检测：`daemon.ping` 心跳 5s，断线走 RpcClient 状态机重连。`ipcMain.handle('rpc:call')` 必须在 `createWindow()` 前注册（见 §5）。
- 重启不自动重放：由 A 的队列状态机保证（重启后 `queue_items.state=pending` 不自动发送）；E 在 main 里不做任何「重连后自动重发」。
- 测试：service.py 的 plist 生成/路径为单测；`launchctl` 交互用可注入的 runner mock；main 的 ensureDaemonRunning 用 vitest 注入 fake connect/spawn。

## 7. 性能与诚实失败（全体）

- 每个 PR 报告写「性能影响」一行；A 的 worker 拉起 ≤ 2s（PRD 11.1），首 token 额外开销 ≤ 800ms 预算内，请在报告里给实测数字（假 ACP agent 也要测拉起时延）。
- 任何 `except` 必须处理或带上下文上抛；worker/子进程失败必须变成 `run.terminated` 或 `daemon.error` 通知。
