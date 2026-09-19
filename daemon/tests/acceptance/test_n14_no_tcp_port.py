"""N14 — PRD 12.2 负面清单:

"守护进程监听 TCP 端口，或开放任何入站网络端口" —— 对应原则 11.3，绝对不能
发生。守护进程"只监听 Unix socket / Named pipe，不开 TCP 端口"。

复用（不重复造）：`test_rpc.py::test_socket_is_created_owner_only`——真实起一
个 daemon（`RpcServer` 走 `asyncio.start_unix_server`，见
`rpc/server.py`），断言监听端是一个 owner-only 权限的 Unix domain socket 文件。

补一条静态源码检查（防回归）：`daemon/src/jones_daemon/` 里不存在任何
`asyncio.start_server` / `socket.AF_INET` 调用——`rpc/server.py` 是唯一的服务端
监听点，它只调用 `start_unix_server`；新增一个入站 TCP 监听点会在这里被直接
抓到，而不必等到运行时抓包（G16）才发现。
"""

from __future__ import annotations

from ._reuse import DAEMON_ROOT, run_existing

_FORBIDDEN = ("asyncio.start_server(", "socket.AF_INET", "create_server(")


def test_n14_no_tcp_listener_in_source() -> None:
    src = DAEMON_ROOT / "src" / "jones_daemon"
    hits = []
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in _FORBIDDEN:
            if needle in text:
                hits.append(f"{path.relative_to(DAEMON_ROOT)}: {needle!r}")
    assert hits == [], f"found a TCP/inbound listener call site: {hits}"


def test_n14_socket_is_unix_domain_owner_only_acceptance() -> None:
    run_existing("tests/test_rpc.py::test_socket_is_created_owner_only")
