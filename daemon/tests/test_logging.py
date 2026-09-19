"""`logging.py::configure_logging`'s `logs_dir` rotation (Issue #23 round-1
review, 04-w5-interfaces.md §5 "logs/ 滚动 7 天") — before this, the daemon never
wrote any file of its own under `logs_dir`, only launchd's `StandardOutPath`/
`StandardErrorPath` redirects, which `store/maintenance.py::rotate_logs`'s mtime
sweep can never touch while the process is up (see that function's own
docstring). This proves the daemon's own structured log stream now gets a real,
self-rotating file handler instead."""

from __future__ import annotations

import json
import logging
from logging.handlers import TimedRotatingFileHandler

from jones_daemon.logging import LOG_RETENTION_DAYS, configure_logging, get_logger


def teardown_function(_fn) -> None:
    # `configure_logging` mutates the shared `logging.getLogger("jones_daemon")`
    # singleton — reset it after each test so one test's handlers (and any open
    # file descriptors they hold) don't leak into the next.
    logger = logging.getLogger("jones_daemon")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def test_configure_logging_without_logs_dir_only_adds_the_stream_handler():
    configure_logging()
    logger = logging.getLogger("jones_daemon")
    assert len(logger.handlers) == 1
    assert isinstance(logger.handlers[0], logging.StreamHandler)


def test_configure_logging_with_logs_dir_adds_a_rotating_file_handler_and_writes_json_lines(
    tmp_path,
):
    logs_dir = tmp_path / "logs"
    configure_logging(logs_dir=logs_dir)

    logger = logging.getLogger("jones_daemon")
    file_handlers = [h for h in logger.handlers if isinstance(h, TimedRotatingFileHandler)]
    assert len(file_handlers) == 1
    handler = file_handlers[0]
    assert handler.backupCount == LOG_RETENTION_DAYS

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

    configure_logging(logs_dir=logs_dir)

    assert logs_dir.exists()
