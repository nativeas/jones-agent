# FR09 · 复用浏览器 MCP Server 探针（spike #4 第二轮）

对应 `docs/design/00-foundation.md` §8「选型：为什么是 Playwright MCP，不是
chrome-devtools-mcp」一节的实测证据。纯 stdlib，不需要 `uv`/pip 依赖，只需要
`node`/`npx`（脚本会用 `npx -y` 联网拉取 `@playwright/mcp` 与
`chrome-devtools-mcp`，版本号锁在脚本里）。

- `mcp_client.py`：最小 MCP stdio JSON-RPC 客户端（无第三方 MCP SDK 依赖）。
- `local_site.py`：只监听 127.0.0.1、随机端口的本地登录态测试站（`/login` 签发
  persistent cookie，`/secure` 校验），不发外部请求、不涉及真实账号。
- `test_persist.py`：核心验证——给定候选（`playwright` 或 `devtools`）和一个
  Jones 专属 profile 目录，跑一遍「登录 → 完整杀掉 MCP server 子进程（模拟
  daemon 重启，不是只断连接）→ 重新拉起一个新 MCP server 进程，同一个 profile
  → 免登录读受保护页」，并打印跨重启是否保住登录态。

跑法：

```bash
python3 daemon/spikes/mcp_reuse_probe/test_persist.py playwright /tmp/jones_pw_profile
python3 daemon/spikes/mcp_reuse_probe/test_persist.py devtools   /tmp/jones_dt_profile
```

实测结论（详见 `docs/design/00-foundation.md` §8.1）：Playwright MCP 因为暴露了
`browser_close` 工具（调用后立即把 cookie 落盘），跨重启保持登录态稳定可复现；
chrome-devtools-mcp 没有等价的「主动 flush 并进入可安全终止状态」工具，
`close_page` 拒绝关闭最后一个页面，实测多种「关闭页面再杀进程」的路径 cookie
均未落盘，只有等 Chromium 内部约 30s 的周期性 flush 才会自发落盘——这个 30s
窗口对 daemon 的懒加载/空闲关闭生命周期是真实风险，不是理论问题。
