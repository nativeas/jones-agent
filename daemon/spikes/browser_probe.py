# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["playwright==1.58.0"]
# ///
"""Spike #4 — 浏览器复用登录态探针 (Issue #4 / FR09)。

独立可运行脚本（不依赖 daemon 包），验证三条路径中可自动化验证的两条：

  a) CDP attach:   对一个由 Jones 自己拉起、带 --remote-debugging-port 的 Chrome，
                    用 playwright.connect_over_cdp() 反复挂接，跨多次挂接读取
                    已登录页面。
  b) Profile copy: 复制一份 Chrome profile 目录（脚本自建的测试 profile，而非
                    用户真实 Default profile —— 见报告「测试环境限制」一节），
                    用复制出的目录起一个新的 persistent context，检查登录态
                    是否还在。同时验证「边运行边复制」与「退出后复制」两种
                    时机。
  c) 扩展 + native messaging：不做代码验证，属于桌面产出物（发布的扩展 +
                    host manifest），本 spike 只做可行性与成本分析，见报告。

用法：
    uv run daemon/spikes/browser_probe.py --step all
    uv run daemon/spikes/browser_probe.py --step attach
    uv run daemon/spikes/browser_probe.py --step singleton
    uv run daemon/spikes/browser_probe.py --step copy

依赖真实 Chrome 二进制（macOS 路径写死在 CHROME_PATH，可用 --chrome 覆盖）和
网络（访问 the-internet.herokuapp.com 的公开登录 demo 页，不涉及任何用户真实
账号）。每个 step 结束会清理自己起的 Chrome 进程与临时 profile 目录。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request

CHROME_PATH_DEFAULT = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
LOGIN_URL = "https://the-internet.herokuapp.com/login"
SECURE_URL = "https://the-internet.herokuapp.com/secure"
USERNAME = "tomsmith"
PASSWORD = "SuperSecretPassword!"


def _work_dir() -> str:
    d = os.path.join(
        os.environ.get("TMPDIR", "/tmp"), "jones_browser_probe_" + str(os.getpid())
    )
    os.makedirs(d, exist_ok=True)
    return d


def _kill_by_userdata(prof: str) -> None:
    subprocess.run(["pkill", "-f", f"user-data-dir={prof}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)


def cdp_json_version(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1.5) as r:
            import json

            return json.loads(r.read())
    except Exception:
        return None


def step_probe_running_chrome() -> None:
    """检查系统里已经在跑的 Chrome，是不是天然就有 CDP 端口 —— 答案应为否。"""
    print("== probe: 用户当前正在运行的 Chrome 是否已暴露 CDP 端口 ==")
    hit = False
    for port in (9222, 9229, 9333):
        v = cdp_json_version(port)
        print(f"  port {port}: {'OPEN -> ' + str(v) if v else 'closed'}")
        hit = hit or bool(v)
    if not hit:
        print("  结论：默认启动的 Chrome 不暴露 CDP，必须以 --remote-debugging-port 重新起进程才能 attach。")


def step_singleton_lock(chrome: str) -> None:
    """验证：往一个已经在运行的 profile 上,第二次带 --remote-debugging-port 启动，
    Chrome 的单实例锁会把这次启动转发给已运行的实例并立即退出，新 flag 被忽略。
    这证明「CDP attach 到已经打开、且未带调试端口的 Chrome」在不重启的前提下不可行。
    """
    print("== singleton lock: 能否在不重启已运行 Chrome 的前提下临时开启 CDP ==")
    work = _work_dir()
    prof = os.path.join(work, "singleton_prof")
    os.makedirs(prof, exist_ok=True)
    p1 = subprocess.Popen(
        [chrome, f"--user-data-dir={prof}", "--no-first-run", "--no-default-browser-check", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(3)
    print(f"  第一进程 pid={p1.pid} 存活={p1.poll() is None}")

    p2 = subprocess.Popen(
        [chrome, f"--user-data-dir={prof}", "--remote-debugging-port=9222", "--no-first-run", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(3)
    rc = p2.poll()
    v = cdp_json_version(9222)
    print(f"  第二次带 --remote-debugging-port 的启动是否被单实例锁秒退={rc is not None}（returncode={rc}）")
    print(f"  9222 端口是否因此打开={bool(v)}（预期 False —— flag 被忽略）")

    p1.terminate()
    try:
        p1.wait(timeout=5)
    except Exception:
        p1.kill()
    _kill_by_userdata(prof)
    shutil.rmtree(work, ignore_errors=True)


def step_cdp_attach(chrome: str) -> None:
    """核心验证：冷启动即带 --remote-debugging-port 的 Chrome，用 playwright 反复
    connect_over_cdp，登录一次、断开连接、重新连接，确认能在不重新登录的情况下
    读取受保护页面内容。"""
    print("== CDP attach: 冷启动带调试端口 -> 登录 -> 断开重连 -> 免登录读取受保护页 ==")
    from playwright.sync_api import sync_playwright

    work = _work_dir()
    prof = os.path.join(work, "attach_prof")
    os.makedirs(prof, exist_ok=True)
    port = 9222
    proc = subprocess.Popen(
        [chrome, f"--user-data-dir={prof}", f"--remote-debugging-port={port}",
         "--no-first-run", "--no-default-browser-check", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(3)
    v = cdp_json_version(port)
    print(f"  CDP 端点已就绪: {bool(v)}  (Browser={v.get('Browser') if v else None})")

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        ctx = browser.contexts[0]
        page = ctx.new_page()
        page.goto(LOGIN_URL, wait_until="load")
        page.fill("#username", USERNAME)
        page.fill("#password", PASSWORD)
        page.click("button[type=submit]")
        page.wait_for_selector("#flash", timeout=5000)
        print("  登录结果:", page.inner_text("#flash").strip().splitlines()[0])
        page.close()
        browser.close()  # 只关连接，不关进程

    still_alive = subprocess.run(["pgrep", "-f", f"user-data-dir={prof}"], capture_output=True).returncode == 0
    print(f"  playwright 连接关闭后 Chrome 进程仍存活: {still_alive}")

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        ctx = browser.contexts[0]
        page = ctx.new_page()
        page.goto(SECURE_URL, wait_until="load")
        h2 = page.inner_text("h2")
        print(f"  重新挂接后免登录读取受保护页 h2 = {h2.strip()!r}")
        page.close()
        browser.close()

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    _kill_by_userdata(prof)
    shutil.rmtree(work, ignore_errors=True)


def _inspect_session_cookie(cookies_db: str) -> tuple[bool, int, int]:
    """返回 (是否存在 rack.session, has_expires, is_persistent)。"""
    if not os.path.exists(cookies_db):
        return False, -1, -1
    tmp = cookies_db + ".probe_copy"
    shutil.copy(cookies_db, tmp)
    try:
        con = sqlite3.connect(tmp)
        cur = con.cursor()
        cur.execute("select has_expires, is_persistent from cookies where name='rack.session'")
        row = cur.fetchone()
        con.close()
    finally:
        os.remove(tmp)
    if row is None:
        return False, -1, -1
    return True, row[0], row[1]


def step_profile_copy(chrome: str) -> None:
    """验证 profile 复制方案在两个关键时机下能否保住登录态：
    1) 运行中复制（可能拿到未落盘的残缺数据）
    2) 干净退出后复制（session-only cookie 会被 Chrome 清空）
    """
    print("== Profile copy: 复制 profile 目录能否带走登录态 ==")
    from playwright.sync_api import sync_playwright

    work = _work_dir()
    prof = os.path.join(work, "copy_src")
    os.makedirs(prof, exist_ok=True)

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            prof, headless=False, executable_path=chrome,
            args=["--no-first-run", "--no-default-browser-check"],
        )
        page = ctx.new_page()
        page.goto(LOGIN_URL, wait_until="load")
        page.fill("#username", USERNAME)
        page.fill("#password", PASSWORD)
        page.click("button[type=submit]")
        page.wait_for_selector("#flash", timeout=5000)
        print("  已在源 profile 登录。")

        # 时机 1：运行中复制
        live_copy = os.path.join(work, "copy_live")
        shutil.rmtree(live_copy, ignore_errors=True)
        errors = []
        try:
            shutil.copytree(prof, live_copy)
        except shutil.Error as e:
            errors = e.args[0]
        found, has_exp, persistent = _inspect_session_cookie(
            os.path.join(live_copy, "Default", "Cookies")
        )
        print(f"  [运行中复制] copytree 报错条目数={len(errors)}（socket/lock 类特殊文件必然报错）")
        print(f"  [运行中复制] 复制出的库里能读到 rack.session: {found}（预期常为 False —— 尚未落盘，存在竞态）")

        ctx.close()  # 正常退出 -> Chrome 会清理 session-only cookie

    # 时机 2：干净退出后复制
    clean_copy = os.path.join(work, "copy_after_quit")
    shutil.rmtree(clean_copy, ignore_errors=True)
    shutil.copytree(prof, clean_copy)
    found, has_exp, persistent = _inspect_session_cookie(
        os.path.join(clean_copy, "Default", "Cookies")
    )
    print(f"  [退出后复制] 复制出的库里 rack.session 行是否还在: found={found} has_expires={has_exp} is_persistent={persistent}")
    print("  [退出后复制] 该 cookie 是 is_persistent=0 的 session-only cookie；Chrome 在干净退出时"
          "是否/何时清理它是内部异步行为，实测两次不一致（行有时还在），不能依赖其存在。"
          "真正有决定性的是下面用复制出的 profile 实际访问受保护页的落地结果：")

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            clean_copy, headless=False, executable_path=chrome,
            args=["--no-first-run", "--no-default-browser-check"],
        )
        page = ctx.new_page()
        page.goto(SECURE_URL, wait_until="load")
        print(f"  [退出后复制] 用复制出的 profile 打开受保护页，落地 URL = {page.url}")
        page.close()
        ctx.close()

    _kill_by_userdata(prof)
    shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", choices=["all", "probe", "singleton", "attach", "copy"], default="all")
    ap.add_argument("--chrome", default=CHROME_PATH_DEFAULT)
    args = ap.parse_args()

    if not os.path.exists(args.chrome):
        print(f"找不到 Chrome 可执行文件: {args.chrome}（用 --chrome 指定路径）", file=sys.stderr)
        return 1

    steps = {
        "probe": lambda: step_probe_running_chrome(),
        "singleton": lambda: step_singleton_lock(args.chrome),
        "attach": lambda: step_cdp_attach(args.chrome),
        "copy": lambda: step_profile_copy(args.chrome),
    }
    order = ["probe", "singleton", "attach", "copy"] if args.step == "all" else [args.step]
    for name in order:
        steps[name]()
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
