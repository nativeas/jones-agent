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

### 3.1 落地时的契约细化（2026-09-19，实现阶段发现，非设计推测；分支 I）

- **目录遍历核对结果**：源码核对 `hermes-agent/toolsets.py::_HERMES_CORE_TOOLS` 确认——**没有**专门的目录遍历/列目录工具（`read_file/write_file/patch/search_files` 之外没有第五个文件工具）；落地用的是本节原文已经预留的后备方案：`terminal ls`/`search_files` 组合（`JONES_E2E` 测试用 `terminal ls`）。
- **G15 的落点改为审查闸（③），不是规则闸的 gate_config（①）**：原文「在规则闸的 gate_config 中作为…一层」的前提，落地时发现与 02-w3-interfaces.md 六轮评审已经定型的规则闸架构对不上——`kernel/plugin/jones_gate/_rules.py::decide()` 对非 `terminal` 工具只做**工具名**精确匹配（01-w2-interfaces.md §4.1 明确「不做 glob/路径匹配」，W3 六轮从未加），没有任何按参数路径匹配 `permissions.json` 规则的机制；要在 gate_config 里做到「`read_file` 默认不可见但可显式放开」需要新增一整套路径感知的规则匹配机制，这超出「只加分类规则」的授权范围，也不在本分支的目录所有权内（`kernel/plugin/jones_gate/` 不在 §1 表格分给 I 的目录里）。改落地点：`permissions/defaults.py` 的默认 deny 路径集合改为在**审查闸**（`permissions/review.py::classify()`，daemon 侧 ③）里生效——`read_file`/`search_files`/`write_file`/`patch` 命中敏感路径、或 `terminal` 命令文本引用敏感路径，一律返回 `high`；`high` 在任何模式下都不会自动放行（`sessions/service.py::_on_request_permission` 的判定树只对 `low` 自动放行），天然满足「均被拒且走权限闸提示」。**用户显式放开的路径不变**：`permissions.json` 写 `{"match":"read_file","action":"allow"}` 这类整工具放行规则，命中时规则闸①在到达审查闸之前就「零 IPC 放行」（`kernel/plugin/jones_gate/__init__.py::_decide` 既有逻辑，未改），review.py 的默认 deny 检查根本不会被调用——PRD 11.3「显式放开」因此依然成立，只是粒度是「整个工具」而不是「这一条路径」（现有规则匹配的粒度本来就是这样，不是本分支引入的限制）。与硬禁止的区分不变：硬禁止在 `_hard_deny.py`，不可配置；这层可以被上面这条既有机制放开。
- **诊断依据**：Hermes 自己的 `agent/file_safety.py::get_read_block_error`/`tools/file_tools_write_guards.py::_check_sensitive_path` 源码核对后确认**不覆盖** `~/.ssh`/`~/.aws`/`~/.gnupg`/浏览器 profile/系统密钥链——前者只挡 Hermes 自己的凭据目录（`mcp-tokens/`、`browser-profile/`、`vault/`、`.env` 系列文件名）和内部缓存，后者只挡系统级路径（`/etc/`、`/private/var/db/` 等）和 Hermes 自己的 config.yaml；G15 点名的用户级敏感目录完全是 Jones 独有的责任，不是重复造轮子。
- **写入 diff 进 Step 的真实挂载点**：核对已安装 `hermes-agent`（`acp_adapter/tools.py`/`acp_adapter/events.py`/`acp_adapter/edit_approval.py`）发现，`write_file`/`patch` 的 diff 内容（ACP `{"type":"diff","path","newText","oldText"}`）**从不出现在完成事件**（`tool_call_update`，`_handle_tool_call_update` 原本假设的挂载点）——`acp_adapter/tools.py::_build_tool_complete_content` 对这两个工具没有特殊处理，落到通用格式化，不带 diff。diff 实际只出现在两处、互斥：① 编辑**自动批准**时，`tool_call` 「started」事件自带（`acp_adapter/events.py::make_tool_progress_cb`，仅当 `should_auto_approve_edit()` 为真才带）；② 编辑**需要人工批准**时，`session/request_permission` 请求本身的 `toolCall.content`（`acp_adapter/edit_approval.py::build_acp_edit_tool_call`），这条请求用的是 Hermes 自己另起的合成 `toolCallId`（`edit-approval-{n}` 计数器），与 `_handle_tool_call_start` 已经登记的真实 ACP `toolCallId` **不是同一个**。落地方案（`sessions/service.py`，仍在 §0 表格授权的 `_handle_tool_call_update` 范围内，但需要 `_handle_tool_call_start`/`_on_request_permission` 两个读取点配合——这两个函数本节授权范围之外的改动已控制到最小，只读取 `content`/暂存进 `_TurnContext.step_diffs`，真正的 DB 写入仍然全部只在 `_handle_tool_call_update` 里发生）：①②任一处提取到的 diff 都先暂存到 `_TurnContext.step_diffs[step_id]`（②场景下 `step_id` 用「本 Turn 内最近一次 started、尚未完成」的 step 作为关联——源码核对 `agent/tool_executor.py::_begin_tool_execution` 确认 `tool.started` 严格先于 `_pre_dispatch_guards`/编辑批准闸触发，且 v1 未开 `delegate_task`、Turn 内工具调用严格顺序执行，这个关联在当前代码库下总是对的，已写进代码注释），真正完成时由 `_handle_tool_call_update` 弹出并合并进 `result_summary`/payload（无 diff 的工具调用序列化形状完全不变，字节级兼容，回放侧无需改动）。
- **性能实测**（本机，`uv run pytest -q -s`，`_handle_tool_call_start`/`_handle_tool_call_update` 各 20 次取样，含真实 DB 写入线程往返）：update → broadcast 内部处理时延 p50 ≈ 0.09–0.13ms，max ≈ 0.27–0.50ms，远低于 200ms 预算（该预算的绝大部分余量留给真实 ACP/子进程调度——对照 02-w3-interfaces.md §1.5 的等价测量，一趟真实 ACP 往返本身约 21ms，daemon 应用层计算只占极小一部分）。
- **G09 的分层验证**：daemon 侧半条链路（`stop()` → 对处于 pending 审批中的终端类工具调用发送 ACP `cancel` 通知 + `_resolve_pending_permissions` 解除挂起）在假 ACP agent 下可完整测（`tests/test_cap_terminal_stop_cancel.py`）；真正「子进程被杀、无孤儿」的证明在 Hermes 一侧（`tools/environments/base.py::_wait_for_process` 的自适应 5ms–200ms 轮询 + `_kill_process_group_posix` 的 SIGTERM→SIGKILL 进程组杀除，源码核对），只有真实 Hermes 能验证——写了 `tests/integration/test_real_hermes_e2e_files_terminal.py`（`JONES_E2E` 门控）但本环境没有 `ANTHROPIC_API_KEY` 未能实跑，见报告「没做什么」。

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
