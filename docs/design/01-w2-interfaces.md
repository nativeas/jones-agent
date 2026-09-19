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

### 2.1 落地时的契约变更（2026-09-19，实现阶段发现，非设计推测）

- **`queue_items.turn_id`**：002 迁移给 `queue_items` 加了一列 `turn_id TEXT REFERENCES turns(id)`。00-foundation.md §5 把 Turn 定义为"一次用户输入"（1 Turn -> 1 Run），但 `queue_items` 原表没有 `turn_id`，无法表达"排队中的输入已经有一行 Turn，只是还没轮到执行"。现在的语义：`session.send` 无论立即执行还是排队都先建 Turn+Message（`turn.messages` 里立刻可见），排队只是"这个 Turn 还没轮到 `worker.client.prompt()`"。理由与实现见 `store/migrations/002_seed_defaults_and_queue_turn.sql` 文件头注释。
- **`permission_decisions.decided_by` 从 `NOT NULL` 改为可空**：001 把这一列声明成 `NOT NULL`，但一个刚创建、还没人裁决的 `decision='pending'` 请求没有诚实的非空值可填——这是 001 的一个 bug，不是设计选择（用实际跑 `INSERT` 复现过 `NOT NULL constraint failed`，不是猜测）。已有迁移不能就地改，修复走 002 里标准的"重建表"手法（见该文件）。
- **`hermes-agent` 依赖形态与 §2 原文不同**：原文"锁到 commit...git 依赖或 PyPI 同版本...提交的 pyproject 必须是可复现的远端引用"这条在实现阶段发现走不通——PyPI 目前最新是 `0.19.0`（早于这个 commit 自报的 `0.21.2`，即这个 commit 还没发过 PyPI 包），而 `hermes-agent` 自己的 `setup.py` 对非 editable 安装（含普通的 `@ git+...` 直接引用）直接抛错拒绝（"Building wheels or sdists for hermes-agent is not supported...use an editable install instead"，已实测复现，非猜测），`uv` 的 `[tool.uv.sources]` 又不接受 `git` + `editable` 同时出现（"cannot specify both `git` and `editable`"，同样已实测）。结果是**唯一能让 `uv sync` 成功的形态是本机路径 editable 依赖**（`daemon/pyproject.toml` 的 `[tool.uv.sources]` 指向 `/Users/nativeas/.hermes/hermes-agent`，恰好是这个 commit 的完整 checkout）——这在别的机器/CI 上不是开箱可复现的，需要那台机器有同一 commit 的本地 checkout（或把这一行改指到它自己的路径）。这是这条子任务范围内能做到的最接近"可复现"的形态；真正跨机器可复现需要 Hermes 官方发一个匹配这个 commit 的 PyPI/可安装 artifact，不是 A 这条分支能解决的。
- **`cryptography` 版本下限从 `>=50.0.1` 放宽到 `>=50.0.0`**：B/#7 的 `uv add cryptography` 落了 `>=50.0.1`；`hermes-agent` 对每个直接依赖都精确锁定（它自己的补给链安全策略），锁的是 `cryptography==50.0.0`，与 `>=50.0.1` 无解可解。50.0.0 已有 `secrets/vault.py` 用到的全部原语（`AESGCM` 多年前就稳定），降下限不影响 B 的实现。
- **Provider 绑定未接入 worker 启动**：`ProviderResolver.resolve()`（B 已落地）目前没有被 `SessionService`/`WorkerManager` 调用——worker 的 `config.yaml` 里没有写入任何 `model:`/`providers:` 段。这不在 Issue #10 验收清单内（验收清单只要求队列/并行/重启/schema 版本，不要求真实模型能出字），且本机没有可用 Key 也无法验证接上之后的真实效果，所以留作明确记录的缺口（`sessions/service.py` 模块 docstring、本 PR 报告"没做什么"一节）而不是没说明地漏掉；`tests/integration/test_real_hermes_e2e.py` 用测试内 monkeypatch 手工绕过这个缺口以证明 worker 生命周期本身是对的。

### 2.2 第 1 轮评审修复：`hermes-agent` 依赖形态再次改变（2026-09-19，取代 §2.1 的"本机路径 editable"结论）

评审指出 §2.1 记录的"本机路径 editable 依赖"形态会让 CI（以及任何没有 `/Users/nativeas/.hermes/hermes-agent` 这份 checkout 的机器）的 `uv sync` 直接失败——已实测复现：把路径换成不存在的目录后，`uv sync` 报 `error: Distribution not found at: file:///nonexistent/hermes-agent`；`.github/workflows/ci.yml` 的 daemon job 第一步就是 `uv sync`，在 CI 的 ubuntu-latest 上这条路径必然不存在。

第一性原理重新审视："`daemon/` 自身代码是否真的需要 `hermes-agent` 才能 `uv sync`？" 答案是否：`src/jones_daemon` 下没有任何一处 `import hermes_agent`/`import acp_adapter`——`workers/manager.py` 只用 `sys.executable -m acp_adapter.entry` 当子进程命令行，从不在 daemon 自己的进程里 import 它；全部 128 个测试都通过 `worker_cmd=[sys.executable, "tests/fake_acp_agent.py"]`（纯 stdlib）注入假 worker，`tests/integration/test_real_hermes_e2e.py` 是唯一需要真实 `hermes-agent` 的测试，且已经用 `JONES_E2E=1`+`ANTHROPIC_API_KEY` 双重门控、本机无 Key 时自行跳过。

修法（对应评审给出的选项 (a)）：

- `hermes-agent[acp]` 从 `dependencies` 移到新的 `[dependency-groups] worker`，并显式加 `[tool.uv] default-groups = ["dev"]`——一个不带 `--group worker` 的 `uv sync`/`uv run` 现在完全不接触这个依赖，无论 `[tool.uv.sources]` 里那条本机路径存不存在。
- 但即便挪到非默认分组，`uv sync`/`uv run`（不带 `--frozen`）在没有既存 `uv.lock` 时仍会重新解析*全部*分组（包括 `worker`）来生成锁文件，同样会在路径不存在时报错；已实测复现，且即便预先提交一份用真实路径生成好的 `uv.lock`，`uv sync`（不带 `--frozen`）依旧会重新校验，同样失败——只有 `uv sync --frozen`（信任已提交的 `uv.lock`，不重新解析）才能在路径不存在的情况下成功。因此 `.github/workflows/ci.yml` 的 `daemon` job 新增 `env: UV_FROZEN: "1"`，对该 job 下的每一条 `uv sync`/`uv run` 生效（已实测：把路径改成 `/nonexistent/...` 后，`UV_FROZEN=1 uv sync && UV_FROZEN=1 uv run ruff check . && UV_FROZEN=1 uv run pytest -q` 全绿，128 passed / 1 skipped）。
- `worker` 分组里把裸的 `"hermes-agent[acp]"` 改成 `"hermes-agent[acp]==0.21.2"`（这个 commit 自报的版本号）——即便可解析来源那一半仍未解决，这至少把 §2 "锁到 commit" 里"锁版本"这一半的 metadata 落进提交物，而不是完全没有约束。
- `[tool.uv.sources]` 本身维持指向本机路径不变（真正的可移植修复仍然需要 Hermes 官方发一个匹配这个 commit 的可安装 artifact，不是这条分支能解决的）——区别在于现在只有显式 `uv sync --group worker` 的人才会撞到它，而不是每一次 `uv sync`/CI。

结果：`daemon/` 目录归属未变，`.github/` 不在任何 Issue 的专属目录表里，这次改动只加了 `env:` 一行 + 沿用已有的 `uv sync`/`uv run ruff check .`/`uv run pytest -q` 三步，未改 CI 的步骤结构本身。

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

### 3.1 六厂商 → Hermes provider 映射（源码核对，2026-09-19，commit `ee4452991d17534aa561f31ee55596d082aa94e7`）

核对方式：直接读 `/Users/nativeas/.hermes/hermes-agent` 这份 checkout 的源码（只读参考，未修改），不是看文档。关键结论：
Anthropic/OpenAI/DeepSeek/Qwen(DashScope)/Gemini 五家都在 Hermes 内置的 `PROVIDER_REGISTRY`
（`hermes_cli/auth.py`）里，靠环境变量自动探测凭据——**worker 的 `config.yaml` 不需要写
`providers.<name>` 块**，只需要 `model.provider` 指到对应的 registry id + 把 Key 放进对应的环境
变量。只有 Ollama（本地、不在 registry 里）需要一个显式的 `providers.<name>` 自定义条目
（`hermes_cli/config_providers.py` 的 "v12+ providers 形状"：`{api, key_env, ...}`）。

| Jones 厂商名 | Hermes provider id | 依据（PROVIDER_REGISTRY 行 / alias） | 默认 base_url | Key 环境变量 | worker `config.yaml` 写法 |
|---|---|---|---|---|---|
| `anthropic` | `anthropic` | `hermes_cli/auth.py` `_REGISTRY_ROWS`：`("anthropic", "Anthropic", "https://api.anthropic.com", ("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"), "ANTHROPIC_BASE_URL")` | `https://api.anthropic.com` | `ANTHROPIC_API_KEY` | 仅 `model: {default: <model>, provider: "anthropic"}`；传输层是 `anthropic_messages`（host-mandated，见 `runtime_provider.py::_HOST_MANDATED_API_MODES["api.anthropic.com"]`），不需要显式 `api_mode` |
| `openai` | `openai-api` | 同上文件：`("openai-api", "OpenAI API", "https://api.openai.com/v1", ("OPENAI_API_KEY",), "OPENAI_BASE_URL")` | `https://api.openai.com/v1` | `OPENAI_API_KEY` | 仅 `model: {default: <model>, provider: "openai-api"}`；官方 OpenAI host 自动走 Responses API（`is_official_openai_host` → `codex_responses`），不需要显式 `api_mode` |
| `deepseek` | `deepseek` | 同上文件：`("deepseek", "DeepSeek", "https://api.deepseek.com/v1", ("DEEPSEEK_API_KEY",), "DEEPSEEK_BASE_URL")` | `https://api.deepseek.com/v1` | `DEEPSEEK_API_KEY` | 仅 `model: {default: <model>, provider: "deepseek"}`（`chat_completions` 默认传输） |
| `qwen` | `alibaba`（DashScope；`qwen` 是别名） | 同上文件：`("alibaba", "Qwen Cloud", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", ("DASHSCOPE_API_KEY",), "DASHSCOPE_BASE_URL")`；别名见 `hermes_cli/providers.py` `_ALIAS_GROUPS["alibaba"]` 含 `"qwen"` | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` | `DASHSCOPE_API_KEY` | 仅 `model: {default: <model>, provider: "alibaba"}`。Jones 直接写 `"alibaba"`（canonical id），不依赖 Hermes 的 alias 表长期稳定 |
| `gemini` | `gemini` | 同上文件：`("gemini", "Google AI Studio", "https://generativelanguage.googleapis.com/v1beta", ("GOOGLE_API_KEY", "GEMINI_API_KEY"), "GEMINI_BASE_URL")` | `https://generativelanguage.googleapis.com/v1beta` | `GOOGLE_API_KEY`（`GEMINI_API_KEY` 是等价别名，registry 里排第二） | 仅 `model: {default: <model>, provider: "gemini"}`；透传 `chat_completions`，Hermes 在 base_url 命中 `generativelanguage.googleapis.com` 且非 `/openai` 结尾时内部转发到原生 Gemini adapter（`agent/gemini_native_adapter.py::is_native_gemini_base_url`），对 Jones 透明 |
| `ollama` | `ollama`（alias → canonical `custom`） | **不在** `PROVIDER_REGISTRY` 里；`hermes_cli/providers.py` `_ALIAS_GROUPS["custom"] = ("ollama",)`，即 `provider: "ollama"` 会被 Hermes 归一化到通用 OpenAI-compatible `custom` 传输 | `http://localhost:11434/v1`（`hermes_cli/models_local.py` 本地默认根 `http://localhost:11434`，OpenAI-compat 路径追加 `/v1`） | 无（本地默认免 Key）；可选，走 `providers.ollama.key_env` 引用一个 Jones 自定义环境变量（不内联 `api_key`，保持"Key 只在 env 里出现一次"） | **需要** `providers: {ollama: {api: "http://localhost:11434/v1"[, key_env: "JONES_OLLAMA_API_KEY"]}}` **加上** `model: {default: <model>, provider: "ollama"}`（`config_providers.py::_normalize_custom_provider_entry` 认的 v12+ providers 形状） |

实现落点：`daemon/src/jones_daemon/providers/catalog.py`（`VENDORS` 表，逐字段注了上面每一行的出处）+
`resolver.py`（`_build_binding` 按这张表拼 `ProviderBinding.env` / `.hermes_config`）。

模型清单（`model.list`）：五个 registry 厂商先用内置静态表（`catalog.py` 的 `VendorSpec.models`，
从这份 checkout 里出现过的真实 model id 摘取的一个小样本种子集，不是全量目录——Hermes 自己的
`model_tools.py`/`hermes_cli/models.py` 是对着一个实时的 models.dev 目录解析的，这张静态表预计
会过时，刷新留给 W5"BYOK 六厂商收口"）；Ollama 走本地 `GET http://localhost:11434/api/tags` 实时探测
（探测不到本地服务时返回空列表，不报错——见 `resolver.py::_ollama_live_models`）。

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
