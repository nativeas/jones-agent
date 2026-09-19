"""Structured JSON-lines logging (DEV.md 工程原则 #4: 诚实失败, 日志有结构)."""

from __future__ import annotations

import json
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

# Round-3 review (controller ruling R-O3): `daemon.log` used to rotate on a
# midnight-UTC/7-day-backupCount `TimedRotatingFileHandler` — a *time* budget
# with no *size* ceiling, so a chatty day could still write an unbounded amount
# before the clock rolled it over. Switched to a size-based
# `RotatingFileHandler`: 10MB per file, 7 files kept (≤70MB worst case,
# independent of how much the daemon logs in any given day) — this supersedes
# `store/maintenance.py`'s old `rotate_logs`/`run_log_rotation_loop` mtime
# sweep for `logs_dir` entirely (see that module's own history for why the
# sweep existed and why it's gone now: nothing but launchd's plain-text
# redirect is left under `logs_dir` for it to have ever cleaned).
DAEMON_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10MB per file
DAEMON_LOG_BACKUP_COUNT = 7  # keep the 7 most-recent rotated files


class JsonLinesFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        extra = getattr(record, "detail", None)
        if extra is not None:
            payload["detail"] = extra
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(
    level: int = logging.INFO, stream: Any = None, logs_dir: Path | None = None
) -> None:
    """Configure the root `jones_daemon` logger to emit one JSON object per line.

    Round-1 review (04-w5-interfaces.md §5 "logs/ 滚动 7 天"): before this, the
    daemon never wrote its own log file at all — `logs_dir` only ever held
    launchd's raw `StandardOutPath`/`StandardErrorPath` redirect of the process's
    stdout/stderr (`service.py::render_plist`), which the old mtime sweep could
    never touch while the process kept appending to it. So the retention
    requirement had nothing under `logs_dir` it could ever actually satisfy.
    When `logs_dir` is given, this adds a *second* handler — a
    `RotatingFileHandler` writing `logs_dir/daemon.log`, capped at
    `DAEMON_LOG_MAX_BYTES` per file and keeping `DAEMON_LOG_BACKUP_COUNT` old
    ones — so the structured JSON-lines log stream this daemon actually
    controls gets real, self-managed, size-bounded rotation.

    Round-3 review (controller ruling R-O3): the stream handler (stderr by
    default) is now only added when its target is an actual live terminal
    (`isatty()`) — under launchd, stderr is `StandardErrorPath`'s plain-text
    file redirect, not a terminal, and every line it received was already a
    duplicate of what `daemon.log` above just wrote in structured form, with
    none of `daemon.log`'s own size-bounded rotation (the exact "unbounded
    file this daemon can't clean up" problem the old mtime sweep existed to
    work around, and still couldn't). Passing an explicit `stream=` (as tests
    do, or a caller that wants a stream regardless of what it is) is checked
    for `isatty()` the same way as the default `sys.stderr` — there is no
    separate "always add it" path; a caller that wants to force a handler
    despite `isatty()` being false should add one itself via
    `logging.getLogger("jones_daemon")` after calling this.
    """
    handlers: list[logging.Handler] = []
    target_stream = stream if stream is not None else sys.stderr
    if getattr(target_stream, "isatty", lambda: False)():
        handlers.append(logging.StreamHandler(target_stream))
    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                logs_dir / "daemon.log",
                maxBytes=DAEMON_LOG_MAX_BYTES,
                backupCount=DAEMON_LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
        )
    logger = logging.getLogger("jones_daemon")
    logger.handlers.clear()
    for handler in handlers:
        handler.setFormatter(JsonLinesFormatter())
        logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"jones_daemon.{name}")
