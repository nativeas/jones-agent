#!/usr/bin/env python3
"""最小 Python 守护进程 spike：监听 Unix socket，收到一行 JSON 回一行 JSON pong。

只用于验证打包/签名/公证/launchd 路径，不是真实 daemon 的实现（daemon/ 目录不属于本 Issue）。
标准库 only，不依赖任何第三方包，方便对比 PyInstaller 与 python-build-standalone
两种打包方式在“空守护进程”场景下的体积与冷启动开销。
"""
from __future__ import annotations

import json
import os
import signal
import socket
import sys
import time
from pathlib import Path

RUNTIME_DIR = Path(os.environ.get("JONES_SPIKE_HOME", Path.home() / ".jones-spike" / "runtime"))
SOCK_PATH = RUNTIME_DIR / "daemon.sock"
PID_PATH = RUNTIME_DIR / "daemon.pid"
LOG_PATH = RUNTIME_DIR / "daemon.log"

_running = True


def log(event: str, **fields) -> None:
    """结构化 JSON lines 日志，追加写，不静默异常（DEV.md 工程原则 4）。"""
    record = {"ts": time.time(), "event": event, **fields}
    line = json.dumps(record, ensure_ascii=False)
    print(line, flush=True)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as exc:
        # 日志目录不可写不应该让进程崩溃，但必须可见地报告。
        print(json.dumps({"ts": time.time(), "event": "log_write_failed", "error": str(exc)}), file=sys.stderr, flush=True)


def _handle_signal(signum, _frame) -> None:
    global _running
    log("signal_received", signum=signum)
    _running = False


def main() -> int:
    global _running
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    if SOCK_PATH.exists():
        # 探活：能连上说明已有活实例，直接退出而不是抢占 socket。
        try:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(0.5)
            probe.connect(str(SOCK_PATH))
            probe.close()
            log("already_running", sock=str(SOCK_PATH))
            return 0
        except OSError:
            SOCK_PATH.unlink(missing_ok=True)

    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(SOCK_PATH))
    server.listen(8)
    server.settimeout(0.5)
    # listen() 一返回，socket 就已经可以 accept 了——这就是"ready"的那一刻，
    # 在这里立刻取单调时钟时间戳，不要等到后面几行 signal 注册、log() 调用之后再取，
    # 避免引入额外的、测量代码自己造成的延迟。用 monotonic_ns 而不是 time.time()，
    # 是因为 measure.sh 要拿这个值跟自己 spawn 前记录的 monotonic_ns 相减算冷启动
    # 耗时——CLOCK_MONOTONIC 在同一台机器上跨进程可比，wall clock 不适合算耗时差。
    ready_ns = time.monotonic_ns()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    start = time.time()
    # 冷启动耗时（exec 之前的时间）在进程内部量不到——但"从 exec 到这里 ready"这段，
    # 现在由 ready_ns 精确标出。measure.sh 只算 spawn（外部）→ ready（这里）之间的差，
    # 不再需要反复 spawn 探测子进程去猜测何时可连（那样会把探测进程自己的启动开销
    # 计进冷启动数字，见评审记录第二轮意见 1）。
    log("started", pid=os.getpid(), sock=str(SOCK_PATH), ready_monotonic_ns=ready_ns)

    try:
        while _running:
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                log("accept_error", error=str(exc))
                continue
            with conn:
                try:
                    conn.settimeout(2.0)
                    buf = b""
                    while b"\n" not in buf:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        buf += chunk
                    line = buf.split(b"\n", 1)[0]
                    if not line:
                        continue
                    req = json.loads(line.decode("utf-8"))
                    if req.get("cmd") == "ping":
                        resp = {"pong": True, "pid": os.getpid(), "uptime_s": round(time.time() - start, 3)}
                    else:
                        resp = {"error": "unknown_cmd", "cmd": req.get("cmd")}
                    conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    log("conn_error", error=str(exc))
    finally:
        server.close()
        SOCK_PATH.unlink(missing_ok=True)
        PID_PATH.unlink(missing_ok=True)
        log("stopped")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
