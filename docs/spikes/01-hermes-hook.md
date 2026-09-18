# Spike #1 · Hermes 内核 hook 点验证（Issue #1）

对应 PRD 13.2 风险 1、6.2/6.3；00-foundation.md §7。

验证对象：[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
commit `0138269` (`main`, 2026-09-18)。

## 结论（先说结论）

1. **A（`from run_agent import AIAgent` + 工具调用前 hook）可行，且是唯一能覆盖任意工具调用的拦截点。**
   Hermes 有一个真实存在、同步阻塞、在任何工具执行前触发的插件 hook：`pre_tool_call`
   （`hermes_cli.plugins`，挂在 `agent/agent_runtime_helpers.py::invoke_tool()` 里）。
   回调可以真的阻塞调用线程等外部信号，返回 `block` 时工具从不执行、拒绝理由原样成为该次工具调用的结果传回给
   agent——**已用真实 hermes-agent 代码实测，不是读源码猜的**（见下文"实测证据"）。

2. **B（`acp_adapter/`，Agent Client Protocol，stdio）也可行，而且比自己发明一个 worker 协议成熟得多。**
   `hermes acp` 起一个标准 ACP JSON-RPC stdio server：真的有会话生命周期
   （`new_session`/`load_session`/`resume_session`/`fork_session`）、真的流式文本/思考增量、真的工具调用
   start/update/complete 事件、真的 `cancel()`（设置一个 agent loop 会检查的 `cancel_event`）、真的
   `session/request_permission`。**但它的 `request_permission` 只接在 Hermes 自己"危险 shell 命令"的启发式侦测上**，
   不会自动帮 Jones 拦下任意工具调用——它是 A 的一种传输层，不是 A 的替代品。

3. **推荐：A + B 组合，不是二选一。**
   - worker 进程跑 ACP server（daemon 是 ACP client），直接复用 `acp_adapter/` 的会话生命周期、流式事件、cancel，
     不重造 stdio 协议——00-foundation.md §3 的"worker stdio JSON-RPC"直接就是 ACP，不用另起一套。
   - worker 启动时额外加载一个 **Jones 自研的 `pre_tool_call` 插件**，做 FR05 要求的 Step 级权限拦截：
     规则闸能本地同步决出的（硬性禁止、`permissions.json` 命中）在 worker 内直接返回，不打一次 daemon IPC；
     审查闸/用户闸需要人工裁决的，复用 worker 已经开着的 ACP 连接，向 daemon（ACP client）发一次标准
     `session/request_permission` 等回复。
   - **关键坑，写进了 00-foundation.md**：`pre_tool_call` 回调本身受 `plugins.hook_callback_timeout`
     限制（config 项，默认 30s，超时直接 fail-closed 拒绝）。这 30s 只够"决定阶段"，不够真人在 Electron
     里点审批用的。Hermes 自己处理这个问题的方式是：`pre_tool_call` 返回 `{"action":"approve"}` 把决定权交给
     `tools.approval.request_tool_approval()`，那次人工等待发生在 `hook_callback_timeout` 计时**之外**。
     Jones 的插件必须照抄这个两段式设计，不能把 `queue.get()` 直接杵在 hook 回调里等审批。

4. **不满足→运行时层代理方案（PRD 13.2 的 Plan B）不需要**：spike 结果是 A 可行，不需要在 daemon
   层再包一层"工具调用先过守护进程"的运行时代理——`pre_tool_call` 本身就是这层代理，而且是 Hermes 原生支持的，
   不用我们自己拦 stdio 流去猜哪一行是工具调用。

## 实测证据

### 环境

- 网络对 `github.com`/`raw.githubusercontent.com` 有带宽限制（~25 KB/s），整仓库 clone（`git clone` 报告
  size ≈ 977 MB）在这个环境下不可行；改用 `git clone --filter=blob:none --depth 1`（treeless partial
  clone）+ 按需 `git show HEAD:<path>` 拉取单个源文件（PyPI/raw 单文件下载不受此限，每个文件 1–2s）。
  按 `hermes_cli.plugins` → `hermes_cli.lifecycle` → `agent.agent_runtime_helpers`（仅到
  `invoke_tool`/`_pre_tool_block_message` 需要的部分）这条真实 import 链，迭代拉取到约 90 个源文件，
  未拉取整仓库（`agent/`、`hermes_cli/`、`tools/` 各有 300–500 个文件，多数是 provider/auth/terminal 相关，
  与本次验证的问题无关）。跑通 `agent.agent_runtime_helpers` 的完整 import（走到需要 openai/anthropic SDK
  的那一层）预计还要再深入约 60+ 个文件外加若干第三方 SDK，本 spike 判断这部分对回答"hook 点是否存在、能否
  阻塞、能否拒绝"这个问题没有增量信息，故未做——`invoke_tool()` 调用 `_pre_tool_block_message()` 这一段
  的源码原文见下方"关键源码"，与本 spike 实测跑的 `_dispatch_pre_tool_call_hooks()` 是同一个函数，只是
  上面还包一层 try/except（失败即放行，不影响结论）。
- `uv venv --python 3.12`，只装了 `pyyaml`（`hermes_cli.plugins` 这条 import 链除了标准库只需要它）。
  **没有配置任何模型 provider API key，也没有发起任何模型调用**——`pre_tool_call` 在工具真正执行前、
  在任何 API 请求发生前同步触发，验证它不需要一个完整 Turn。

### 关键源码（hermes-agent 原文，未改写）

`agent/agent_runtime_helpers.py`（工具真正派发前的调用点）：

```python
def _pre_tool_block_message(agent, function_name, function_args, effective_task_id, tool_call_id, middleware_trace):
    """Plugin pre-tool-call hook verdict: ``(block_message, function_args)``; failures never block."""
    try:
        from hermes_cli.plugins import _dispatch_pre_tool_call_hooks
        block_message, modified_args = _dispatch_pre_tool_call_hooks(
            function_name, function_args, task_id=effective_task_id or "",
            session_id=getattr(agent, "session_id", "") or "", tool_call_id=tool_call_id or "",
            turn_id=getattr(agent, "_current_turn_id", "") or "",
            api_request_id=getattr(agent, "_current_api_request_id", "") or "",
            middleware_trace=list(middleware_trace),
        )
        return block_message, (modified_args if modified_args is not None else function_args)
    except Exception:
        return None, function_args


def invoke_tool(agent, function_name: str, function_args: dict, effective_task_id: str, ...) -> str:
    ...
    block_message: Optional[str] = None
    if not pre_tool_block_checked:
        block_message, function_args = _pre_tool_block_message(
            agent, function_name, function_args, effective_task_id, tool_call_id, _tool_middleware_trace
        )
    if block_message is not None:
        result = json.dumps({"error": block_message}, ensure_ascii=False)
        emit_terminal_post_tool_call(..., status="blocked", error_type="plugin_block", error_message=block_message, ...)
        return result   # <-- 工具从未执行；这个字符串就是 agent 收到的"工具结果"
    ...
    # 只有走到这里才真正派发到 model_tools.handle_function_call / inline_executor
```

`hermes_cli/plugins.py`（`_dispatch_pre_tool_call_hooks` 本体，本 spike 直接调用的函数）：

```python
def _get_pre_tool_call_directive_details(tool_name, args, task_id="", session_id="", tool_call_id="",
                                         turn_id="", api_request_id="", middleware_trace=None):
    """block（否决；message 变成 tool result）或 approve（把任意工具升级到人工审批闸，
    走 tools.approval 里跟"危险命令"共用的同一套人工裁决机制）。"""
    hook_results = invoke_lifecycle_hook("pre_tool_call", tool_name=tool_name, args=args or {}, ...)
    for result in hook_results:
        ...
        return _PreToolCallDirective(action=action, message=message, rule_key=rule_key, modified_args=modified_args)
    return _PreToolCallDirective(modified_args=modified_args)


def _resolve_block_from_details(details, tool_name, *, turn_id="", tool_call_id="", session_id=""):
    """唯一的 fail-closed 收口：block 直接拒；approve 交给 tools.approval.request_tool_approval()
    （这一步的人工等待不计入 pre_tool_call 自身的 hook_callback_timeout）；网关/裁决出错也拒，不静默放行。"""
    if details.action == "block":
        return details.message
    if details.action != "approve":
        return None
    ...
    result = request_tool_approval(tool_name, details.message or "", rule_key=details.rule_key or tool_name)
    if not result.get("approved"):
        return str(result.get("message") or f"BLOCKED: plugin approval required for {tool_name}")
    return None


def _dispatch_pre_tool_call_hooks(tool_name, args, **hook_kwargs):
    details = _get_pre_tool_call_directive_details(tool_name, args, **hook_kwargs)
    block_msg = _resolve_block_from_details(details, tool_name, ...)
    return (block_msg, details.modified_args)
```

`plugins.yaml` 的 hook 是 opt-in 白名单（`config.yaml` 的 `plugins.enabled`），未列入的插件即便被发现也不会加载
——这本身就是 Hermes 自带的"第三方能力需显式启用"闸门，跟 PRD N15 的精神一致。

### demo：真实跑 hermes-agent 代码，阻塞 + 拒绝 + agent 收到拒绝结果

`docs/spikes/hermes_hook_demo.py`（本仓库，可独立重跑，见文件头的复现步骤）驱动的是**真实的**
`hermes_cli.plugins` 插件发现/加载/分发机制（真实 `plugin.yaml` + `__init__.py` 落盘、真实
`PluginManager.discover_and_load()`），唯一被模拟的是"daemon 那一端"——`jones_demo` 插件里用一个
后台线程 + `queue.Queue` 扮演"daemon 想了一会儿才给裁决"，真实系统里这段换成 worker 到 daemon 的
ACP `session/request_permission` 往返，阻塞的*形状*完全一样。

运行输出（原文，2026-09-18，退出码 0）：

```
======================================================================
1. Plugin discovery (real PluginManager, real plugin.yaml on disk)
======================================================================
has_hook('pre_tool_call'): True

======================================================================
2. Read-only tool -> approved instantly, no daemon round trip
======================================================================
block_message=None modified_args=None elapsed=0.002s

======================================================================
3. Local rule-gate deny (write under /etc) -> blocked with ZERO daemon wait
======================================================================
block_message='JONES RULE GATE: writes under /etc/ are never allowed.' elapsed=0.000s

======================================================================
4. Dangerous tool call, daemon ASKED and DENIES after a delay -> agent gets the rejection
======================================================================
block_message='JONES USER GATE: denied (user gate: operator clicked Deny); waited 1.28s for daemon.' elapsed=1.285s
tool result the agent would receive: {"error": "JONES USER GATE: denied (user gate: operator clicked Deny); waited 1.28s for daemon."}

======================================================================
5. Same dangerous tool, daemon ASKED and APPROVES after a delay -> proceeds (None)
======================================================================
block_message=None elapsed=0.630s

======================================================================
6. Timeout path: daemon never answers -> fail CLOSED, not open
======================================================================
(see the plugin's `except queue.Empty` branch above: block, fail-closed by construction; not exercised here at full 10s to keep the demo fast)

======================================================================
ALL ASSERTIONS PASSED
======================================================================
RC=0
```

（跑的时候 stderr 还会打几条 `Built-in observability hook failed / No module named
'hermes_cli.observability'` —— 这是本 spike 为控制拉取范围，没有拉取 Hermes 内部一个无关的遥测 hook
模块，Hermes 自己 `try/except` 吞掉了，不影响任何一条断言，不是本 spike 引入的问题。）

第 4 步是关键断言：`_dispatch_pre_tool_call_hooks()` 调用处，主线程实测被真实阻塞了 1.285 秒（对应
demo 里模拟的 daemon 决策延迟 1.2s），证明这不是一个 fire-and-forget 通知，而是真同步等待；返回后
`block_message` 非空，且 `json.dumps({"error": block_message})`——与 `invoke_tool()` 真实源码构造
tool result 的方式完全一致——就是 agent 会看到的工具结果。第 5 步换成"daemon 批准"，同样真实阻塞
0.63s 后放行（`block_message is None`），证明同一条路径两个分支都真实可达，不是只测了一边。

### ACP 侧证据（`acp_adapter/`，未做端到端 stdio 往返 demo，源码读 + 关键片段核对）

以下基于对 `acp_adapter/server.py`、`session.py`、`events.py`、`permissions.py`、`tools.py` 的源码核对
（同一 commit），未跑端到端 ACP stdio 会话（见"未做的事"）：

- **工具调用事件**：`acp_adapter/events.py::make_step_cb()` 在每个工具调用结束时发 `session/update`
  的 `ToolCallComplete`（`build_tool_complete`，携带工具名、参数、结果）；`tools.py::build_tool_start()`
  在派发前发 `ToolCallStart`。历史回放（`load_session`/`resume_session`）走的是同一套事件构造函数
  （`_history_replay_updates`），保证实时流和回放流形状一致。
- **流式文本**：`make_thinking_cb()` / `make_message_cb()` 包一层 `_make_text_cb()`，把 AIAgent 的
  `thinking_callback`/`stream_delta_callback` 转成 ACP 的 `update_agent_thought_text`/
  `update_agent_message_text` 增量通知，`None` 是"这条消息结束"的哨兵（下一条 delta 开新 messageId）。
- **`session/request_permission`**：`acp_adapter/permissions.py::make_approval_callback()` 把 ACP 的
  `request_permission` 协程包成 Hermes 的 `approval_callback(command, description, ...) -> str` 签名，
  `server.py::_wire_turn_callbacks()` 里 `cbs.approval_cb = make_approval_callback(conn.request_permission, ...)`
  接到 AIAgent 的危险命令检测上——**只接在这一处**，没有接到 `pre_tool_call` 的 `approve` 分支（那条分支走的是
  `tools.approval.request_tool_approval`，同一份人工裁决核心逻辑，但触发来源不同）。这是"B 不能替代 A"这条
  结论的直接依据。
- **取消**：`HermesACPAgent.cancel(session_id)` 真的 `state.cancel_event.set()`，不是空实现。
- **会话生命周期**：`new_session`/`load_session`/`resume_session`/`fork_session`/`list_sessions` 齐全，
  `fork_session` 直接对应 PRD 9.6 的子会话派生需求。

## 风险

- **`hook_callback_timeout` 默认 30s，是 Hermes 自己的 config 项，不是 Jones 能绕开的硬编码**：
  Jones 的 `pre_tool_call` 插件如果图省事把整段人工等待都塞进 hook 回调本身，一旦真人 30 秒内没点审批，
  Hermes 侧会自动 fail-closed 拒绝——这个拒绝对用户不可见、不会显式提示"超时"，只会让 agent 收到一个
  BLOCKED 的工具结果。必须用"决定阶段快速返回 + 人工等待走 ACP `request_permission`"的两段式，已写进
  00-foundation.md §7；daemon 侧还应该把这个 30s 读出来做健康检查（值被人改小会静默削弱权限闸的可用性,
  不是安全性——闸依然会拒，只是拒绝理由从"用户否决"变成"回调超时"，日志埋点要能区分这两种，否则误导用户
  以为自己被拒绝了但其实是配置项出问题）。
- **未验证并发工具调用下的隔离**：Hermes 支持并发工具调用（`_MAX_TOOL_WORKERS = 8`，见
  `run_agent.py`），`pre_tool_call` 会在多个线程上同时触发；demo 里用 `tool_call_id` 分流决策队列，
  真实插件要保证多路并发裁决不串号——设计上没问题（`tool_call_id` 是 Hermes 传入的），但没有并发场景的
  实测，留给 daemon 骨架落地时的集成测试覆盖。
- **`pre_tool_call` 插件本身是单点**：如果 Jones 的插件抛异常，看 `_pre_tool_block_message()` 源码是
  `except Exception: return None, function_args`——**静默放行**，不是 fail-closed。这跟 DEV.md「诚实失败」
  原则冲突，也跟 PRD N09「错误被静默吞掉」是负面清单项直接冲突。**这是本 spike 发现的、需要 daemon 骨架
  实现时特别处理的点**：Jones 的插件代码本身要有自己的 try/except，把内部错误转成一次显式的
  `{"action":"block","message":"权限闸内部错误：<detail>"}`，绝不能让异常穿透到 Hermes 的兜底静默放行。
  建议写进 daemon 骨架 Issue 的验收标准。
- **`gateway/` 命名冲突**：Hermes 自己的 `gateway/` 是 IM 机器人网关（Telegram/Discord/Slack…），PRD
  6.1 里"Channel Gateway"说的是同一类东西但不是同一份代码；后续如果决定接 Hermes 的 IM 网关实现 FR21，
  文档/代码里要避免用同一个词指两个不同的东西。
- **网络环境限制**：本次验证在带宽受限（~25 KB/s 对 github.com）的环境下完成，用了 treeless partial
  clone + 按需拉取，只覆盖了回答问题所需的 ~90 个源文件，不是整仓库；`agent.agent_runtime_helpers`
  完整 import（含 openai/anthropic SDK 等第三方依赖）未验证，`invoke_tool()` 里 `_pre_tool_block_message`
  之后的路径（真实工具派发、并发执行）依赖源码阅读，不是本 spike 的实测范围。

## 下一步

1. daemon 骨架 Issue：worker 启动流程里加载 Jones 自研 `pre_tool_call` 插件（含上面"静默放行"风险的
   fail-closed 包装），worker↔daemon 走 ACP stdio。
2. 权限闸 Issue（W3，FR05）：实现两段式设计——规则闸本地同步、审查闸/用户闸走 ACP
   `session/request_permission`；把 `permission_decisions` 表的写入点接到 `pre_tool_call` 回调里。
3. 建议后续单独起一个小 spike（不阻塞 W1）验证 `hermes_state_*.py` 作为 worker 内会话存储、
   `cron/` 复用到什么程度，`tui_gateway/`（Hermes 自带的 TUI/Desktop JSON-RPC 后端，本 spike 顺带
   发现的第三条候选协议，未评估）值不值得看一眼。

## 验证方式

```bash
git clone --filter=blob:none --depth 1 https://github.com/NousResearch/hermes-agent
cd hermes-agent && git checkout 0138269
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python pyyaml   # 最小依赖；完整依赖见 pyproject.toml
cd ..
HERMES_HOME=$(mktemp -d) PYTHONPATH=hermes-agent hermes-agent/.venv/bin/python \
    docs/spikes/hermes_hook_demo.py
# 期望：6 个 section 全部打印，退出码 0
```
