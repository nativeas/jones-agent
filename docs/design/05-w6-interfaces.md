# 05 · W6：验收、性能、打包签名、升级迁移（v1.0 macOS）

对应 Issue #24 #25。上游：PRD 11（非功能）、12（验收：G01–G21、N01–N18、12.3）、13（W6）、`docs/spikes/02-packaging.md`、`packaging/`。

## 0. 原则

- W6 不加功能。发现缺口 → 开 Issue 修，不在验收分支里顺手改产品代码（除测试与打包脚本）。
- 验收证据要**可复现**：每条 G/N 一个自动化用例或一份带命令与输出的手工记录（`docs/acceptance/v1.0/`），不是勾选框。
- 打包是「把已验证的东西原样装进 .app」：daemon 与 Hermes 以**源码树 + 固定依赖**随包分发（Hermes 上游无可安装 artifact，`setup.py` 拒绝非 editable 构建——spike 与 W2 已确认），运行时用随包的 python-build-standalone 解释器。

## 1. 分工与文件所有权

| 分支 | Issue | 独占 | 允许的共享改动 |
|---|---|---|---|
| P `w6/24-acceptance` | #24 | `docs/acceptance/v1.0/**`、`daemon/tests/acceptance/**`、`apps/desktop/tests/acceptance/**`、`.github/workflows/acceptance.yml` | 无产品代码；发现缺口 → `gh issue create` 并在报告列出 |
| Q `w6/25-perf-packaging` | #25 | `packaging/**`、`daemon/tests/perf/**`、`apps/desktop/tests/perf/**`、`scripts/release/**`、`docs/design/00-foundation.md` §2/§3 追加 | `apps/desktop/src/main/index.ts`：只改打包模式下 daemon 可执行路径的解析（`process.resourcesPath`）；`daemon/pyproject.toml`：只加 build 相关 extras / 脚本；`store/migrator.py`：只加升级前备份与版本校验的钩子（若缺） |

## 2. P：验收（#24）

- **门禁 G01–G21（macOS 口径，G18 双架构）**：能自动化的写成 `daemon/tests/acceptance/test_gNN_*.py` / `apps/desktop/tests/acceptance/*.test.ts`（用假 ACP agent、provider stub、真实 daemon 起停、真实 Electron smoke/e2e），每个测试 docstring 引用 PRD 12.1 原文；不能自动化的（G01 全新机器安装、G16 全程抓包、G18 Intel 真机、需要模型 Key 的 G02 六厂商）写手工记录模板 `docs/acceptance/v1.0/GNN.md`（步骤、期望、实测输出、日期、机器），并明确标「待执行」。
- **负面清单 N01–N18**：可自动化的（N01–N04、N07、N12、N14、N10、N11、N15、N18）进 CI `acceptance.yml`，每次 PR 跑；其余同上手工模板。
- **12.3 P0 逐条**：对照表 `docs/acceptance/v1.0/FR-checklist.md`：每项 → 对应测试文件/手工记录链接 → 状态（通过 / 待 Key / 缺口 Issue#）。
- **缺口处理**：发现实现不满足 → `gh issue create -l P0 -m "W6 验收与发布"`，标题以 `[验收缺口]` 开头，正文引用 G/N 编号与复现；报告汇总。
- 性能门 G17 由 Q 提供数字，P 只引用。

## 3. Q：性能、打包、升级（#25）

### 3.1 性能（PRD 11.1 / 11.2）
- `daemon/tests/perf/`：冷启动（spawn → socket 可 accept）、worker 拉起（假 ACP agent 与真实 Hermes 两档，后者 JONES_E2E）、send → 首条 message.delta 运行时开销、空闲内存（RSS）、空闲 CPU（10s 内无唤醒：用 `psutil`/`ps` 采样或 `dtrace` 不可用则 `top -l`）；输出 JSON 到 `docs/acceptance/v1.0/perf-<date>.json`，与 11.1/11.2 阈值比对，超标即测试失败。
- `apps/desktop/tests/perf/`：窗口可见 ≤ 1.5s、可输入 ≤ 3s（Electron 启动计时，复用 smoke 的启动路径）、Electron RSS。
- 参考机口径写进结果文件（芯片、内存、macOS 版本）。

### 3.2 打包（spike 02 结论落地）
- `packaging/build.sh`（或 `scripts/release/build-mac.sh`）：
  1. `uv export` 出 daemon 依赖 → 用 python-build-standalone（universal2 或 arm64 + x86_64 双份）建随包 site-packages；
  2. **Hermes 源码树**：从固定 commit（`daemon/pyproject.toml` 里锁的那个）`git archive` 到 `Resources/daemon/hermes-agent/`，运行时以 `PYTHONPATH` 注入（不用 pip 安装它；把它的 `uv.lock` 依赖并进同一 site-packages，冲突项报告列出）；
  3. daemon 入口脚本 `Resources/daemon/bin/jones-daemon`；
  4. electron-builder：`extraResources` 收纳 daemon；`afterPack` 对 Python 动态库签名（hardened runtime + entitlements，沿用 spike 02 的 entitlements，去掉 apple-events）；`--timestamp` 正常；
  5. 无开发者证书环境 ad-hoc 签名跑通；有证书时 `CSC_NAME` 与 `notarytool` 流程写在 `docs/release.md`。
- Electron main 打包模式：daemon 路径 = `process.resourcesPath/daemon/bin/jones-daemon`；launchd plist 的 `ProgramArguments` 指向它（`service install` 在打包模式下用这个路径）。
- 验收：本机产出 `.dmg`（arm64 实测运行；x86_64 结构验证），`make check` + smoke 在打包产物上再跑一次（启动打包后的 .app，走 e2e）。

### 3.3 升级迁移（PRD 11.3 / N17 / G19）
- `store/migrator.py`：升级前自动备份（已有）+ 版本校验：新版打开旧库 → 迁移；**旧版打开新库**（schema_version 高于自身）→ 明确拒绝启动并提示，不静默降级。
- 端到端测试：用上一发布版本（当前 main 的某个 tag 作为「上一版」，若无则 `git tag v0.9-pre` 于 W5 收尾提交）产生 `JONES_HOME`，用当前版本打开，断言会话/记忆占位/Agent 完整。
- `docs/release.md`：发布检查单（引用 P 的验收结果、Q 的性能 JSON、签名/公证状态）。

## 4. 全体
- 两条分支互不依赖，可并行；P 若需要 Q 的性能数字，先用占位链接，合并后补。
- 报告里写：哪些 G/N 通过、哪些待 Key、哪些待真机、哪些缺口开了 Issue。
