"""实测：给定的浏览器 MCP server 能否以 Jones 指定的 user-data-dir 启动 Chrome，
跨（MCP server 进程）重启保持登录态，并通过 MCP tools/call 完成导航 + 读页。

跑法：
  python3 test_persist.py playwright  <jones_profile_dir>
  python3 test_persist.py devtools    <jones_profile_dir>
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

from local_site import local_login_server
from mcp_client import McpStdioClient


def _text_from_result(result: dict) -> str:
    parts = result.get("content", [])
    return "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")


def run_playwright_mcp(profile_dir: str, base_url: str):
    def spawn():
        return McpStdioClient(
            ["npx", "-y", "@playwright/mcp@0.0.81", "--browser", "chrome",
             "--user-data-dir", profile_dir, "--headless"]
        )

    print("[playwright-mcp] 第一次启动 MCP server（全新 user-data-dir）")
    c = spawn()
    try:
        c.initialize()
        nav = c.call_tool("browser_navigate", {"url": f"{base_url}/login"})
        print("  navigate /login ->", _text_from_result(nav).splitlines()[0] if _text_from_result(nav) else "(空)")
        c.call_tool("browser_navigate", {"url": f"{base_url}/secure"})
        snap = c.call_tool("browser_evaluate", {"function": "() => document.body.innerText"})
        text = _text_from_result(snap)
        print("  首次登录后 navigate /secure，读页结果:", text.strip()[:200])
        assert "Secure Area" in text, f"首次登录后应能看到 Secure Area，实际: {text[:300]}"
        # 关键一步：cookie 要落盘，必须先让浏览器正常关闭 page/context（等价于
        # Chrome 正常退出走 checkpoint），直接 SIGTERM 掉 MCP server 子进程不会
        # flush cookie store —— 实测：不调 browser_close 直接杀进程，重启后
        # Cookies 库里该行是空的；调用后才会出现。这是给 daemon 实现的真实约束。
        c.call_tool("browser_close", {})
    finally:
        c.close()
    print("[playwright-mcp] 已调用 browser_close 落盘 cookie，再完整关闭 MCP server 子进程（模拟 daemon 重启）")
    time.sleep(1)

    print("[playwright-mcp] 第二次启动 MCP server（同一个 user-data-dir，不重新登录）")
    c2 = spawn()
    try:
        c2.initialize()
        c2.call_tool("browser_navigate", {"url": f"{base_url}/secure"})
        snap = c2.call_tool("browser_evaluate", {"function": "() => document.body.innerText"})
        text = _text_from_result(snap)
        print("  重启后直接 navigate /secure，读页结果:", text.strip()[:200])
        ok = "Secure Area" in text
        print(f"  结论：跨重启保持登录 = {ok}")
        return ok
    finally:
        c2.close()


def run_chrome_devtools_mcp(profile_dir: str, base_url: str):
    def spawn():
        return McpStdioClient(
            ["npx", "-y", "chrome-devtools-mcp@1.9.0",
             "--userDataDir", profile_dir, "--headless"]
        )

    print("[chrome-devtools-mcp] 第一次启动 MCP server（全新 userDataDir）")
    c = spawn()
    try:
        c.initialize()
        pid = 1  # 冷启动只有一个默认页，pageId 从 1 开始（list_pages 实测确认）
        nav = c.call_tool("navigate_page", {"pageId": pid, "type": "url", "url": f"{base_url}/login"})
        print("  navigate_page /login ->", _text_from_result(nav).splitlines()[0] if _text_from_result(nav) else "(空)")
        c.call_tool("navigate_page", {"pageId": pid, "type": "url", "url": f"{base_url}/secure"})
        ev = c.call_tool("evaluate_script", {"pageId": pid, "function": "() => document.body.innerText"})
        text = _text_from_result(ev)
        print("  首次登录后读页结果:", text.strip()[:200])
        assert "Secure Area" in text, f"首次登录后应能看到 Secure Area，实际: {text[:500]}"
        # 与 playwright-mcp 一样：关页面让 Chrome 走一次正常 checkpoint 再杀进程，
        # 而不是直接对 MCP server 子进程 SIGTERM（同样的 cookie flush 约束，
        # chrome-devtools-mcp 最后一个 page 不能被 close_page 关掉——"The last
        # open page cannot be closed."——改用 navigate_page 导航到 about:blank
        # 让当前受保护页离开，不依赖 close_page）。
        c.call_tool("navigate_page", {"pageId": pid, "type": "url", "url": "about:blank"})
    finally:
        c.close()
    print("[chrome-devtools-mcp] 已完整关闭第一个 MCP server 子进程（模拟 daemon 重启）")
    time.sleep(1)

    print("[chrome-devtools-mcp] 第二次启动 MCP server（同一个 userDataDir，不重新登录）")
    c2 = spawn()
    try:
        c2.initialize()
        pid = 1
        c2.call_tool("navigate_page", {"pageId": pid, "type": "url", "url": f"{base_url}/secure"})
        ev = c2.call_tool("evaluate_script", {"pageId": pid, "function": "() => document.body.innerText"})
        text = _text_from_result(ev)
        print("  重启后读页结果:", text.strip()[:200])
        ok = "Secure Area" in text
        print(f"  结论：跨重启保持登录 = {ok}")
        return ok
    finally:
        c2.close()


def main():
    which = sys.argv[1]
    profile_dir = sys.argv[2]
    shutil.rmtree(profile_dir, ignore_errors=True)
    os.makedirs(profile_dir, exist_ok=True)
    with local_login_server() as base_url:
        print(f"本地测试站: {base_url}")
        if which == "playwright":
            ok = run_playwright_mcp(profile_dir, base_url)
        elif which == "devtools":
            ok = run_chrome_devtools_mcp(profile_dir, base_url)
        else:
            raise SystemExit(f"unknown candidate {which}")
    print(f"\n=== {which}: 跨重启登录持久化 = {ok} ===")
    # 确保没有残留 Chrome 子进程（按 profile_dir 匹配 kill，防止探针自身泄漏）
    subprocess.run(["pkill", "-f", profile_dir], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
