# FR-checklist — PRD 12.3 P0 Feature 验收口径

对应 Issue #24（PRD 12.3、design/05-w6-interfaces.md §2）。逐条列出 PRD 8.1
FR01-FR16（P0，v1 必须有）的验收口径原文、对应测试/手工记录链接、状态。

状态取值：**通过**（自动化用例已绿）/ **待 Key**（逻辑已测，缺真实厂商 Key）/
**待真机**（逻辑已测，缺真机步骤）/ **缺口 Issue#**（发现未满足）。

## FR01 — 多会话工作台

> "三栏 UI，左右栏可开关；主会话（唯一、常驻）+ 子会话；会话归属 Project"

- 左右栏可独立开关且状态记住：
  `apps/desktop/src/renderer/src/store/__tests__/layoutStore.test.ts`
  （开关独立 + 持久化到 `localStorage`）。
- 主会话唯一且不可删除：
  `daemon/tests/test_sessions_service.py`（迁移 004 保证唯一主会话）、
  `apps/desktop/src/renderer/src/store/__tests__/sessionsStore.test.ts::
  "exposes exactly one main session, and it is the only one flagged is_main"`；
  `daemon/tests/test_storage_maintenance.py::
  test_delete_session_refuses_the_main_session`（真删也拒绝删主会话）。
- 子会话树形展示父子关系：
  `sessionsStore.test.ts::describe('buildProjectGroups (pure tree building)')`
  （"nests children under their parent, arbitrarily deep"）。

**状态：通过**

## FR02 — Project 工作区

> "以目录路径为锚点创建 Project；同目录下的 Session 归组管理、可统一操作"

- 目录路径创建/幂等：`daemon/tests/test_projects_service.py::
  test_create_anchors_a_project_on_an_existing_directory`、
  `test_create_is_idempotent_on_the_same_path`。
- agent 文件工具默认限定在该目录：`daemon/tests/test_cap_files_g15.py::
  test_read_file_outside_workspace_is_high_fr07`（越界走用户闸）。
- 删除级联/拒绝条件：`test_storage_maintenance.py::
  test_delete_project_refuses_when_sessions_reference_it` 等。
- Project 隔离（Agent/Skill 不跨 Project 可见）：
  `daemon/tests/acceptance/test_g13_project_isolation.py`（G13）。

**状态：通过**

## FR03 — Agent 配置

> "人设 / 语气 / 原则 / 工具白名单 / Skill 集合 / 模型偏好；可创建多个 Agent
> 并绑定到 Session"

- 六项字段可编辑：`daemon/tests/test_agents_service.py`、
  `test_agents_methods.py`（`agent.upsert` 全字段）。
- 同一 Agent 可绑多个 Session：`test_agents_service.py::
  test_delete_refuses_when_a_session_is_bound_to_the_agent`（反证：绑定关系
  真实存在，删除会被挡）。
- 项目级 Agent 覆盖用户级：`test_agents_service.py::
  test_list_includes_user_level_plus_the_given_projects_own_agents`。
- 桌面端编辑 UI：`apps/desktop/src/renderer/src/store/__tests__/
  settingsStore.test.ts::upsertAgent() ...`。

**状态：通过**

## FR04 — BYOK 多厂商模型

> "设置页配置各厂商 Key；Session / Agent 级别可指定模型；Key 永不完整回显"

- Key 存取/校验/路由：`daemon/tests/test_providers_methods.py`、
  `test_providers_resolver.py`。
- Key 永不完整回显：`daemon/tests/acceptance/test_g03_key_never_reechoed.py`
  （G03/N02）；桌面端只存 hint：`settingsStore.test.ts::
  "setProviderKey() stores only the key hint, never the full key (PRD FR04)"`。
- 六家厂商真实完成 Turn：`docs/acceptance/v1.0/G02.md`（**待 Key**）。

**状态：待 Key**（逻辑与防泄漏已通过；六厂商真实连通性待真实 Key）

## FR05 — 权限三道闸

> "规则闸 → 审查闸 → 用户闸；拒绝即不执行；不可逆删除永不执行，任何授权不可
> 覆盖"

- 见 G04、G05、G14、N01、N03（`daemon/tests/acceptance/test_g04_*.py`、
  `test_g05_*.py`、`test_g14_*.py`、`test_n01_*.py`、`test_n03_*.py`）。
- **"建议新增"条款**（worker 启动 Hermes 时 `HERMES_YOLO_MODE`/
  `approvals.mode: off`/持久 `command_allowlist`/`HERMES_SAFE_MODE` 四条内置
  绕过路径不生效，且启动自检确认 Jones 插件已加载）：
  `daemon/tests/test_workers_manager.py::
  test_worker_subprocess_env_never_carries_yolo_or_safe_mode`、
  `test_ensure_started_passes_self_check_and_isolates_hermes_home`、
  `test_startup_self_check_rejects_a_worker_whose_probe_completes`（及同组其余
  三个 `test_startup_self_check_rejects_*`）。
- 桌面端用户闸面板：`apps/desktop/src/renderer/src/components/__tests__/
  PermissionPanel.test.tsx`。

**状态：通过**

## FR06 — Run 回放

> "每个 Run 的 Step 序列可完整回放：参数、结果、耗时、审批结果；回放数据以
> SQLite 记录为事实源"

见 G07：`daemon/tests/acceptance/test_g07_run_replay_complete.py`、
`apps/desktop/tests/acceptance/test_g07_replay_ui.test.ts`（逐 Step 前进/
后退、不触发真实动作）。

**状态：通过**

## FR07 — 文件五件套

> "读 / 写 / 编辑 / 搜索 / 目录遍历；受 Project 工作区与权限闸约束"

- 写/编辑 diff 在 Step 中可见：`daemon/tests/test_cap_files_diff_step.py`
  （`test_auto_approved_edit_diff_lands_in_the_step`、
  `test_needs_approval_edit_diff_correlates_to_the_real_step`）。
- 越界路径走权限闸：`daemon/tests/test_cap_files_g15.py::
  test_read_file_outside_workspace_is_high_fr07`。
- 敏感目录默认拒绝：见 G15（`daemon/tests/acceptance/
  test_g15_sensitive_dirs_hidden.py`）。
- 读/搜索/遍历的风险分级：`daemon/tests/test_gates_review.py`（`read_file`/
  `search_files` 相关用例）。

**状态：通过**

## FR08 — 终端

> "执行命令、流式输出、可中断；高危命令走权限闸"

- 流式输出延迟 < 200ms：`daemon/tests/test_cap_terminal_streaming_latency.py`
  （`test_tool_call_start_update_to_broadcast_latency_is_well_under_200ms`、
  `test_tool_call_update_to_broadcast_latency_is_well_under_200ms`）。
- 中途可中断、子进程被回收：见 G09（`daemon/tests/acceptance/
  test_g09_clean_termination.py`）。
- 高危命令标红（`rm`/`sudo`/`curl | sh` 等）：`daemon/tests/
  test_cap_terminal_danger.py`（`test_sudo_is_high_risk`、
  `test_curl_pipe_sh_is_high_risk` 等一整组）。

**状态：通过**

## FR09 — 浏览器

> "Jones 专属常驻浏览器 profile；登录一次后持续复用登录态；导航 / 读页 / 点击
> / 填表"

- profile 常驻、进程复用/重连：`daemon/tests/test_cap_browser.py`
  （`test_ensure_started_launches_and_is_idempotent`、
  `test_ensure_started_reattaches_to_a_live_orphan_after_manager_replaced`）。
- 登录态跨重启存活：`daemon/tests/integration/test_cap_browser_e2e.py::
  test_login_state_survives_a_graceful_shutdown_and_restart`（`JONES_E2E=1`，
  真实 Hermes + 真实浏览器）。
- 导航/读页/点击/填表各一：同文件
  `test_snapshot_then_type_then_click_covers_the_form_filling_acceptance_bar`、
  `test_real_hermes_browser_navigate_goes_through_the_gate`。
- FR05 三道闸分级（只读走规则闸、点击/填表走审查闸、外发/存储写入走用户闸）：
  `daemon/tests/test_gates_review.py`（browser 相关的 low/medium/high 分级
  一整组）、`test_real_hermes_browser_navigate_goes_through_the_gate`、
  `test_browser_navigate_to_a_loopback_target_is_refused_by_hermes_itself`。

**状态：通过**（`JONES_E2E=1` 门控的真实浏览器用例不在默认 `pytest -q`/CI 下
跑，属于本仓库既有的、有意为之的分级——见该测试文件自身的 gating 说明）

## FR10 — 互联网与深度调研

> "搜索 + 抓取 + 多步调研；产出带引用的报告；引用链接可达率 ≥ 90%"

`daemon/tests/integration/test_cap_research.py::
test_web_search_results_meet_the_citation_reachability_bar`（字面断言可达率
≥ 90%）、`test_web_extract_reads_real_page_content`（真实抓取）。

**状态：通过**（该文件需要真实网络，非 `JONES_E2E` 门控——见文件本身；CI 的
`daemon` job 默认联网环境下可跑）

## FR11 — Cron 定时任务

> "Cron 表达式 + prompt 模板；触发 Task → Run；结果推回主会话；连续失败 3 次
> 自动停用并提醒"

- upsert/list/delete/run_now：`daemon/tests/test_scheduler_methods.py`。
- 分钟级触发、派发为真实 Run：`daemon/tests/test_scheduler_service.py::
  test_dispatched_task_row_is_created_with_source_cron`、
  `test_default_mode_is_auto_and_dispatches_against_real_session_service`。
- 连续失败 3 次自动停用并提醒：`test_scheduler_service.py::
  test_three_consecutive_failures_auto_disable_and_notify`。
- 关闭 Electron 仍触发（自动化半）：G11（`daemon/tests/acceptance/
  test_g11_daemon_resident.py`）；真实 launchd 全流程：
  `docs/acceptance/v1.0/G11.md`（**待真机**）。

**状态：通过**（daemon 内触发逻辑）/ **待真机**（真实 launchd 关闭/重开
Electron 全流程，见 G11.md）

## FR12 — Skill 加载与内置 Skill

> "文件形式加载 Skill；v1 内置 Skill 至少含办公文档生成、媒体生成"

- 加载/项目级遮蔽用户级：`daemon/tests/test_skills_service.py`（含 G13 的
  新用例 `daemon/tests/acceptance/test_g13_project_isolation.py`）。
- 内置 Skill 两项都在且 valid：`daemon/tests/test_bundled_skills.py::
  test_both_bundled_skills_are_listed_valid_in_the_builtin_tier`。
- 办公文档 Markdown/Docx/PDF/表格 各一份可打开：`test_bundled_skills.py::
  test_md_to_docx_produces_a_docx_python_docx_can_read_back`、
  `test_md_to_xlsx_produces_an_xlsx_openpyxl_can_read_back`、
  `test_md_to_pdf_produces_a_pdf_with_a_real_page`。
- 媒体生成图/音/视频 各一份可播放：`daemon/tests/integration/
  test_media_gen_hermes_e2e.py::
  test_text_to_speech_produces_a_playable_audio_file`、
  `test_image_generate_produces_a_real_image`、
  `test_video_generate_produces_a_real_video`（`JONES_E2E=1`，需要真实媒体生成
  Key/服务——文件本身按此门控）。

**状态：通过**（办公文档一档，本地无外部依赖）/ **待 Key**（媒体生成一档，
`JONES_E2E=1` 且需要真实媒体生成服务的 Key）

## FR13 — MCP 接入

> "stdio 与 HTTP 两种 MCP Server 各接一个；其工具进入能力注册表与透明页；受
> 白名单与权限闸约束"

`daemon/tests/integration/test_mcp_e2e.py::
test_stdio_and_http_mcp_tools_are_both_callable_by_a_real_worker`（真实起
`mcp_echo_stdio.py`/`mcp_echo_http.py` 两种 transport，`JONES_E2E=1`）；
注册表/白名单/权限闸约束见 G21、N15（`daemon/tests/acceptance/
test_g21_capability_transparency.py`、`test_n15_no_unconfirmed_mcp_skill.py`）。

**状态：通过**

## FR14 — 失败诚实与错误面板

> "网络 / 配额 / 工具异常显式呈现；重试 / 换模型 / 放弃的选择权交给用户；永不
> 白屏、永不假装成功"

- daemon 侧诚实失败：见 G08、N07、N08/N09（`daemon/tests/acceptance/
  test_g08_four_faults_no_crash.py`、`test_n07_*.py`、`test_n08_n09_*.py`）。
- 错误卡片三个操作：`apps/desktop/src/renderer/src/components/chat/
  TerminationCard.tsx`（`重试`/`换模型`/`放弃` 三个按钮，接到
  `retryLastMessage`/`goToAgentModelSettings`/`dismissTermination`）；
  `apps/desktop/src/renderer/src/store/__tests__/chatStore.test.ts::
  "retryLastMessage() resends the most recent user message (PRD 9.3 错误终止
  卡片 "重试")"`、`"dismissTermination() removes only the named card ..."`。
- 永不白屏：N16（`apps/desktop/tests/acceptance/test_n16_no_white_screen.test.ts`）。

**状态：通过**

## FR15 — 常驻守护进程

> "注册为系统用户级服务，随登录自启（可关）、崩溃自动拉起；Electron 关闭后
> 运行时仍存活；进程重启后排队中的外发动作不自动重放"

- launchd plist 渲染/安装/卸载/状态：`daemon/tests/test_service.py`。
- 崩溃自动拉起 + 连续 3 次失败显式报错（daemon 侧健康检测逻辑）：
  `apps/desktop/src/main/__tests__/daemonLifecycle.test.ts`（
  `retries kickstart/spawnDev/connect and succeeds on a later attempt`、
  `reports unreachable exactly once after exhausting all retries`、
  `runs the full recovery sequence after HEARTBEAT_FAILURE_THRESHOLD
  consecutive misses`）。
- 重启不自动重放：G10/N04（`daemon/tests/acceptance/
  test_g10_restart_no_autoresend.py`）。
- 真实 launchd 5 秒内拉起 + Electron 关闭仍常驻：`docs/acceptance/v1.0/G11.md`
  （**待真机**）。

**状态：通过**（逻辑层）/ **待真机**（真实 launchd 拉起延迟，见 G11.md）

## FR16 — 能力透明页

> "展示本轮给 Agent 装配了哪些工具、隐藏了哪些、为什么；由能力注册表实时生成"

见 G21：`daemon/tests/acceptance/test_g21_capability_transparency.py`、
`apps/desktop/tests/acceptance/test_g21_capability_transparency.test.ts`
（drift 警告 UI + 每个隐藏工具标注 `hidden_reason`）。

**状态：通过**

---

## 汇总

| FR | 状态 |
|---|---|
| FR01 多会话工作台 | 通过 |
| FR02 Project 工作区 | 通过 |
| FR03 Agent 配置 | 通过 |
| FR04 BYOK 多厂商 | 待 Key（六厂商真实连通性） |
| FR05 权限三道闸 | 通过 |
| FR06 Run 回放 | 通过 |
| FR07 文件五件套 | 通过 |
| FR08 终端 | 通过 |
| FR09 浏览器 | 通过 |
| FR10 互联网与深度调研 | 通过 |
| FR11 Cron 定时任务 | 通过（逻辑）/ 待真机（launchd 全流程） |
| FR12 Skill 加载与内置 Skill | 通过（办公文档）/ 待 Key（媒体生成） |
| FR13 MCP 接入 | 通过 |
| FR14 失败诚实与错误面板 | 通过 |
| FR15 常驻守护进程 | 通过（逻辑）/ 待真机（launchd 全流程） |
| FR16 能力透明页 | 通过 |

P1（FR17-20）不在 v1 发布门禁范围内，不在本表；FR18 长期记忆是 G20"记忆"分句
在 v1 下空真的原因，见 `docs/acceptance/v1.0/G20.md` 等价说明（已写在
`daemon/tests/acceptance/test_g20_real_delete.py` 的模块 docstring 里，未
单独建 G20.md——G20 本身已全自动化，不需要手工模板）。

已知、已跟踪、不在本次验收范围内新开的缺口：
- **#36**（P0，发布阻塞）：x86_64 daemon 构建失败，阻塞 G18。
- **#37**（P0）：PRD 11.2 单 Run 最大 Step 数（200）/ 最大时长（2h）无触发点
  ——影响 G17 的"11.2 全部指标通过"，本轮验收未新开重复 Issue，见该 Issue。
