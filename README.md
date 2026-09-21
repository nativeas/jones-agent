# Jones Agent

[![CI](https://github.com/nativeas/jones-agent/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/nativeas/jones-agent/actions/workflows/ci.yml?query=branch%3Amain)
[![Acceptance](https://github.com/nativeas/jones-agent/actions/workflows/acceptance.yml/badge.svg?branch=main)](https://github.com/nativeas/jones-agent/actions/workflows/acceptance.yml?query=branch%3Amain)

装在你自己电脑上、自主运行的通用 AI 工作体。

以对话为核心，配备文件、终端、浏览器、互联网、媒体生成、办公文档等全域能力，在你授权的边界内自主完成多步骤工作，全程可观察、可拦截、可回放。

## 核心原则

- **本地优先**：所有数据只存本机 `~/.jones/`，无云端同步、零遥测
- **无账号、BYOK**：没有注册登录订阅，只需你自己的模型 Key，兼容六家厂商
- **动作可拦**：改变外部世界的动作默认经你批准，拒绝即不执行，执行即留痕
- **失败诚实**：错误显式呈现，永不假装成功
- **开放扩展**：Skill、MCP 都能接，边界由你决定
- **不可逆删除永不执行**：任何模式、任何授权都无法覆盖

## 架构

```
Electron 壳（React 三栏工作台，无 Node 权限）
        │ 本地 socket + JSON-RPC
Jones daemon（Python，launchd 托管，Electron 关闭后仍常驻）
        │ 会话 / 队列 / 三道闸 / 回放 / Cron / 存储
        │ stdio · ACP
Hermes worker（每会话一个子进程，复用 hermes-agent 循环引擎）
        │ pre_tool_call 插件 jones_gate = 规则闸
能力域：文件 / 终端 / 浏览器 / 互联网 / 媒体 / Skill / MCP
```

**权限三道闸**（PRD FR05）：规则闸在 Hermes 进程内零 IPC 判定（硬禁止 + 模式 + Agent 白名单）；终端类工具在插件侧永不放行，daemon 是它们唯一的放行点；命令含任何无法静态分析的成分（引号内 `$`、`$(`、重定向、`;&|`、解释器/`xargs`/`eval` 等）一律升到用户闸并原样展示。不可逆删除硬编码拒绝，任何配置无法放宽。

**三种工作模式**：纯对话（零工具）/ 任务（写动作逐条弹闸）/ 自动（规则闸内直接执行，高危仍弹）。

## 开发

需要：macOS 13+、[uv](https://docs.astral.sh/uv/)（daemon 用 Python 3.12）、Node 22 + pnpm 10。

```bash
# daemon
cd daemon && uv sync && uv run python -m jones_daemon        # 前台跑
uv run python -m jones_daemon service install|status|uninstall  # launchd 托管

# 桌面端
cd apps/desktop && pnpm install
pnpm dev          # 连真实 daemon
pnpm dev:mock     # 不依赖 daemon，走 mock transport
pnpm smoke        # 真机启动 Electron 做一次冒烟
pnpm e2e          # 真 daemon + 真 Electron 端到端

# 全量检查（提交前必跑）
make check        # ruff + pytest + eslint + tsc + vitest
```

Hermes 内核目前是本机 path 依赖（上游无可安装 artifact），`make check` 用 `UV_FROZEN=1` 绕开该组解析——细节见 [docs/DEV.md](docs/DEV.md) §2.2。

### CI

每次 push 到 `main` 与每个 PR 跑两个 workflow（状态见页首徽章）：

- **[CI](.github/workflows/ci.yml)** — `daemon`（ubuntu：ruff + 全量 pytest）、`desktop`（macos：eslint + tsc + vitest + build + 真机 Electron smoke + 真 daemon e2e）
- **[Acceptance](.github/workflows/acceptance.yml)** — 只跑 G01–G21 / N01–N18 里可自动化的发布门禁，两端各一个 job。与 CI 有意重复：CI 证明「全仓库绿」，Acceptance 证明「发布门禁绿」，发布前一眼可辨。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/PRD.md](docs/PRD.md) | 唯一产品权威文档：定义、原则、架构、对象模型、Feature、行为契约、验收标准 |
| [docs/DEV.md](docs/DEV.md) | 开发规则：分支、目录所有权、工程原则、完成定义 |
| [docs/design/](docs/design/) | 架构与接口契约（00 基础 → 05 W6），改接口先改文档 |
| [docs/spikes/](docs/spikes/) | 四份技术验证报告：Hermes hook、打包、向量库、浏览器登录态 |
| [docs/acceptance/v1.0/](docs/acceptance/v1.0/) | 验收：FR 对照表、手工记录模板、性能实测 JSON |
| [docs/release.md](docs/release.md) | 发布检查单：打包、签名公证、升级迁移 |

## 状态

**v1.0（macOS）实施中**，P0 共 25 项已完成 24 项，测试 1030 pytest + 129 vitest 全绿。

已落地：三栏工作台与多会话、Project / Agent 配置、BYOK 六厂商、权限三道闸、Run 回放、文件五件套、终端、浏览器（Jones 专属 Chrome，CDP attach）、深度调研、MCP 接入与能力透明页、Skill 三层加载与内置 Skill、Cron 定时任务、守护进程生命周期、存储收口与真删、打包与性能基准、G01–G21 / N01–N18 验收套件。

发布前待办（[open issues](https://github.com/nativeas/jones-agent/issues)）：

- **#36** `cryptography` 无 macOS x86_64 wheel，arm64 机器出不了 Intel 包（挡 G18 双架构）
- **#33** 真实 Apple 公证（需开发者证书）、**#35** daemon 退出阶段偶发挂起
- 手工验收：G01 全新机器、G02 六厂商（需 Key）、G11 launchd 全流程、G16 抓包、G18 Intel 真机

后续版本：Goal 长效目标、多模态长期记忆、飞书接入、Code Review、IM 通道与远程审批、Windows v1.1。
