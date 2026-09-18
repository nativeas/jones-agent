# ext_probe — 路径 (c) 扩展 + chrome.debugger 验证工具（未跑通，留给有 GUI 权限的人）

对应 `docs/spikes/04-browser-login-state.md` 里「c 路径尝试记录」一节，评审 #2。

这是一个最小 MV3 扩展（`manifest.json` + `background.js`），用来验证：在一个**从未用
`--remote-debugging-port` 启动过**的正常 Chrome 窗口里，一个装了 `debugger` 权限的
扩展能不能用 `chrome.debugger` attach 到一个已经登录的 tab、读到受保护页面内容——
这是三条路径里唯一可能不经过 Jones 专属 profile、真正满足 FR09 原文「复用用户已登录
的浏览器态」字面意思的路径。

`background.js` 会在扩展被加载（`chrome.runtime.onInstalled`）时自动：
1. 打开 the-internet.herokuapp.com/login，用 `chrome.scripting.executeScript`
   （普通扩展能力，不涉及 `chrome.debugger`）自动填表登录——这一步只是让 tab 进入
   「已登录」状态，不是本次要验证的东西。
2. 导航到 /secure。
3. `chrome.debugger.attach` 到这个 tab，用 `Runtime.evaluate` 读 `h2` 文本。
4. 把 `{login_ok, debugger_attach_ok, debugger_read_ok, secure_h2_text, error}`
   POST 到 `http://127.0.0.1:8765/report`。

## 为什么没跑通

本次修复实测过（在真实 macOS + Chrome 153 stable 上）：
- `chrome --load-extension=<dir> --user-data-dir=<fresh profile>`：扩展**完全没有
  被注册**（`Preferences` 里 `extensions.settings` 长度为 0，连「已加载但被禁用」的
  状态都没有），不是权限问题，是命令行 flag 被静默忽略。
- 直接在 Chrome 未启动时手工往 `Preferences` 写入
  `extensions.ui.developer_mode: true` 再启动：同样无效（该字段被 Chrome 启动时
  重置/清空，不是这个机制的正确开关，或者已不再是它）。
- 结论：当前 stable 频道的 Chrome 要求用户在 `chrome://extensions` 页面**手动点一次**
  「开发者模式」开关（这是一个网页内的 UI 开关，不是系统级设置，没有可靠的命令行/
  配置文件旁路）之后，`--load-extension` / 「加载已解压的扩展程序」才会生效。
- 本次执行环境没有可用的 GUI 点击工具（读取 macOS 访达/Accessibility 做界面自动化
  超出这次修复的合理范围，且这类操作本身也应该谨慎对待），因此止步于此。

## 如何手动跑完（给有 GUI 权限的人，成本 ~10 分钟）

```bash
# 1. 起本地上报服务器（阻塞等待，收到一次 report 就退出）
python3 -c "
import http.server, json
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get('Content-Length', 0))
        print(json.dumps(json.loads(self.rfile.read(n)), ensure_ascii=False, indent=2))
        self.send_response(200); self.end_headers()
        raise SystemExit
http.server.HTTPServer(('127.0.0.1', 8765), H).handle_request()
"

# 2. 另一个终端：用一个全新的临时 profile 起 Chrome，带上这个扩展目录
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --user-data-dir=/tmp/jones_ext_manual \
  --load-extension="$(pwd)/daemon/spikes/ext_probe" \
  --no-first-run --no-default-browser-check about:blank

# 3. 在打开的窗口里去 chrome://extensions，打开右上角「开发者模式」，
#    找到 "Jones Spike4 Debugger Probe"，点一次它的「重新加载」（或直接关闭
#    该 tab 重开 Chrome，让 onInstalled 重新触发）。
# 4. 第一步的终端会打印出 JSON 结果并退出。
```

结果里 `debugger_read_ok: true` 且 `secure_h2_text: "Secure Area"` 即视为路径 (c)
验证通过；把结果补回 `docs/spikes/04-browser-login-state.md` 的相应位置。
