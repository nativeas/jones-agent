"""Structured JSON-lines logging (DEV.md 工程原则 #4: 诚实失败, 日志有结构)."""

from __future__ import annotations

import json
import logging
import sys
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

# Mirrors `store/maintenance.py::LOG_RETENTION_DAYS` (PRD 10.3 "守护进程 ... 日志
# ... 滚动保留 7 天"). Duplicated as a plain constant, not imported, to keep this
# module import-cycle-free — `store/maintenance.py` already imports
# `get_logger` from here, so the reverse import isn't possible.
LOG_RETENTION_DAYS = 7


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
    stdout/stderr (`service.py::render_plist`), which `store/maintenance.py::
    rotate_logs`'s mtime sweep can't touch while the process keeps appending to
    them (see that function's own docstring — still true, still not fixed here).
    So the 7-day retention requirement had nothing under `logs_dir` it could ever
    actually satisfy. When `logs_dir` is given, this adds a *second* handler — a
    `TimedRotatingFileHandler` writing `logs_dir/daemon.log`, rolling at
    midnight UTC and keeping only `LOG_RETENTION_DAYS` old files — so the
    structured JSON-lines log stream this daemon actually controls gets real,
    self-managed 7-day rotation, independent of launchd or the mtime sweep. The
    existing stderr handler stays either way (dev/foreground convenience, and
    still what launchd's redirect captures) — this is additive, not a
    replacement.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler(stream or sys.stderr)]
    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            TimedRotatingFileHandler(
                logs_dir / "daemon.log",
                when="midnight",
                utc=True,
                backupCount=LOG_RETENTION_DAYS,
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
