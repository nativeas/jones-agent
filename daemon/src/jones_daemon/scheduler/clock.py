"""Injectable clock for `CronService` (04-w5-interfaces.md §2: "可注入时钟测试").

`CronService` never calls `datetime.now()`/`asyncio.sleep()` directly — every
"what time is it" / "wait until woken or until this deadline" goes through a
`Clock`, so tests can drive scheduling deterministically (advance time instantly,
assert exactly one wait was issued for the idle case) instead of sleeping in
wall-clock real time or racing a background timer.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, timezone-aware (UTC)."""
        ...

    async def wait(self, event: asyncio.Event, *, timeout: float | None) -> None:
        """Block until `event` is set or `timeout` seconds elapse (None = forever).
        Mirrors `asyncio.wait_for(event.wait(), timeout)` — a timeout is not an
        error here, it's the normal "the timer fired" outcome, so this swallows it
        rather than raising."""
        ...


class RealClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    async def wait(self, event: asyncio.Event, *, timeout: float | None) -> None:
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            pass
