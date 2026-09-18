# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["playwright==1.58.0"]
# ///
"""Spike #4 — 浏览器复用登录态探针 (Issue #4 / FR09)。

独立可运行脚本（不依赖 daemon 包），验证三条路径中可自动化验证的两条，外加评审后
补的几组对照实验：

  a) CDP attach:   对一个由 Jones 自己拉起、带 --remote-debugging-port 的 Chrome，
                    用 playwright.connect_over_cdp() 反复挂接，跨多次挂接读取
                    已登录页面。用随机端口（0）+ 轮询 DevToolsActivePort +
                    lsof 校验端口归属，而不是硬编码 9222（评审 #5 #7）。
  b) Profile copy: 复制一份 Chrome profile 目录（脚本自建的测试 profile，而非
                    用户真实 Default profile —— 见报告「测试环境限制」一节），
                    用复制出的目录起一个新的 persistent context，检查登录态
                    是否还在。覆盖 session-only cookie（the-internet 站点）与
                    persistent cookie（本脚本自带的本地测试站）两种场景，并
                    加一组「直接重启同一 profile、不经过复制」的对照，把
                    「重启本身丢状态」和「复制额外丢状态」分开看（评审 #4）。
  c) 扩展 + chrome.debugger：本次修复尝试过实测（见
                    daemon/spikes/ext_probe/ 的最小扩展源码与 README），但当前
                    stable Chrome 加载未打包扩展需要先在 chrome://extensions
                    手动开一次「开发者模式」（GUI 单选开关，命令行 flag 与直接
                    改 Preferences 均确认无法绕过 —— 详见
                    docs/spikes/04-browser-login-state.md「c 路径尝试记录」），
                    本环境没有可用的 GUI 点击工具，因此仍未跑通，如实标注为
                    「未验证」而非「技术可行但不做」（评审 #2）。

用法：
    uv run daemon/spikes/browser_probe.py --step all
    uv run daemon/spikes/browser_probe.py --step attach
    uv run daemon/spikes/browser_probe.py --step singleton
    uv run daemon/spikes/browser_probe.py --step copy

依赖真实 Chrome 二进制（macOS 路径写死在 CHROME_PATH，可用 --chrome 覆盖，也可指向
Chrome for Testing 的旧版本二进制做跨版本回归）和网络（访问 the-internet.herokuapp.com
的公开登录 demo 页，不涉及任何用户真实账号；以及本机 127.0.0.1 上的一个探针自带的
本地测试服务器，用于 persistent-cookie 场景，不发出外部请求）。每个 step 用
try/finally 保证清理自己起的 Chrome 进程与临时 profile 目录，即使中途抛异常
（评审 #8）。
"""

from __future__ import annotations

import argparse
import contextlib
import http.server
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request

CHROME_PATH_DEFAULT = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
LOGIN_URL = "https://the-internet.herokuapp.com/login"
SECURE_URL = "https://the-internet.herokuapp.com/secure"
USERNAME = "tomsmith"
PASSWORD = "SuperSecretPassword!"

LOCAL_SERVER_PORT = 8899
LOCAL_COOKIE_NAME = "session_probe"


def _work_dir() -> str:
    d = os.path.join(
        os.environ.get("TMPDIR", "/tmp"), "jones_browser_probe_" + str(os.getpid())
    )
    os.makedirs(d, exist_ok=True)
    return d


def _kill_by_userdata(prof: str) -> None:
    subprocess.run(["pkill", "-f", f"user-data-dir={prof}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)


def _terminate(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()


def cdp_json_version(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1.5) as r:
            import json

            return json.loads(r.read())
    except Exception:
        return None


def _wait_for_file(path: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(0.1)
    return os.path.exists(path)


def _read_devtools_active_port(profile_dir: str, timeout: float = 10.0) -> int:
    """从 --remote-debugging-port=0 冷启动的 Chrome 在 profile 目录下写出的
    DevToolsActivePort 文件读实际端口（评审 #5：这是文档 :51 推荐给实现者的生产
    配置，之前从未被跑过）。"""
    p = os.path.join(profile_dir, "DevToolsActivePort")
    if not _wait_for_file(p, timeout=timeout):
        raise RuntimeError(f"DevToolsActivePort 未在 {timeout}s 内出现: {p}")
    with open(p, encoding="utf-8") as f:
        lines = f.read().splitlines()
    return int(lines[0])


def _listening_pids(port: int) -> set[int]:
    """评审 #7：探针曾经硬编码 9222 且从不校验「应答的是不是我刚起的那个进程」。
    这里用 lsof 查真正监听该端口的 pid 集合，供调用方核对端点归属。"""
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return set()
    pids = set()
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) > 1:
            try:
                pids.add(int(parts[1]))
            except ValueError:
                pass
    return pids


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

    评审 #7 修复：两次启动都不再硬编码 9222 —— 用 --remote-debugging-port=0，
    通过「DevToolsActivePort 文件是否出现」判断端口是否真的被打开，不依赖某个
    固定端口当前有没有被别的进程占用。
    """
    print("== singleton lock: 能否在不重启已运行 Chrome 的前提下临时开启 CDP ==")
    work = _work_dir()
    prof = os.path.join(work, "singleton_prof")
    os.makedirs(prof, exist_ok=True)
    p1: subprocess.Popen | None = None
    p2: subprocess.Popen | None = None
    try:
        p1 = subprocess.Popen(
            [chrome, f"--user-data-dir={prof}", "--no-first-run", "--no-default-browser-check", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(3)
        print(f"  第一进程 pid={p1.pid} 存活={p1.poll() is None}")

        dtap = os.path.join(prof, "DevToolsActivePort")
        p2 = subprocess.Popen(
            [chrome, f"--user-data-dir={prof}", "--remote-debugging-port=0", "--no-first-run", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        rc = None
        try:
            rc = p2.wait(timeout=5)
        except subprocess.TimeoutExpired:
            rc = p2.poll()
        appeared = _wait_for_file(dtap, timeout=2.0)
        print(f"  第二次带 --remote-debugging-port 的启动是否被单实例锁秒退={rc is not None}（returncode={rc}）")
        print(f"  DevToolsActivePort 是否因此出现={appeared}（预期 False —— flag 被忽略，未监听任何端口）")
    finally:
        _terminate(p1)
        _terminate(p2)
        _kill_by_userdata(prof)
        shutil.rmtree(work, ignore_errors=True)


def step_cdp_attach(chrome: str) -> None:
    """核心验证：冷启动即带 --remote-debugging-port=0（随机端口）的 Chrome，从
    DevToolsActivePort 读实际端口、校验监听该端口的确实是我们刚拉起的进程
    （评审 #5 #7），再用 playwright 反复 connect_over_cdp，登录一次、断开连接、
    重新连接，确认能在不重新登录的情况下读取受保护页面内容。"""
    print("== CDP attach: 冷启动带调试端口(随机) -> 登录 -> 断开重连 -> 免登录读取受保护页 ==")
    from playwright.sync_api import sync_playwright

    work = _work_dir()
    prof = os.path.join(work, "attach_prof")
    os.makedirs(prof, exist_ok=True)
    proc: subprocess.Popen | None = None
    try:
        proc = subprocess.Popen(
            [chrome, f"--user-data-dir={prof}", "--remote-debugging-port=0",
             "--no-first-run", "--no-default-browser-check", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        port = _read_devtools_active_port(prof)
        owners = _listening_pids(port)
        owned = proc.pid in owners
        print(f"  随机端口={port}；监听该端口的 pid={owners or '{}'}；是否为本次拉起的进程(pid={proc.pid})：{owned}")
        if not owned:
            raise RuntimeError(
                "端点归属校验失败：应答该端口的不是本次拉起的 Chrome 进程，"
                "为避免误连别的浏览器实例，中止 attach（评审 #7 要求的校验）"
            )

        v = cdp_json_version(port)
        print(f"  CDP 端点已就绪: {bool(v)}  (Browser={v.get('Browser') if v else None})")

        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            # 复用已有 tab 而不是 new_page()：Edge 冷启动会自带一个
            # edge://sync-confirmation-dialog tab，实测 new_page() 开的新
            # 后台 tab 在它存在时点击/表单提交会被吞掉（后台 tab 节流），
            # 复现 100%；复用已有 tab（就地 goto 覆盖掉它）在 Chrome / Edge
            # 上都正常——这是给实现者的真实坑，不是本脚本的边角实现细节。
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(LOGIN_URL, wait_until="load")
            page.fill("#username", USERNAME)
            page.fill("#password", PASSWORD)
            page.click("button[type=submit]")
            page.wait_for_selector("#flash", timeout=5000)
            print("  登录结果:", page.inner_text("#flash").strip().splitlines()[0])
            browser.close()  # 只关连接，不关进程

        still_alive = subprocess.run(["pgrep", "-f", f"user-data-dir={prof}"], capture_output=True).returncode == 0
        print(f"  playwright 连接关闭后 Chrome 进程仍存活: {still_alive}")

        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(SECURE_URL, wait_until="load")
            h2 = page.inner_text("h2")
            print(f"  重新挂接后免登录读取受保护页 h2 = {h2.strip()!r}")
            browser.close()
    finally:
        _terminate(proc)
        _kill_by_userdata(prof)
        shutil.rmtree(work, ignore_errors=True)


def _copy_sqlite_with_wal(src_db: str, dst_db: str) -> None:
    """复制一个 SQLite 数据库文件时，把同目录下的 -wal / -shm 边车文件一并复制到
    目标同名位置。Chrome 的 Cookies 库默认开 WAL，已提交但尚未 checkpoint 的写入
    全在 -wal 里；只拷主文件必然读不到这些行，和「数据是否真的落盘」是两回事
    （评审 #9 指出的问题——原 _inspect_session_cookie 只 shutil.copy 了主文件）。
    """
    shutil.copy(src_db, dst_db)
    for suffix in ("-wal", "-shm"):
        src_side = src_db + suffix
        if os.path.exists(src_side):
            shutil.copy(src_side, dst_db + suffix)


def _inspect_cookie(cookies_db: str, name: str) -> tuple[bool, int, int]:
    """返回 (是否存在指定名字的 cookie, has_expires, is_persistent)。WAL-aware：
    见 _copy_sqlite_with_wal。"""
    if not os.path.exists(cookies_db):
        return False, -1, -1
    tmp = cookies_db + ".probe_copy"
    _copy_sqlite_with_wal(cookies_db, tmp)
    try:
        con = sqlite3.connect(tmp)
        cur = con.cursor()
        cur.execute("select has_expires, is_persistent from cookies where name=?", (name,))
        row = cur.fetchone()
        con.close()
    finally:
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                os.remove(tmp + suffix)
    if row is None:
        return False, -1, -1
    return True, row[0], row[1]


class _LocalLoginHandler(http.server.BaseHTTPRequestHandler):
    """本地测试站：/login 签发一个 persistent cookie（Max-Age，
    is_persistent=1），/secure 校验该 cookie。跟 the-internet 的
    session-only cookie 场景对照着测，覆盖评审 #4 指出的「决定性实验只测了
    session-only 场景，不能推广到持久 cookie（真实登录态最常见的形态）」这一点。
    不发出任何外部网络请求，不涉及任何真实账号。
    """

    def do_GET(self):  # noqa: N802 (http.server 的既定接口名)
        if self.path == "/login":
            self.send_response(302)
            self.send_header("Set-Cookie", f"{LOCAL_COOKIE_NAME}=logged_in; Max-Age=86400; Path=/")
            self.send_header("Location", "/secure")
            self.end_headers()
        elif self.path == "/secure":
            cookie = self.headers.get("Cookie", "")
            if f"{LOCAL_COOKIE_NAME}=logged_in" in cookie:
                body = b"<html><body><h2>Secure Area</h2></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(302)
                self.send_header("Location", "/login-page")
                self.end_headers()
        elif self.path == "/login-page":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body><h2>Please log in</h2></body></html>")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):  # noqa: A002 - 静默，探针输出已经够多了
        pass


@contextlib.contextmanager
def _local_login_server():
    srv = http.server.HTTPServer(("127.0.0.1", LOCAL_SERVER_PORT), _LocalLoginHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{LOCAL_SERVER_PORT}"
    finally:
        srv.shutdown()
        t.join(timeout=5)


def _launch_ctx(pw, chrome: str, prof: str):
    return pw.chromium.launch_persistent_context(
        prof, headless=False, executable_path=chrome,
        args=["--no-first-run", "--no-default-browser-check"],
    )


def _run_copy_scenario(chrome: str, work: str, label: str, login_url: str, secure_url: str, cookie_name: str) -> None:
    """跑一遍完整的「复制 profile 目录」场景：登录 -> 运行中复制 -> 干净退出 ->
    退出后复制 -> 用复制出的 profile 访问受保护页；外加一个「直接重启同一
    profile（不复制）」的对照组，把「重启本身丢状态」和「复制额外丢状态」分开
    （评审 #4 指出原来的对比不公平：a 全程不重启，b 强制重启，两者不能直接比较；
    这里让 b 内部先有一个重启-不复制的对照基线）。"""
    from playwright.sync_api import sync_playwright

    print(f"-- 场景: {label} (cookie={cookie_name}) --")
    prof = os.path.join(work, f"copy_src_{cookie_name}")
    os.makedirs(prof, exist_ok=True)

    with sync_playwright() as pw:
        ctx = _launch_ctx(pw, chrome, prof)
        page = ctx.new_page()
        page.goto(login_url, wait_until="load")
        if "the-internet" in login_url:
            page.fill("#username", USERNAME)
            page.fill("#password", PASSWORD)
            page.click("button[type=submit]")
            page.wait_for_selector("#flash", timeout=5000)
        print(f"  已在源 profile 登录（{label}）。")

        live_copy = os.path.join(work, f"copy_live_{cookie_name}")
        shutil.rmtree(live_copy, ignore_errors=True)
        errors = []
        try:
            shutil.copytree(prof, live_copy)
        except shutil.Error as e:
            errors = e.args[0]
        found, has_exp, persistent = _inspect_cookie(
            os.path.join(live_copy, "Default", "Cookies"), cookie_name
        )
        print(f"  [运行中复制] copytree 报错条目数={len(errors)}（socket/lock 类特殊文件必然报错）")
        print(f"  [运行中复制] 复制出的库里能读到 {cookie_name}: {found} has_expires={has_exp} is_persistent={persistent}")

        ctx.close()  # 正常退出

    # 对照组：不复制，直接用同一个 profile 重启，隔离「重启本身」造成的丢失
    with sync_playwright() as pw:
        ctx = _launch_ctx(pw, chrome, prof)
        page = ctx.new_page()
        page.goto(secure_url, wait_until="load")
        landed = page.url
        print(f"  [重启同一 profile / 不复制的对照组] 直接重启后访问受保护页落地 URL = {landed}")
        page.close()
        ctx.close()

    clean_copy = os.path.join(work, f"copy_after_quit_{cookie_name}")
    shutil.rmtree(clean_copy, ignore_errors=True)
    shutil.copytree(prof, clean_copy)
    found, has_exp, persistent = _inspect_cookie(
        os.path.join(clean_copy, "Default", "Cookies"), cookie_name
    )
    print(f"  [退出后复制] 复制出的库里 {cookie_name} 行是否还在: found={found} has_expires={has_exp} is_persistent={persistent}")

    with sync_playwright() as pw:
        ctx = _launch_ctx(pw, chrome, clean_copy)
        page = ctx.new_page()
        page.goto(secure_url, wait_until="load")
        print(f"  [退出后复制] 用复制出的 profile 打开受保护页，落地 URL = {page.url}")
        page.close()
        ctx.close()

    _kill_by_userdata(prof)


def step_profile_copy(chrome: str) -> None:
    """验证 profile 复制方案能否带走登录态，覆盖两种 cookie 生命周期：

    1) session-only（the-internet.herokuapp.com 的 rack.session，is_persistent=0）
    2) persistent（本脚本自带本地测试站签发的 Max-Age cookie，is_persistent=1，
       更接近真实登录场景里常见的「记住我」类持久态 —— 评审 #4 指出原实验
       只覆盖了 (1)，不能把结论推广到 (2)）

    每种场景都跑一次「直接重启同一 profile（不复制）」的对照，把「重启本身
    丢状态」和「复制额外丢状态」分开看。
    """
    print("== Profile copy: 复制 profile 目录能否带走登录态（session-only vs persistent）==")
    work = _work_dir()
    try:
        _run_copy_scenario(
            chrome, work, "session-only cookie (the-internet.herokuapp.com)",
            LOGIN_URL, SECURE_URL, "rack.session",
        )
        with _local_login_server() as base_url:
            _run_copy_scenario(
                chrome, work, "persistent cookie (本地测试站)",
                f"{base_url}/login", f"{base_url}/secure", LOCAL_COOKIE_NAME,
            )
    finally:
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
