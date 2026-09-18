# Spike 04 · 浏览器复用登录态（Chrome / Edge）

对应 Issue #4、PRD FR09、13.2 风险 4、12.3 FR09 验收口径。

> 本文件已按评审意见修订（见文末「修复记录」「修复记录·第二轮」）。FR09 的
> 接口契约草案已移到 `docs/design/00-foundation.md` 第 8 节——按 `docs/DEV.md`
> 目录所有权表，`docs/spikes/` 只承载验证报告，契约的家是 `docs/design/`。
>
> **第二轮修订提醒**：下表「推荐」一栏里 a) CDP attach 的**技术结论**
> （必须是 Jones 自己拉起、全程不重启的浏览器进程；不能字面接管用户当前
> Chrome；等等）仍然成立，但**由谁实现这条 CDP 客户端代码**这一点变了——
> 控制者裁定 v1 不再由 Jones 自研 `browser.*` 工具集，改为复用现成浏览器
> MCP Server（Playwright MCP）+ 同一个 Jones 专属 profile，理由与实测证据见
> `docs/design/00-foundation.md` §9。本文件的 (a)/(b)/(c) 三条路径实测结论
> 全部不受这次变更影响——它们回答的是「Chrome/Edge 在这种用法下会怎么表现」，
> 跟「这段自动化代码是 Jones 自己写还是复用别人写好的 MCP Server」是两个
> 独立的问题。

## 结论先行

| 路径 | 可行性 | 对用户的侵入性 | 稳定性 | 推荐 |
|---|---|---|---|---|
| a) CDP attach（Jones 专属常驻 profile） | **可行**，实测通过（Chrome + Edge 均实测，见下） | 见「关键约束」——不能白嫖用户已开着的默认 Chrome，需要一个**由 Jones 自己拉起、全程不重启**的浏览器进程 | 高（CDP 是 Chromium 十年未变的调试协议，跨版本极稳；本次在 Chrome 153 与 Edge 153 上都验证通过） | **v1 推荐** |
| b) 复制 profile 目录 | **看 cookie 类型而定**——persistent cookie 实测可行；session-only cookie 实测不可行；读真实用户 profile 的系统权限、Keychain 加密解密两点仍未验证（见下「方案 (b) 补充实验」与「Keychain 加密解密」两节） | 高：需要读另一个 App 的私有数据目录 | 中：对持久登录态可靠，对会话态不可靠，且额外依赖两个未验证前提 | v1 不采用（见理由），非「结构性不可行」的一刀切结论 |
| c) 扩展 + chrome.debugger | **未验证**（本次修复实测过一次，被 Chrome 当前 stable 频道的一个 GUI-only 开关挡住，见下） | 中：需要用户安装扩展、手动开一次开发者模式、容忍常驻调试提示条 | 未知 | v1 不做，记录为需要人工继续跑的 P1+ 待验证项，**不是**「技术可行但成本太高」的定论 |

**推荐方案不变（但第二轮起，这段 CDP 客户端代码由谁写变了——见文首提醒与 §8）**：Jones 自己维护一个**专属 Chrome/Edge profile**（不是用户日常用的 Default profile），首次使用浏览器能力时用 `--remote-debugging-port=0`（随机端口，或复用 MCP Server 自带的等价机制）+ 该专属 `--user-data-dir` 冷启动一个 Chromium 进程，此后所有 turn 都对这个**同一个、常驻的**进程反复挂接；不主动重启它。用户在这个 Jones 专属窗口里登录一次要用到的网站。（这一段描述的 CDP attach 机制本身仍然成立，只是第二轮起「谁来拉起这个进程、谁来发 CDP 请求」从「Jones 自研代码」改成了「复用的浏览器 MCP Server」——daemon 变成这个 MCP Server 的调用方，不再自己直接调 Playwright/CDP。）**但这次修复把 (b) 的结论从「结构性不可行」改成了「对部分登录态可行、但不满足覆盖所有登录场景的可靠性要求」——这是证据支撑的降级，不是文字游戏**：详见下面「方案 (b) 补充实验」。选 (a) 而不是 (b) 的理由也相应从「(b) 做不到」改成了「(a) 对所有登录态类型都均匀有效，(b) 只对一部分有效，且 (b) 还有两个本次没能验证掉的额外前提」。FR09 的接口草案见 `docs/design/00-foundation.md` §9。

## 为什么不是字面意义的「接管用户当前 Chrome」——三个实测事实

1. **默认启动的 Chrome 不暴露 CDP 端口。** 探测 9222/9229/9333，用户正常打开的 Chrome 一个都没开（见 `browser_probe.py --step probe`）。CDP 端口必须在**进程启动时**通过 `--remote-debugging-port` 传入，运行时无法动态开启。
2. **往已运行的 profile 上补开调试端口会被单实例锁吞掉。** 实测：用户 Chrome 已经用某个 `user-data-dir` 跑着时，再拿同一个 `user-data-dir` + `--remote-debugging-port=0` 启动第二个进程，这个新进程会被 Chrome 的单实例锁（SingletonLock/SingletonSocket）检测到、把命令行转发给已运行实例、然后**立即退出（returncode 0）**——新加的 flag 被直接忽略，不会有任何 `DevToolsActivePort` 文件被写出、不会监听任何端口（`browser_probe.py --step singleton`，复现 100%，这是 Chromium 的既定行为，不是环境噪音；本轮修复把这一步从硬编码 9222 改成了随机端口 + 判断 `DevToolsActivePort` 是否出现，结论不变，但校验方式更严谨，见「修复记录」#7）。
   - 结论：要让 CDP 端口生效，**必须让 Chrome 从零冷启动**（先退出现有进程，或本来就没在跑）。没有「不打扰用户正在开着的窗口、偷偷挂上调试口」这条路。
3. **多数登录态是 session-only cookie，冷启动/退出重开本身就会丢掉它；但持久（persistent）cookie 不受此影响。** 见下「方案 (b) 补充实验」——这一条本次做了实质性修订。

**因此**：对所有登录态类型都均匀可靠的，只有「Jones 自己拉起、自己全程持有、从不重启」的浏览器进程，通过 CDP 反复挂接。这与 FR09 原文「复用用户已登录的浏览器态」在字面上有一处偏差——PRD 已同 PR 更新（FR09 表述、12.3 验收口径），不再是本文件单方面的「理解」。

## 实测通过的部分（CDP attach，Chrome + Edge）

`browser_probe.py --step attach`：

1. 冷启动 `Google Chrome --user-data-dir=<临时目录> --remote-debugging-port=0`（随机端口）。
2. 从 `<临时目录>/DevToolsActivePort` 读实际端口；用 `lsof -iTCP:<port> -sTCP:LISTEN` 核对监听该端口的 pid 确实是刚拉起的这个进程（不是假设端口没被别人占用——评审 #7）。
3. `playwright.chromium.connect_over_cdp("http://127.0.0.1:<port>")`，拿到已有 context，登录 the-internet.herokuapp.com/login。
4. `browser.close()`（只关 CDP 连接，不发 kill）——Chrome 进程仍存活（`pgrep` 确认）。
5. **新开一个独立的 playwright 进程/连接**，再次 `connect_over_cdp` 到同一个端口，直接访问 `/secure`，无需重新登录，读到 `Secure Area`。

这模拟的正是 daemon 的真实使用形态：daemon 常驻，每次 `session.send` 触发浏览器动作时新开一次 CDP 连接、干完活断开，Chrome 进程本身在多个 turn、多个 session 之间不重启，登录态天然保留在这一个进程里。

**本轮修复新增：同一套流程在 Microsoft Edge 153.0.4234.46（arm64，`Chrome for Testing` 之外，用 `pkgutil --expand-full` 非侵入式解出的官方 .pkg，没有安装到 `/Applications`）上重跑了一遍**——第一次跑暴露了一个真实的 Edge 特有坑：Edge 冷启动会自带一个 `edge://sync-confirmation-dialog` tab，如果这时候 `ctx.new_page()` 开一个新的后台 tab 去登录，表单提交会被浏览器的后台 tab 节流吞掉（复现 100%，登录静默失败，`flash` 显示"You must login…"而不是登录成功）。改成复用 `ctx.pages[0]`（就地 `goto` 覆盖掉已有 tab）后，Chrome 与 Edge 上都稳定通过。这个坑已经写进 `browser_probe.py` 的注释和 `docs/design/00-foundation.md` §9 的实现约束里，不是「Edge 基本等同 Chrome」这种空判断能预见的。

## 方案 (b) 补充实验：session-only vs persistent cookie，加一个公平对照组

原报告的决定性实验只覆盖了 the-internet.herokuapp.com 的 `rack.session`——这是 `is_persistent=0` 的会话态 cookie，且 (a) 全程不重启、(b) 强制重启，两者不是公平对照。本轮修复补了两件事（`browser_probe.py --step copy`，完整输出见下）：

1. **给 (b) 加一个「直接重启同一 profile、不做任何复制」的对照组**，把「重启这件事本身丢状态」和「复制这个操作额外丢状态」分开看。
2. **加一个 persistent cookie 场景**：脚本自带一个本地测试站（`http.server`，只监听 `127.0.0.1`，不发外部请求、不涉及任何真实账号），`/login` 签发 `Max-Age=86400` 的 cookie（`is_persistent=1`），`/secure` 校验它——这是「记住我」类登录态的最小复现，也是真实用户默认 Chrome profile 里最常见的登录态形态。

实测输出（节选，完整可复现）：

```
-- 场景: session-only cookie (the-internet.herokuapp.com) --
[退出后复制] rack.session 行是否还在: found=True has_expires=0 is_persistent=0
[重启同一 profile / 不复制的对照组] 落地 URL = https://the-internet.herokuapp.com/login   <- 直接重启也丢
[退出后复制] 用复制出的 profile 打开受保护页，落地 URL = https://the-internet.herokuapp.com/login  <- 复制也丢

-- 场景: persistent cookie (本地测试站) --
[退出后复制] session_probe 行是否还在: found=True has_expires=1 is_persistent=1
[重启同一 profile / 不复制的对照组] 落地 URL = http://127.0.0.1:8899/secure   <- 直接重启不丢
[退出后复制] 用复制出的 profile 打开受保护页，落地 URL = http://127.0.0.1:8899/secure  <- 复制也不丢！
```

> **第二轮修复重跑（评审 #3）**：原来的登录步骤对本地测试站场景用 `ctx.new_page()`
> 开新 tab 去登录，且从不校验登录是否真的成功——这正是 `step_cdp_attach` 里已经
> 实测过、会被后台 tab 节流静默吃掉表单提交的同一个坑（见上「实测通过的部分」一节），
> 只是这里原来没人踩过所以没暴露。已修：改用复用已有 tab（与 `step_cdp_attach`
> 一致），并在登录动作后立刻校验目标 cookie 是否真的出现在 context 里，不出现则
> 直接抛异常中止（不带着一个没登录成功的 profile 继续跑，诚实失败）。重跑
> `uv run daemon/spikes/browser_probe.py --step copy` 后的真实输出（端口已改随机，
> 见评审 #4）：
>
> ```
> -- 场景: session-only cookie (the-internet.herokuapp.com) (cookie=rack.session) --
>   登录表单提交结果: You logged into a secure area!
>   已在源 profile 登录（session-only cookie (the-internet.herokuapp.com)），已校验 cookie 'rack.session' 存在。
>   [运行中复制] 复制出的库里能读到 rack.session: False has_expires=-1 is_persistent=-1
>   [重启同一 profile / 不复制的对照组] 直接重启后访问受保护页落地 URL = https://the-internet.herokuapp.com/login
>   [退出后复制] 复制出的库里 rack.session 行是否还在: found=True has_expires=0 is_persistent=0
>   [退出后复制] 用复制出的 profile 打开受保护页，落地 URL = https://the-internet.herokuapp.com/login
> -- 场景: persistent cookie (本地测试站) (cookie=session_probe) --
>   已在源 profile 登录（persistent cookie (本地测试站)），已校验 cookie 'session_probe' 存在。
>   [运行中复制] 复制出的库里能读到 session_probe: False has_expires=-1 is_persistent=-1
>   [重启同一 profile / 不复制的对照组] 直接重启后访问受保护页落地 URL = http://127.0.0.1:53687/secure
>   [退出后复制] 复制出的库里 session_probe 行是否还在: found=True has_expires=1 is_persistent=1
>   [退出后复制] 用复制出的 profile 打开受保护页，落地 URL = http://127.0.0.1:53687/secure
> ```
>
> **重跑后结论不变**：数值和落地 URL 与原输出一致（端口从原来的固定 8899 换成
> 这次实际分配到的 53687，是随机端口生效的正常表现，不是结果差异）。多出来的
> 「已校验 cookie ... 存在」「登录表单提交结果」两行是新增的登录成功校验证据，
> 不是新结论。

**读法**：
- session-only 场景里，「不复制、只重启」**同样**丢登录态——证明原结论「关浏览器即失效」是 cookie 生命周期的固有属性，不是复制这个动作额外造成的，原来「结构性问题」这个判断对 session-only cookie 是成立的、有公平对照支撑的。
- persistent 场景里，「不复制、只重启」保住了登录态（预期内——同一个 profile），**但「退出后复制到一个全新目录、再用这个复制出的目录起进程」也保住了登录态**——这与原报告「复制方案结构性不可行」的一刀切结论直接矛盾。持久 cookie 在磁盘上是完整可复制的，方案 (b) 对这类登录态**实测可行**。
- 「运行中复制」（不等浏览器退出）在两种场景下都读不到目标 cookie（`found=False`），包括 persistent 场景——这一点原报告的「未落盘/竞态」解释不能再直接套用了，因为本轮已经把 `_inspect_session_cookie` 的 WAL 边车文件复制漏洞修掉了（见「修复记录」#9），持久 cookie 修完之后仍然读不到，说明运行中这个时间点上 Chrome 确实还没把这行写进它自己进程可见的库文件（可能在内存/独立于 WAL 的其他缓冲里），这一点本轮没有继续深挖（不在评审要求范围内，运行中复制从一开始就不是被推荐的时机），如实标注为未完全解释、不影响退出后复制这条决定性证据。

**因此方案 (b) 的最终定性**：不是「结构性不可行」，而是「对 persistent cookie 的登录态可行，对 session-only cookie 的登录态不可行，且即使只考虑 persistent 场景，仍有两个本轮没能验证掉的前提——见下」。v1 不选 (b) 的理由相应改写为：(b) 的可靠性依赖用户具体网站用的是哪种 cookie（对 Jones 不透明、逐站点不同），(a) 对所有站点均匀有效；即使只做 persistent 场景，(b) 还依赖「读真实用户 Default profile 的系统权限」（本次探针执行环境里被拒绝，`OSError: Operation not permitted`，真实 daemon 是否会撞到同样的墙未知）与「解密 Keychain 里的 Chrome Safe Storage 密钥」（见下，本次因为工具权限限制没有尝试，不是因为判断它没问题）两个都没有被消灭的不确定性，而 (a) 不依赖这两者。这是一个基于证据的取舍，不是回避。

## Keychain 加密解密：本次未尝试，原因是工具权限限制而非疏漏

原报告认定「cookie value 被 Keychain 加密」是方案 (b) 的一个额外结构性障碍，但没有实测过解密能否成功。本轮修复过程中尝试执行 `security find-generic-password -s "Chrome Safe Storage"` 去读 macOS Keychain 里的 `Chrome Safe Storage` 密钥（这是 Chromium 在 mac 上加密 cookie value 用的 AES key 来源），被这次修复所在的 Claude Code 会话的自动模式分类器直接拒绝执行（判定为访问系统 Keychain 的敏感操作）。这是一个合理的、不应绕过的工具权限限制，因此如实标注：**Keychain 解密这一步在本次修复里仍未验证，既不是「已确认会挡住」也不是「已确认不会挡住」**，跟原报告一样是未知项，只是现在有了「为什么没测」的明确记录，而不是被简单略过。

## c) 扩展 + chrome.debugger 路径尝试记录（本轮新增，未跑通）

原报告只做了案头分析。本轮修复实际写了代码验证，遇到一个真实的环境阻塞点：

- 写了一个最小 MV3 扩展（`daemon/spikes/ext_probe/`）：`background.js` 在被加载时自动登录 the-internet.herokuapp.com、导航到 `/secure`、用 `chrome.debugger.attach` + `Runtime.evaluate` 读页面内容、把结果 POST 到本地上报服务器——这正是评审要求的「实测 chrome.debugger 能否在用户已登录窗口上读到受保护页」。
- 实测：`chrome --load-extension=<dir> --user-data-dir=<全新临时 profile>`（**不带** `--remote-debugging-port`，模拟一个正常、非调试模式的用户窗口）之后，扩展**完全没有被注册**——`Preferences` 里 `extensions.settings` 长度为 0，不是「加载了但被禁用」，是命令行 flag 被静默忽略。
- 进一步实测：在 Chrome 从未启动过的全新 profile 里，提前手工把 `Preferences` 写入 `extensions.ui.developer_mode: true` 再启动，同样无效（该值在 Chrome 启动后被清空/不是正确开关）。
- 结论：**当前 stable 频道 Chrome 加载未打包扩展，要求用户先在 `chrome://extensions` 页面手动点一次「开发者模式」这个网页内 UI 开关**——这是一个纯 GUI 交互，没有可靠的命令行或配置文件旁路。本次执行环境没有可用的 GUI 点击/界面自动化工具，止步于此。
- 扩展源码、完整复现步骤与手动跑完的说明见 `daemon/spikes/ext_probe/README.md`，交给下一个有 GUI 权限的人 10 分钟内可以跑完并把结果补回本节。**这是「未验证」，不是原报告写的「技术上可行但成本远超 v1 范围」——后者是一个没有代码验证支撑的判断，本轮已经把它改成了诚实的未知状态。**

## 版本兼容性

本机验证环境：macOS 26（Tahoe，Darwin 27.0.0，arm64）。

**Chrome 153.0.8010.50（当前 stable）**：全部 step 实测通过。

**Microsoft Edge 153.0.4234.46（当前 stable）**：本轮新增实测，`attach` 全部 step 通过（含发现并修掉的后台 tab 节流坑，见上）。用的是 `pkgutil --expand-full` 从官方 .pkg 里非侵入式解出的 `.app`，没有安装进 `/Applications`、不影响主机环境。

**Chrome 次新大版本（152.x）**：本轮尝试通过 [Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/)（Google 官方提供、专门用于自动化测试的历史版本 Chrome 二进制，不是 Chromium 社区构建，是同一个 Chrome）下载 152.0.7977.82 的 mac-arm64 包（187MB）做真正的跨大版本回归——这条路径本身是可行的（不需要卸载重装、不影响主机现有 Chrome，解压即用），比原报告「本机只有一个版本，没法测」的说法更进了一步。但下载过程中 `storage.googleapis.com` 在本次执行环境的网络条件下异常缓慢（实测约 150 KB/s，与同一会话里 Microsoft 官方 CDN 下载 430MB 的 Edge 安装包只用了不到两分钟形成明显对比，怀疑是这一具体 bucket/域名被本机网络环境限速，而非通用带宽不足），187MB 包在本轮修复的时间预算内没有下完，因此没有跑成。**如实标注为「尝试但未完成」，不是「未尝试」也不是「装作测过」**——机制已经找到并验证可行，遗留的只是这次会话没能等到下载结束；下一次有更宽松时间预算或更快网络路径的人可以直接复现：`curl -L https://storage.googleapis.com/chrome-for-testing-public/152.0.7977.82/mac-arm64/chrome-mac-arm64.zip -o chrome152.zip && unzip chrome152.zip -d chrome152 && uv run daemon/spikes/browser_probe.py --step attach --chrome "$(pwd)/chrome152/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"`。

**Edge 次新大版本**：未尝试（Microsoft 没有提供类似 Chrome for Testing 的历史稳定版下载渠道，需要卸载重装当前版本才能测次新版，成本明显更高，本轮时间预算内没有做）。

基于公开、稳定的事实做出的判断（非实测，仅对未覆盖到的版本组合适用）：
- CDP（Chrome DevTools Protocol）自 `Page`/`Target`/`Network` 这几个 v0 就存在的 domain 起，跨 Chromium 大版本高度向后兼容；Playwright/Puppeteer/Selenium 等主流自动化工具长期依赖它，Chromium 团队对外部工具依赖的核心 domain 的破坏性变更极少。
- 建议支持范围：**Chrome / Edge 最近两个稳定大版本**（当前对应 ~152–153），与 PRD 13.2 风险表的口径一致。

## 安全含义（cookie 泄露面）

- CDP 一旦开启，`127.0.0.1:<port>` 上任何能连到该端口的本机进程都能拿到浏览器的完整控制权（包括读 cookie、执行任意 JS、截屏）。**必须**：
  - 绑定 `127.0.0.1`，不监听 `0.0.0.0`（与 DEV.md「守护进程不开放入站网络端口」的精神一致，浏览器调试口虽由 Chrome 自己监听，daemon 也要保证不做端口转发/暴露）。
  - 用随机端口（`--remote-debugging-port=0`，Chrome 会把实际端口写进 `DevToolsActivePort` 文件）而非固定端口，**并且 attach 前用 lsof 核对监听该端口的确实是自己刚拉起的进程**（本轮补的校验，评审 #7——否则理论上会连到同机另一个恰好也在用调试端口的浏览器实例）。
  - Jones 专属 profile 与用户日常 profile 物理隔离（不同 `user-data-dir`），用户在 Jones 窗口里的登录动作是显式、可见、用户自己做的操作，不是 Jones 偷偷拿走已有 cookie——这也规避了「偷别的 App 数据目录」在安全评审上的观感问题。
  - Step（工具调用）执行日志/回放里绝不记录完整 cookie 值，只记录域名/动作摘要，与 PRD N02（凭据明文）对齐。
- Profile 复制方案在安全上更差，且不只是「实测拿不到登录态」这一个理由（这一点本轮已改写为「看 cookie 类型」）：需要读取另一个 App 的私有数据目录（Cookies、Login Data 等 SQLite 库，部分字段用 macOS Keychain 里的 "Chrome Safe Storage" 密钥加密——本轮确认这一步无法在当前工具权限下验证，见上），一旦 Jones 进程有这个读权限，理论上也能读到用户其它未预期要给浏览器能力的敏感 cookie（银行、邮箱等），攻击面/误用面比「用户在 Jones 专属窗口里主动登录」大得多。

## 验收对照（Issue #4）

- [x] 能访问一个需登录页面并读取内容 —— `browser_probe.py --step attach`，Chrome + Edge 均实测通过（登录页 the-internet.herokuapp.com，非真实用户账号；见上）。
- [ ] 明确支持的 Chrome / Edge 版本范围 —— Chrome 153、Edge 153 均为本机当前 stable 实测；但「最近两个稳定大版本」这个验收口径要求的次新大版本（Chrome 152.x、Edge 次新版）本轮都没有实测完成（见「版本兼容性」：Chrome 152 下载在时间预算内没跑完，Edge 次新版本身没有可用渠道）。**评审第二轮 #1（诚实）：这条原来打了 [x]，但正文自己写的是「未完全验证覆盖度」——勾选和文字互相矛盾，已改成 [ ]。** 只测过当前 stable 这一个大版本，不满足「明确支持范围」这个验收项要求的完整覆盖，发布前需要补跑（复现命令见「版本兼容性」一节）。
- [ ] 不可行则 FR09 降 P1 —— **不适用**：CDP attach 路径可行（Chrome + Edge 均实测），FR09 保持 P0；FR09 的表述与 12.3 验收口径已同 PR 更新为「Jones 专属常驻 profile，登录一次后复用」，不再是本文件单方面的语义澄清。

## 没做 / 已知缺口（本轮更新）

- **Edge 次新大版本**：未测（见「版本兼容性」）。
- **Chrome 次新大版本（152.x）**：本轮尝试下载 Chrome for Testing 152.0.7977.82 做真实回归，下载在会话时间预算内没有跑完（网络异常慢，见「版本兼容性」），机制已验证可行、复现命令已留下，下一轮直接跑即可。
- **扩展 + chrome.debugger（路径 c）**：本轮写了可运行的验证代码，但被 Chrome stable 频道要求手动开发者模式这一 GUI-only 开关挡住，未跑通，已把扩展源码和手动复现步骤留在 `daemon/spikes/ext_probe/`，如实标注为「未验证」。
- **真实用户 Default profile 的读取**：本次 spike 执行环境本身无法读取 `~/Library/Application Support/Google/Chrome`（`OSError: Operation not permitted`），因此复制方案的测试用的是脚本自建的临时 profile，不是用户真实 profile。真实 daemon（launchd 用户级常驻进程，非本次探针所在的沙箱环境）是否会撞到同一堵墙未知。
- **Keychain 加密解密**：本轮尝试实测，被本次修复所在会话的工具权限策略直接拒绝执行（访问系统 Keychain 密钥），如实标注为「未验证」而非「假定会挡住」，见上专节。
- 没有触碰 `daemon/` 目录里除 `daemon/spikes/` 之外的任何文件——`daemon/` 骨架本身归属另一个并行 Issue（W1 里的 daemon skeleton），本 spike 严格按目录所有权规则只新增了 spike 脚本本身，未创建 `pyproject.toml`/包结构，避免和骨架 Issue 打架。

## 如何复现验证

```bash
cd daemon
uv run spikes/browser_probe.py --step all      # 依次跑 probe / singleton / attach / copy
uv run spikes/browser_probe.py --step attach    # 只跑核心的 CDP attach 验证
uv run spikes/browser_probe.py --step attach --chrome "/path/to/Microsoft Edge"   # 换浏览器
uv run spikes/browser_probe.py --step copy      # session-only vs persistent cookie 对照
```

需要本机已装 Google Chrome（默认路径 `/Applications/Google Chrome.app/...`，可用 `--chrome` 指定别的路径/Edge）、可联网访问 `the-internet.herokuapp.com`（公开的自动化测试站，非任何真实账号）；`--step copy` 额外会在本机随机端口（评审第二轮 #4：不再硬编码 8899，避免端口被占用时莫名其妙绑定失败）起一个只服务 `127.0.0.1` 的本地测试站（脚本自带，不发外部请求）。脚本自行拉起/清理它起的 Chrome 进程和临时 profile 目录，用 try/finally 保证异常时也不残留（评审 #8；评审第二轮 #4 把 `_run_copy_scenario` 内部也补上了同样的兜底——原来只有函数最外层一处清理，中途登录校验失败等异常会跳过清理），不触碰、不读取用户的真实 Chrome profile。

## 修复记录（评审后，同分支 `w1/4-spike-browser-login`）

逐条对照评审意见处理，全部实测验证过，无静默忽略：

1. **[已处理]** FR09 语义偏差未同 PR 更新 PRD/design、接口契约放错目录。已同 PR 更新 `docs/PRD.md` FR09 表述（8.1 表）与 12.3 验收口径；接口契约整段从本文件移到 `docs/design/00-foundation.md` §9，本文件只保留结论引用。
2. **[已处理，结论改为诚实未知]** 路径 (c) 零代码验证却被当作「技术可行但成本高」的定论。本轮写了可运行的最小扩展并实测，发现真实阻塞点（Chrome stable 要求手动开发者模式），结论改写为「未验证」，附完整复现材料。
3. **[已处理，如实降级]** 版本兼容性验收项零实测却打勾。Edge 153 本轮补了完整实测（含发现并修掉一个真实坑）；Chrome 152 尝试下载但未在时间预算内完成；验收表相应改为未完全勾选，如实反映覆盖度。
4. **[已处理，结论实质性修订]** 方案 (b) 「结构性不可行」只测了 session-only 场景。本轮加了 persistent cookie 场景（脚本自带本地测试站）与「重启不复制」的公平对照组，实测结果显示 persistent cookie 场景下复制方案实际可行，(b) 的结论已从「结构性不可行」改写为「因 cookie 类型而异，且有两个未验证的额外前提」。
5. **[已处理]** 推荐给实现者的 `--remote-debugging-port=0` 配置从未跑过、写死 9222。`browser_probe.py` 的 `attach`/`singleton` 两个 step 均已改为 port=0 + 轮询 `DevToolsActivePort`，并实测通过（Chrome + Edge）。
6. **[已处理，第二轮已证明这条本身论证错误，见「修复记录·第二轮」#2]** 未评估复用 MCP 能力。~~已在 `docs/design/00-foundation.md` §9 补一段取舍论证：权限闸需要卡在「工具调用前」而不是「进程边界」，外部 MCP Server 是黑盒进程没有天然介入点；CDP 进程生命周期需要跟 daemon 自己的子进程收拢机制统一，交给外部 MCP Server 会分裂成两套生命周期。结论不变（v1 自研），但补上了论证。~~ **这段论证是事实错误**——daemon 本身就是 MCP Server 的调用方，权限闸只要长在 daemon 发起 `tools/call` 之前就天然介入了，不需要额外代理层。保留删除线而不是直接改写，是为了让这个错误判断本身可追溯（原文照抄画删除线，结论见下面第二轮记录，不是本条目自己悄悄改的）。
7. **[已处理]** 探针硬编码 9222、不校验端点归属。已改为随机端口 + `lsof` 校验监听该端口的确实是本进程拉起的 Chrome，未通过校验会主动中止而不是继续 attach。
8. **[已处理]** 三个 step 缺 try/finally，异常时泄漏进程和目录。`browser_probe.py` 全部三个会拉起浏览器进程的 step（`singleton`/`attach`/`copy`）已重写为 try/finally 包裹清理逻辑。
9. **[已处理]** cookie 检查只复制主 DB 文件、丢 -wal，因果结论证据不足。新增 `_copy_sqlite_with_wal`，复制 Cookies 时一并带上 `-wal`/`-shm` 边车文件再读；结合 #4 的 persistent cookie 实验，「is_persistent=0 场景下运行中复制读不到」这一点不再和「有没有复制到 WAL」这个变量混在一起（因为对 persistent cookie 场景应用同一套 WAL-aware 复制后，运行中复制仍然读不到，说明原因确实是数据尚未写入 Chrome 自己的库文件，不是探针的复制漏洞——见「方案 (b) 补充实验」小节最后一段）。

## 修复记录·第二轮（评审后，同分支 `w1/4-spike-browser-login`）

逐条对照第二轮评审意见处理，全部实测验证过，无静默忽略：

1. **[已处理]** 验收项打勾但文字承认未实测。`## 验收对照（Issue #4）` 里「明确支持的 Chrome / Edge 版本范围」一项原来是 `[x]`，但正文自己写的是「次新大版本本轮尝试但未在时间预算内完成」——勾选和文字互相矛盾。已改为 `[ ]` 并在该行内写清楚具体缺口（只测过当前 stable 一个大版本，不满足「最近两个稳定大版本」这个验收口径要求的覆盖度）。
2. **[已处理，结论反转]** 否决复用 MCP 的核心论证（「权限闸没有天然介入点，除非再插一层代理」）是事实错误：daemon 本身就是 MCP Server 的 `tools/call` 调用方，闸只要长在 daemon 发起这次调用之前就天然介入了。按控制者裁定（工程原则 1：不重写已有能力）重新调研 + 实测：
   - 实测 `@playwright/mcp@0.0.81` 与 `chrome-devtools-mcp@1.9.0` 两个候选，均能以指定 `--user-data-dir`/`--userDataDir` 启动 Chrome；用一段独立的 MCP stdio 客户端探针（`daemon/spikes/mcp_reuse_probe/`，新增）跑通「登录 → 完整杀掉 MCP server 子进程（不是只断连接）→ 重新起进程 → 免登录读受保护页」，并通过 `tools/call` 完成导航 + 读页。
   - 关键发现：Playwright MCP 暴露 `browser_close` 工具，调用后立刻把 cookie 落盘（实测：不调用直接杀进程，重启后 cookie 库里是空的；调用后才会出现）；chrome-devtools-mcp 没有等价原语——`close_page` 拒绝关闭最后一个页面，改用 `navigate_page` 导航到 `about:blank` 再杀进程、或开新页面后 `close_page` 关掉登录页，两种路径实测 cookie 均未落盘，只有等 Chromium 内部约 30s 的周期性 flush 才会自发落盘（同样实测确认：sleep 35s 后再杀进程，cookie 在）。这是本轮取舍的决定性证据，不是空对比。
   - 比较结果（工具粒度、维护活跃度、依赖体积）写入 `docs/design/00-foundation.md` §9.1 的表格；三项大体相当，不构成决定性差异，决定性差异是上面这条 cookie 落盘可控性。
   - 结论：v1 选型改为 **Playwright MCP + Jones 专属常驻 profile**，Jones 不再自研 `browser.*` 工具集。`docs/design/00-foundation.md` §9 已整段重写（旧的五个自研工具定义、旧的错误论证整段删除）；PRD `docs/PRD.md` FR09 一行（8.1 表）与 12.3 验收口径同 PR 更新，改动限制在语义澄清 + 权限分级口径这两处，没有扩大改动面。
3. **[已处理，重跑]** `step_profile_copy`（对应 `browser_probe.py` 的 `_run_copy_scenario`）登录步骤原来无条件 `ctx.new_page()`，撞的是和 `step_cdp_attach` 里同一个「新开后台 tab 表单提交被节流吞掉」的坑，且登录后从不校验是否真的成功。已修：复用已有 tab（`ctx.pages[0] if ctx.pages else ctx.new_page()`，三处调用点全改），登录后用「目标 cookie 是否出现在 context 里」做校验，失败则抛异常中止而不是带着假设继续跑。重跑 `uv run daemon/spikes/browser_probe.py --step copy`（以及完整 `--step all`）：**重跑后结论不变**——两种 cookie 场景的数值和落地 URL 与原报告一致，完整新输出见上文「方案 (b) 补充实验」小节内的第二轮重跑区块。
4. **[已处理]**
   - 本地测试站端口从硬编码 `8899` 改成 `("127.0.0.1", 0)` 由系统分配随机端口（`_local_login_server`），重跑已验证（上面 #3 的重跑输出里端口是随机分配到的 53687，不是 8899）。
   - `_run_copy_scenario` 原来只在函数最外层结尾调一次 `_kill_by_userdata(prof)`，中途（登录校验失败、copytree 报错等）抛异常会跳过它，且完全没覆盖它后面额外起的 `clean_copy` 这个 profile。已重写为外层 `try/finally` 统一兜底 `_kill_by_userdata(prof)` 与 `_kill_by_userdata(clean_copy)`，内层三个 `with sync_playwright()` 块各自的 `ctx.close()` 也都包进 `try/finally`，任意一步炸了都不漏清理；重跑 `--step all` 后确认没有残留的 `jones_browser_probe_*` 进程或临时目录（`pgrep`/`ls $TMPDIR` 均为空）。
   - 本文件（`docs/spikes/04-browser-login-state.md`）与 scratchpad 报告（`spike4-browser-report.md`）已同步更新，不再各自一份互相矛盾的记录。
