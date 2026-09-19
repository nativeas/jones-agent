"""Tiny JSON-file read/write helpers shared by `config/resolver.py` and
`config/methods.py` — user-level `~/.jones/config/*.json` and project-level
`<project>/.jones/*.json` are the storage medium for settings/permissions/mcp
(PRD §10.2/§10.3), not SQLite.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from jones_daemon.logging import get_logger

logger = get_logger("config")


def read_json_result(path: Path) -> tuple[dict[str, Any] | None, bool]:
    """Read `path` as a JSON object, distinguishing "nothing here" from "something
    here that's broken". Returns `(data, ok)`:
      - missing file:                    `(None, True)`  — nothing configured yet
      - present, valid JSON object:       `(data, True)`
      - present but unparsable / not an
        object (hand-edit broke it):      `(None, False)`

    `read_json` (below) is the convenience wrapper for callers that don't care
    about the distinction (settings.json, mcp.json — losing an override there is
    a nuisance). `ok=False` matters specifically for permissions.json: silently
    falling back to `default` there means every deny rule at that scope vanishes
    with no signal a caller can act on (see `ConfigResolver.permissions`'s
    `degraded` flag) — dangerous enough that the caller needs to see it.
    """
    if not path.exists():
        return None, True
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "failed to read config file, falling back to defaults",
            extra={"detail": {"path": str(path), "error": str(exc)}},
        )
        return None, False
    if not isinstance(data, dict):
        logger.warning(
            "config file did not contain a JSON object, falling back to defaults",
            extra={"detail": {"path": str(path)}},
        )
        return None, False
    return data, True


def read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    """Best-effort read: a missing file is the normal "nothing configured at this
    scope yet" case, not an error. A file that exists but fails to parse is a real
    problem (user hand-edited it and broke it, or it's corrupt) — surfacing that as
    a fallback to `default` would silently discard the user's rules (dangerous for
    permissions.json specifically, which is why that call site uses
    `read_json_result` directly instead — see its docstring), so that case logs a
    warning and still falls back rather than crashing the whole resolver on one bad
    project.
    """
    data, ok = read_json_result(path)
    return dict(default) if not ok or data is None else data


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Write `data` as pretty JSON, atomically: a crash or power loss mid-write
    must never leave `path` holding a truncated/partial file — that's exactly the
    "broken JSON" case `read_json`/`read_json_result` above have to degrade for,
    and for permissions.json a truncated file reads as "no rules configured"
    (fail-open). Write to a sibling temp file first, then `os.replace` it over the
    real path — `os.replace` is atomic on the same filesystem (POSIX rename(2)),
    so any reader only ever sees the old complete file or the new complete file,
    never a partial one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_name)
        raise
