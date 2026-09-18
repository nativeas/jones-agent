# 开发规则（并行多代理）

本仓库由多个实现代理并行开发，控制者负责评审与合并。所有代理遵守以下规则。

## 单一事实源
- 产品需求：`docs/PRD.md`。Issue 只引用编号，不复述。
- 架构与接口：`docs/design/*.md`。**改接口先改文档**，同一 PR 内。
- 冲突时：PRD > design > Issue > 代码注释。

## 分支与提交
- 分支名：`w<周>/<issue号>-<slug>`，例 `w1/6-daemon-skeleton`。从 `main` 最新提交切出。
- 提交信息：`<type>(<scope>): <摘要> (#<issue>)`，type ∈ feat/fix/refactor/test/docs/chore/spike。
- 代理**不合并、不推送到 main**。只在自己的分支提交，结束时报告分支名与最终 commit。
- 一个 Issue 一条分支。不要顺手改别的 Issue 的范围。

## 目录所有权（并行不打架）
| 目录 | 归属 |
|---|---|
| `daemon/` | 守护进程（Python 3.12，uv） |
| `apps/desktop/` | Electron + React 桌面壳（pnpm） |
| `docs/spikes/` | 技术验证报告，一个 spike 一个文件 |
| `docs/design/` | 架构文档；改动需在 PR 说明中标注 |
| `packaging/` | 打包、签名、launchd |
| `.github/` | CI |

一个分支只应触碰它的 Issue 所需目录。需要改别的目录的接口 → 先在 `docs/design/` 里改契约，并在报告中显式说明。

## 工程原则
1. **第一性原理**：先问「这个问题的本质约束是什么」，再选方案。不要因为某个库流行就用它；不要因为 Hermes 有某个东西就绕开它重写——能直接复用 Hermes 的（会话状态、工具、Skill、MCP、cron、provider）就复用，只在 Jones 独有的地方（权限闸、Project/Agent 模型、Electron 前端、回放）写代码。
2. **不打补丁**：发现前提错了就改前提（文档 + 结构），不要在错的结构上加 if。一个 PR 里出现第三个 workaround 时停下来重新设计。
3. **性能是需求**：PRD 11.1 / 11.2 的数字是验收项。守护进程空闲不轮询、不常驻大对象；IPC 用 NDJSON 流式，不做大 JSON 一次性序列化；SQLite 开 WAL、写走事务、读不锁写；前端列表虚拟化、不在渲染路径做 IPC。每个 PR 说明中写一行「性能影响」。
4. **诚实失败**：任何 except 必须要么处理要么向上抛带上下文；禁止 `except: pass`。日志有结构（JSON lines）。
5. **可测**：daemon 用 pytest；desktop 用 vitest。改了行为必须有测试；测试必须真的断言。CI 绿才算完成。
6. **不做的事**：不加未被 Issue 要求的功能；不引入 ORM/DI 框架等重型抽象；不写「以后可能用到」的代码。

## 完成定义（DoD）
- Issue 验收 checklist 全部可勾（做不到的写清原因）
- 测试通过，本地 `make check` 绿
- 报告文件写完：做了什么、没做什么、接口变更、性能影响、给评审者的关注点
- 分支 rebase 到最新 main，无冲突
