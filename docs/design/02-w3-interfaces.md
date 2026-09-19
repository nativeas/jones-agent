# 02 · W3：权限三道闸、Run 回放、W2 集成收口

对应 Issue #11（FR05）、#12（FR06）。两条分支并行 + 一组集成收口项。上游：PRD 5.4/5.7、9.1/9.3/9.4/9.6、FR05/FR06、12.1 G04–G07、12.2 N01/N03/N07/N10/N12/N13；`00-foundation.md` §8（Hermes 接入清单）、§9（FR09 分级）；`01-w2-interfaces.md` §4.1（permissions.json 结构）。

## 0. 分工与文件所有权

| 分支 | Issue | 独占 | 允许的共享改动 |
|---|---|---|---|
| F `w3/11-permission-gates` | #11 | `daemon/src/jones_daemon/permissions/`（新）、`kernel/plugin/jones_gate/`、`sessions/modes.py`（新）、对应 tests | `sessions/service.py`：只改 `_on_request_permission`、`permission_decide`、`send`（模式检查一处调用）、`_resolve_pending_permissions`；`kernel/acp_client.py`：只改 `_answer_request_permission` 及其调用；`workers/manager.py`：只改 `_worker_env`/`_prepare_hermes_home`（写入规则闸配置给插件） |
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
      - 低风险 且 auto 模式 → 自动 allow（记录 decided_by=rule）
      - 高风险 或 task 模式 → 用户闸：写 permission_decisions(pending)，推 permission.requested，等待 permission.decide / 超时(只能 deny)
```

- **规则闸配置下发**：`_prepare_hermes_home` 把该会话生效的规则（`ctx.config.permissions(project_id)` 合并结果 + 会话模式 + Agent 工具白名单）写成 `<HERMES_HOME>/jones_gate.json`；模式切换 / 规则变更时 daemon 重写该文件并（若 worker 活着）通过 ACP 发一个自定义 `session/update`？——**不要**：ACP 没有这种反向配置通道。裁定：插件每次 `pre_tool_call` 读一次 `jones_gate.json`（几 KB，mtime 缓存），零协议扩展；模式切换即时生效（PRD 9.1）。
- **硬禁止清单**（代码常量，不可配置放宽）：`rm -rf`/`rm -r` 指向非临时目录、`trash`/清空回收站、`git push --force` 到默认分支、`shred`、`mkfs`、`diskutil erase*`、以及对 `~/.jones/`、`<project>/.jones/permissions.json` 的写删。终端类工具按命令词法解析（shlex）判定，不要用子串匹配打补丁式黑名单——写一个小的命令分类器，有测试。
- **审查闸的风险分级**：v1 用**确定性规则**（工具名 + 参数特征：写文件在工作区内/外、终端命令是否含网络外发 `curl|wget|ssh|scp`、浏览器工具按 §9 分级表），不接第二个 LLM 客户端。`review/` 子模块暴露 `classify(tool, args, ctx) -> Risk(low|medium|high, reasons)`；后续要换成模型判断时只换这个函数。**理由写进文档**：PRD 说审查闸是「模型对高危动作二次判断」，v1 用规则先满足 G04/G05/G06 的可测性，模型判断作为 W4+ 增强并在 PRD 中标注。
- **模式**（`sessions/modes.py`）：`chat` 插件 block 一切工具；`task` 写动作逐条用户闸；`auto` 规则闸 allow 范围内直接执行、审查闸 high 才用户闸。子会话模式不得比父宽（N13：`create(parent_id, mode)` 校验；工具白名单用 `agents/policy.is_tool_allowlist_subset`）。
- **审批超时**：`settings.approval_timeout_minutes`，到期自动 deny 并按 9.3 错误终止（卡片注明「审批超时」）；无「超时自动批准」。
- **remember**：`permission.decide.remember = session|project` → 写入会话级内存规则 / 项目级 `permissions.json`（只能是 allow 收窄到具体 match，不得触碰硬禁止）。
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
