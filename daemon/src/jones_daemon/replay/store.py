"""Payload store for Run replay (docs/design/02-w3-interfaces.md §2, PRD 10.3):

    <user_root>/runs/<run_id>/<step_seq>.<ext>       # full tool output/screenshot
    <user_root>/runs/<run_id>/prompt_snapshot.json    # what `_run_turn` could honestly
                                                        # capture of the sent prompt

`steps.payload_ref`/`runs.prompt_snapshot_ref` store the *relative* ref
(`"<run_id>/<name>"`) returned by `write_payload`/`write_prompt_snapshot`, never an
absolute path — SQLite stays the source of truth for whether a payload exists at
all (PRD 10.4); this module only ever answers "given a ref the DB already trusts,
where's the file" or "make one".

Every function here does blocking file I/O and must be called off the asyncio event
loop thread — via `asyncio.to_thread` for the write path (02-w3-interfaces.md §3:
"回放 payload 写入异步、不阻塞 ACP 读循环" — the ACP client's read loop awaits
`SessionService._on_session_update` inline for every incoming line, so a write done
inline there would stall reading the *next* ACP event on the wire) and via
`store.run_in_db_thread` is NOT required for these (no sqlite3.Connection touched),
but callers reading a large payload for an RPC response should still offload with
`asyncio.to_thread` for the same "don't block the event loop" reason, just not for
the same-thread-affinity reason `run_in_db_thread` exists for.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

# 1 MiB — `run.payload`'s pagination threshold (02-w3-interfaces.md §2: "大于 1MB
# 走分片 offset/limit"). Not a hard cap: a caller may still request a larger
# `limit` explicitly; this is only the size past which a whole-file read isn't the
# right default for an RPC response (00-foundation.md §4's 16 MiB NDJSON line cap
# would eventually choke on an un-chunked huge payload anyway).
CHUNK_THRESHOLD_BYTES = 1024 * 1024

_PROMPT_SNAPSHOT_NAME = "prompt_snapshot.json"


class PayloadRefError(ValueError):
    """Raised for a `ref` that doesn't resolve to a real file under `runs/`, or
    that attempts to escape it (e.g. `../../secrets/vault.enc`) — a `ref` this
    module is asked to read always ultimately came from a `steps.payload_ref`/
    `runs.prompt_snapshot_ref` DB column, but callers (RPC handlers) must not
    trust that without checking (defense in depth, PRD N10/10.4: replay must never
    become a path to reading files outside `runs/`)."""


def runs_root(user_root: Path) -> Path:
    return user_root / "runs"


def payload_dir(user_root: Path, run_id: str) -> Path:
    return runs_root(user_root) / run_id


def _resolve_ref(user_root: Path, ref: str) -> Path:
    root = runs_root(user_root).resolve()
    candidate = (root / ref).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise PayloadRefError(f"payload ref escapes runs/ root: {ref!r}") from None
    if not candidate.is_file():
        raise PayloadRefError(f"no payload file for ref: {ref!r}")
    return candidate


def write_payload(user_root: Path, run_id: str, seq: int, data: bytes, ext: str) -> str:
    """Write one Step's full payload. Synchronous/blocking — see module docstring
    for why callers must offload this off the event loop thread."""
    d = payload_dir(user_root, run_id)
    d.mkdir(parents=True, exist_ok=True)
    # `ext` always comes from a small fixed set this codebase chooses (json/txt/png
    # today), never from worker-controlled data — no sanitization needed beyond
    # this being a plain filename component.
    path = d / f"{seq}.{ext}"
    path.write_bytes(data)
    return f"{run_id}/{seq}.{ext}"


def write_prompt_snapshot(user_root: Path, run_id: str, snapshot: dict[str, Any]) -> str:
    """Write the honest, partial prompt-snapshot JSON for a Run (see `_run_turn`'s
    call site — 00-foundation.md §7's "如实记录「Hermes 未暴露」" requirement)."""
    d = payload_dir(user_root, run_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / _PROMPT_SNAPSHOT_NAME
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"{run_id}/{_PROMPT_SNAPSHOT_NAME}"


def payload_size(user_root: Path, ref: str) -> int:
    return _resolve_ref(user_root, ref).stat().st_size


def read_payload(
    user_root: Path, ref: str, *, offset: int = 0, limit: int | None = None
) -> bytes:
    """Read (a byte range of) a payload file. `offset`/`limit` implement
    02-w3-interfaces.md §2's ">1MB 走分片" — `limit=None` reads to EOF."""
    path = _resolve_ref(user_root, ref)
    with path.open("rb") as f:
        f.seek(offset)
        return f.read(limit) if limit is not None else f.read()


def purge_run(user_root: Path, run_id: str) -> None:
    """Delete `<user_root>/runs/<run_id>/` entirely — the filesystem half of "删
    Run 时联动删除" (PRD 10.3) and of the 90-day retention sweep
    (`replay/retention.py`). The caller is responsible for clearing the DB's
    `payload_ref`/`prompt_snapshot_ref` columns afterward (SQLite stays the source
    of truth for *whether* a payload exists — PRD 10.4 — this function only ever
    touches the filesystem side)."""
    d = payload_dir(user_root, run_id)
    if d.exists():
        shutil.rmtree(d)
