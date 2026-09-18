# Spike 04 · 浏览器复用登录态（Chrome / Edge）

对应 Issue #4、PRD FR09、13.2 风险 4、12.3 FR09 验收口径。

## 结论先行

| 路径 | 可行性 | 对用户的侵入性 | 稳定性 | 推荐 |
|---|---|---|---|---|
| a) CDP attach | **可行**，实测通过 | 见下「关键约束」——不能白嫖用户已开着的默认 Chrome，需要一个**由 Jones 自己拉起、全程不重启**的 Chrome 进程 | 高（CDP 是 Chromium 十年未变的调试协议，跨版本极稳） | **推荐** |
| b) 复制 profile 目录 | **不可行**（结构性问题，非权限问题能绕开的） | 高：需要读另一个 App 的私有数据目录 | 低：登录态是否带过去不确定、不可靠 | 不采用 |
| c) 扩展 + native messaging | 技术上可行，但成本远超 v1 范围 | 中：需要用户安装扩展、注册 native messaging host | 中 | v1 不做，记录为 P1+ 备选 |

**推荐方案**：Jones 自己维护一个**专属 Chrome/Edge profile**（不是用户日常用的 Default profile），daemon 首次使用浏览器能力时用 `--remote-debugging-port=0`（随机端口，避免多实例冲突）+ 该专属 `--user-data-dir` 冷启动一个 Chromium 进程，此后所有 turn 都对这个**同一个、常驻的**进程做 `connect_over_cdp`；不主动重启它。用户在这个 Jones 专属窗口里登录一次要用到的网站，之后的 turn 复用的是「Jones 自己的浏览器一直开着、一直登录着」，而不是字面意义上「接管用户此刻正在用的那个 Chrome 窗口」。FR09 的接口草案见文末。

## 为什么不是字面意义的「接管用户当前 Chrome」——三个实测事实

1. **默认启动的 Chrome 不暴露 CDP 端口。** 探测 9222/9229/9333，用户正常打开的 Chrome 一个都没开（见 `browser_probe.py --step probe`）。CDP 端口必须在**进程启动时**通过 `--remote-debugging-port` 传入，运行时无法动态开启。
2. **往已运行的 profile 上补开调试端口会被单实例锁吞掉。** 实测：用户 Chrome 已经用某个 `user-data-dir` 跑着时，再拿同一个 `user-data-dir` + `--remote-debugging-port=9222` 启动第二个进程，这个新进程会被 Chrome 的单实例锁（SingletonLock/SingletonSocket）检测到、把命令行转发给已运行实例、然后**立即退出（returncode 0）**——新加的 flag 被直接忽略，9222 端口始终没开（`browser_probe.py --step singleton`，复现 100%，这是 Chromium 的既定行为，不是环境噪音）。
   - 结论：要让 CDP 端口生效，**必须让 Chrome 从零冷启动**（先退出现有进程，或本来就没在跑）。没有「不打扰用户正在开着的窗口、偷偷挂上调试口」这条路。
3. **多数登录态是 session-only cookie，冷启动/退出重开本身就会丢掉它。** 用公开登录 demo（the-internet.herokuapp.com，账号 tomsmith，非任何真实用户账号）实测：登录成功后写入的 `rack.session` cookie 在 SQLite `Cookies` 表里 `is_persistent=0`、`has_expires=0`——这是标准的「关闭浏览器即失效」型 cookie，绝大多数网站（含大量真实登录场景）用的就是这种。围绕它做的两个复制实验：
   - **边运行边复制**：`shutil.copytree` 会在 `SingletonSocket` 等特殊文件上报错（需要过滤），且此时登录 cookie **往往还没落盘**（内存态，未 flush），复制出来的库里读不到。
   - **退出后复制**：cookie 行在磁盘上「是否还在」两次实测不一致（Chrome 的退出清理是异步的，不可靠），但**决定性的是最终结果**：无论行是否还在，用复制出的 profile 目录重新起一个 persistent context 访问受保护页，两次都被 302 回登录页——复制方案实测拿不到登录态。这不是权限没配对的问题，是 cookie 生命周期的结构性问题：会话态很大一部分根本不在磁盘上。
   - 另外，读用户真实 `~/Library/Application Support/Google/Chrome` 在本次 spike 的执行环境里直接被拒绝（`OSError: Operation not permitted`，errno 1，非 EACCES）——这是本机沙箱/权限设置对这次探针进程的限制，真实 daemon（launchd 用户级常驻进程，非 App Sandbox）通常不会撞到同一堵墙，但也说明「读别的 App 的私有数据目录」这条路本来就依赖一个不在 Jones 控制范围内的系统权限前提，进一步降低了方案 b 的确定性。

**因此**：真正稳定可行的只有「Jones 自己拉起、自己全程持有、从不重启」的 Chrome 进程，通过 CDP 反复挂接。这与 FR09 原文「复用用户已登录的浏览器态」在字面上有一处偏差，需要更新理解——见文末接口草案里的说明。

## 实测通过的部分（CDP attach）

`browser_probe.py --step attach`：

1. 冷启动 `Google Chrome --user-data-dir=<临时目录> --remote-debugging-port=9222`。
2. `playwright.chromium.connect_over_cdp("http://127.0.0.1:9222")`，拿到已有 context，登录 the-internet.herokuapp.com/login。
3. 显式 `browser.close()`（只关 CDP 连接，不发 kill）——Chrome 进程仍存活（`pgrep` 确认）。
4. **新开一个独立的 playwright 进程/连接**，再次 `connect_over_cdp` 到同一个端口，直接访问 `/secure`，无需重新登录，读到 `Secure Area`。

这模拟的正是 daemon 的真实使用形态：daemon 常驻，每次 `session.send` 触发浏览器动作时新开一次 CDP 连接、干完活断开，Chrome 进程本身在多个 turn、多个 session 之间不重启，登录态天然保留在这一个进程里。

## 版本兼容性

本机验证环境：macOS 26（Tahoe，Darwin 27.0.0）+ Google Chrome 153.0.8010.50（当前 stable）。**本机没有 Edge，也没有上一个 Chrome 大版本的安装包，无法在本次 spike 里做真正的跨版本回归测试**——这是诚实要写清楚的缺口，不装作测过。

基于公开、稳定的事实做出的判断（非实测）：
- CDP（Chrome DevTools Protocol）自 `Page`/`Target`/`Network` 这几个 v0 就存在的 domain 起，跨 Chromium 大版本高度向后兼容；Playwright/Puppeteer/Selenium 等主流自动化工具长期依赖它，Chromium 团队对外部工具依赖的核心 domain 的破坏性变更极少。
- Edge 基于 Chromium，CDP 行为与 Chrome 基本一致；Edge 的二进制路径、user-data-dir 位置不同（需要在 daemon 里做浏览器发现层，而不是硬编码 Chrome 路径）。
- 建议支持范围：**Chrome / Edge 最近两个稳定大版本**（当前对应 Chrome ~151–153），与 PRD 13.2 风险表的口径一致；发布前建议至少手动在一个次新版本上跑一遍 `browser_probe.py --step attach` 做真正验证，本 spike 未覆盖此项，标记为待办。

## 安全含义（cookie 泄露面）

- CDP 一旦开启，`127.0.0.1:<port>` 上任何能连到该端口的本机进程都能拿到浏览器的完整控制权（包括读 cookie、执行任意 JS、截屏）。**必须**：
  - 绑定 `127.0.0.1`，不监听 `0.0.0.0`（与 DEV.md「守护进程不开放入站网络端口」的精神一致，浏览器调试口虽由 Chrome 自己监听，daemon 也要保证不做端口转发/暴露）。
  - 用随机端口（`--remote-debugging-port=0`，Chrome 会把实际端口写进 `DevToolsActivePort` 文件）而非固定 9222，降低同机其他进程蹭连的概率。
  - Jones 专属 profile 与用户日常 profile物理隔离（不同 `user-data-dir`），用户在 Jones 窗口里的登录动作是显式、可见、用户自己做的操作，不是 Jones 偷偷拿走已有 cookie——这也规避了「偷别的 App 数据目录」在安全评审上的观感问题。
  - Step（工具调用）执行日志/回放里绝不记录完整 cookie 值，只记录域名/动作摘要，与 PRD N02（凭据明文）对齐。
- Profile 复制方案在安全上更差：需要读取另一个 App 的私有数据目录（Cookies、Login Data 等 SQLite 库，部分字段用 macOS Keychain 里的 "Chrome Safe Storage" 密钥加密），一旦 Jones 进程有这个读权限，理论上也能读到用户其它未预期要给浏览器能力的敏感 cookie（银行、邮箱等），攻击面/误用面比「用户在 Jones 专属窗口里主动登录」大得多。这是不推荐方案 b 的另一个独立理由，不只是「实测拿不到登录态」。

## 验收对照（Issue #4）

- [x] 能访问一个需登录页面并读取内容 —— `browser_probe.py --step attach`，实测通过（登录页 the-internet.herokuapp.com，非真实用户账号；见上）。
- [x] 明确支持的 Chrome / Edge 版本范围 —— 见「版本兼容性」，Chrome 本机版本实测，其余基于 CDP 协议稳定性做出的工程判断，非实测，已标注。
- [ ] 不可行则 FR09 降 P1 —— **不适用**：CDP attach 路径可行，FR09 保持 P0，仅需按下面的接口草案澄清语义（「Jones 专属常驻浏览器」而非「接管用户任意已开窗口」）。

## FR09 接口草案（daemon ⇄ worker/kernel）

浏览器能力作为 worker 侧的一组工具（类比 FR07 文件五件套），不在 RPC v0 的前端方法表里新增顶层方法，走既有的 `session.send` → Step 机制；这里定义的是 daemon 内部 `kernel/` 与「浏览器子系统」之间的契约草案，供 daemon 骨架 Issue 实现时参考：

```
BrowserSession（daemon 内部状态，非 SQLite 表，进程重启即丢——与 Step/Run 的持久化记录不冲突，
                回放读的是 Step 里记录的动作参数/结果摘要，不依赖这个活对象）：
  - profile_dir: <user_root()>/browser/profile   # Jones 专属，非用户 Default profile
  - chrome_path: 自动发现（macOS: /Applications/Google Chrome.app/... 或 Edge 对应路径）
  - proc: 冷启动的 Chrome/Edge 子进程句柄，--remote-debugging-port=0
  - cdp_endpoint: 从 DevToolsActivePort 文件读到的实际 ws endpoint
  - 生命周期：daemon 启动后懒加载（第一次浏览器工具调用才拉起）；daemon 退出/崩溃后此进程
    独立存活或一并退出待定（倾向：daemon 退出时优雅关闭，避免孤儿进程——需要 daemon 骨架
    Issue 里统一子进程收拢策略，此处只声明约束，不重复实现）

工具（暴露给 kernel/worker，经权限闸）：
  browser.navigate   {url}                          -> {title, url}
  browser.read       {selector?}                    -> {text | html}       # 只读，规则闸可默认放行
  browser.click       {selector}                     -> {ok}
  browser.fill        {selector, value}               -> {ok}
  browser.submit      {selector}                      -> {ok}               # 表单提交，必须过用户闸（PRD 12.3 FR09 口径）

约束：
  - navigate/read 为只读动作，可被规则闸放行；click/fill 视目标风险可能触发审查闸；
    submit 一律用户闸（表单提交视为有副作用的外发动作，对齐 12.3 FR09「表单提交走用户闸」）。
  - Step 记录 args_json 时对 fill 的 value 做脱敏（若字段名/上下文疑似密码则不落明文），
    对齐 N02。
  - 不做「自动发现并接管用户当前 Chrome 窗口」的路径；浏览器能力首次使用时，若 Jones
    专属 profile 里未登录目标网站，工具应返回明确错误/提示（而不是静默失败），提示用户
    到 Jones 浏览器窗口里手动登录一次——对齐诚实失败原则。
```

## 没做 / 已知缺口

- 没有做 Edge 的实机验证（本机未装 Edge）；没有做「次新版」Chrome 的跨版本验证（本机只有当前 stable 一个版本）。风险：版本兼容性结论部分基于协议稳定性的工程判断，非全覆盖实测。
- 没有做扩展 + native messaging 路径的代码验证，只做了案头分析：该路径需要（1）一个通过 Chrome Web Store 审核或以「开发者模式加载」方式安装的扩展，(2) 注册在 `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/` 的 native messaging host manifest，(3) 扩展用 `chrome.debugger` API 挂到当前活动 tab（这是三条路径里唯一能不重启就拿到「用户当前已登录窗口」的方式，因为它不依赖 `--remote-debugging-port`），但需要用户主动安装扩展并容忍 Chrome 对使用 `chrome.debugger` 的扩展常驻显示的「正在调试此浏览器」提示条，成本和摩擦明显高于「Jones 专属浏览器窗口」方案，v1 不做，记为后续如果用户强烈要求「必须是我当前这个窗口」时的备选。
- 没有触碰 `daemon/` 目录里除 `daemon/spikes/browser_probe.py` 之外的任何文件——`daemon/` 骨架本身归属另一个并行 Issue（W1 里的 daemon skeleton），本 spike 严格按目录所有权规则只新增了 spike 脚本本身，未创建 `pyproject.toml`/包结构，避免和骨架 Issue 打架；脚本用 PEP 723 内联依赖声明（`uv run` 可直接跑），不依赖尚不存在的 daemon 包。
- 未修改 `docs/design/00-foundation.md` 或 `docs/PRD.md`：本 spike 认为 FR09 保持 P0、CDP attach 可行，不触发「不可行降 P1」的改契约条件；FR09 语义上的澄清（「Jones 专属浏览器」而非「接管用户任意窗口」）已写在本文件的接口草案里，供后续 daemon 浏览器子系统 Issue 与 PRD 维护者参考，是否要点回 PRD 措辞由 PRD 所有者决定。

## 如何复现验证

```bash
cd daemon
uv run spikes/browser_probe.py --step all      # 依次跑 probe / singleton / attach / copy
uv run spikes/browser_probe.py --step attach    # 只跑核心的 CDP attach 验证
```

需要本机已装 Google Chrome（默认路径 `/Applications/Google Chrome.app/...`，可用 `--chrome` 指定别的路径/Edge）、可联网访问 `the-internet.herokuapp.com`（公开的自动化测试站，非任何真实账号）。脚本自行拉起/清理它起的 Chrome 进程和临时 profile 目录，不触碰、不读取用户的真实 Chrome profile。
