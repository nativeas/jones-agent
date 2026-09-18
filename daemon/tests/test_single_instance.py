import asyncio
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from jones_daemon import paths
from jones_daemon.__main__ import _check_existing_instance


@pytest.fixture(autouse=True)
def jones_home(monkeypatch):
    # Short-lived dir directly under the system temp root: an AF_UNIX socket path
    # under pytest's nested tmp_path can exceed macOS's ~104-byte sun_path limit.
    home = Path(tempfile.mkdtemp(prefix="jn-"))
    monkeypatch.setenv("JONES_HOME", str(home))
    yield
    shutil.rmtree(home, ignore_errors=True)


def test_no_pid_file_is_a_noop():
    _check_existing_instance()  # must not raise / exit
    assert not paths.pid_file().exists()


def test_stale_pid_file_dead_process_is_cleaned_up():
    # PID 0 is never a real user process id we own; pick something guaranteed dead by
    # spawning and immediately reaping a child.
    import subprocess

    proc = subprocess.Popen(["true"])
    proc.wait()
    dead_pid = proc.pid

    paths.pid_file().write_text(str(dead_pid))

    _check_existing_instance()  # must not exit

    assert not paths.pid_file().exists()


def test_pid_alive_but_socket_not_accepting_is_cleaned_up():
    paths.pid_file().write_text(str(os.getpid()))
    # No socket listening at paths.sock_file() -> treated as stale.
    _check_existing_instance()
    assert not paths.pid_file().exists()


async def test_pid_alive_and_socket_accepting_exits(unused=None):
    paths.pid_file().write_text(str(os.getpid()))
    server = await asyncio.start_unix_server(lambda r, w: None, path=str(paths.sock_file()))
    try:
        with pytest.raises(SystemExit) as exc_info:
            _check_existing_instance()
        assert exc_info.value.code == 1
    finally:
        server.close()
        await server.wait_closed()
