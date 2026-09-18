"""Entry point: `python -m jones_daemon`.

Startup: probe for an already-running instance via the PID file + socket, write a
fresh PID file, open the SQLite store (applying pending migrations), start the RPC
server on the Unix socket, and run until SIGTERM/SIGINT for a graceful shutdown
(design §3).
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import signal
import socket
import sys

from jones_daemon import paths
from jones_daemon.logging import configure_logging, get_logger
from jones_daemon.rpc.methods import register_builtin_methods
from jones_daemon.rpc.server import RpcServer
from jones_daemon.store import apply_pending, connect

logger = get_logger("main")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


def _socket_alive(sock_path: str) -> bool:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(0.5)
        sock.connect(sock_path)
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _check_existing_instance() -> None:
    """Exit the process if another daemon instance is already up and serving."""
    pid_path = paths.pid_file()
    if not pid_path.exists():
        return
    try:
        pid = int(pid_path.read_text().strip())
    except ValueError:
        pid_path.unlink(missing_ok=True)
        return
    if _pid_alive(pid) and _socket_alive(str(paths.sock_file())):
        logger.error(
            "daemon already running",
            extra={"detail": {"pid": pid, "sock": str(paths.sock_file())}},
        )
        sys.exit(1)
    # Stale PID file (process dead, or socket not accepting): clean up and continue.
    pid_path.unlink(missing_ok=True)
    paths.sock_file().unlink(missing_ok=True)


async def _run() -> None:
    configure_logging()
    _check_existing_instance()

    paths.pid_file().write_text(str(os.getpid()))
    conn = connect(paths.db_path())
    version = apply_pending(conn)
    logger.info("store ready", extra={"detail": {"schema_version": version}})

    server = RpcServer(paths.sock_file())
    register_builtin_methods(server)
    await server.start()
    logger.info(
        "daemon listening",
        extra={"detail": {"sock": str(paths.sock_file()), "pid": os.getpid()}},
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    serve_task = asyncio.create_task(server.serve_forever())
    await stop.wait()
    logger.info("shutting down", extra={"detail": {}})

    serve_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await serve_task
    await server.stop()
    conn.close()
    paths.pid_file().unlink(missing_ok=True)


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
