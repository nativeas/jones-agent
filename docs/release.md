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
`cd daemon && make check-daemon` + `cd apps/desktop && pnpm run perf`，并核对
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
     重签）。
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
