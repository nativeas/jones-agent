"""Entry point: `python -m jones_daemon`.

Startup: probe for an already-running instance via the PID file + socket, write a
fresh PID file, open the SQLite store (applying pending migrations), start the RPC
server on the Unix socket, and run until SIGTERM/SIGINT for a graceful shutdown
(design §3).
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import signal
import sys
from typing import TextIO

from jones_daemon import paths
from jones_daemon.logging import configure_logging, get_logger
from jones_daemon.rpc.methods import register_builtin_methods
from jones_daemon.rpc.server import RpcServer
from jones_daemon.store import apply_pending, connect

logger = get_logger("main")


def _acquire_single_instance_lock() -> TextIO:
    """Exit the process unless we're the only instance — enforced by an OS-level lock.

    The previous check (read PID file, `kill(pid, 0)`, then probe-connect the
    socket) is three separate steps with no lock between them: a missing PID file
    (cleared runtime/, a backup restore, launchd restarting before the old process
    finished exiting) makes it a silent no-op, and nothing stops two daemons from
    both passing the check and then both unlinking+binding the same socket path
    (whichever runs `RpcServer.start()` second silently steals it from the first,
    which stays alive as an orphan). `flock(LOCK_EX | LOCK_NB)` on the PID file
    makes "holds the lock" the actual, kernel-enforced definition of "the running
    instance" — there is no gap for a second process to observe "no instance" while
    one is still starting up, and a killed process releases the lock automatically
    on exit/crash regardless of whether it got to clean up the PID file.
    """
    pid_path = paths.pid_file()
    fh = open(pid_path, "a+")  # noqa: SIM115 - kept open for the process lifetime, closed in _run()'s finally
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        logger.error("daemon already running", extra={"detail": {"pid_file": str(pid_path)}})
        sys.exit(1)
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


async def _run() -> None:
    configure_logging()
    lock_fh = _acquire_single_instance_lock()

    try:
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
    finally:
        # Closing the fd releases the flock, so this must happen last: anything
        # above that fails (or a future exception) still leaves the lock held
        # until here rather than opening a window for a second instance to start.
        lock_fh.close()
        paths.pid_file().unlink(missing_ok=True)


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
