"""Structured JSON-lines logging (DEV.md 工程原则 #4: 诚实失败, 日志有结构)."""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any


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


def configure_logging(level: int = logging.INFO, stream: Any = None) -> None:
    """Configure the root `jones_daemon` logger to emit one JSON object per line."""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonLinesFormatter())
    logger = logging.getLogger("jones_daemon")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"jones_daemon.{name}")
