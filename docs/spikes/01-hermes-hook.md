# Spike #1 · Hermes 内核 hook 点验证（Issue #1）

对应 PRD 13.2 风险 1、6.2/6.3；00-foundation.md §7。

验证对象：[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)。

**统一后的 CANONICAL commit（第二轮评审修复，2026-09-19）：`ee4452991d17534aa561f31ee55596d082aa94e7`**
（本机已有完整 checkout，`/Users/nativeas/.hermes/hermes-agent`）。本文件下方"实测证据""demo""ACP
侧证据"三节、`docs/spikes/hermes_hook_demo.py` 头部、本文件末尾"验证方式"，现在**全部**指向这一个
commit——不再有第二个数字。历史沿革（不影响上面这条统一结论，只解释为什么之前不一致，供后来者追溯）：
原始 spike（2026-09-18）用的是 commit `0138269`（`main`，treeless partial clone + 按需拉取，受限于
~25 KB/s 带宽，见下"实测证据·环境"）；**同一天**的第一轮评审修复改用本机已有的完整 checkout 复核结论，
但只更新了正文的部分引用，"验证方式"一节的 clone 步骤仍留着 `0138269`，造成文档内部两个 commit 并存
（第二轮评审第 3 条指出）。第二轮修复把全部引用收敛到 `ee4452991d17534aa561f31ee55596d082aa94e7`；
浅克隆看不到它与 `0138269` 的祖先关系，未做网络核实，但涉及的源码路径（`hermes_cli/plugins.py`、
`plugins_dispatch.py`、`tools/approval.py`、`tools/approval_context.py`、`tools/terminal_tool.py`、
`acp_adapter/*`）逻辑一致，是同一上游项目相隔不远的两个快照，不是两套设计——"验证方式"一节仍保留
`0138269` 作为"网络不受限环境下按标准流程 clone"的备选路径说明，但用清晰的文字标注它是历史路径，不是
本文档当前验证用的 commit。

## 结论（先说结论）

1. **A（`from run_agent import AIAgent` + 工具调用前 hook）可行，且是唯一能覆盖任意工具调用的拦截点。**
   Hermes 有一个真实存在、同步阻塞、在任何工具执行前触发的插件 hook：`pre_tool_call`
   （`hermes_cli.plugins`，挂在 `agent/agent_runtime_helpers.py::invoke_tool()` 里）。
   回调可以真的阻塞调用线程等外部信号，返回 `block` 时工具从不执行、拒绝理由原样成为该次工具调用的结果传回给
   agent——**已驱动真实 `invoke_tool()`（不是手写等价 JSON）实测，只用一个 ~5 行的 stub agent对象，
   不需要完整 `AIAgent` 或任何 provider SDK**（见下文"实测证据"）。

2. **B（`acp_adapter/`，Agent Client Protocol，stdio）也可行，而且比自己发明一个 worker 协议成熟得多，
   且已经把 A 的人工审批分支接通了。**
   `hermes acp` 起一个标准 ACP JSON-RPC stdio server：真的有会话生命周期
   （`new_session`/`load_session`/`resume_session`/`fork_session`）、真的流式文本/思考增量、真的工具调用
   start/update/complete 事件、真的 `cancel()`（设置一个 agent loop 会检查的 `cancel_event`）、真的
   `session/request_permission`。**这条 `request_permission` 不止接在 Hermes 自己"危险 shell 命令"的
   启发式侦测上——`acp_adapter/server.py::_run_agent_turn()` 把 `tools/terminal_tool.py` 的按线程审批回调槽
   绑定到它，而 `pre_tool_call` 的 `approve` 分支（`tools/approval.py::request_tool_approval()`）正是从
   同一个槽里取审批回调的。** 也就是说：worker 跑在 ACP 之上时，Jones 插件返回一次 `approve`，这次审批
   就自动变成一次真实的 ACP `session/request_permission` 往返——不需要 Jones 自己再讲一遍 ACP（详见
   "ACP 侧证据"）。它还有第二个独立的 `request_permission` 接入点管 `write_file`/`patch` 的编辑审批
   （`acp_adapter/edit_approval.py`），见"风险"一节。

3. **推荐：A + B 组合，不是二选一，且 A 的"人工审批"分支本身就是 B。**
   - worker 进程跑 ACP server（daemon 是 ACP client），直接复用 `acp_adapter/` 的会话生命周期、流式事件、cancel，
     不重造 stdio 协议——00-foundation.md §3 的"worker stdio JSON-RPC"直接就是 ACP，不用另起一套。
   - worker 启动时额外加载一个 **Jones 自研的 `pre_tool_call` 插件**，做 FR05 要求的 Step 级权限拦截：
     规则闸能本地同步决出的（硬性禁止、`permissions.json` 命中）在插件里直接返回 `{"action":"block",...}`，
     零等待，不打一次 daemon IPC；审查闸/用户闸需要人工裁决的，插件**立即**返回
     `{"action":"approve","message","rule_key"}`——不在回调里等待，不自己发起 ACP 往返。真正的人工等待由
     Hermes 自己的 `request_tool_approval()` 完成，经既有的 `terminal_tool` 审批回调槽自动落到 ACP 的
     `session/request_permission` 上（结论 2）。
   - **关键坑，写进了 00-foundation.md，本轮已实测**：`pre_tool_call` 回调本身受 `plugins.hook_callback_timeout`
     限制（config 项，默认 30s，超时直接 fail-closed 拒绝）。这 30s 只卡"这次回调本身要跑多久"；插件立即
     返回 `approve` 时几乎不占用这个窗口，随后 `request_tool_approval()` 在调用者线程上做的真人等待发生在
     hook 派发**已经返回之后**，因此不计入这 30s——**已实测**：把 `hook_callback_timeout` 压到 0.5s、模拟
     人工审批耗时 1.5s（3 倍于超时），往返仍完整跑完、拒绝理由原样传回 agent（demo 第 4 节）。
     **反过来，如果插件自己在回调里阻塞等待**（自建 ACP 往返、`queue.get()` 等——这正是本 spike 早期草稿
     的写法，已改掉），这段阻塞就计入这 30s；超时后不仅这一次调用被 fail-closed，**同一个已注册回调**
     在后续 60s 抑制窗口内的**每一次** `pre_tool_call`（含完全无关的工具调用）都会被跳过判定为 block——
     不是单次失败（demo 第 6/7 节已实测这个连锁效应，见"风险"一节）。

4. **不满足→运行时层代理方案（PRD 13.2 的 Plan B）不需要**：spike 结果是 A 可行，不需要在 daemon
   层再包一层"工具调用先过守护进程"的运行时代理——`pre_tool_call` 本身就是这层代理，而且是 Hermes 原生支持的，
   不用我们自己拦 stdio 流去猜哪一行是工具调用。

5. **结论 3 的"approve 交给 Hermes 的 `request_tool_approval()` 走 ACP"有一个前提，本轮才发现且必须写清楚：
   `request_tool_approval()` 不是无条件问人的——它自己前面有三道能让它零等待直接放行、`session/request_permission`
   根本不会被发出的短路**（源码见下"Hermes 内置的批准绕过路径"一节）：进程级 `HERMES_YOLO_MODE` 环境变量、
   `approvals.mode: off` 配置、以及命中会话级/**持久化落盘**的 `command_allowlist` 的调用。Jones 把用户闸"委托"
   给 Hermes 的 `request_permission`，前提是这三条短路对 worker 进程全部不生效；worker 启动 Hermes 时必须显式
   配置成"不生效"，这不是默认状态，是需要 Jones 主动做的事——写进了 FR05 的验收项建议（见下）。

6. **`permission_decisions`/`steps` 表的写入点不在 `pre_tool_call` 回调里，且规则闸/审查闸两条决策路径的
   写入时机不同**：`pre_tool_call` 回调本身对 approve 分支只是"路过"（立即返回，从不知道人工最终批准还是拒绝，
   见结论 3）；真正知道每次工具调用最终结果（block/成功/超时）的，是同一个 worker 进程里**另一个**插件
   hook——`post_tool_call`（`hermes_cli.plugins` 同样支持的 hook 类型，`model_tools._emit_post_tool_call_hook`
   经 `agent/inline_tool_executors.py::emit_terminal_post_tool_call` 在 `invoke_tool()` 内、无论走 block
   分支还是走完真实工具执行都会触发，晚于 `pre_tool_call`，携带 `status`/`error_type`/`error_message`/
   `duration_ms`）；而真正**做**用户闸/审查闸决定的是 daemon 自己（它是 ACP client，`session/request_permission`
   是 worker 向它发的 RPC，daemon 算出 allow/deny 的那一刻就是 daemon 自己知道决定内容的那一刻）。这两条路径
   在哪个进程、什么时刻拥有"决定"这件事的完整信息不一样，因此不可能有一个单一的"写入点"，只能有一张按来源
   分叉的时序图——完整时序见下"审计写入时序"一节，取代了原"下一步"里"写入点接到 pre_tool_call 回调里"这句
   过时的表述。

## 实测证据

### 环境

- 网络对 `github.com`/`raw.githubusercontent.com` 有带宽限制（~25 KB/s），整仓库 clone（`git clone` 报告
  size ≈ 977 MB）在这个环境下不可行；改用 `git clone --filter=blob:none --depth 1`（treeless partial
  clone）+ 按需 `git show HEAD:<path>` 拉取单个源文件（PyPI/raw 单文件下载不受此限，每个文件 1–2s）。
  按 `hermes_cli.plugins` → `hermes_cli.lifecycle` → `agent.agent_runtime_helpers`（仅到
  `invoke_tool`/`_pre_tool_block_message` 需要的部分）这条真实 import 链，迭代拉取到约 90 个源文件，
  未拉取整仓库（`agent/`、`hermes_cli/`、`tools/` 各有 300–500 个文件，多数是 provider/auth/terminal 相关，
  与本次验证的问题无关）。
- **原始 spike 的判断有误，本轮已纠正**：原始报告认为"跑通 `agent.agent_runtime_helpers` 的完整 import
  还要再深入约 60+ 个文件外加 openai/anthropic 等第三方 SDK"，因此没有驱动真实 `invoke_tool()`，只是手写了
  一段等价的 JSON 打印来代表"agent 会收到什么"。这个理由不成立——在有完整 checkout 的机器上实测，
  `import agent.agent_runtime_helpers`（系统 `python3`，不装任何依赖）一次通过，不需要任何 provider SDK；
  `invoke_tool()` 本身只在真正派发到 registry/inline 工具之前需要 provider 相关的东西，而这正是
  block 路径永远不会走到的分支。本轮用一个 ~5 行的 stub agent（只带 `session_id`/`_current_turn_id`/
  `_current_api_request_id` 三个属性）驱动了真实的 `agent.agent_runtime_helpers.invoke_tool()`，见
  demo 第 3/4/6 节。
- `uv venv --python 3.12`，只装了 `pyyaml`（`hermes_cli.plugins` 这条 import 链除了标准库只需要它）。
  **没有配置任何模型 provider API key，也没有发起任何模型调用**——`pre_tool_call` 在工具真正执行前、
  在任何 API 请求发生前同步触发，验证它不需要一个完整 Turn。

### 关键源码摘录（hermes-agent，含中文注解，非逐字——上一版本声明"未改写"但实际做了改写，已更正标题并恢复省略号标注）

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

`hermes_cli/plugins.py`（`_dispatch_pre_tool_call_hooks` 本体，本 spike 直接调用的函数；**逐字英文
docstring，本轮对照 `ee4452991d` 恢复，并补回了上一版本删掉的 `_thread_tool_whitelist` 分支**）：

```python
def _get_pre_tool_call_directive_details(
    tool_name: str, args: Optional[Dict[str, Any]], task_id: str = "", session_id: str = "",
    tool_call_id: str = "", turn_id: str = "", api_request_id: str = "",
    middleware_trace: Optional[List[Dict[str, Any]]] = None,
) -> _PreToolCallDirective:
    """Check ``pre_tool_call`` hooks for ``{"action": "block", "message"}`` (veto; message becomes
    the tool result) or ``{"action": "approve", "message", "rule_key"?}`` (escalate ANY tool to the
    human-approval gate; ``rule_key`` picks the ``[a]lways`` allowlist grain). First valid directive
    wins; irrelevant returns are ignored."""
    allowed = getattr(_thread_tool_whitelist, "allowed", None)
    if allowed is not None and tool_name not in allowed:
        fmt = getattr(_thread_tool_whitelist, "fmt", "Tool '{tool_name}' denied")
        return _PreToolCallDirective(action="block", message=fmt.format(tool_name=tool_name))
    from hermes_cli.lifecycle import invoke_hook as invoke_lifecycle_hook
    hook_results = invoke_lifecycle_hook("pre_tool_call", tool_name=tool_name, args=args or {}, ...)
    for result in hook_results:
        ...
        return _PreToolCallDirective(action=action, message=message, rule_key=rule_key, modified_args=modified_args)
    return _PreToolCallDirective(modified_args=modified_args)


def _resolve_block_from_details(details, tool_name, *, turn_id="", tool_call_id="", session_id=""):
    """The ONE place for the fail-closed approval logic: ``block`` blocks with its message; an
    ``approve`` whose gate errors, denies, or times out is blocked; anything else proceeds."""
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

（上一版本这里多了一句括号"（这一步的人工等待不计入 pre_tool_call 自身的 hook_callback_timeout）"混进了
声称逐字引用的代码块——`ee4452991d` 的真实 docstring 没有这句话。这个结论本身是对的（见结论 3），但
出处是 `hermes_cli/plugins_dispatch.py::invoke_hook`/`_run_hook_callback_bounded` 的调度逻辑，不是
这段 docstring 的原文，已挪到下面"ACP 侧证据"和"风险"里，并给出了对应的源码行号。）

`hermes_cli/plugins_dispatch.py`（hook 超时/抑制机制本体，`_dispatch_pre_tool_call_hooks` 走的
`invoke_lifecycle_hook` 最终落到这里；本轮新增引用，上一版本没有摘录过这个文件）：

```python
_HOOK_TIMEOUT_FAIL_CLOSED_HOOKS: Set[str] = {"pre_tool_call"}
_HOOK_TIMEOUT_SUPPRESSION_SECONDS = 60.0  # After a timeout, suppress the same callback this long.

def invoke_hook(self, hook_name, **kwargs):
    ...
    for cb in self._hooks.get(hook_name, []):
        if use_timeout:
            ret = self._run_hook_callback_bounded(hook_name, cb, kwargs, timeout)
            if ret is _HOOK_SKIPPED:
                if fail_closed:  # pre_tool_call: fail closed with a block directive
                    results.append({"action": "block", "message": _PRE_TOOL_CALL_TIMEOUT_BLOCK_MESSAGE})
                continue
        ...

def _run_hook_callback_bounded(self, hook_name, cb, kwargs, timeout):
    callback_key = (hook_name, id(cb))  # keyed on the CALLBACK, not the individual call
    with self._hook_timeout_lock:
        suppressed_until = self._hook_timeout_suppressed_until.get(callback_key)
        running = callback_key in self._hook_running_callbacks
        if (suppressed_until is not None and suppressed_until > time.monotonic()) or running:
            return _HOOK_SKIPPED  # every later call for this SAME callback, any tool, is skipped
        ...
        self._hook_running_callbacks[callback_key] = token
    ...
    thread = threading.Thread(target=_runner, ...)  # daemon=True; never joined if it times out
    thread.start()
    if not done.wait(timeout=timeout):
        self._hook_timeout_suppressed_until[callback_key] = time.monotonic() + self._hook_timeout_suppression_seconds
        return _HOOK_SKIPPED
    ...
```

`plugins.yaml` 的 hook 是 opt-in 白名单（`config.yaml` 的 `plugins.enabled`），未列入的插件即便被发现也不会加载
——这本身就是 Hermes 自带的"第三方能力需显式启用"闸门，跟 PRD N15 的精神一致。

### demo：真实跑 hermes-agent 代码——block 路径经真实 `invoke_tool()`；approve 路径经真实 `request_tool_approval()`

`docs/spikes/hermes_hook_demo.py`（本仓库，可独立重跑，见文件头的复现步骤）驱动的是**真实的**
`hermes_cli.plugins` 插件发现/加载/分发机制（真实 `plugin.yaml` + `__init__.py` 落盘、真实
`PluginManager.discover_and_load()`），本轮重写后额外驱动真实的
`agent.agent_runtime_helpers.invoke_tool()`、`tools.approval.request_tool_approval()`、
`tools.terminal_tool.set_approval_callback()`/`_get_approval_callback()`、
`tools.approval_context.set_hermes_interactive_context()`。唯一被模拟的是 ACP 传输本身——一个
`fake_request_permission()` 函数站在 `acp_adapter/permissions.py::make_approval_callback()` 包起来的
`conn.request_permission` 的位置上，真正起一个 ACP stdio 连接超出本 spike 范围（见"未做的事"）；它前后
的一切（`terminal_tool` 的按线程回调槽、`request_tool_approval()` 的等待/拒绝/放行逻辑、`invoke_tool()`
把拒绝理由包回工具结果）都是未改写的 hermes-agent 代码。

运行输出（原文，2026-09-18，对 `ee4452991d`，退出码 0）：

```
======================================================================
1. Plugin discovery (real PluginManager, real plugin.yaml on disk)
======================================================================
has_hook('pre_tool_call'): True

======================================================================
2. Read-only tool -> approved instantly, no daemon round trip
======================================================================
block_message=None modified_args=None elapsed=0.005s

======================================================================
3. Local rule-gate deny (write under /etc) -> real invoke_tool(), agent gets the rejection
======================================================================
elapsed=0.183s
tool result the agent ACTUALLY received (from real invoke_tool(), not hand-built): {"error": "JONES RULE GATE: writes under /etc/ are never allowed."}

======================================================================
4. Plugin returns approve; Hermes's OWN request_tool_approval() waits, then DENIES
======================================================================
hook_callback_timeout is 0.5 s; the simulated human below takes 3x that to answer.
  [fake ACP session/request_permission] 'operator must confirm this'
elapsed=1.652s
tool result the agent ACTUALLY received: {"error": "BLOCKED: User denied this potentially dangerous action (matched 'operator must confirm this'). Do NOT retry — the user has explicitly rejected it."}
PASS: a human wait (1.50s) longer than hook_callback_timeout (0.5s) completed anyway -- confirmed NOT counted against it, because the plugin returned `approve` instead of waiting in-callback.

======================================================================
5. Same shape, daemon APPROVES after a delay -> proceeds (block_message None)
======================================================================
block_message=None elapsed=0.893s

======================================================================
6. ANTI-PATTERN: blocking pre_tool_call itself (not returning approve) -> fails closed AT THE TIMEOUT, not at the full delay
======================================================================
hangy_tool sleeps 2.00s INSIDE the callback; hook_callback_timeout is 0.5s.
elapsed=0.644s tool result the agent received: {"error": "pre_tool_call plugin callback timed out or is still running"}
PASS: the anti-pattern loses the tool call's own deliberation AND fails closed after only 0.5s, not the 2.00s the (simulated) daemon actually needed.

======================================================================
7. BLAST RADIUS: that ONE timeout now fails EVERY later pre_tool_call for this callback, including unrelated tools, until the suppression window elapses
======================================================================
(suppression window shrunk to 1.00s for this demo; production default is 60s)
immediately-after safe_read_file result (should ALSO be blocked, even though it is harmless and has nothing to do with hangy_tool): {"error": "pre_tool_call plugin callback timed out or is still running"}
waiting 1.56s for both the abandoned worker thread and the (shrunk) suppression window to clear...
after the window elapses: block_message=None
PASS: one timed-out tool call silently degraded EVERY later pre_tool_call for the same plugin callback for the whole suppression window -- not a single-call failure.

======================================================================
ALL ASSERTIONS PASSED
======================================================================
RC=0
```

（跑的时候 stderr 还会打几条 `Built-in observability hook failed / No module named
'hermes_cli.observability'` —— 这是本 spike 为控制拉取范围，没有拉取 Hermes 内部一个无关的遥测 hook
模块，Hermes 自己 `try/except` 吞掉了，不影响任何一条断言，不是本 spike 引入的问题。用完整 checkout
跑时改成一条不同的、同样无关的插件加载警告，见 demo 输出前的 stderr 行。）

第 3 步是"验收项 2"的直接证据：调的是**真实** `invoke_tool()`，不是手写一遍等价 JSON——返回值就是
agent 会拿到的字面工具结果。第 4 步是本轮修复的核心：把 `hook_callback_timeout` 压到 0.5s、模拟人工
审批耗时 1.5s（3 倍于超时），往返仍然完整跑完、真实阻塞了 1.652s，证明"approve 之后的人工等待不计入
hook_callback_timeout"不是读代码猜的，是量出来的。第 6/7 步反向验证："自己在回调里等"这个反面写法
会在超时点被切断（0.644s 而不是 2.00s），而且会把同一个回调注册对象在抑制窗口内的所有后续调用（含完全
无关的 `safe_read_file`）一起拖下水，直到孤儿线程跑完 + 抑制窗口过期才恢复——这正是 PRD/foundation
现在写进"关键坑"里的那句话的实测依据。

### ACP 侧证据（`acp_adapter/`，源码读 + 关键片段核对；`approve` 分支的接线链条本轮已用完整 checkout 逐跳核实）

基于对 `acp_adapter/server.py`、`session.py`、`events.py`、`permissions.py`、`edit_approval.py`、
`tools.py`、`tools/approval.py`、`tools/approval_context.py`、`tools/terminal_tool.py` 的源码核对：

- **工具调用事件**：`acp_adapter/events.py::make_step_cb()` 在每个工具调用结束时发 `session/update`
  的 `ToolCallComplete`（`build_tool_complete`，携带工具名、参数、结果）；`tools.py::build_tool_start()`
  在派发前发 `ToolCallStart`。历史回放（`load_session`/`resume_session`）走的是同一套事件构造函数
  （`_history_replay_updates`），保证实时流和回放流形状一致。
- **流式文本**：`make_thinking_cb()` / `make_message_cb()` 包一层 `_make_text_cb()`，把 AIAgent 的
  `thinking_callback`/`stream_delta_callback` 转成 ACP 的 `update_agent_thought_text`/
  `update_agent_message_text` 增量通知，`None` 是"这条消息结束"的哨兵（下一条 delta 开新 messageId）。
- **`session/request_permission` 接线到 `pre_tool_call` 的 `approve` 分支——上一版本这里的结论是反的，
  本轮改正**：调用链是通的，逐跳如下——
  1. `hermes_cli/plugins.py::_resolve_block_from_details()` 对 `approve` 调
     `tools.approval.request_tool_approval(tool_name, message, rule_key=...)`，**不传**
     `approval_callback`。
  2. `tools/approval.py::request_tool_approval()` → `_run_approval_gate()` → `_presence(None)`。
  3. `tools/approval.py::_presence()` → `tools/approval_context.py::_resolve_cli_approval_callback(None)`
     → 兜底走 `tools.terminal_tool._get_approval_callback()`（一个按**线程**存放的槽，
     `tools/terminal_tool.py::set_approval_callback`/`_get_approval_callback`）。
  4. `acp_adapter/server.py::_run_agent_turn()`（第 746-747 行附近，注释原文点明"Approval routing is
     thread-local, so it MUST be bound here"）在跑 turn 的那个线程上执行
     `terminal_tool.set_approval_callback(approval_cb)`，并 `set_hermes_interactive_context(True)`
     （否则 `_presence()` 判定为"无交互上下文"，直接 fail-closed，本轮实测踩过这个坑，见"修复记录"）。
  5. `approval_cb` 就是 `server.py::_wire_turn_callbacks()` 里的
     `make_approval_callback(conn.request_permission, loop, session_id)`（`acp_adapter/permissions.py`）。
  6. 并发工具调用不破坏这条链：`agent/tool_executor.py` 用 `propagate_context_to_thread` 把 thread-local
     的审批回调带进 8 线程池的 worker（docstring 明写）——**但这条"传进 worker 线程后槽会不会丢"的路径
     本 spike 没有实测**，只核对了源码，见"风险"一节。
  - 即：插件返回 `{"action":"approve", ...}` 时，只要 worker 是以 ACP agent 身份在跑，Hermes **已经**会把
    这次人工裁决发成 ACP `session/request_permission`，Jones 的插件不需要自己再拿到一个 ACP 连接句柄。
- **第二个 `request_permission` 接入点——编辑审批，上一版本完全没提到**：`acp_adapter/edit_approval.py`
  的 `maybe_require_edit_approval()` / `make_acp_edit_approval_requester()`，由 `server.py`（约
  863-866 行）绑定、`acp_adapter/events.py`（约 97/107 行）对 `write_file`/`patch`/`skill_manage`
  触发，走的是**同一个** `conn.request_permission`，但策略独立（ask/workspace_session/session），
  另有 `SENSITIVE_AUTO_APPROVE_NAMES`（`.env`、`.env.local`、`id_rsa`、`id_ed25519` 等）自动放行名单。
  对 Jones 的影响：daemon 作为 ACP client 时，每次 `write_file`/`patch` 会**同时**撞上这一路和 Jones
  自己 `pre_tool_call` 闸的 `write_file`/`patch` 拦截——要么用户被问两遍，要么两套审批策略打架。本 spike
  不定案，留给 W3 权限闸 Issue（见"下一步"）。
- **取消**：`HermesACPAgent.cancel(session_id)` 真的 `state.cancel_event.set()`，不是空实现。
- **会话生命周期**：`new_session`/`load_session`/`resume_session`/`fork_session`/`list_sessions` 齐全，
  `fork_session` 直接对应 PRD 9.6 的子会话派生需求。

## Hermes 内置的批准绕过路径（YOLO / allowlist）——本轮评审新增，源码核对于 `ee4452991d`

结论 5 的展开。Jones 把"审查闸/用户闸"设计成插件返回 `approve`、交给 Hermes 自己的
`tools/approval.py::request_tool_approval()` 去问人——但 `request_tool_approval()` 内部先过
`_run_approval_gate()`，这个函数**前两步就是短路**（`tools/approval.py:888,891`）：

```python
def _run_approval_gate(...):
    if _yolo_active():          # 短路 1：见下
        return _approved()
    session_key = get_current_session_key()
    if is_approved(session_key, pattern_key):   # 短路 2：见下
        return _approved()
    ...  # 只有过了这两关，才会真的调 _human_decision() → ACP session/request_permission
```

`request_tool_approval()` 自己（`tools/approval.py:1001-1022`）把 Jones 插件的 `rule_key` 原样映射成
`pattern_key=f"plugin_rule:{rule_key}"`——这条短路对 Jones 用插件转发过去的审批请求和 Hermes 自己的
"危险 shell 命令"审批走的是**同一套**短路逻辑，不是分开的两套。三条具体短路路径：

1. **进程级 YOLO（`HERMES_YOLO_MODE` 环境变量）**：`tools/approval.py:45`
   ```python
   # Frozen at import: reading os.environ per call would let any skill running in the process set
   # this and bypass every approval check (prompt-injection escalation path).
   _YOLO_MODE_FROZEN: bool = is_truthy_value(os.getenv("HERMES_YOLO_MODE", ""))
   ```
   **在模块被 import 的那一刻**读一次、冻结，之后这个进程里永远读的是这个冻结值。**要求**：daemon 拉起
   `python -m acp_adapter.entry`（worker）子进程时，传给它的环境变量里**不能有** `HERMES_YOLO_MODE`
   （不能从 daemon 自己的环境原样继承，必须显式构造子进程 env、排除这个键）。

2. **`approvals.mode: off` 配置**：`tools/approval.py:1080`/`1158`
   ```python
   approval_mode = approval_context._get_approval_mode()
   if _yolo_active() or approval_mode == "off":
       return _approved()
   ```
   `_get_approval_mode()`（`tools/approval_context.py:228-236`）读的是活动 profile 的 `config.yaml` 里
   `approvals.mode`（未设置时默认 `"manual"`，不是 off，本身安全，但**只要 worker 的 `config.yaml` 被写成
   `off`（或任何代码/后续维护者手滑写成 off）这一路径就整体失效**）。**要求**：daemon 为 worker 生成的
   `config.yaml` 不写 `approvals.mode: off`（不写这个键，走默认 `manual` 即可；不要因为"Jones 自己已经有
   规则闸/审查闸了，Hermes 这层用不上"就顺手把它设成 off——一旦设 off，连 Jones 转发过去的 `approve` 请求
   也会被短路，等于用户闸形同虚设）。

3. **持久化的 `command_allowlist`（跨会话、写在磁盘上）**：`tools/approval.py:1205`（**模块级代码，import
   `tools.approval` 时无条件执行一次，与是否是 CLI/ACP/gateway 无关**）
   ```python
   # Load permanent allowlist from config on module import
   load_permanent_allowlist()
   ```
   `load_permanent_allowlist()` 从活动 profile 的 `config.yaml` 的 `command_allowlist` 列表读，塞进
   `is_approved()`（`tools/approval.py:315-320`）检查的并集里；短路 2 命中的就是这个集合（加上同会话内
   `/approve always` 产生的会话级近似）。**这不是要不要禁用的问题，是要不要"共享"的问题**：如果 worker
   的 `HERMES_HOME` 就是用户自己交互式跑 `hermes` CLI 那个 `~/.hermes`，用户在自己 CLI 会话里对某个模式
   点过一次"always"，这条豁免会原样出现在 Jones worker 的 `command_allowlist` 里，反之亦然——**两个身份
   的审批状态互相渗透**。Hermes 自己已经支持"按 profile 隔离"（`tools/approval.py` 的
   `_permanent_set()` 注释："Routed multiplex profiles: one permanent allowlist per profile home"，
   经 `hermes_constants.get_hermes_home_override()`/`hermes_home_key()`）。**要求**：daemon 给每个
   worker 子进程设置**独立的 `HERMES_HOME`**（不能是用户默认的 `~/.hermes`；例如 `~/.jones/hermes_home/`
   下按 project 或 session 分的子目录——具体粒度留给 daemon 骨架 Issue 定，本 spike 只确认"必须隔离"这个
   约束，不是"可以不隔离"），且 Jones 自己往这个隔离 home 的 `config.yaml` 写 `command_allowlist` 之前要
   想清楚这就是在把某条规则"永久放行"，不是一次性的。`docs/spikes/hermes_hook_demo.py` 已经在用
   `HERMES_HOME=$(mktemp -d)` 隔离，做法本身没错，只是原报告没把它当成一条**必须**的安全要求写下来。

**会话级 `/yolo` 与"会话恢复自动重开 YOLO"——本轮核实：对纯 ACP worker 不构成第四条短路，但依赖的是
一个当前源码里"未接线"的事实，不是协议层保证**：`enable_session_yolo(session_key)`
（`tools/approval.py:213-222`）本身是通的，但驱动它的两条入口都在 `hermes_cli/` 里：
- 交互 CLI/gateway 的 `/yolo` 斜杠命令（`hermes_cli/cli_session_mixin.py:850-910` 一类）；
- 会话恢复时从 `hermes_state_sessions.py:730-735` 的 `model_config.yolo_mode` 自动 `_restore_session_yolo()`
  （`hermes_cli/cli_session_mixin.py:168-184`，调用点在 `hermes_cli/cli_agent_setup_mixin.py:424`、
  `hermes_cli/cli_commands_mixin.py:1322`）。

`acp_adapter/commands.py::SlashCommandsMixin._COMMANDS`（ACP 自己的斜杠命令表，`help`/`model`/`tools`/
`context`/`reset`/`compress`/`steer`/`queue`/`version`）**没有 `yolo`**，`_restore_session_yolo` 的三个
调用点也都在 `hermes_cli/*_mixin.py`，本 spike 没有找到 `acp_adapter/session.py`/`server.py` 里对它的
调用——即：**只要 daemon 拉起的 worker 进程只跑 `python -m acp_adapter.entry`（不初始化任何
`hermes_cli` 的 CLI/gateway session mixin），这条会话级绕过在当前代码里没有可达路径**。写清楚这是
"当前源码里没找到调用点"（一种"未使用"，不是"协议层禁止"或"配置项关闭"）——Hermes 后续版本给
`acp_adapter` 加一个 `/yolo` 等价物完全可能，且不会被当成一次破坏性变更。**建议**：daemon 骨架/权限闸
落地时加一条自动化检查（例如集成测试里断言"给 worker 发一条以 `dangerous_tool` 命中审查闸的调用后，
往 ACP 连接发 `/yolo` 文本消息，下一次同名工具调用仍然收到 `session/request_permission`"），把"当前没有
调用点"这个脆弱的事实变成一条持续验证的回归测试，而不是一份文档里的静态断言。

**FR05 验收项建议**：把上面 1-3 三条（`HERMES_YOLO_MODE` 未设置、`approvals.mode` 非 `off`、worker
`HERMES_HOME` 与用户默认 `~/.hermes` 隔离且其 `command_allowlist` 由 Jones 自己管理）列为 FR05
（`docs/PRD.md` §12.2 G04 / §6.3）的验收项之一：**"worker 启动 Hermes 时，上述三条内置绕过路径全部不
生效"**，建议的验证方式是本节这套源码引用 + 一条集成测试（往 worker 发一个会命中用户闸的工具调用，同时
在 daemon 侧模拟"永不回应"，断言超时后仍是 deny 而不是被三条短路里任何一条提前放行）。这是本 spike 的
建议，不是本 spike 替 W3 权限闸 Issue 写死的验收标准；已同步写入 `docs/PRD.md` FR05 一行与 §6.3（见"接口
或契约变更"）。

## 审计写入时序（permission_decisions / steps）——本轮评审新增，替换原"下一步"里过时的表述

原报告"下一步"第 2 条写的是"把 `permission_decisions` 表的写入点接到 `pre_tool_call` 回调里"——这句话
在"插件立即返回 approve、真正的等待发生在 `pre_tool_call` 已经返回之后"这个新设计下不成立：`pre_tool_call`
回调返回的那一刻，approve 分支的真实结果（人到底批准没批准）根本还没发生，插件代码此时已经不在调用栈里，
无从"写入"一个还不存在的决定。三条来源各自的信息在哪个进程、哪个时刻才齐全，答案不一样，所以画三条独立
时序，而不是找一个共同的"写入点"：

**① 直接放行（未触发任何闸）**——最简单，无 `permission_decisions` 行，`steps.permission_id` 为 NULL：

```
worker: tool.started 事件 ──(ACP session/update ToolCallStart)──> daemon：insert steps(status=running)
worker: pre_tool_call 返回 None（不拦截）→ 工具真正执行
worker: 工具执行完 ──(ACP session/update ToolCallComplete)──> daemon：update steps(status=ok, result_summary, duration_ms)
```

**② 规则闸本地拒绝（零等待，worker 从不联系 daemon 做这次判断）**——决定在 worker 单进程内做完，daemon
只能事后从 ACP 事件流里**异步**得知，不可能同步拿到：

```
worker: tool.started 事件 ──(ACP ToolCallStart)──> daemon：insert steps(status=running)
worker: pre_tool_call 命中规则闸，同步返回 {"action":"block","message":"JONES RULE GATE: ..."}
        （零等待，不打一次 daemon IPC——这是刻意的性能设计，见结论 3/00-foundation.md §7）
worker: invoke_tool() 把 {"error": message} 当工具结果返回给 agent（工具从未执行）
worker: post_tool_call 钩子同步触发（同一进程内，晚于 pre_tool_call），拿到 status="blocked"、
        error_type="plugin_block"、error_message=message、duration_ms
worker: 工具"执行完"（其实是被拦下）──(ACP ToolCallComplete，result 就是上面那条 {"error": ...})──> daemon
daemon：从 ToolCallComplete 的 result 文本里识别出这是 Jones 自己的规则闸拒绝（需要一个 Jones 定义、
        daemon 能可靠解析的约定前缀/结构，例如现有 demo 里的 "JONES RULE GATE: " 前缀——具体解析约定
        留给 W3 权限闸 Issue 定，本节只确认"daemon 只能从这条异步事件里拿到规则闸决定，没有别的同步
        信号"这个约束本身），insert permission_decisions(gate="rule", decided_by="rule", decision="deny")，
        update steps(permission_id=刚插入的行, status=blocked)
```

**③ 审查闸/用户闸（approve 分支，走 ACP `session/request_permission`）**——daemon 自己就是这次决定的
拍板者，"决定发生的时刻"和"daemon 知道决定内容的时刻"是同一个时刻，天然没有异步落差：

```
worker: tool.started 事件 ──(ACP ToolCallStart)──> daemon：insert steps(status=running)
worker: pre_tool_call 立即返回 {"action":"approve","message","rule_key"}（不等待，见结论 3）
worker: hermes_cli/plugins.py::_resolve_block_from_details() 调 request_tool_approval()
        → 过了 YOLO/allowlist 短路检查后 → _human_decision() → conn.request_permission(...)
        （ACP JSON-RPC 请求，worker 是 server 端发起方，daemon 是 client 端接收方，这是一次真实的
        跨进程往返，worker 这个线程原地阻塞等 daemon 回复——见"未验证"一节对并发工具调用下这条阻塞
        的补充说明）
daemon：收到 session/request_permission 请求，走 daemon 自己的审批 UI/规则，算出 allow/deny 的那一刻
        insert permission_decisions(gate="review"/"user", decided_by="model"/"user", decision=...)
        —— 写库必须排在"把 ACP 响应发回去"之前，不能反过来（见下"失败时如何保证不漏记"）
daemon：发送 ACP 响应（allow_once/allow_session/allow_always/deny/...）
worker: request_tool_approval() 收到结果，pre_tool_call 整条调用链返回，invoke_tool() 按结果放行或
        block；post_tool_call 钩子同步触发（晚于 ACP 往返已经结束）
worker: 工具执行完/被拦下 ──(ACP ToolCallComplete)──> daemon：update steps(permission_id=②③已写好
        的那一行, status=ok/blocked, duration_ms)
```

**`steps`/`permission_decisions` 之间靠什么关联，这是本轮发现的一个未解决的口子，如实写出**：ACP 的
`session/request_permission` 请求携带的 `tool_call` 字段是**合成的**（`acp_adapter/permissions.py::
_build_permission_tool_call()` 生成一个新的 `perm-check-N` id，`N` 是进程内自增计数器），**不是**
`tool.started` 事件里那个真实的 ACP `tool_call_id`（`acp_adapter/events.py::make_tool_call_id()`
生成，走的是按工具名的 FIFO 队列 `tool_call_ids: Dict[str, Deque[str]]` 做"完成时"匹配，同名并发调用
本身也只是"先进先出"近似，不是精确 id 匹配——这是 ACP 桥接层已有的设计权衡，不是本 spike 新引入的问题）。
也就是说 daemon 收到 `session/request_permission` 时，手里只有 `session_id` + 命令/工具描述文本
（`command`、`description`），**没有**一个能直接对上 `steps` 表某一行的 id。daemon 侧要把 ③ 里新写的
`permission_decisions` 行和正确的 `steps` 行关联起来，目前只能靠"同一 session、按时间顺序匹配最近一个
状态为 running 且尚未有 `permission_id` 的 `steps` 行"这种启发式（本质上和 ACP 自己在 `tool_call_ids`
里做的 FIFO 近似是同一类做法），**在单线程顺序执行工具调用时是安全的，但 Hermes 支持并发工具调用
（`_MAX_TOOL_WORKERS = 8`），并发场景下这条启发式可能对错行——本 spike 没有实测并发场景下这条关联是否
真的会错位，只确认了"协议本身不提供精确关联手段"这个事实，是一个必须在 W3 权限闸 Issue 里解决的开放
设计问题（可选方向：daemon 用 `description` 文本里编码的 `rule_key`/工具名做更强的近似匹配；或者更彻底
地——升级 Jones 自己的插件，把 Hermes 内部的真实 `tool_call_id` 塞进 `request_tool_approval(rule_key=...)`
的 `rule_key`/`reason` 文本里一起传过去，daemon 解析出来做精确关联，需要验证这样做不会破坏"结论 3"里
`rule_key` 控制的 `[a]lways` allowlist 粒度语义）。

**失败时如何保证不漏记（诚实失败，DEV.md 原则 4）**：
- ③（daemon 自己拍板的路径）：写库必须发生在"daemon 决定 allow/deny"和"daemon 把 ACP 响应发出去"之间，
  顺序固定为**先写库、写库成功后再回 ACP**——如果反过来（先回复、后写库），daemon 在两步之间崩溃会导致
  "一次真实生效的用户闸决定，永久没有审计记录"，这正是 N09（错误被静默吞掉）在权限闸场景下最坏的样子。
  如果写库失败（磁盘满、锁冲突等），daemon 不能悄悄当作"写成功"继续回复批准——按 DEV.md「诚实失败」，
  应该把这次 ACP 响应也算失败处理（例如回 deny + 一条 `daemon.error` 通知，而不是静默批准且不留痕），
  具体降级策略留给 W3 权限闸 Issue 定，本节只确认"顺序不能反、失败不能被吞"这两条约束。
- ②（规则闸，daemon 事后从 ACP 事件流异步得知）：worker 侧的执行结果已经发生（工具真的被拦下了），
  daemon 写库失败不会导致"未经审批的动作被执行"（方向上比③安全），但仍然不能 `except: pass`——daemon
  处理 `session/update` 事件的代码本身要遵守 DEV.md「诚实失败」，写库异常要么重试要么显式记入
  `daemon.error`/结构化日志，不能让一条 ACP 事件"进来了但没有任何痕迹地消失"。

这一节替换了原"下一步"第 2 条里"写入点接到 pre_tool_call 回调里"的说法；`docs/design/00-foundation.md`
§7 对应描述也已同步更正（见"接口或契约变更"）。**"steps/permission_decisions 关联方式"和"daemon 写库
失败的具体降级策略"仍然是留给 W3 权限闸 Issue 拍板的开放设计问题**，本 spike 的职责是把时序和约束条件
钉清楚，不是替 W3 写实现。

## 风险

- **`hook_callback_timeout` 默认 30s，超时的影响范围比"这一次调用被拒"大一个量级——本轮已实测，不再只是
  读代码推断**：一旦 Jones 的插件在 `pre_tool_call` 回调里自己阻塞等待（而不是立即返回 `approve`），超时
  后 `hermes_cli/plugins_dispatch.py::_run_hook_callback_bounded()` 会把 `(hook_name, id(cb))` 这个
  **回调级**的 key 写进 `_hook_timeout_suppressed_until`，且被抛弃的 worker 线程在真正跑完之前，同一个
  key 也留在 `_hook_running_callbacks` 里——两者任一条件成立，`invoke_hook()` 对该回调的**每一次**后续
  `pre_tool_call`（不管是不是同一个工具、同一次调用）都直接返回 `_HOOK_SKIPPED`，对 fail-closed 的
  `pre_tool_call` 即等价于 block。demo 第 6/7 节把 `hook_callback_timeout` 和抑制窗口都调小后实测到：
  一次 `hangy_tool` 超时后，紧接着一次完全无关、原本会秒过的 `safe_read_file` 也被 block，直到（a）被
  抛弃的 worker 线程自己跑完**和**（b）60s 抑制窗口都过期，才恢复正常。对 Jones 的含义：如果真人一次没在
  30s 内点审批，且插件写成了"自己等"的错误形状，整个会话的所有工具调用会被连续拒绝到抑制窗口结束——这是
  权限闸可用性的硬约束，必须写进 daemon 骨架/权限闸 Issue 的验收标准（"决定阶段快速返回"是正确性要求，
  不是性能优化）。**照本文档"结论 3"的正确设计（插件只返回 `approve`）不会触发这条路径**，因为回调本身
  几乎不占用 `hook_callback_timeout` 的窗口——但一旦任何插件代码路径里混进一次同步阻塞（哪怕是无意的，
  比如一次同步网络调用），后果就是这里描述的连锁失效，值得在 daemon 骨架里加一层"pre_tool_call 回调必须
  在 N ms 内返回"的自测（不是本 spike 的产出，留给后续 Issue）。
- **未验证并发工具调用下的审批回调隔离**：Hermes 支持并发工具调用（`_MAX_TOOL_WORKERS = 8`，见
  `run_agent.py`），`pre_tool_call` 会在多个线程上同时触发，`approve` 分支最终落到的
  `terminal_tool` 按线程审批回调槽也要在并发下正确传播。`agent/tool_executor.py` 用
  `propagate_context_to_thread` 把 thread-local 的审批回调带进 worker 线程，源码/docstring 看设计上
  没问题，但**没有并发场景的实测**——这是本次评审明确点出的、本 spike 仍然没有覆盖的点，留给 daemon 骨架
  落地时的集成测试覆盖（见"下一步"）。
- **`pre_tool_call` 插件本身是单点**：如果 Jones 的插件抛异常，看 `_pre_tool_block_message()` 源码是
  `except Exception: return None, function_args`——**静默放行**，不是 fail-closed。这跟 DEV.md「诚实失败」
  原则冲突，也跟 PRD N09「错误被静默吞掉」是负面清单项直接冲突。**这是本 spike 发现的、需要 daemon 骨架
  实现时特别处理的点**：Jones 的插件代码本身要有自己的 try/except，把内部错误转成一次显式的
  `{"action":"block","message":"权限闸内部错误：<detail>"}`，绝不能让异常穿透到 Hermes 的兜底静默放行。
  建议写进权限闸 Issue 的验收标准。
- **第二个编辑审批闸未决**：见"ACP 侧证据"——`acp_adapter/edit_approval.py` 对 `write_file`/`patch` 有
  独立于 `pre_tool_call` 的 ACP 审批闸，PRD 6.3 定稿前应说清楚这一处是关掉、复用、还是让 Jones 的闸接管，
  否则要么用户被问两遍要么两套审批逻辑打架。
- **`gateway/` 命名冲突**：Hermes 自己的 `gateway/` 是 IM 机器人网关（Telegram/Discord/Slack…），PRD
  6.1 里"Channel Gateway"说的是同一类东西但不是同一份代码；后续如果决定接 Hermes 的 IM 网关实现 FR21，
  文档/代码里要避免用同一个词指两个不同的东西。
- **网络环境限制**：原始验证在带宽受限（~25 KB/s 对 github.com）的环境下完成，用了 treeless partial
  clone + 按需拉取，只覆盖了回答问题所需的 ~90 个源文件，不是整仓库；本轮修复用本机已有的完整 checkout
  复核了受评审影响的结论，但两次用的是不同 commit（见文首说明），不是同一次连续验证。

## 下一步

1. daemon 骨架 Issue：worker 启动流程里加载 Jones 自研 `pre_tool_call`（+ `post_tool_call`，见"审计
   写入时序"一节）插件（含"静默放行"风险的 fail-closed 包装，以及"Hermes 内置的批准绕过路径"一节要求的
   三条禁用配置），worker↔daemon 走 ACP stdio；插件的 approve 分支只返回 `{"action":"approve",...}`，
   不自己讲 ACP（本轮修复的核心结论）。详细的启动命令行/env/plugin.yaml 路径/daemon 需实现的 ACP client
   方法清单，见 `docs/design/00-foundation.md` §8「给 W2/W3 实现者的接入清单」（本轮新增）。
2. 权限闸 Issue（W3，FR05）：实现两段式设计——规则闸本地同步、审查闸/用户闸返回 `approve` 交给 Hermes
   走 ACP `session/request_permission`；`permission_decisions`/`steps` 的写入时序见本文件"审计写入时序"
   一节（daemon 侧写，不是 `pre_tool_call` 回调里写），其中"`steps`↔`permission_decisions` 精确关联"
   和"daemon 写库失败的降级策略"两点本 spike 未定案，需要 W3 拍板；**决定** `acp_adapter/edit_approval.py`
   那条 `write_file`/`patch` 编辑审批闸是关掉、复用还是被 Jones 的闸接管（本 spike 未定案）；**验证**
   "Hermes 内置的批准绕过路径"一节列出的三条禁用配置在真实 daemon 骨架里确实生效（建议的集成测试见该节
   "FR05 验收项建议"）。
3. **并发场景集成测试**（本次评审明确要求，本 spike 未覆盖）：在 daemon 骨架落地、有真实 8 线程工具
   worker 池时，验证 `terminal_tool` 的按线程审批回调槽在并发工具调用下不会跨线程串号或丢失。
4. 建议后续单独起一个小 spike（不阻塞 W1）验证 `hermes_state_*.py` 作为 worker 内会话存储、
   `cron/` 复用到什么程度，`tui_gateway/`（Hermes 自带的 TUI/Desktop JSON-RPC 后端，本 spike 顺带
   发现的第三条候选协议，未评估）值不值得看一眼。

## 验证方式

CANONICAL commit（与本文件、`hermes_hook_demo.py` 头部一致）：`ee4452991d17534aa561f31ee55596d082aa94e7`。

```bash
git clone https://github.com/NousResearch/hermes-agent
cd hermes-agent && git checkout ee4452991d17534aa561f31ee55596d082aa94e7
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e .   # 完整依赖，见 pyproject.toml
cd ..
HERMES_HOME=$(mktemp -d) PYTHONPATH=hermes-agent hermes-agent/.venv/bin/python \
    docs/spikes/hermes_hook_demo.py
# 期望：7 个 section 全部打印，"ALL ASSERTIONS PASSED"，退出码 0，整个脚本几秒内跑完
# （section 4/6/7 各带 1-2 秒的 sleep，用来真实测量超时/抑制窗口行为，不是卡住）
```

历史备选路径（网络对 `github.com` 带宽受限、拉不动上面这条 `-e .` 完整安装时）：treeless partial clone
+ `git checkout 0138269`（原始 spike 当时能拉到的最新 `main`，见文首"验证对象"）+ 只装 `pyyaml`，见
`docs/spikes/hermes_hook_demo.py` 文件头注释。**这条路径验证的是一个更早的快照，不是本文档当前引用的
commit**——网络不受限时应优先用上面的标准路径。

## 修复记录（2026-09-18，评审后）

逐条处理本轮代码评审的 9 条意见（见 PR/commit 消息里的评审原文），均确认成立，无不同意项：

1. **[critical] PRD 6.3 自相矛盾且谎称"实测"** → 改写 PRD §6.3 对应段落（`docs/PRD.md`）：拆成规则闸/
   审查闸/反例/待定四段，删掉"通过 worker 已经开着的 ACP 连接……发一次标准 session/request_permission"这个
   由插件发起 ACP 往返的错误框架，改成"插件只返回 approve，Hermes 自己的 request_tool_approval 经既有的
   terminal_tool 槽自动落到 ACP 上"；"实测见 spike #1" 改成了真实可复核的实测（demo 第 4 节，压缩
   hook_callback_timeout 到 0.5s、人工等待 1.5s 仍完整跑完）。
2. **[important] ACP request_permission 覆盖不到 approve 分支——与源码不符** → 本文件"结论 2"、
   "ACP 侧证据"整段重写，补上完整调用链（`_resolve_block_from_details` → `request_tool_approval` →
   `_presence` → `_resolve_cli_approval_callback` → `terminal_tool._get_approval_callback` →
   `server.py::_run_agent_turn` 绑定 `make_approval_callback`），标注仍未实测的部分（8 线程池下的
   thread-local 传播，见风险清单与"下一步"第 3 条）。
3. **[important] 验收项 2 没有真实运行证据，"不可行"理由是错的** → 重写 `hermes_hook_demo.py`：block
   路径全部改成驱动真实 `agent.agent_runtime_helpers.invoke_tool()`（~5 行 stub agent，无需 SDK），
   demo 第 3 节现在打印的是 `invoke_tool()` 的真实返回值，不是手写 JSON。
4. **[important] 漏掉编辑审批（write_file/patch）第二个 request_permission 接入点** → 加进"结论 2"、
   "ACP 侧证据"、"风险"、PRD §6.3、00-foundation.md §7，标记为 W3 权限闸 Issue 定案前必须决定的开放
   设计问题（关掉/复用/Jones 接管三选一），本 spike 不越权替后续 Issue 拍板。
5. **[critical] 同意见 1**，一并处理。
6. **[important] 同意见 2**，一并处理；另外确认了 `agent/tool_executor.py::propagate_context_to_thread`
   在并发路径上的作用，写进"未验证"清单。
7. **[important] 超时影响范围被低估——60s 抑制窗口是按回调而非按调用生效** → "风险"一节重写，用
   demo 第 6/7 节的实测数据（压缩窗口后，一次超时确实拖累了后续完全无关的调用）取代此前"只影响这一次
   调用"的错误描述；PRD/foundation 同步更新。
8. **[important] demo 把反面写法标成"真实集成的形状"** → 重写 `hermes_hook_demo.py`：`_on_pre_tool_call`
   现在对"需要人工"的场景返回 `approve`（正确形状），`hangy_tool` 单独作为一个明确标注"反面示例，仅用于
   演示其失败模式"的分支保留，供第 6/7 节测失败模式用，不再是"真实集成应该长这样"的示例。
9. **[important] "未改写"标注失实——docstring 被中文化、删了 `_thread_tool_whitelist` 分支、混入了一句
   本文档自己的推论** → 本文件"关键源码"一节标题改为"含中文注解，非逐字"，恢复英文 docstring 原文、
   补回 `_thread_tool_whitelist` 分支，把那句推论移出代码块、标注它的真实出处
   （`plugins_dispatch.py::invoke_hook`/`_run_hook_callback_bounded`）并新增该文件的摘录。

修复过程中一个意料之外的坑（记录以防后来者重复踩）：光调用
`tools.terminal_tool.set_approval_callback()` 绑定审批回调不够，`tools/approval_context.py::_presence()`
还要求 `_is_interactive_cli()` 为真，否则 `_run_approval_gate()` 直接 fail-closed（"no interactive
user/gateway is present"）——`acp_adapter/server.py::_run_agent_turn()` 是连着
`set_hermes_interactive_context(True)` 一起绑的，demo 第 4/5 节照抄了这一步才测通，这也印证了"结论 2"
里"approve 分支要求 worker 确实是以 ACP agent 身份在跑"这个前提本身是真实存在的，不是可有可无的细节。

测试结果：`docs/spikes/hermes_hook_demo.py` 在 `ee4452991d` 上重跑，7 个 section 全过，`ALL ASSERTIONS
PASSED`，退出码 0（见上方"demo"一节的原文输出）。仓库没有其它自动化测试覆盖本 Issue 的范围（纯 spike，
无代码接口变更），`make check` 在本分支没有可跑的目标（daemon/、apps/desktop/ 都还没有代码）。
