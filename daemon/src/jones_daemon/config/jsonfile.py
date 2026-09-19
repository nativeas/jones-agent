"""Tiny JSON-file read/write helpers shared by `config/resolver.py` and
`config/methods.py` — user-level `~/.jones/config/*.json` and project-level
`<project>/.jones/*.json` are the storage medium for settings/permissions/mcp
(PRD §10.2/§10.3), not SQLite.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jones_daemon.logging import get_logger

logger = get_logger("config")


def read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    """Best-effort read: a missing file is the normal "nothing configured at this
    scope yet" case, not an error. A file that exists but fails to parse is a real
    problem (user hand-edited it and broke it, or it's corrupt) — surfacing that as
    a fallback to `default` would silently discard the user's rules (dangerous for
    permissions.json specifically), so that case logs a warning and still falls
    back rather than crashing the whole resolver on one bad project.
    """
    if not path.exists():
        return dict(default)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "failed to read config file, falling back to defaults",
            extra={"detail": {"path": str(path), "error": str(exc)}},
        )
        return dict(default)
    if not isinstance(data, dict):
        logger.warning(
            "config file did not contain a JSON object, falling back to defaults",
            extra={"detail": {"path": str(path)}},
        )
        return dict(default)
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
