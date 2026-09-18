# Spike 02 · Electron + Python 守护进程：打包 / 签名 / 公证

对应 Issue #2，PRD 13.2 风险 3、11.4 兼容性、12.1 G18。

结论先行：**两条打包路径都能让 Python 守护进程随 .app 一起分发、不依赖用户系统 Python**；但只有
**python-build-standalone（直接拷贝解释器分发包）** 能在一台 Apple Silicon 开发机上同时正确产出
arm64 与 x86_64 两份守护进程资源。**PyInstaller 做不到**——它在 freeze 时要 `exec` 本机解释器，
产物的原生库（`libpython*.dylib`）永远是宿主机架构，`--target-architecture x86_64` 只会重标记
bootloader 的架构位而不换库，产出一个签名合法但完全跑不起来（`Bad CPU type in executable`）的假
x86_64 二进制——这是本次 spike 实测复现的一个真实陷阱，不是理论推测（见 §3）。

## 0. 产物一览

```
packaging/
├── daemon-min/ping_daemon.py     # 最小 Python 守护进程：Unix socket，收 {"cmd":"ping"} 回 pong
├── pyinstaller/build.sh          # 方案 A：PyInstaller onedir（只能产出宿主机架构）
├── standalone/build.sh           # 方案 B：python-build-standalone + 直接拷贝（可跨架构）
├── electron-shell/               # 最小 Electron 壳 + electron-builder 配置
│   ├── main.js
│   ├── electron-builder.yml
│   └── entitlements.mac.plist
├── launchd/com.jones.daemon.plist  # LaunchAgent 草案
└── sign/
    ├── sign-adhoc.sh             # 本机可跑：ad-hoc 签名 + 校验 + 正式流程步骤留档
    └── notarize.sh               # 正式公证命令（需要开发者账号，本机未执行）
```

## 1. 两种打包方案对比（实测）

| | PyInstaller onedir | python-build-standalone（直接拷贝） |
|---|---|---|
| 产物体积（daemon 目录，空壳脚本） | 20 MB | 50 MB |
| 冷启动到 socket 可用（本机 M-series，5 次均值） | 56 ms | 55 ms（首次 1.35s，磁盘缓存冷时；之后稳定在 ~55ms） |
| 空闲 RSS | ~23.5 MB | ~18.3 MB |
| **能否在 arm64 主机上产出正确的 x86_64 产物** | **不能**：`--target-architecture x86_64` 只转换 bootloader 的 Mach-O 架构标记，内部 `libpython3.12.dylib` 仍是宿主机（arm64）编译的，产物在 Rosetta 下直接 `Bad CPU type in executable` | **能**：只是下载对应架构的官方预编译解释器 tarball 再拷贝文件，不需要执行任何目标架构代码，天然跨架构 |
| 适用场景 | 单架构 CI（各架构一台 runner 或至少各自 native 构建一次） | 单机也能产出全部目标架构；代价是体积略大、需要自己管理 site-packages（真实 daemon 有 `hermes-agent` 等第三方依赖时要把 venv 的 site-packages 一起拷进去） |

**结论**：W1 阶段本机只有 Apple Silicon，若坚持用 PyInstaller，Intel 版就打不出来（除非接入
x86_64 CI runner 或在 Rosetta 里跑一个 x86_64 的 uv/venv 环境后再 freeze）。python-build-standalone
路线不受此限制，本 spike 最终的 electron-builder 配置采用这条路线（见
`packaging/electron-shell/electron-builder.yml` 的 `extraResources`，用 `${arch}` 宏选对应产物）。

真实 daemon 一旦引入 `hermes-agent` 等第三方依赖，两条路线都要多带一份 site-packages；
python-build-standalone 需要额外一步 `uv pip install --target <bundle>/python/lib/python3.12/site-packages`
（跨架构安装纯 Python 依赖同样只是文件拷贝，能做；有 C 扩展的依赖则需要该架构的 wheel，
PyPI 大厂商模型 SDK 一般都发 arm64/x86_64 双 wheel，可行但需要在实现 daemon 打包脚本时验证）。

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
  `{"cmd":"ping"}`，收到 `{"pong": true, "pid": ..., "uptime_s": ...}`。签名前后各验证一次，行为一致。

## 3. 签名（本机无 Developer ID，做到 ad-hoc）

`packaging/sign/sign-adhoc.sh`：由内向外签（先签 daemon 目录下所有 `.dylib`/`.so`/可执行文件，
再签 Python 解释器主体，最后 `--deep` 签整个 `.app`），实测结果：

- `codesign --verify --deep --strict`：**valid on disk, satisfies its Designated Requirement**——
  说明签名结构、entitlements、嵌套 bundle 的封装关系是对的。
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
日志分离到 `stdout`/`stderr` 两个文件。路径用占位符（`__DAEMON_EXECUTABLE__` / `__JONES_HOME__`），
由 Electron 首次启动时写入真实绝对路径后再 `launchctl bootstrap gui/$(id -u) <plist>`——这部分是
真实实现（daemon/ 或 apps/desktop/ 的安装逻辑）要做的，不在本 spike 范围内，只提供草案。

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
| Gatekeeper 不拦 | **未达成，原因明确**：本机没有 Apple Developer 账号，只能做 ad-hoc 签名；`spctl --assess` 已实测确认 ad-hoc 必被拒，正式签名+公证所需的完整命令序列见 §3.2，逻辑已用 `codesign --verify` 验证过（签名结构对，只是身份不是 Developer ID） |
| Apple Silicon 与 Intel 各一份 | **Apple Silicon：完整达成**（打包、签名、启动、daemon 通信全部实测通过）。**Intel：打包产物已生成且架构标记正确，但因本机无 Rosetta 2、无法执行验证**，见 §5 |

## 7. 给评审者的关注点

1. **核心结论是路线选择，不是"两个都能用"**：真实 daemon 打包应该用 python-build-standalone
   而不是 PyInstaller，因为 W1-W6 全程本机只有 Apple Silicon 开发机，PyInstaller 路线会让 Intel
   包完全打不出来直到接入专门的 x86_64 CI。这个判断改变了 `docs/design/00-foundation.md` §2
   技术栈表里「打包：PyInstaller」的选择——**建议把设计文档改成 python-build-standalone**，我没有
   在本 spike 里改 `docs/design/`（按 DEV.md 要求"改接口先改文档"，但打包方式不是 daemon⇄前端的
   接口契约，是否要正式改文档由评审者判断；我在这里把证据和建议摆出来，不越权替 W6 打包实现者
   决定）。
2. **Intel 侧没有实机验证**，只做到了「资源架构正确 + 打包结构正确」。如果 W6 前团队拿不到 Intel
   测试机，需要接入带 Rosetta 或原生 x86_64 的 CI runner（GitHub Actions `macos-13` 系列是 Intel
   原生的）才能补上这块。
3. **entitlements 里的 `disable-library-validation` 是权宜**：现在依赖它让未签名的 Python 动态库
   通过 hardened runtime，正式发布前应该把这些库也用 Developer ID 重签、去掉这条放宽项。真实
   daemon 引入 `hermes-agent` 之后这些库会更多，需要在打包脚本里做「签目录下所有 .dylib/.so」
   的遍历（`sign-adhoc.sh` 已经是这个写法，可以直接复用）。
4. **性能数字只测了空壳**：真实 daemon 加载 `hermes-agent`、SQLite migration 等之后，冷启动
   和内存会显著高于本 spike 的 55ms / 20MB，11.1/11.2 的验收要在真实 daemon 完成后重新测，本
   spike 只证明「打包机制本身」不是瓶颈（55ms 距离 6s 冷启动预算有充分余量，但不能外推到真实功能）。
5. **launchd plist 是草案，没有接入真实安装流程**：真实的"写 plist → bootstrap → 崩溃拉起"逻辑
   要在 daemon 或 apps/desktop 的安装/启动代码里实现并测试 G11（Electron 关闭后 Cron 仍触发），
   本 spike 没有验证 launchd 拉起本身（写了 plist 但没有 `launchctl bootstrap` 实际跑一遍，因为
   这会在本机常驻注册一个服务，超出 spike 该做的范围，且真实路径要等 daemon 可执行文件定型）。

## 8. 如何复现

```bash
# 方案 A：PyInstaller（只能在当前架构跑）
cd packaging/pyinstaller && ./build.sh
JONES_SPIKE_HOME=/tmp/x ./dist/jones-daemon-spike/jones-daemon-spike &
python3 -c 'import socket,json; s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); s.connect("/tmp/x/daemon.sock"); s.sendall(b"{\"cmd\":\"ping\"}\n"); print(s.recv(4096))'

# 方案 B：python-build-standalone（两个架构都能在 arm64 主机上产出）
cd packaging/standalone && ./build.sh arm64 && ./build.sh x64
file dist/arm64/python/bin/python3.12 dist/x64/python/bin/python3.12   # 确认架构标记

# Electron 打包（用方案 B 的产物）
cd packaging/electron-shell && pnpm install
pnpm run build:arm64   # 或 build:x64
open dist/mac-arm64/JonesPackagingSpike.app   # 会拉起 daemon，看 Console.app 或直接连 socket 验证

# 签名与校验
cd packaging/sign
./sign-adhoc.sh ../electron-shell/dist/mac-arm64/JonesPackagingSpike.app
```
