"""Bar clock: align cycles to interval boundaries (UTC) with a small grace period after the close."""

from __future__ import annotations

import asyncio
import time


class SystemClock:
    def now_ms(self) -> int:
        return int(time.time() * 1000)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


def bar_open_ms(now_ms: int, interval_ms: int) -> int:
    return (now_ms // interval_ms) * interval_ms


def last_closed_bar_open_ms(now_ms: int, interval_ms: int) -> int:
    """Open time of the most recent bar whose close is already in the past."""
    return bar_open_ms(now_ms, interval_ms) - interval_ms


def next_close_ms(now_ms: int, interval_ms: int) -> int:
    return bar_open_ms(now_ms, interval_ms) + interval_ms


async def wait_for_bar_close(clock: object, interval_ms: int, grace_seconds: float = 5.0) -> int:
    """Sleep until the current bar closes (+grace).  Returns that bar's open time (the bar that just closed)."""
    now = clock.now_ms()  # type: ignore[attr-defined]
    target = next_close_ms(now, interval_ms) + int(grace_seconds * 1000)
    while True:
        now = clock.now_ms()  # type: ignore[attr-defined]
        remaining = (target - now) / 1000.0
        if remaining <= 0:
            break
        await clock.sleep(min(remaining, 60.0))  # type: ignore[attr-defined]
    return target - int(grace_seconds * 1000) - interval_ms
