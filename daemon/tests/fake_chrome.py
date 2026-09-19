#!/usr/bin/env python3
"""Stdlib-only stand-in for `Google Chrome --remote-debugging-port=0
--user-data-dir=<dir>`, used by `test_cap_browser.py` so `BrowserManager`'s
process-lifecycle logic (launch/reattach/shutdown) is exercised against a real
subprocess and a real HTTP listener without needing a real Chrome binary in CI
(same pattern as `fake_acp_agent.py` for the ACP worker).

Parses just enough of Chrome's real argv shape to behave the same way for the
three things `capabilities/browser.py` actually depends on:
  - writes `<user-data-dir>/DevToolsActivePort` as `<port>\\n<ws_path>\\n`
  - writes `<user-data-dir>/SingletonLock` as a `<hostname>-<pid>` symlink
    (real Chrome's single-instance guard — `capabilities/browser.py`'s
    `_read_singleton_lock_pid` reads this to recover a pid for a reattached
    orphan, review finding #10)
  - answers `GET /json/version` on that port (so `_probe_cdp_alive` succeeds)
Exits cleanly on SIGTERM (mirroring real Chrome's observed behavior in this
report's manual trials) unless `--fake-ignore-sigterm` is passed, which
`test_cap_browser.py` uses to exercise the SIGKILL fallback path.
`--fake-no-cdp-response` writes DevToolsActivePort pointing at a port nothing
listens on (and starts no HTTP server at all), to exercise the "port file
appeared but CDP never answered" launch-failure path (review findings #3/#11).
"""
from __future__ import annotations

import http.server
import json
import os
import signal
import socket
import sys
import threading
from pathlib import Path

_WS_PATH = "/devtools/browser/fake-chrome-id"


def main() -> int:
    argv = sys.argv[1:]
    user_data_dir = None
    ignore_sigterm = False
    no_cdp_response = False
    for arg in argv:
        if arg.startswith("--user-data-dir="):
            user_data_dir = Path(arg.split("=", 1)[1])
        elif arg == "--fake-ignore-sigterm":
            ignore_sigterm = True
        elif arg == "--fake-no-cdp-response":
            no_cdp_response = True
    if user_data_dir is None:
        print("fake_chrome.py: --user-data-dir is required", file=sys.stderr)
        return 2

    user_data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = user_data_dir / "SingletonLock"
    try:
        lock_path.unlink(missing_ok=True)
        os.symlink(f"{socket.gethostname()}-{os.getpid()}", lock_path)
    except OSError:
        pass  # best-effort, matches real Chrome's own tolerance for a locked-down profile dir

    if no_cdp_response:
        (user_data_dir / "DevToolsActivePort").write_text(
            "1\n/devtools/browser/fake\n", encoding="utf-8"
        )
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_a: (None if ignore_sigterm else stop.set()))
        try:
            stop.wait()
        except KeyboardInterrupt:
            pass
        return 0

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_a: object) -> None:  # noqa: D401 — silence per-request logging
            pass

        def do_GET(self) -> None:
            if self.path == "/json/version":
                body = json.dumps(
                    {"Browser": "fake-chrome/1.0", "webSocketDebuggerUrl": f"ws://127.0.0.1:{server.server_port}{_WS_PATH}"}
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    (user_data_dir / "DevToolsActivePort").write_text(
        f"{server.server_port}\n{_WS_PATH}\n", encoding="utf-8"
    )

    stop = threading.Event()

    def _on_sigterm(_signum: int, _frame: object) -> None:
        if not ignore_sigterm:
            stop.set()

    signal.signal(signal.SIGTERM, _on_sigterm)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        stop.wait()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
