"""ULID generation for domain object primary keys (docs/design/00-foundation.md §5:
"所有表有 id TEXT PRIMARY KEY（ULID）"). Stdlib only — no new dependency for a ~30
line algorithm (DEV.md 工程原则 #6: 不引重型抽象).

ULID = 48-bit millisecond timestamp + 80 bits of randomness, Crockford Base32
encoded to 26 characters; lexicographic order matches creation order, which is
useful for `messages`/`steps` ordering even before `seq` is assigned.
"""

from __future__ import annotations

import os
import time

_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> str:
    ms = time.time_ns() // 1_000_000
    value = (ms << 80) | int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(26):
        value, rem = divmod(value, 32)
        chars.append(_CROCKFORD_ALPHABET[rem])
    return "".join(reversed(chars))
