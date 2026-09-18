# Spike 02 · Electron + Python 守护进程：打包 / 签名 / 公证

对应 Issue #2，PRD 13.2 风险 3、11.4 兼容性、12.1 G18。

结论先行：**两条打包路径都能让 Python 守护进程随 .app 一起分发、不依赖用户系统 Python**，本
Issue 最终采用 **python-build-standalone（直接拷贝解释器分发包）**。

**更正（评审后重新实测，见 §1 末尾「根因更正」）**：早期版本的本文档把这个选择的唯一支柱写成
「PyInstaller 在 macOS 上无法跨架构」，并归因为 PyInstaller 的固有限制。**这个归因是错的**。
真正的约束是：PyInstaller 从基础解释器的 `libpython`/`Python` 动态库里**提取**目标架构的切片，
如果基础解释器是 thin（单架构）build，就没有 x86_64 切片可提取，`--target-architecture x86_64`
只会转换 bootloader 的 Mach-O 架构标记、内部动态库仍是宿主机架构——这不是"PyInstaller 做不到
跨架构"，而是"喂给它单架构的解释器就得不到多架构的输出"。**给它一个真正 universal2（同时含
arm64 + x86_64 切片）的基础解释器，PyInstaller 能正确产出跑得起来的 x86_64 二进制**——已实测
验证，见下方「根因更正」。

选 python-build-standalone 的真实理由（见下）不是"PyInstaller 做不到"，而是它跟本仓库已有的
`uv` 工具链管理解释器的方式一致、不需要额外的管理员权限安装步骤。

## 0. 产物一览

```
packaging/
├── daemon-min/ping_daemon.py     # 最小 Python 守护进程：Unix socket，收 {"cmd":"ping"} 回 pong
├── legacy/pyinstaller/build.sh   # 方案 A：PyInstaller onedir（本机基础解释器是 thin arm64，只能产出宿主机架构；给 universal2 基础解释器能跨架构，见 §1 根因更正）。**控制者裁定打包方案定案为 python-build-standalone，本脚本移到 legacy/ ——不是生产路径，留着只为 §1 对比表格 / 根因更正结论可复现**
├── standalone/build.sh           # 方案 B：python-build-standalone + 直接拷贝（可跨架构）
├── electron-shell/               # 最小 Electron 壳 + electron-builder 配置
│   ├── main.js
│   ├── electron-builder.yml
│   └── entitlements.mac.plist
├── launchd/
│   ├── com.jones.daemon.plist     # LaunchAgent 草案
│   └── install.sh                 # 生成真实 plist + 预创建日志目录（不执行 launchctl bootstrap，见 §4）
└── sign/
    ├── sign-adhoc.sh             # 本机可跑：ad-hoc 签名 + 校验 + 正式流程步骤留档
    └── notarize.sh               # 正式公证命令（需要开发者账号，本机未执行）
```

## 1. 两种打包方案对比（实测）

冷启动 / 空闲 RSS 数字用 `packaging/daemon-min/measure.sh <daemon 可执行文件> [重复次数，默认 3]`
复现。

**计时方法（第二轮评审后修正）**：daemon 自己在 socket 进入可 accept 状态的那一刻，把
`time.monotonic_ns()` 写进结构化日志（`ping_daemon.py` 的 `started` 事件，`ready_monotonic_ns`
字段）；`measure.sh` 只用「spawn 前自己取的 monotonic_ns」减「daemon 报告的 ready
monotonic_ns」，不再像早期版本那样反复 spawn 一个 Python 探测子进程去轮询"socket 能不能连上"
——那种做法会把每次探测子进程自身的 fork+exec 开销（数十毫秒量级）计进"冷启动"数字里，两条
打包路线之间几毫秒到十几毫秒的差值在这个噪音面前没有意义（评审记录第二轮意见 1）。修正后
轮询循环只用来判断"该不该继续等"（读文件 + grep，不再 spawn 解释器），不参与计时。

（`ping_daemon.py` 里没有 `boot_s` 字段——早期版本试过在进程内部用两行代码之间的
`time.time()` 减法测冷启动，那个数字恒为 0，因为 exec 之前的时间进程自己根本看不到，
已删掉，改成上面这种外部 spawn 时刻 + 内部 ready 时刻各自上报、事后相减的方式。）

| | PyInstaller onedir | python-build-standalone（直接拷贝，已裁剪 tcl/tk） |
|---|---|---|
| 产物体积（daemon 目录，空壳脚本） | 20 MB | 39 MB（裁剪前 50MB，见下方修复记录：standalone 默认带 tcl/tk，headless daemon 用不到，PyInstaller 会自动裁剪、之前不是同口径对比） |
| 冷启动到 daemon 自报 ready（`measure.sh` 复现，3 次取中位数，spawn→ready，见上方「计时方法」） | ~28 ms | ~28 ms |
| 空闲 RSS（同一批 3 次运行，取中位数） | ~23.2 MB | ~17.9 MB |
| **能否在 arm64 主机上产出正确的 x86_64 产物** | **取决于基础解释器**（见下方「根因更正」）：给 `uv python install cpython-3.12-macos-x86_64-none` 这种 thin 解释器——不能，`--target-architecture x86_64` 只转换 bootloader 的 Mach-O 架构标记，内部 `libpython3.12.dylib` 仍是宿主机（arm64）编译的；给 python.org 官方 universal2 安装器装出来的解释器——**能**，已实测确认内部 `Python`/`.so` 全部是真正的 x86_64 切片 | **能**：只是下载对应架构的官方预编译解释器 tarball 再拷贝文件，不需要执行任何目标架构代码，天然跨架构，且直接用本仓库已有的 `uv python install`，不需要额外装什么 |
| 拿到可用基础解释器的方式 | 需要 python.org 官方 **universal2** 安装器（`.pkg`，装到 `/Library/Frameworks`，需要管理员权限）——`uv python install` 不提供 universal2 build，只有 thin per-arch build | `uv python install cpython-3.12-macos-{aarch64,x86_64}-none`，本仓库其他地方（`daemon/`）已经在用 uv 管理解释器，零额外步骤，不需要管理员权限 |
| 适用场景 | 有 universal2 基础解释器时单机可跨架构；否则退化为单架构 CI（各架构一台 runner，或至少各自 native 构建一次） | 单机零配置产出全部目标架构；代价是体积略大、需要自己管理 site-packages（真实 daemon 有 `hermes-agent` 等第三方依赖时要把 venv 的 site-packages 一起拷进去） |

**修正后的冷启动数字说明**：两条路线的 spawn→ready 耗时在修正计时方法后都是 ~28ms，
统计上没有可辨的差异——这符合预期，因为两条路线用的都是官方 CPython 解释器（一个是官方
预编译 tarball，一个是 PyInstaller 打包同一份宿主机解释器），冷启动瓶颈是 CPython 解释器
自身的初始化，不是打包机制。**选型不基于冷启动数字**（两者打平），理由仍然是上面写的
工具链一致性（见下方「结论」）。

**结论**：两条路线都能在 arm64 开发机上产出正确的 x86_64 产物，**但前提不同**。PyInstaller 需要
一个 universal2 基础解释器，本仓库的解释器管理工具 `uv`（`daemon/` 已经在用）不提供这种 build，
只能额外装 python.org 的官方安装器（需要管理员权限，且这个安装器脱离了 uv 的解释器版本管理，
后续升级 Python 版本要多维护一条路径）。python-build-standalone 直接用 `uv python install
<target-triple>` 就能拿到对应架构的解释器，和仓库其余部分的解释器管理方式一致，零额外步骤、
不需要管理员权限。**这是本 spike 最终选 python-build-standalone 的真实理由**——不是"PyInstaller
在 macOS 上做不到跨架构"（这个说法不成立，见下方根因更正），是"给定本仓库已选定的工具链（uv），
python-build-standalone 零额外步骤，PyInstaller 需要多一条脱离 uv 管理的安装路径"。

本 spike 最终的 electron-builder 配置采用 python-build-standalone（见
`packaging/electron-shell/electron-builder.yml` 的 `extraResources`，用 `${arch}` 宏选对应产物）。

真实 daemon 一旦引入 `hermes-agent` 等第三方依赖，两条路线都要多带一份 site-packages；
python-build-standalone 需要额外一步 `uv pip install --target <bundle>/python/lib/python3.12/site-packages`
（跨架构安装纯 Python 依赖同样只是文件拷贝，能做；有 C 扩展的依赖则需要该架构的 wheel，
PyPI 大厂商模型 SDK 一般都发 arm64/x86_64 双 wheel，可行但需要在实现 daemon 打包脚本时验证）。

### 根因更正：PyInstaller 能否跨架构，取决于基础解释器是不是 universal2

评审指出：本文档早期版本把"PyInstaller 在 macOS 上无法跨架构"写成 PyInstaller 的固有属性，但
`build.sh` 用的基础解释器（`uv venv --python 3.12` 解析到
`~/.local/share/uv/python/cpython-3.12.12-macos-aarch64-none/bin/python3.12`）本身就是 thin
arm64 build——PyInstaller 在 macOS 上的跨架构支持前提是基础 CPython 必须是 universal2 build
（它从 fat 二进制里按需提取对应架构的切片，不是凭空生成机器码），给它一个 thin 解释器，产出
"标签对、内容错"的假 x86_64 产物正是预期行为，不是 PyInstaller 的 bug。这个对照实验此前没做，
本次评审后补上：

**对照实验**（在本分支之外的 scratchpad 里做的，不影响本分支任何产物）：

1. 下载 python.org 官方 `python-3.12.8-macos11.pkg`（universal2 安装器），确认
   `lipo -info` 显示 `x86_64 arm64` 两个真实架构切片（不是重标记）。
2. 该安装器默认要装到 `/Library/Frameworks/Python.framework`（需要 root）；本机无 sudo，
   用 `pkgutil --expand-full` 解包到本地目录，再用 `install_name_tool -change` 把所有
   引用 `/Library/Frameworks/Python.framework/...` 的 Mach-O（`bin/python3.12`、
   `Resources/Python.app/Contents/MacOS/Python`、`_ssl`/`_hashlib` 等扩展模块，共 13 个文件）
   改成指向本地路径，`codesign --sign -` 重签——这是本机无管理员权限时的变通做法，**正常开发
   机上直接 `sudo installer -pkg python-3.12.8-macos11.pkg -target /` 不需要这些步骤**。
3. 用这个可运行的 universal2 解释器建 venv、装 `pyinstaller==6.11.1`，对
   `packaging/daemon-min/ping_daemon.py` 跑 `--target-architecture x86_64`：
   ```
   $ file dist/jones-daemon-spike-x86/_internal/Python
   dist/.../_internal/Python: Mach-O 64-bit dynamically linked shared library x86_64
   $ file dist/jones-daemon-spike-x86/_internal/lib-dynload/_socket.cpython-312-darwin.so
   dist/.../_socket.cpython-312-darwin.so: Mach-O 64-bit bundle x86_64
   ```
   `_internal/Python`（真正的 libpython）和所有扩展模块都是**真实的 x86_64 切片**，不是重标记。
4. 对照组：用原来 `build.sh` 那个 thin arm64 解释器（`uv python install
   cpython-3.12-macos-aarch64-none`）重复同一条 PyInstaller 命令：
   ```
   $ file dist/jones-daemon-spike-thinrepro/_internal/libpython3.12.dylib
   dist/.../libpython3.12.dylib: Mach-O 64-bit dynamically linked shared library arm64
   ```
   复现了原文档的发现：bootloader 可执行文件标签是 x86_64，内部 `libpython3.12.dylib`
   却是 arm64——这正是"基础解释器是 thin build"导致的，不是 PyInstaller 在 macOS 上
   跨架构能力的固有缺陷。

（本机仍然没有 Rosetta 2，两组产物都无法在本机实际跑起来验证运行时行为——这一限制与更正前
一致，没有变化，见 §5、§6。）

**结论没有翻转，理由整个换了**：本 spike 仍然建议用 python-build-standalone（见上），但不是
因为"PyInstaller 做不到"，是因为它更贴合本仓库已经选定的 uv 工具链、不需要在 CI/开发机上
额外安装一个脱离 uv 管理的 python.org universal2 解释器。如果团队后续决定要接入 x86_64 CI
runner，或者不介意让 uv 之外的官方安装器进入构建链路，PyInstaller + universal2 基础解释器
是一条同样可行的路径。

## 2. Electron 集成：extraResources 跑通

`packaging/electron-shell/`：最小 Electron 33+ 兼容壳（本机用 43.4.0，因为该版本已在本机缓存，
避免重复下载 120MB+；`apps/desktop/` 真实实现仍按设计文档锁 33+，两者互不影响）。

- `electron-builder.yml` 的 `mac.extraResources` 把 `packaging/standalone/dist/${arch}` 整个目录
  拷进 `<App>.app/Contents/Resources/daemon/`。`${arch}` 是 electron-builder 内置宏，`--arm64` /
  `--x64` 构建时自动展开，一份配置覆盖两个架构。
- `main.js` 用 `spawn(Resources/daemon/run.sh)` 拉起守护进程，环境变量传 `JONES_SPIKE_HOME` 指向
  `app.getPath("userData")` 下的运行时目录（真实实现会是 `~/.jones/runtime`）。
- **实测跑通**：`open JonesPackagingSpike.app` → Electron main 进程 spawn 守护进程 → 用独立 Python
  脚本连 `~/Library/Application Support/jones-packaging-spike/spike-runtime/daemon.sock` 发
  `{"cmd":"ping"}`，收到 `{"pong": true, "pid": ..., "uptime_s": ...}`。

  **更正**：ping_daemon.py 的探活逻辑是「socket 能连上就直接退出，不抢占」（见 §0 的 daemon-min），
  main.js 原来只挂 `window-all-closed`，Cmd+Q 走的是 `app.quit()` → `will-quit`，不会先发
  `window-all-closed`（Electron 文档明确的行为），旧 daemon 会变成孤儿进程继续占着 socket。
  这意味着「签名前后各验证一次，行为一致」这个结论有被污染的风险：如果第一次验证后用 Cmd+Q
  关闭、daemon 没被杀掉，第二次 `open .app` 时新 spawn 的 daemon 会因为探活到旧实例而立刻退出，
  外部脚本连上的其实是上一轮的旧进程，签名后的验证可能根本没有跑在签名后的二进制上。
  已修复 `main.js`（`will-quit` 也挂清理逻辑，`spawnDaemon` 打印 `spawned daemon pid=...`），
  验证时应对比这个 pid 与 `pong` 响应里的 `pid` 字段，确认连的是本轮刚起的实例，不是遗留进程
  （见「修复记录」）。

## 3. 签名（本机无 Developer ID，做到 ad-hoc）

`packaging/sign/sign-adhoc.sh`：由内向外签（先签 daemon 目录下所有 `.dylib`/`.so`/可执行文件，
再签 Python 解释器主体，最后 `--deep` 签整个 `.app`），实测结果：

- `codesign --verify --deep --strict`：**valid on disk, satisfies its Designated Requirement**——
  但这条只证明 `.app` 本体与 `--deep` 会遍历的标准嵌套位置（`Contents/Frameworks` 下的
  Framework / Helper.app 等）签名结构是对的。**实测确认它不覆盖 `Contents/Resources/daemon`**：
  在本分支产物上跑 `codesign -dv --deep --strict --verbose=4`，输出里涉及
  `Resources/daemon` 路径的条数是 0——`--deep` 只走 Apple 认识的标准嵌套 code 位置，
  `Resources` 下的文件只是作为资源被哈希封装进外层签名，不会被当作独立代码校验。
  daemon 载荷本身（Python 解释器与其 `.dylib`/`.so`）的签名结构是否正确，这条命令
  证明不了——真正会做这项检查的是 notarytool 扫描（见 §3.2 与 Issue #33）。
- `spctl --assess --type execute`：**rejected**——这是**预期行为**，不是 bug。ad-hoc 签名
  （`--sign -`）没有 Apple 信任的 Developer ID，Gatekeeper 必然拒绝，任何本机没有付费开发者账号的
  情况下都会是这个结果。签名后重新 `open` 验证：daemon 依旧能正常拉起并回 pong，说明签名（含
  hardened runtime + entitlements）没有破坏功能。

### 3.1 Hardened Runtime entitlements（`entitlements.mac.plist`）

PyInstaller/python-build-standalone 打的 Python 解释器与其扩展模块不是我们自己编译签名的，
Hardened Runtime 默认会拦。用到的三项：

| entitlement | 为什么需要 |
|---|---|
| `com.apple.security.cs.allow-jit` | CPython 部分 C 扩展运行时分配可执行内存 |
| `com.apple.security.cs.allow-unsigned-executable-memory` | CPython 解释器自身运行时写可执行内存段 |
| `com.apple.security.cs.disable-library-validation` | bundle 里的 `.dylib`/`.so` 不是用我们的 Team ID 签的；正式发布前应该用同一 Team ID 重签这些库、去掉这条以收紧安全面，而不是长期依赖它 |

### 3.2 正式签名 + 公证需要的步骤（本机无证书，未执行，留档在 `sign-adhoc.sh` 末尾注释 + `notarize.sh`）

1. Apple Developer Program（$99/年）→ 生成 **Developer ID Application** 证书，导入本机 Keychain。
2. 用 `--sign "Developer ID Application: <名称> (<TEAMID>)"` 重复 §3 的由内向外签名流程；去掉
   `disable-library-validation`，改为把 PyInstaller/standalone 打出的所有 `.dylib` 用同一个 Team ID
   重签。
3. 打 `.dmg`（electron-builder 的 dmg target 已自动做）。
4. `xcrun notarytool store-credentials` 把 App 专用密码存进 Keychain（不落明文，呼应本仓库
   「凭据不落明文」原则），然后 `xcrun notarytool submit *.dmg --keychain-profile <profile> --wait`。
5. `xcrun stapler staple *.dmg`——离线也能通过 Gatekeeper。
6. `spctl --assess --type open --context context:primary-signature -v *.dmg` 应输出
   `accepted, source=Notarized Developer ID`。

## 4. launchd LaunchAgent 草案

`packaging/launchd/com.jones.daemon.plist`：`RunAtLoad` + `KeepAlive.Crashed=true`（崩溃拉起，
正常退出不拉起，避免用户主动 quit 后被强行复活）、`ThrottleInterval=10` 防止崩溃死循环耗电、
日志分离到 `stdout`/`stderr` 两个文件、`ProcessType=Interactive`（第二轮评审更正，理由见 plist
内注释与修复记录第 6 条：daemon 走普通 Unix socket 服务 Electron 的交互式 RPC，不经过 XPC，
`Adaptive` 档依赖的 Mach 重要性传导用不上，`Interactive` 是不依赖调用方式的显式声明）。路径用
占位符（`__DAEMON_EXECUTABLE__` / `__JONES_HOME__`）。

`packaging/launchd/install.sh`（第二轮评审新增）：把 plist 里的占位符替换成真实路径、写到
`~/Library/LaunchAgents/`、**在此之前先 `mkdir -p` 日志目录**（launchd 不会为
`StandardOutPath`/`StandardErrorPath` 自动建父目录，目录不存在时 job 要么 bootstrap 直接报错，
要么静默丢日志，行为随 macOS 版本而异，不可依赖）。跟 plist 本身一样是草案：本脚本写到
"生成 plist + 建目录"为止，不实际执行最后一步 `launchctl bootstrap`——那会在本机常驻注册一个
真实服务，超出 spike 范围；真实的"首次启动时安装"逻辑最终要写进 apps/desktop/ 的 Electron
启动代码（daemon 可执行文件路径届时是已知的），这里只提供可读的参考实现，供本 spike 验证
日志目录预创建这一步本身是否正确（已用一次性临时 `JONES_HOME` 跑通，见下方「修复后验证」）。

## 5. 各架构出包与实测数据

| | Apple Silicon (arm64，本机原生) | Intel (x86_64) |
|---|---|---|
| daemon 打包方式 | python-build-standalone，直接在本机产出 | python-build-standalone，同一份配置换 `--x64` 参数产出（不需要 Intel 机器或 Rosetta——见 §1） |
| daemon 二进制架构校验 | `file` 确认 `Mach-O 64-bit executable arm64` | `file` 确认 `Mach-O 64-bit executable x86_64` |
| .app 体积 | 344 MB | 359 MB |
| .dmg 体积 | 131 MB | 144 MB |
| 端到端验证（spawn daemon → socket ping → pong） | **通过**（本机可执行） | 结构验证通过（资源路径、架构标记正确，`Contents/MacOS/JonesPackagingSpike` 与 `Resources/daemon/python/bin/python3.12` 均确认为 `Mach-O 64-bit executable x86_64`）；**无法在本机执行**——本机未装 Rosetta 2（`arch -x86_64 /usr/bin/true` 报 `Bad CPU type`），冷启动/内存的 Intel 实测数据需要一台真实 Intel Mac 或装了 Rosetta 的 CI 跑 |
| ad-hoc 签名 + `codesign --verify` | 通过 | 通过（`valid on disk, satisfies its Designated Requirement`，由内向外签名流程与 arm64 完全一致） |
| Gatekeeper（`spctl --assess`） | rejected（ad-hoc 非 Developer ID，预期行为） | rejected（同样实测确认，签名机制不区分架构） |

daemon 空壳本身的冷启动/内存数据（§1 表格）是在本机 arm64 上实测的；Intel 参考机
（PRD 11.1 指定 Intel i5 12 代 / 16GB）需要真机复测，架构层面预期一致（standalone 方案两边用的是
同一份 CPython 官方发行版，没有理由有数量级差异），但不应把 arm64 实测数值直接当 Intel 验收依据。

## 6. Issue #2 验收对照

| 验收项 | 结果 |
|---|---|
| 全新 mac 上安装并启动成功 | **未验证**——需要真实的全新 mac（本机是开发机，有大量已装软件/缓存），也需要正式签名+公证（ad-hoc 包在全新 mac 上会被 Gatekeeper 挡住，这不是「安装启动失败」而是「没有开发者证书的必然结果」，已在 §3 复现并解释） |
| Gatekeeper 不拦 | **未达成，原因明确**：本机没有 Apple Developer 账号，只能做 ad-hoc 签名；`spctl --assess` 已实测确认 ad-hoc 必被拒，正式签名+公证所需的完整命令序列见 §3.2。`codesign --verify` 只验证了 `.app` 本体与标准嵌套位置的签名结构，**不能**证明 daemon 载荷（`Resources/daemon` 下的 Python 解释器与其动态库）的签名/entitlements/hardened runtime 是对的——见 §3 的更正。这条风险实际尚未退休，后续追踪见 [Issue #33](https://github.com/nativeas/jones-agent/issues/33) |
| Apple Silicon 与 Intel 各一份 | **Apple Silicon：完整达成**（打包、签名、启动、daemon 通信全部实测通过）。**Intel：打包产物已生成且架构标记正确，但因本机无 Rosetta 2、无法执行验证**，见 §5 |

## 7. 给评审者的关注点

1. **核心结论是路线选择，不是"两个都能用"**：真实 daemon 打包用 python-build-standalone 而不是
   PyInstaller，理由是它和本仓库已选定的解释器管理工具 `uv` 一致、零额外步骤、不需要管理员权限
   （见上方「根因更正」）——**不是**"PyInstaller 在 macOS 上做不到跨架构"，那个说法已被证明不成立
   （给它一个 universal2 基础解释器，PyInstaller 一样能产出正确的 x86_64 产物，已实测）。
   `docs/design/00-foundation.md` §2 技术栈表已经在本次修复里同步改成 python-build-standalone，
   理由写的是这条更正后的真实理由，不是被撤回的错误断言。
2. **Intel 侧没有实机验证**，只做到了「资源架构正确 + 打包结构正确」。如果 W6 前团队拿不到 Intel
   测试机，需要接入带 Rosetta 或原生 x86_64 的 CI runner（GitHub Actions `macos-13` 系列是 Intel
   原生的）才能补上这块。
3. **entitlements 里的 `disable-library-validation` 是权宜**：现在依赖它让未签名的 Python 动态库
   通过 hardened runtime，正式发布前应该把这些库也用 Developer ID 重签、去掉这条放宽项。真实
   daemon 引入 `hermes-agent` 之后这些库会更多，需要在打包脚本里做「签目录下所有 .dylib/.so」
   的遍历（`sign-adhoc.sh` 已经是这个写法，可以直接复用）。
4. **性能数字只测了空壳**：真实 daemon 加载 `hermes-agent`、SQLite migration 等之后，冷启动
   和内存会显著高于本 spike §1 表格的数字（~28ms / ~18-24MB，`measure.sh` 可复现），11.1/11.2
   的验收要在真实 daemon 完成后重新测，本 spike 只证明「打包机制本身」不是瓶颈（这个量级距离
   6s 冷启动预算有充分余量，但不能外推到真实功能）。
5. **launchd plist + `install.sh` 是草案，没有接入真实安装流程**：`install.sh`（第二轮评审新增）
   把"生成真实 plist + 预创建日志目录"这一步做成可复现脚本，但真实的"写 plist → bootstrap →
   崩溃拉起"完整逻辑要在 daemon 或 apps/desktop 的安装/启动代码里实现并测试 G11（Electron 关闭后
   Cron 仍触发），本 spike 没有验证 launchd 拉起本身（`install.sh` 故意不执行最后一步
   `launchctl bootstrap`，因为这会在本机常驻注册一个服务，超出 spike 该做的范围，且真实路径要等
   daemon 可执行文件定型）。

## 8. 如何复现

```bash
# 方案 A：PyInstaller（只能在当前架构跑）
cd packaging/legacy/pyinstaller && ./build.sh
JONES_SPIKE_HOME=/tmp/x ./dist/jones-daemon-spike/jones-daemon-spike &
python3 -c 'import socket,json; s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); s.connect("/tmp/x/daemon.sock"); s.sendall(b"{\"cmd\":\"ping\"}\n"); print(s.recv(4096))'

# 方案 B：python-build-standalone（两个架构都能在 arm64 主机上产出）
cd packaging/standalone && ./build.sh arm64 && ./build.sh x64
file dist/arm64/python/bin/python3.12 dist/x64/python/bin/python3.12   # 确认架构标记

# Electron 打包（用方案 B 的产物）
cd packaging/electron-shell && pnpm install
pnpm run build:arm64   # 或 build:x64
open dist/mac-arm64/JonesPackagingSpike.app   # 会拉起 daemon，控制台会打印 "spawned daemon pid=<PID>"
python3 -c 'import socket,os; s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); \
  s.connect(os.path.expanduser("~/Library/Application Support/jones-packaging-spike/spike-runtime/daemon.sock")); \
  s.sendall(b"{\"cmd\":\"ping\"}\n"); print(s.recv(4096))'
# 把 pong 里的 pid 和控制台打印的 "spawned daemon pid=" 对比，确认连的是这一轮刚起的实例，
# 不是上一轮遗留的孤儿 daemon（见 §2 的更正）。验证完用 Cmd+Q 或 window 关闭退出，
# 确认 daemon 也退出了（ps 里找不到、~/Library/.../spike-runtime/daemon.sock 消失）。

# 冷启动 / 空闲 RSS（spawn→daemon 自报 ready，3 次取中位数，见 §1「计时方法」）
packaging/daemon-min/measure.sh packaging/standalone/dist/arm64/run.sh
packaging/daemon-min/measure.sh packaging/legacy/pyinstaller/dist/jones-daemon-spike/jones-daemon-spike

# 签名与校验
cd packaging/sign
./sign-adhoc.sh ../electron-shell/dist/mac-arm64/JonesPackagingSpike.app
```
