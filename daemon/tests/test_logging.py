"""`logging.py::configure_logging`'s `logs_dir` rotation (Issue #23 round-1
review, 04-w5-interfaces.md §5 "logs/ 滚动 7 天") — before this, the daemon never
wrote any file of its own under `logs_dir`, only launchd's `StandardOutPath`/
`StandardErrorPath` redirects, which the old `store/maintenance.py::rotate_logs`
mtime sweep could never touch while the process was up. This proves the
daemon's own structured log stream now gets a real, self-rotating file handler
instead.

Round-3 review (controller ruling R-O3): `daemon.log` moved from time-based
(`TimedRotatingFileHandler`, midnight/7-day) to size-based
(`RotatingFileHandler`, 10MB × 7 files) rotation, and the stderr handler is now
gated on `isatty()` — added only for an actual live terminal, not launchd's
plain-text file redirect (which duplicated every line `daemon.log` already
wrote, unboundedly, with none of `daemon.log`'s own rotation)."""

from __future__ import annotations

import io
import json
import logging
from logging.handlers import RotatingFileHandler

from jones_daemon.logging import (
    DAEMON_LOG_BACKUP_COUNT,
    DAEMON_LOG_MAX_BYTES,
    configure_logging,
    get_logger,
)


def teardown_function(_fn) -> None:
    # `configure_logging` mutates the shared `logging.getLogger("jones_daemon")`
    # singleton — reset it after each test so one test's handlers (and any open
    # file descriptors they hold) don't leak into the next.
    logger = logging.getLogger("jones_daemon")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


class _NotATty(io.StringIO):
    def isatty(self) -> bool:
        return False


class _ATty(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_configure_logging_with_a_non_tty_stream_and_no_logs_dir_adds_no_handler():
    # Round-3 review: launchd's stderr redirect is exactly this shape — not a
    # terminal, no logs_dir wired through it. No handler at all means nothing
    # duplicates `daemon.log`'s structured stream into an unrotated file.
    configure_logging(stream=_NotATty())
    logger = logging.getLogger("jones_daemon")
    assert logger.handlers == []


def test_configure_logging_with_a_tty_stream_adds_the_stream_handler():
    stream = _ATty()
    configure_logging(stream=stream)
    logger = logging.getLogger("jones_daemon")
    assert len(logger.handlers) == 1
    assert isinstance(logger.handlers[0], logging.StreamHandler)

    get_logger("test").info("hello")
    logger.handlers[0].flush()
    assert "hello" in stream.getvalue()


def test_configure_logging_with_logs_dir_adds_a_rotating_file_handler_and_writes_json_lines(
    tmp_path,
):
    logs_dir = tmp_path / "logs"
    configure_logging(stream=_NotATty(), logs_dir=logs_dir)

    logger = logging.getLogger("jones_daemon")
    file_handlers = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
    assert len(file_handlers) == 1
    handler = file_handlers[0]
    assert handler.backupCount == DAEMON_LOG_BACKUP_COUNT
    assert handler.maxBytes == DAEMON_LOG_MAX_BYTES

    get_logger("test").info("hello", extra={"detail": {"k": "v"}})
    for h in logger.handlers:
        h.flush()

    log_file = logs_dir / "daemon.log"
    assert log_file.exists()
    lines = [ln for ln in log_file.read_text(encoding="utf-8").splitlines() if ln]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["message"] == "hello"
    assert record["detail"] == {"k": "v"}


def test_configure_logging_creates_logs_dir_if_missing(tmp_path):
    logs_dir = tmp_path / "does" / "not" / "exist" / "yet"
    assert not logs_dir.exists()

    configure_logging(stream=_NotATty(), logs_dir=logs_dir)

    assert logs_dir.exists()
