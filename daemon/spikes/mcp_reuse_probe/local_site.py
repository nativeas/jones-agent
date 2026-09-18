"""本地持久登录态测试站：/login 签发 Max-Age cookie，/secure 校验。
随机端口，只监听 127.0.0.1，不发外部请求。"""
from __future__ import annotations

import contextlib
import http.server
import threading

COOKIE_NAME = "jones_mcp_probe"


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/login":
            self.send_response(302)
            self.send_header("Set-Cookie", f"{COOKIE_NAME}=logged_in; Max-Age=86400; Path=/")
            self.send_header("Location", "/secure")
            self.end_headers()
        elif self.path == "/secure":
            cookie = self.headers.get("Cookie", "")
            if f"{COOKIE_NAME}=logged_in" in cookie:
                body = b"<html><body><h2>Secure Area</h2></body></html>"
                self.send_response(200)
            else:
                body = b"<html><body><h2>Please log in</h2></body></html>"
                self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass


@contextlib.contextmanager
def local_login_server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.shutdown()
        t.join(timeout=5)
