"""The loop's own metrics recording: one truth for research and live (DL-Q6 / KILL-Q11).

KILL-Q11's finding is that research and live read *different sources* - the T+1 daily archive and the
30-day REST window - so a metrics signal validated on one and traded on the other has never been
tested on what it will actually see.  The archive/REST stamp offset showed how quietly that goes
wrong: the same numbers under two conventions, and a five-minute look-ahead in every bucket.

The answer is not a second copy of the data, it is a recording of *what the loop could read*.  The
loop polls the REST window each cycle and appends the buckets to its own store; that store is the
common truth, and the archive becomes something to CHECK it against rather than something to trade on.

**No separate five-minute daemon**, and the reasoning matters more than the code.  The granularity
that matters belongs to the BUCKET (5m), not to the poll: the REST window is 30 days deep, so one
poll an hour retrieves all twelve buckets of the past hour with nothing missed.  Polling at the bar
close also does something a 5m daemon cannot - it records exactly the buckets that had CLOSED when the
loop made its decision, which is precisely the set `align_to_bars` selects.  A daemon would be a
second process, a second failure mode and a second thing to restart, in exchange for a superset of the
same rows recorded at instants no decision was made at.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

import pandas as pd

from beidou_data.metrics import parse_rest_rows, period_ms
from beidou_data.store import MetricsStore

METRICS_PATH = "/futures/data/openInterestHist"


class _Getter(Protocol):
    async def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any: ...


async def snapshot_metrics(
    client: _Getter,
    store: MetricsStore,
    symbols: Sequence[str],
    *,
    period: str = "5m",
    limit: int = 12,
) -> dict[str, Any]:
    """Record the metrics buckets this loop could read, symbol by symbol.

    Best-effort by contract, like every other instrument in the cycle: collecting data may never be
    the reason a book stops trading, so a failure is returned in the result rather than raised.
    """
    step = period_ms(period)
    stored: dict[str, int] = {}
    try:
        for symbol in symbols:
            rows = await client.get(METRICS_PATH, {"symbol": symbol, "period": period, "limit": int(limit)})
            if not rows:
                continue
            stored[symbol] = store.append(symbol, parse_rest_rows(rows, step))
    except Exception as exc:
        return {"stored": stored if stored else {}, "error": f"{type(exc).__name__}: {exc}"}
    return {"stored": stored}


def metrics_parity(snapshot: pd.DataFrame, archive: pd.DataFrame, *, tolerance: float = 1e-6) -> dict[str, Any]:
    """M-011: do the two sources agree on the buckets they share?

    The rate is ``None`` rather than 1.0 when nothing overlaps.  Zero disagreements out of zero
    comparisons is not agreement - reporting it as perfect is how a dead snapshot stream would look
    healthiest exactly while it stopped recording.
    """
    if snapshot.empty or archive.empty:
        return {"overlapping": 0, "differing": 0, "rate": None}
    columns = [c for c in snapshot.columns if c in archive.columns and c not in ("open_time", "symbol")]
    merged = snapshot.merge(archive, on="open_time", suffixes=("_snap", "_arch"))
    if merged.empty or not columns:
        return {"overlapping": 0, "differing": 0, "rate": None}
    differing = pd.Series(False, index=merged.index)
    for column in columns:
        left, right = merged[f"{column}_snap"], merged[f"{column}_arch"]
        differing |= ~((left - right).abs() <= tolerance) & left.notna() & right.notna()
    count = int(differing.sum())
    return {"overlapping": len(merged), "differing": count, "rate": count / len(merged)}


def live_coverage_bars(store: MetricsStore, symbols: Sequence[str], *, interval_ms: int, period: str = "5m") -> int:
    """How many whole BARS the live snapshot can answer for, across the whole universe.

    Counted in bars because that is the unit a strategy's warmup is in, and taken as the THINNEST
    symbol because a book trades a universe: coverage the whole universe does not have is not
    coverage, it is coverage of a different book.

    Counted from the number of buckets actually held, not from first-to-last span.  A span treats a
    gap - the loop was down for a day - as covered, and for a GATE that is the wrong direction to be
    wrong in: it would admit a strategy onto data with holes.  Counting buckets under-reports across a
    gap instead, which delays a promotion rather than allowing a bad one.
    """
    if not symbols:
        return 0
    step = period_ms(period)
    counts: list[int] = []
    for symbol in symbols:
        frame = store.load(symbol)
        if frame.empty:
            return 0
        counts.append(len(frame))
    return int(min(counts) * step // int(interval_ms))
