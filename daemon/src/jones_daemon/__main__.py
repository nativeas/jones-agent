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
import sqlite3
import sys
from typing import TextIO

from jones_daemon import paths
from jones_daemon.agents.methods import register as register_agents
from jones_daemon.capabilities.browser import shutdown_all as shutdown_all_browsers
from jones_daemon.capabilities.methods import register as register_capabilities
from jones_daemon.config.methods import register as register_config
from jones_daemon.config.resolver import DefaultConfigResolver
from jones_daemon.context import DaemonContext
from jones_daemon.logging import configure_logging, get_logger
from jones_daemon.projects.bootstrap import bootstrap_projects_and_agents
from jones_daemon.projects.methods import register as register_projects
from jones_daemon.providers import methods as providers_methods
from jones_daemon.providers.resolver import DaemonProviderResolver
from jones_daemon.rpc.methods import register_builtin_methods, register_daemon_status
from jones_daemon.rpc.server import RpcServer
from jones_daemon.scheduler import methods as scheduler_methods
from jones_daemon.secrets.vault import build_default_vault
from jones_daemon.service import maybe_handle_cli
from jones_daemon.sessions import methods as sessions_methods
from jones_daemon.skills.methods import register as register_skills
from jones_daemon.store import apply_pending, connect, run_in_db_thread
from jones_daemon.store.migrator import SchemaTooNewError

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


def _release_single_instance_lock(fh: TextIO) -> None:
    """Release the lock acquired by `_acquire_single_instance_lock`, in the only
    order that keeps "holds the lock" and "the PID file exists" consistent at
    every instant: unlink the PID file *while still holding the flock*, and only
    then close the fd (which is what actually releases the lock).

    Reversed, this reopens the exact race the lock exists to close: closing the
    fd first releases the lock immediately, and in the window before this
    process gets around to unlinking, a second daemon can acquire the
    now-free lock and write *its own* PID into the file — which this process
    then deletes out from under it on its next line, leaving the new instance's
    PID file gone while it's still very much running.
    """
    paths.pid_file().unlink(missing_ok=True)
    fh.close()


async def _run() -> None:
    configure_logging()
    lock_fh = _acquire_single_instance_lock()

    try:
        # connect() *and* apply_pending() must run on the same dedicated DB
        # thread: connect() is what establishes a check_same_thread=True
        # connection's home thread, so opening it on the event loop thread and
        # then only offloading later queries would still violate that
        # invariant (see store/db.py's module docstring).
        def _open_store() -> tuple[sqlite3.Connection, int]:
            db_conn = connect(paths.db_path())
            schema_version = apply_pending(db_conn)
            return db_conn, schema_version

        try:
            conn, version = await run_in_db_thread(_open_store)
        except SchemaTooNewError as exc:
            # PRD 11.3 "升级不丢数据" / 05-w6-interfaces.md §3.3: an older build
            # opening a database a newer build already migrated must refuse to
            # start, explicitly — not silently downgrade or half-start against
            # tables/columns it doesn't understand. Logged (not just raised) so
            # this shows up in the structured daemon.err.log a launchd-managed
            # instance leaves behind, not only in an interactive terminal's
            # traceback.
            logger.error("refusing to start: database schema is newer than this build",
                          extra={"detail": {"error": str(exc)}})
            raise
        logger.info("store ready", extra={"detail": {"schema_version": version}})

        # C (#8 #9): default Project/Agent bootstrap — the logic itself lives in
        # projects/bootstrap.py (C's own package), not here; see that module's
        # docstring for why. Must run on the DB thread, before the RPC server
        # starts accepting connections.
        await run_in_db_thread(bootstrap_projects_and_agents, conn)

        server = RpcServer(paths.sock_file())
        register_builtin_methods(server)

        # Real `DaemonContext` (docs/design/01-w2-interfaces.md §1): B/#7's
        # `DaemonProviderResolver` and C/#8#9's `DefaultConfigResolver`, shared by
        # every module's register().
        vault = build_default_vault(paths.secrets_dir())
        ctx = DaemonContext(
            db=conn,
            paths=paths,
            server=server,
            providers=DaemonProviderResolver(conn, vault),
            config=DefaultConfigResolver(conn),
        )
        providers_methods.register(server, ctx)
        register_config(server, ctx)
        register_projects(server, ctx)
        register_agents(server, ctx)
        register_skills(server, ctx)
        register_capabilities(server, ctx)  # Issue #17/#19 daemon 侧: `capability.list`
        session_service = sessions_methods.register(server, ctx)
        register_daemon_status(server, session_service)  # 02-w3-interfaces.md §2 集成收口 #2
        await session_service.startup()
        # Issue #20 (docs/design/04-w5-interfaces.md §2): built on top of
        # `session_service`'s own public create/send/get surface, so it must be
        # constructed (and started) after `startup()` above has ensured the main
        # session exists.
        cron_service = scheduler_methods.register(server, ctx, session_service)
        await cron_service.start()
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
        # Stop dispatching new cron triggers before tearing `session_service`
        # down — it dispatches through that service's public methods and must
        # not still be mid-call into it once `shutdown()` starts closing workers.
        await cron_service.stop()
        await session_service.shutdown()
        # J/#15 (review findings #2/#9): gracefully close the Jones-dedicated
        # Chrome, if one was ever started, before the daemon exits — SIGTERM
        # before SIGKILL is load-bearing for login-state persistence
        # (00-foundation.md §9.1 证据 3), and `shutdown_all()` had no caller at
        # all until this line. Off the event loop thread since it blocks on
        # process I/O (up to `DEFAULT_SHUTDOWN_TIMEOUT_S`).
        await asyncio.to_thread(shutdown_all_browsers)
        # close() is also a synchronous sqlite3 call bound to the connection's
        # home thread (check_same_thread=True) — it must run there too.
        await run_in_db_thread(conn.close)
    finally:
        # Anything above that fails (or a future exception) still leaves the
        # lock held until here rather than opening a window for a second
        # instance to start — see _release_single_instance_lock for why the
        # unlink-then-close order (not the reverse) is what makes that true.
        _release_single_instance_lock(lock_fh)


def main() -> None:
    # `python -m jones_daemon service install|uninstall|status` (Issue #6, design §6)
    # short-circuits here instead of starting the daemon event loop below.
    exit_code = maybe_handle_cli(sys.argv[1:])
    if exit_code is not None:
        sys.exit(exit_code)
    asyncio.run(_run())


if __name__ == "__main__":
    main()
