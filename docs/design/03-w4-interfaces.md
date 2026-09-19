# 03 · W4：能力域——文件 / 终端 / 浏览器 / 调研 / MCP + 注册表 / Skill / 透明页

对应 Issue #13 #14 #15 #16 #17 #18 #19，以及 bug #34。上游：PRD FR07–FR10、FR12（加载）、FR13、FR16，12.1 G09/G13/G15/G21，12.2 N15；`00-foundation.md` §8、§9；`02-w3-interfaces.md` §1（闸）。

## 0. 第一性原理：W4 主要是「接线 + 证明」，不是「写工具」

Hermes 已经带全了能力域（`toolsets.py::_HERMES_CORE_TOOLS`：`read_file/write_file/patch/search_files`、`terminal/process_manage`、`browser_*`、`web_search/web_extract`、`skills_*`、`memory`、`execute_code`、`delegate_task`，以及 `mcp_servers` 客户端）。Jones 在 W4 要做的是：
1. **按 Agent/Session 选择并下发** Hermes 的 toolset 与 MCP 配置到 worker 的 `HERMES_HOME/config.yaml`；
2. **知道实际装配了什么**（注册表 + 透明页，G21：透明页 = 真实装配集合）；
3. **让每一类能力都经过三道闸并有测试证明**（G09/G15、§9 浏览器分级、N15）；
4. **Skill 目录接进 Hermes 的 skills 机制**（格式兼容，PRD 11.4）。
不重写任何 Hermes 已有工具。发现 Hermes 工具不满足 PRD 时，先写进报告再决定包一层还是改 PRD——不许在 Jones 里悄悄再写一个同名工具。

## 1. 分工与文件所有权

| 分支 | Issue | 独占 | 允许的共享改动 |
|---|---|---|---|
| H `w4/17-capabilities-mcp` | #17（+ #19 的 daemon 侧） | `daemon/src/jones_daemon/capabilities/`（新）、`kernel/plugin/jones_gate/` 的 `on_session_start` hook、tests | `workers/manager.py::_prepare_hermes_home`（**唯一**允许改它的分支：写 `toolsets`/`mcp_servers`/skills 路径进 config.yaml）、`rpc` 新方法 `capability.list` 在自己包的 `methods.py` |
| I `w4/13-14-files-terminal` | #13 #14 | `daemon/src/jones_daemon/permissions/defaults.py`（新：敏感目录默认 deny、终端高危分类补全）、`daemon/tests/test_cap_files*.py test_cap_terminal*.py`、`daemon/tests/integration/` 中对应用例 | `permissions/review.py`：只加分类规则不改结构；`sessions/service.py::_handle_tool_call_update`：把 ACP `diff` 内容写进 Step（若 Hermes 通过 ACP 传了 diff） |
| J `w4/15-16-browser-research` | #15 #16 | `daemon/src/jones_daemon/capabilities/browser.py`（Jones 专属 Chrome profile 与进程生命周期）、`daemon/tests/test_cap_browser*.py test_cap_research*.py`、`docs/design/00-foundation.md` §9（可修订） | 需要 H 的 `_prepare_hermes_home` 下发浏览器配置：J 在 `capabilities/browser.py` 暴露 `browser_worker_config(ctx, session) -> dict`，H 调它（H 未合并前 J 自己在测试里直接调） |
| K `w4/18-19-skills-ui` | #18（+ #19 UI）+ #34 | `daemon/src/jones_daemon/skills/`（新）、`apps/desktop/src/renderer/**`、`daemon/tests/test_skills*.py` | 无 daemon 侧共享改动；暴露 `skills.worker_skill_dirs(ctx, session) -> list[Path]` 给 H 调 |

`__main__.py` 加一行原则不变。

## 2. H：能力注册表 + MCP 接入（#17，#19 daemon 侧）

- **CapabilityRegistry**（`capabilities/registry.py`）：三种来源 `builtin`（Hermes toolset 内工具）、`mcp`（`ctx.config.mcp_servers(project_id)`，来自 C 的 mcp.json 合并）、`skill`（K 提供）。对每个 Session 计算 **期望装配集合**：Agent 白名单 ∩ 模式允许 ∩ 规则闸未 deny，未入选者带 `hidden_reason ∈ {not_in_allowlist, denied_by_rule, mode_chat, mcp_server_down, unknown_tool}`。
- **实际装配集合**：jones_gate 插件在 `on_session_start` 把 Hermes 当前会话真实注册的工具名清单写到 `<HERMES_HOME>/jones_tools.json`（源码核对该 hook 的 kwargs 能拿到什么；拿不到就在第一次 `pre_tool_call` 之外用 Hermes 的 registry API 读，写清出处）。`capability.list` 返回 期望 ∪ 实际 的对账结果：`{tools:[{name, source, enabled, hidden_reason?, actually_loaded: bool}], drift:[...]}`，`drift` 非空 = G21 失败信号，同时推 `daemon.error`（诚实失败）。
- **下发**：`_prepare_hermes_home` 写 config.yaml 的 `toolsets`（按 Hermes 语义选核心工具集合并禁用 Jones 不要的：`kanban_*`、`ha_*`、`computer_use`、`delegate_task` v1 关）、`mcp_servers`（Jones mcp.json → Hermes 格式，源码核对 `tools/mcp_tool.py` 接受的字段；stdio 与 HTTP 两种传输）、`skills` 路径（K）。第三方 MCP 工具默认不进 Agent 白名单（N15）：注册表把它们标 `not_in_allowlist` 直到用户在 Agent 配置里显式启用。
- **测试**：假 ACP agent 扩展一个 `tools/list` 等价通道来模拟 `jones_tools.json`；真实 Hermes 的 MCP 接入用 `JONES_E2E` 门控，接一个本地 stdio MCP（例如 Python 写的 5 行 echo server）与一个 HTTP MCP。

## 3. I：文件五件套 + 终端（#13 #14）

- 全部使用 Hermes 原生 `read_file/write_file/patch/search_files` + 目录遍历（核对 Hermes 用哪个工具遍历目录，若无则 `terminal ls`/`search_files` 组合，报告写明）与 `terminal/process_manage`。
- **敏感目录默认不可见**（G15）：`permissions/defaults.py` 给出默认 deny 路径集合（`~/.ssh`、`~/.aws`、`~/.gnupg`、浏览器 profile 目录、`~/.jones/secrets`、系统密钥链），在规则闸的 gate_config 中作为**用户不可放宽**的一层（与硬禁止同级但语义是「不可见」：读也拒，拒绝理由说明可在 permissions.json 中显式放开——PRD 11.3 允许显式放开，所以这层**可以**被用户级 permissions.json 的显式 allow 覆盖，但默认 deny；与硬禁止区分清楚）。
- **写入 diff 进 Step**（FR07 验收）：核对 Hermes 的 `write_file/patch` 通过 ACP `session/update` 的 tool_call content 是否带 `diff`；带 → `_handle_tool_call_update` 存进 `steps.result_summary`/payload；不带 → 在 jones_gate 的 `post_tool_call` 里算 diff 写到 payload（这是 Step 数据，不是新通道）。
- **终端**：流式输出经 ACP tool_call update 增量到 `step.started/…` → 前端 <200ms（测 daemon 内从收到 update 到 broadcast 的时延）；`session.stop` → ACP cancel → Hermes 终止子进程，测试断言无孤儿进程（G09）；高危命令分类补全（`sudo`、`curl|sh`、`chmod -R`、`dd`、`git push --force`、写 shell rc 文件）进审查闸 high。
- **测试**：G09、G15 各一条真实断言；`JONES_E2E` 下用真实 Hermes 跑一次 read/write/patch/search/terminal。

## 4. J：浏览器 + 深度调研（#15 #16）

- **浏览器——修订 §9 的裁定，二选一并实测**：
  - 方案 α（优先，第一性原理更省一层）：用 **Hermes 原生 `browser_*` 工具集**，配置其 backend 走 CDP，attach 到 **Jones 拉起的专属 Chrome**（`--user-data-dir=<user_root>/browser/profile`，`--remote-debugging-port=0` + DevToolsActivePort 发现；进程由 `capabilities/browser.py` 管：懒启动、daemon 退出时优雅关闭、崩溃重启）。核对 `tools/browser_tool_cdp.py` / `browser_tool_session.py` 支持的配置项（cdp url、是否允许自带 user-data-dir）。
  - 方案 β（后备，§9 现文）：Playwright MCP 作为 MCP server 接入（经 H 的 mcp_servers），Hermes `browser` toolset 关掉避免重名。
  - 判据：登录一次 → 杀 worker/daemon → 重启 → 登录态仍在；工具经三道闸（§9 分级表按实际工具名映射）；依赖与进程数。实测两者各一遍，选定后改写 §9 并说明理由。**不允许**两套同时开。
- **深度调研**（#16）：Hermes `web_search/web_extract` + 自带 `skills/research`（核对内容），Jones 侧只做：默认 Agent 的 Skill 集合包含它；产出「带引用的报告」的验收由 `JONES_E2E` 用例检查：报告中的引用 URL 可达率 ≥ 90%（脚本抽取链接 HEAD 请求）。搜索 provider 需要 Key 的（如 Hermes 用的 search API）走 B 的 vault/provider 机制——在报告里写清需要哪个 Key，缺 Key 时 `provider_error` 卡片而非静默。

## 5. K：Skill 加载 + 透明页 UI + #34（#18 #19）

- **Skill**：`skills/service.py`：用户级 `~/.jones/skills/`、项目级 `<project>/.jones/skills/`、内置（随 daemon 分发的 `jones_daemon/skills/bundled/`，W5 再放办公文档/媒体生成）三层；`worker_skill_dirs(ctx, session)` 给 H 写进 worker config 或 symlink 到 `<HERMES_HOME>/skills/`（核对 Hermes `get_skills_dir()` 与 optional skills 的加载方式，选侵入最小的）。格式与 Hermes 原生一致：验收 = 把 `/Users/nativeas/.hermes/hermes-agent/skills/` 里任选一个 skill 拷进 `~/.jones/skills/` 能被列出并被 Agent 引用。RPC `skill.list`（新增到 RPC v0 表，H 的白名单常量同步——K 改 renderer 白名单常量文件，写进报告）。
- **透明页 UI**（#19）：设置页新增「能力透明」：按 Session 展示 `capability.list`——工具名、来源、启用/隐藏、隐藏原因、`actually_loaded`；`drift` 非空显示醒目警告。切换 Agent / 启停 MCP / 增删 Skill 后即时刷新。
- **#34**：MessageList 把对象形态 `Message.content`（`{kind,text}`）当字符串渲染 → React #31 白屏。修法要改前提：renderer 的 `domain/types.ts` 与 daemon `sessions/queries.py::_d()` 对齐成同一形状，加 error boundary（白屏是 N16），并把 e2e 里那个 1s sleep 换成确定性等待（chatStore 暴露 bind 完成信号）。
- **审批卡片可读性**（F 的 minor）：`permission.requested` 载荷若已含解码后的工具名/参数，卡片直接渲染；若 daemon 侧不含，K 只在 UI 做占位并在报告里写明需要 daemon 侧（H 或后续）补——不要在 renderer 里猜测解析。

## 6. 全体

- 每条分支报告给性能数字：H 注册表对账耗时、I 终端 update→broadcast 时延、J 浏览器冷启动与工具往返、K 透明页渲染。
- 诚实失败：MCP server 起不来 → `mcp_server_down` 隐藏原因 + `daemon.error`；浏览器起不来 → 明确错误卡片；Skill 格式错 → 列出并标 invalid，不静默跳过。

## 7. H 落地时对 §2 的契约修正（2026-09-19，实现阶段发现，源码核对，非设计推测）

本节记录 H/#17 实现时发现、需要修正 §2 原文假设的三处，源码核对对象是
`/Users/nativeas/.hermes/hermes-agent`（`ee4452991d17534aa561f31ee55596d082aa94e7`）。

- **不存在"按 Agent/Session 选择 Hermes 命名 toolset"这回事**：`acp_adapter/
  session.py::SessionManager._make_agent` 把 ACP 会话的 `enabled_toolsets`
  硬编码为 `["hermes-acp"] + [f"mcp-{name}" for name in <config.yaml 的
  mcp_servers 键>]`——没有任何 `config.yaml` 字段、也没有任何 ACP 协议字段
  （`NewSessionRequest` 只有 `cwd`/`mcpServers`）能让客户端换一个内置
  toolset 包。`capabilities/registry.py::BUILTIN_TOOLS` 是 `"hermes-acp"`
  toolset 的真实展开（手抄自 `toolsets.py`，逐行核对），是 ACP 会话能暴露
  的**全部**内置工具全集；`kanban_*`/`ha_*`/`computer_use` 根本不在这个集合
  里（它们的 `check_fn` 依赖 Jones 从不配置的子系统，结构性缺席，不需要
  `disabled_toolsets`）；`delegate_task` 在集合里且 `check_fn` 恒真——对它
  "v1 默认关"唯一可用的杠杆是 F/W3 已建好的 `jones_gate.json`
  `tool_allowlist` 机制（执行期拦截，不影响模型是否"看得见"这个工具的
  schema），不是 `_prepare_hermes_home` 能做到的。默认 Agent 的
  `tool_allowlist_json` 是否已经排除 `delegate_task`，属于 Agent 默认值/
  种子数据的产品决策，不在 `agents/store.py` 非本 Issue 所有目录范围内，见
  报告"评审关注点"。
- **`config.yaml` 的 `mcp_servers:` 字段才是真正接线点，不是 ACP `session/
  new` 的 `mcpServers` 参数**：`acp_adapter/entry.py` 在 ACP server 启动时
  就从 `config.yaml` 后台发现/连接 `mcp_servers`（`hermes_cli/mcp_startup.py`
  ），且 `_make_agent` 用同一个字段计算 `enabled_toolsets` 的 `mcp-*` 项——
  两者共享同一份配置。ACP 协议自带的 `mcpServers` 参数是给"编辑器按会话传
  项目级 MCP 配置"用的另一条独立通道，Jones 每个 Session 本来就有专属隔离
  `HERMES_HOME`，用不上。`_prepare_hermes_home` 因此写 `config.yaml` 的
  `mcp_servers:`（JSON flow 语法，是合法 YAML 1.2，不需要引入 YAML 库也能
  正确转义任意嵌套结构），不写 ACP `new_session` 参数。
- **`mcp.json` 每条 server 记录的具体字段**（01-w2-interfaces.md §4.1 原文
  只定义到 `{"name": string, ...}`，留给实际消费者定稿）：`capabilities/
  mcp_config.py` 的模块 docstring是这份具体化后的规范，源码核对对象是
  `tools/mcp_tool_server_run.py::_prepare_run`（`"url" in config` 判定
  stdio/http）与 `acp_adapter/server.py::_mcp_server_config`（ACP 侧同款
  两种形状，佐证字段名）：stdio 是
  `{"name","transport":"stdio","command","args","env","enabled"}`，http 是
  `{"name","transport":"http","url","headers","enabled"}`（`"transport":
  "sse"` 可选，选 legacy SSE）。已用真实 Hermes（`uv sync --group worker` +
  额外 `mcp==2.0.0`，见 `daemon/pyproject.toml` 的 `worker` 组新增一行）验证
  一个 stdio + 一个 HTTP echo server 都能被 `tools.mcp_tool_discovery.
  register_mcp_servers` 正确连接，工具名为 `mcp__<server>__<tool>`（
  `tools/mcp_tool_schema.py::MCP_TOOL_NAME_PREFIX`/`build_mcp_tool_name`）。
