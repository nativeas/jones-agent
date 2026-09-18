import os
import shutil
import tempfile
from pathlib import Path

import pytest

from jones_daemon import paths
from jones_daemon.__main__ import _acquire_single_instance_lock, _release_single_instance_lock


@pytest.fixture(autouse=True)
def jones_home(monkeypatch):
    # Short-lived dir directly under the system temp root: an AF_UNIX socket path
    # under pytest's nested tmp_path can exceed macOS's ~104-byte sun_path limit.
    home = Path(tempfile.mkdtemp(prefix="jn-"))
    monkeypatch.setenv("JONES_HOME", str(home))
    yield
    shutil.rmtree(home, ignore_errors=True)


def test_first_instance_acquires_the_lock_and_writes_its_pid():
    fh = _acquire_single_instance_lock()
    try:
        assert paths.pid_file().read_text().strip() == str(os.getpid())
    finally:
        fh.close()


def test_second_instance_is_rejected_while_the_first_holds_the_lock():
    fh = _acquire_single_instance_lock()
    try:
        with pytest.raises(SystemExit) as exc_info:
            _acquire_single_instance_lock()
        assert exc_info.value.code == 1
    finally:
        fh.close()


def test_lock_is_released_when_the_holder_closes_its_handle():
    fh = _acquire_single_instance_lock()
    fh.close()  # simulates process exit: the OS releases the flock automatically

    fh2 = _acquire_single_instance_lock()  # must not raise SystemExit
    fh2.close()


def test_missing_pid_file_does_not_bypass_the_lock_check():
    # The old PID+socket probe treated "no PID file" as "no instance running" and
    # returned early with no check at all — a cleared runtime/ dir or a restored
    # backup could then let a second daemon start alongside a live one. There must
    # be no such bypass: acquiring the lock still has to work correctly (and be
    # exclusive) even though nothing has ever created the PID file yet.
    assert not paths.pid_file().exists()
    fh = _acquire_single_instance_lock()
    try:
        assert paths.pid_file().read_text().strip() == str(os.getpid())
        with pytest.raises(SystemExit):
            _acquire_single_instance_lock()
    finally:
        fh.close()


def test_release_unlinks_the_pid_file_before_releasing_the_flock(monkeypatch):
    # Regression test for the exit-order bug: releasing the flock (closing fh)
    # before unlinking the PID file reopens a window where a second daemon can
    # acquire the now-free lock and write *its own* PID into the file, only for
    # this process to then unlink that live file out from under it. The fix is
    # strictly about order, so assert the order directly: unlink must be
    # observed before close.
    fh = _acquire_single_instance_lock()
    pid_path = paths.pid_file()
    call_order: list[str] = []

    real_unlink = Path.unlink

    def tracked_unlink(self, *args, **kwargs):
        if self == pid_path:
            call_order.append("unlink")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", tracked_unlink)
    real_close = fh.close

    def tracked_close():
        call_order.append("close")
        return real_close()

    monkeypatch.setattr(fh, "close", tracked_close)

    _release_single_instance_lock(fh)

    assert call_order == ["unlink", "close"]
    assert not pid_path.exists()


def test_release_lets_a_new_instance_acquire_the_lock_afterwards():
    fh = _acquire_single_instance_lock()
    _release_single_instance_lock(fh)

    fh2 = _acquire_single_instance_lock()  # must not raise SystemExit
    try:
        assert paths.pid_file().read_text().strip() == str(os.getpid())
    finally:
        fh2.close()


def test_stale_pid_file_from_a_crashed_process_does_not_block_a_new_instance():
    # A PID file can be left behind with a stale value and no lock held (the
    # process was SIGKILLed before it could clean up). flock is per-open-file, not
    # tied to the PID text written inside it, so a new instance must still be able
    # to acquire it and overwrite the stale content.
    paths.pid_file().write_text("999999")
    fh = _acquire_single_instance_lock()
    try:
        assert paths.pid_file().read_text().strip() == str(os.getpid())
    finally:
        fh.close()
