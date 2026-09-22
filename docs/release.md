# Jones Agent v1.0 (macOS) — 发布检查单

对应 Issue #25（PRD 11.1 / 11.2 / 11.3、13 W6），docs/design/05-w6-interfaces.md
§3。本文档是「打包 + 性能 + 签名/公证 + 升级迁移」这条线的发布检查单；G01–G21 /
N01–N18 的验收结果由 Issue #24（P）产出，本文档只引用，不复述。

## 1. 性能（PRD 11.1 / 11.2，G17）

自动化用例：`daemon/tests/perf/`（`make check-daemon` 的一部分，`pytest -q` 默认
收集）、`apps/desktop/tests/perf/measure-startup.mjs`（`pnpm run perf`，需要先
`pnpm run build`）。两者写入同一份 `docs/acceptance/v1.0/perf-<date>.json`。

**参考机口径**：PRD 11.1 指定 Apple M1/16GB 与 Intel i5 12 代/16GB 两台参考机。
本分支的自动化数字在 **Apple M4 / 32GB / macOS 27.0** 上实测——不是参考机，数字
仅供方向性参考（`perf-<date>.json` 的 `machine.is_prd_reference_machine` 字段如实
标 `false`）。发布前必须在真实参考机（或至少一台 M1）上重跑
`JONES_PERF_RECORD=1 cd daemon && uv run pytest -q tests/perf` + `JONES_PERF_RECORD=1 pnpm run perf`（不设该变量时只断言阈值、不写文件；`make check` 不产生工作区改动），并核对
`docs/acceptance/v1.0/perf-<date>.json` 里的 `all_passed` 与各项数字。

最近一次本机实测结果：见 `docs/acceptance/v1.0/perf-<date>.json`（生成于本分支
最后一次跑 `make check-daemon` + `pnpm run perf` 时）。全部 8 项指标本机实测通过
（daemon 冷启动、worker 冷启动、send→首条 delta、空闲 RSS、空闲 CPU、Electron
窗口可见、Electron 可输入、Electron RSS）——**但 Electron RSS 一项余量很小**
（本机实测 ≈276MB，阈值 300MB，见该 JSON 的 `desktop_electron_rss_mb`
`detail.processes` 明细），发布前必须在参考机上复测，不能只信这台开发机的数字。

## 2. 打包（spike 02 结论落地，design §3.2）

- 构建：`scripts/release/build-mac.sh [arm64|x64|both]`
  1. `scripts/release/build-daemon-bundle.sh <arch>`：python-build-standalone
     解释器 + 真实 site-packages（`uv build --wheel` 出 daemon 自身的 wheel +
     `uv export --no-default-groups --group worker` 导出的合并依赖闭包，见
     `docs/design/00-foundation.md` §3.1）+ Hermes 源码树（锁定 commit
     `git archive`，PYTHONPATH 注入，不 pip 安装）+ 入口脚本
     `bin/jones-daemon`。每个架构的 `DEPENDENCY-REPORT.md` 记录安装了多少个
     发行包、为什么没有版本冲突需要列（见该文件）。
  2. `apps/desktop`：`pnpm install && pnpm run build`（electron-vite）。
  3. `packaging/release/`（自带 `electron-builder@25.1.8` devDependency，独立
     于 `apps/desktop/package.json`，与 `packaging/electron-shell/` 的既有
     spike 模式一致）：`electron-builder --project apps/desktop --config
     packaging/release/electron-builder.yml`（`extraResources` 用 `${arch}`
     宏把上一步的 daemon 目录整个装进 `Contents/Resources/daemon/`）。
  4. `afterPack`（`packaging/release/afterPack.cjs`）：`mac.identity: null`
     时 electron-builder 自己完全跳过签名（spike 02 §3 已确认，
     app-builder-lib 源码核实），这一步用 `packaging/sign/sign-adhoc.sh`
     手工由内向外签名（先签 `Resources/daemon` 下所有 `.dylib`/`.so`/可执行
     文件与 Python 解释器本体，再 `--deep` 签整个 `.app`）。

### 2.1 本机实测（arm64，实际可执行）

- `scripts/release/build-mac.sh arm64` 产出 `build/release/dist/Jones-0.1.0-arm64.dmg`。
- daemon 载荷真实可运行：`Contents/Resources/daemon/bin/jones-daemon` 独立起停
  验证过（`JONES_HOME=<临时目录> .../jones-daemon` → 日志 `daemon listening` →
  `daemon.ping` 收到真实 pong）；Hermes 源码树 PYTHONPATH 可用（`import
  acp_adapter`、`import hermes_cli.plugins` 在打包产物的解释器下验证过，见报告）。
- `codesign --verify --deep --strict`：通过。`spctl --assess --type execute`：
  **rejected（预期行为）**——ad-hoc 签名没有 Developer ID，Gatekeeper 必然拒绝，
  见 §3。
- `.app` 打包产物上跑了 smoke（`window.jones.rpc.call` 桥接存在）与 e2e
  （project.list → session.create → session.send 全链路，见报告「打包产物
  smoke/e2e」一节的具体命令与结果）。

### 2.2 x86_64（**本机实际构建失败，不只是不能运行**——比 spike 02 的结论更差，诚实写下）

spike 02 的空壳 daemon（纯标准库，零第三方依赖）能在 arm64 主机上产出可用的
x86_64 python-build-standalone 产物，只是不能在本机执行验证（没有 Rosetta）。
**真实 daemon 一旦引入编译型依赖，这个结论不成立**：

- **实测**（`scripts/release/build-daemon-bundle.sh x64`）：解释器本体下载没问题
  （`uv python install cpython-3.12-macos-x86_64-none` 只是拷贝官方预编译
  tarball），但随后 `uv pip install --python-platform x86_64-apple-darwin
  --python 3.12 -r <合并依赖> ...` 在装 `cryptography==50.0.0` 时失败：
  **PyPI 上 `cryptography==50.0.0` 没有发布 macOS x86_64 wheel**（实测核对
  `https://pypi.org/pypi/cryptography/50.0.0/json`：这个版本的 macOS 产物只有
  `cp39-abi3-macosx_11_0_arm64` / `cp311-abi3-macosx_11_0_arm64` /
  `cp314-cp314t-macosx_11_0_arm64` 三个 arm64 wheel，没有任何 `x86_64` 条目）。
  `uv` 因此退回从源码构建（`maturin`/`cargo` 编译 Rust 扩展），而本机是 arm64
  主机、没有 `x86_64-apple-darwin` 的 Rust 交叉编译工具链，构建在链接阶段失败
  （`_cffi_backend...so`「have x86_64, need arm64e...」——构建产物架构与请求架构
  不匹配）。
- **这不是"本机不能验证"，是"本机造不出这个产物"**：spike 02 §1 的
  "python-build-standalone 不需要在目标架构上执行任何代码，纯文件拷贝" 这句话
  只对纯 Python/已发布 wheel 的依赖成立；一旦某个依赖在某个版本上**没有**目标
  架构的预编译 wheel，`uv pip install --target` 就会退化成"在当前（宿主）架构
  上编译，产出宿主架构的二进制"，而不是报错说"这个 wheel 不存在"——这是本次
  实测新发现的、spike 02 未覆盖的失效模式（spike 02 的空壳 daemon 没有任何
  第三方依赖，从未触发过这条路径）。
- **影响范围**：不只是 `cryptography`——任何后续给 `daemon/pyproject.toml` 加的
  编译型依赖（C 扩展/Rust 扩展）都可能在某个版本上缺 x86_64 wheel，同样的失败
  会重演。这条风险在真实依赖引入之前完全不可见。
- **真正可行的路径**（本分支未执行，留给后续）：
  1. 在真实 x86_64 Mac（或 GitHub Actions `macos-13` 系列，Intel 原生 runner）
     上跑 `scripts/release/build-daemon-bundle.sh x64`——同架构编译，没有交叉
     编译问题；
  2. 或给这台 arm64 开发机装 Rust 的 `x86_64-apple-darwin` target
     （`rustup target add x86_64-apple-darwin`）+ 确保 `cargo`/`maturin` 走
     交叉编译标志——未验证是否足够（`cryptography` 的 `pyo3`/`maturin` 构建链
     路是否正确识别 `--python-platform` 传下来的目标架构，本分支没有实测）；
  3. 或等上游给 `cryptography==50.0.0`（或后续版本）补上 macOS x86_64 wheel。
- **结论**：v1.0 macOS 双架构（G18）在把「真实依赖」这个变量算进来之前，
  x86_64 这条腿的可行性其实没有被真正验证过——spike 02 的"两条路线都能在
  arm64 主机上产出正确的 x86_64 产物"这句结论，只对它自己测的空壳成立，
  不能不加验证地外推到真实 daemon。这是本分支交给 P（#24，G18 验收）和后续
  发布决策的一条关键信息，不应该被"反正 spike 说了两条路都行"带过去。

### 2.3 #36 解除：CI 原生构建双架构（2026-09-22，方案 a 落地）

上面 §2.2 的结论本身没有变——arm64 主机确实造不出 x86_64 产物，这不是
"本机限制"，是"从源码构建 `cryptography` 需要在目标架构上跑 Rust 编译器"
这个硬约束。但 #36 讨论里的方案 (a)（CI 用原生 Intel runner 直接出
x86_64 包）已经在 GitHub Actions 上真实跑通，不是本地推演：

- **工作流**：`.github/workflows/release-bundle.yml`（新增，独立于
  `ci.yml`/`acceptance.yml`，不挂在每个 PR 上——单次构建含一次真实 Rust
  编译，比常规 lint/test 慢得多）。触发：`workflow_dispatch` +
  push 一个 `v*` tag。两个 job，矩阵化，都是**原生**编译（不是交叉）：
  - `x64` 腿：`macos-15-intel` runner（原生 x86_64）。**注意**：Issue #36
    原文与本节前面写的都是 `macos-13`——本分支验证过程中发现 `macos-13`
    镜像已于 2025-12-04/08 被 GitHub 完全退役（不是排队慢，是这个标签下
    已经没有 runner 可分配：排队 20+ 分钟、`started_at` 一直为空，取消后
    换成 `macos-15-intel` 才真正起跑）。`macos-15-intel` 是 GitHub 给
    macos-13 退役后指定的替代标签，同样原生 x86_64、同样是免费的标准
    runner（不是要单独计费的 `-large`/`-xlarge` larger-runner SKU）。
  - `arm64` 腿：`macos-14`，原生 arm64（此前只在本地开发机上验证过，这是
    它第一次在 CI 上跑）。
- **`scripts/release/build-daemon-bundle.sh` 的改动**：目标架构与主机架构
  相同（原生构建）时不再传 `--python-platform`——那个参数是为了在**不**
  匹配的宿主上伪装目标平台的 wheel tag（cross 用），原生构建下这个人为
  伪装反而没必要，直接让 `uv` 用宿主自己的解释器/平台/工具链装。
- **CI 上踩到、也修掉的两个真 bug**（第一次 CI 跑两个 job 都显示绿勾，但
  实际是假阳性——产出的是一个只有裸解释器、没有任何第三方依赖的坏
  bundle，日志里混进一行 `unbound variable` 错误没人注意到）：
  1. macOS 系统 `/bin/bash` 停在 3.2（Apple 不发 GPLv3 版本），这个版本的
     `set -u` 对**空数组**展开（`"${PLATFORM_FLAGS[@]}"`，原生分支下
     `PLATFORM_FLAGS=()`）判 `unbound variable`，当场终止脚本——本地反复
     实测复现，不是猜测。改成 bash-3.2 安全写法
     `"${PLATFORM_FLAGS[@]+"${PLATFORM_FLAGS[@]}"}"`。
  2. 脚本原有的 `trap 'rm -f "$REQS_FILE"' EXIT` 没有显式 `exit`，trap 里
     最后一条命令（`rm -f`，必然成功）的退出码会覆盖脚本本该报的失败退出
     码——上面那个崩溃因此被悄悄吞成了 `exit 0`，upload-artifact 正常跑、
     job 显示成功。改成 `trap 'ec=$?; rm -f "$REQS_FILE"; exit $ec' EXIT`
     保住真实退出码。这个 trap 缺陷比这次改动本身更老（只是之前从未被
     触发过），修完之后任何一步真的失败（比如 Rust 编译真的挂了）现在会
     如实让 job 变红，不会再被悄悄吞掉。
- **真实跑通记录**（两个 bug 都修完之后的那次跑，不是第一次假阳性的那次）：
  run [`35693372847`](https://github.com/nativeas/jones-agent/actions/runs/35693372847)。
  - `arm64`（macos-14）：06:06:26–06:08:15，共 1:49；`uname -m` 实测
    `arm64`；最终 bundle 354M（压缩后 artifact 124,764,102 字节）；日志里
    看得到脚本自己的收尾消息 `built: .../build/release/daemon/arm64`，
    证明真的跑到了最后一步（含 hermes-agent `git archive`、
    `bin/jones-daemon` 入口脚本、`DEPENDENCY-REPORT.md`），不是中途假装
    成功。
  - `x64`（macos-15-intel）：06:06:27–06:12:16，共 5:49；`uname -m` 实测
    `x86_64`（`uname -a` 也确认内核是 `RELEASE_X86_64`）；日志里明确可见
    `Building cryptography==50.0.0` → `Built cryptography==50.0.0`
    （06:08:23–06:11:10，实打实编译了 2:47，Rust 工具链是 runner 自带的
    `cargo 1.98.0`/`rustc 1.98.0`，不是本分支另外装的）；最终 bundle
    335M（压缩后 artifact 112,115,965 字节）。
- **hermes-agent 源码树怎么来的**：`build-daemon-bundle.sh` 原本从
  `daemon/pyproject.toml` 的 `[tool.uv.sources]`（本机绝对路径
  `/Users/nativeas/.hermes/hermes-agent`）读 checkout 位置，CI runner 上
  这条路径不存在——脚本已经支持的 `HERMES_AGENT_PATH` 环境变量覆盖正好是为
  这种情况留的口子，工作流里 `git clone` 公开仓库
  `NousResearch/hermes-agent`、切到 `daemon/pyproject.toml` 锁定的那个
  commit（`ee4452991d17534aa561f31ee55596d082aa94e7`），指过去。
- **这一步没做、留给后续的**：CI 只出「daemon bundle」这一层产物（两个
  `daemon-bundle-<arch>` artifact），不是完整签名 `.dmg`——`scripts/
  release/build-mac.sh` 后面 electron-builder 打包 + ad-hoc 签名那几步
  没有接入这个工作流（Issue #36 原文只要求"CI 出 x86_64 daemon bundle 证
  明可行"，没有要求完整发布管线上 CI）。真正出 `Jones-<version>-x64.dmg`
  仍需要本地或后续另一个工作流跑 `build-mac.sh`，把 CI 产出的
  `daemon-bundle-x64` 换掉本机现在造不出来的那一份、或者本身就在
  `macos-15-intel` runner 上原生跑完整的 `build-mac.sh x64`（未验证，
  `build-mac.sh` 目前假定单机跑完两条架构，没有按 runner 拆分过）。
- **结论**：v1.0 macOS 双架构（G18）的构建阻塞（#36）解除，走的是方案
  (a)，不需要方案 (b)（降版本，已被 hermes-agent 的 exact pin 挡死）或
  方案 (c)（v1.0 只发 Apple Silicon）。剩下的是 G18 验收本身要求的「一台
  真实 Intel Mac 上手工烟测」，见 `docs/acceptance/v1.0/G18.md`——那一步
  本质上无法被 CI 的 macos-15-intel runner 代替（Issue #36 的教训就是
  "runner 名字里有 intel 不代表能免验证"，这次是真机 x86_64，但完整应用
  层的手工烟测是另一件事）。

## 3. 签名与公证（本机无 Developer ID Program 账号）

- ad-hoc 签名（`--sign -`）已跑通（见 §2.1）；`spctl --assess` 必然 `rejected`
  ——这是预期行为，不是 bug（ad-hoc 身份不是 Apple 信任的 Developer ID）。
- **正式发布所需步骤**（本机未执行，留档，见 `packaging/sign/sign-adhoc.sh`
  末尾注释与 `packaging/sign/notarize.sh`）：
  1. Apple Developer Program（$99/年）→ 生成 **Developer ID Application** 证书，
     导入 Keychain。
  2. `packaging/release/electron-builder.yml` 的 `mac.identity: null` 换成
     `"Developer ID Application: <名称> (<TEAMID>)"`——让 electron-builder
     走它自己的 Frameworks/Helpers 签名逻辑；**不要**照搬 `sign-adhoc.sh` 的
     `--deep` 一把梭（Apple TN3125：`--deep` 不该用于正式签名，只用于校验/
     调试）。
  3. `Resources/daemon` 下的 Python 解释器与依赖仍需要单独的「由内向外」签名
     （`extraResources` 不是 electron-builder 认的标准代码位置，第 2 步不会
     碰它）——`afterPack.cjs`/`sign-adhoc.sh` 的遍历逻辑可以直接复用，但要
     去掉 `--timestamp=none`（改用默认安全时间戳，notarytool 要求）、
     `--sign -` 换成真实 Developer ID、去掉 entitlements 里的
     `disable-library-validation`（改为把这些 `.dylib` 也用同一 Team ID
     重签）。**`afterPack.cjs` round-1 评审后已改为在检测到非空 `mac.identity`
     时直接 fail fast（不会静默继续跑 ad-hoc 签名）**——这一步必须先在
     `afterPack.cjs` 里实现并用真实证书验证过（本仓库从未做过），否则第 2 步
     一换证书，打包就会在这里硬停，而不是悄悄产出一个仍然 ad-hoc 签名的
     `Resources/daemon`。
  4. `xcrun notarytool store-credentials` 存 App 专用密码（不落明文）→
     `xcrun notarytool submit *.dmg --keychain-profile <profile> --wait`。
  5. `xcrun stapler staple *.dmg`。
  6. `spctl --assess --type open --context context:primary-signature -v *.dmg`
     应输出 `accepted, source=Notarized Developer ID`。
- 追踪：[Issue #33](https://github.com/nativeas/jones-agent/issues/33)（spike 02
  已记录：`codesign --verify --deep --strict` 不覆盖 `Resources/daemon`，正式
  发布前这条风险仍未退休）。

## 4. 升级迁移（PRD 11.3、N17、G19）

- **旧版打开新库拒绝启动**：`daemon/src/jones_daemon/store/migrator.py`
  `apply_pending()` 在 `schema_version` 高于本 build 已知的最高迁移版本时抛
  `SchemaTooNewError`，`__main__.py` 捕获后记结构化日志并让进程以非零退出码
  终止——不静默降级、不尝试带着不认识的表结构继续跑。单测：
  `daemon/tests/test_migrator.py::
  test_apply_pending_refuses_a_database_newer_than_this_build_knows`。
- **端到端验证**：`daemon/tests/test_migrator_upgrade_e2e.py`。用本分支创建的
  本地 tag `v0.9-pre`（打在本分支与 `main` 的 merge-base 上，**未 push** ——
  见报告「升级迁移」一节）通过真实 `git worktree` + 独立 `uv sync` 拉起「上一版」
  daemon，产生真实数据（主会话、默认 Project/Agent、一个额外 Session），再用
  当前版本打开同一个 `JONES_HOME`，断言 Project/Agent/Session 集合（含主会话）
  完整不丢。**本仓库尚未发布过 v1.0**，`v0.9-pre` 与当前 HEAD 目前用的是同一套
  migrations（W6 本身不加 schema 变更），所以这条 e2e 验证的是「跨 build 打开
  同一个 JONES_HOME 数据完整」这个属性，不是一次真正跨 schema 版本的迁移——
  真正的版本落差场景由上一条（`SchemaTooNewError`）的单测覆盖，用合成的
  migrations 目录构造出「db 版本 > 本 build 已知最高版本」的场景。
- 备份：升级前自动备份已有（`_backup_before_migrating`，W2/#7 落地，本分支未改
  这部分逻辑，只加了上面的版本校验拒绝钩子）。

## 5. 已知缺口 / 没做什么（诚实写，不是回避）

- **首次安装时自动 `service install`**：本分支只把 `service install
  --program <path>` 这条命令在打包产物上验证跑得通（daemon 自己的 CLI），
  没有把「首次启动时自动调用它、把 LaunchAgent 装进
  `~/Library/LaunchAgents/`」接进 Electron 安装流程——那是完整安装器/首次运行
  向导的范畴，超出 05-w6-interfaces.md §1 给 Q 的授权（只允许改「打包模式下
  daemon 可执行路径的解析」）。`apps/desktop/src/main/index.ts` 现在做的是
  01-w2-interfaces.md §6 原文已经指定的兜底：`ensureDaemonRunning()` 重试序列
  最后一步直接 `spawn` 打包产物里的 daemon 可执行文件（不经过 launchd）。
- **公证/正式签名未执行**：本机没有 Apple Developer 账号，见 §3。
- **x86_64 未实机验证**：见 §2.2，本机没有 Rosetta。
- **图标**：本分支未提供自定义 `.icns`，用 electron-builder 默认图标——纯打包
  管线问题，不在 §1 授权范围内新增视觉资产。
- **`packaging/electron-shell/`（spike 产物）与 `packaging/release/`（本分支的
  真实打包配置）并存**：前者是 Issue #2 的技术验证痕迹，按 05-w6-
  interfaces.md §0「不加功能」的同一精神，本分支不删除/合并它——它仍是
  §1「根因更正」表格可复现的依据。
