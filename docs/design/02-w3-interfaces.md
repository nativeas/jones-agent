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

## 3. 全体

- 性能：规则闸零 IPC；审查闸纯计算 < 1ms；用户闸等待不占 worker CPU；回放 payload 写入异步、不阻塞 ACP 读循环。报告里给数字。
- 诚实失败：任何闸出错 → **拒绝**（fail-closed）并推 `daemon.error`，绝不放行。
