"""Shared id/timestamp helpers for domain rows (docs/design/00-foundation.md §5: every
domain table has `id TEXT PRIMARY KEY` as a ULID, `created_at`/`updated_at` as
ISO-8601 UTC text).

Lives under `config/` (owned by branch C, docs/design/01-w2-interfaces.md §0) rather
than a new top-level module, since both `projects/` and `agents/` (also owned by C)
need it and `config/` has no dependency on either — avoids a cross-package import
cycle without adding a shared file outside C's owned directories.

Pure stdlib: no new dependency for a ~15-line ULID encoder.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime

_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> str:
    """A ULID: 48-bit millisecond timestamp + 80 bits of randomness, Crockford
    base32 encoded to 26 characters. Lexicographically sortable by creation time,
    which is incidental here (nothing in this codebase relies on the ordering) —
    it's simply the standard, and the id format §5 already commits to.
    """
    ms = int(time.time() * 1000)
    payload = ms.to_bytes(6, "big") + os.urandom(10)
    value = int.from_bytes(payload, "big")
    chars = [""] * 26
    for i in range(25, -1, -1):
        value, rem = divmod(value, 32)
        chars[i] = _CROCKFORD_ALPHABET[rem]
    return "".join(chars)


def now_iso() -> str:
    """Millisecond-precision ISO-8601 UTC, `Z`-suffixed (design §4.1: 所有时间为
    ISO-8601 UTC)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
