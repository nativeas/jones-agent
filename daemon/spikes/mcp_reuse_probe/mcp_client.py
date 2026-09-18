#!/usr/bin/env python3
"""最小 MCP stdio JSON-RPC 客户端，用于探针脚本，不依赖任何 MCP SDK。

MCP stdio transport：每条消息是一行 JSON（换行分隔），不是 LSP 那种
Content-Length 帧头。服务端启动横幅/日志走 stderr，不会污染 stdout。
"""
from __future__ import annotations

import json
import subprocess
import threading
import queue
import time


class McpStdioClient:
    def __init__(self, cmd: list[str], env: dict | None = None):
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        self._id = 0
        self._out_q: queue.Queue = queue.Queue()
        self._err_lines: list[str] = []
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self):
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._out_q.put(json.loads(line))
            except json.JSONDecodeError:
                pass  # 非 JSON 行（不应出现在 stdout，容错跳过）

    def _read_stderr(self):
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self._err_lines.append(line.rstrip())

    def stderr_tail(self, n: int = 20) -> str:
        return "\n".join(self._err_lines[-n:])

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def request(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        rid = self._next_id()
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + timeout
        pending = []
        while time.time() < deadline:
            try:
                resp = self._out_q.get(timeout=0.5)
            except queue.Empty:
                if self.proc.poll() is not None:
                    raise RuntimeError(
                        f"MCP 子进程已退出 (returncode={self.proc.returncode})，stderr 末尾:\n{self.stderr_tail()}"
                    )
                continue
            if resp.get("id") == rid:
                return resp
            pending.append(resp)  # 通知/其它响应，忽略顺序问题放回队列末尾
        raise TimeoutError(f"等待 {method} 响应超时({timeout}s)。stderr 末尾:\n{self.stderr_tail()}")

    def notify(self, method: str, params: dict | None = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def initialize(self) -> dict:
        resp = self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "jones-spike4-probe", "version": "0.0.1"},
            },
        )
        self.notify("notifications/initialized")
        return resp

    def list_tools(self) -> list[dict]:
        resp = self.request("tools/list")
        if "error" in resp:
            raise RuntimeError(f"tools/list 失败: {resp['error']}")
        return resp["result"]["tools"]

    def call_tool(self, name: str, arguments: dict | None = None, timeout: float = 30.0) -> dict:
        resp = self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout)
        if "error" in resp:
            raise RuntimeError(f"tools/call {name} 失败: {resp['error']}")
        return resp["result"]

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
