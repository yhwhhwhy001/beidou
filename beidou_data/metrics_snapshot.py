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

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

import pandas as pd

from beidou_data.metrics import REST_SOURCES, parse_rest_rows, period_ms, rest_stamp_offset_ms
from beidou_data.store import MetricsStore

#: Kept so anything naming the open-interest page by hand still resolves; the poll uses `REST_SOURCES`.
METRICS_PATH = REST_SOURCES[0][0]


class _Getter(Protocol):
    async def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any: ...


async def snapshot_metrics(
    client: _Getter,
    store: MetricsStore,
    symbols: Sequence[str],
    *,
    period: str = "5m",
    limit: int = 12,
    concurrency: int = 6,
) -> dict[str, Any]:
    """Record the metrics buckets this loop could read, symbol by symbol.

    Best-effort by contract, like every other instrument in the cycle: collecting data may never be
    the reason a book stops trading, so a failure is returned in the result rather than raised.
    """
    step = period_ms(period)
    stored: dict[str, int] = {}
    missing: dict[str, list[str]] = {}
    # Five endpoints x eighteen symbols is ninety requests, and serially that measured 28.6s against a
    # cycle that takes about twenty - which would push every rebalance a half-minute later and move
    # M-Q08's own reference (adverse fill vs the DECISION CLOSE) by that much.  Bounded rather than
    # unbounded: this runs beside the loop's own market-data fetches on a proxy path that intermittently
    # 503s, and a burst of ninety is how a shared route starts refusing the requests that matter.
    gate = asyncio.Semaphore(concurrency)

    async def one(symbol: str) -> tuple[str, pd.DataFrame | None, list[str], dict[str, int]]:
        frame: pd.DataFrame | None = None
        absent: list[str] = []
        newest: dict[str, int] = {}
        for path, mapping in REST_SOURCES:
            async with gate:
                rows = await client.get(path, {"symbol": symbol, "period": period, "limit": int(limit)})
            if not rows:
                # Recorded per endpoint rather than folded into the row count: an endpoint that stops
                # answering leaves its columns NaN, and NaN in this store is exactly what
                # `metrics_parity` cannot see (it skips NaN pairs and calls that agreement).
                absent.append(path.rsplit("/", 1)[-1])
                continue
            page = parse_rest_rows(rows, step, mapping, symbol=symbol, stamp_offset_ms=rest_stamp_offset_ms(path, step))
            if not page.empty:
                newest[path.rsplit("/", 1)[-1]] = int(page["open_time"].max())
            frame = page if frame is None else _merge(frame, page, mapping.values())
        return symbol, frame, absent, newest

    # Per page, the oldest and the freshest newest-bucket OPEN across the symbols polled: how far behind the
    # bar close REST was when the loop asked (`beidou_live.report_data.snapshot_lag`).  Research aligns a bar
    # to the bucket closing AT its close; `alignment.METRICS` says the latency past that belongs in a report.
    spans: dict[str, list[int]] = {}
    try:
        for symbol, frame, absent, newest in await asyncio.gather(*(one(symbol) for symbol in symbols)):
            for name, opened in newest.items():
                low, high = spans.get(name, [opened, opened])
                spans[name] = [min(low, opened), max(high, opened)]
            if absent:
                missing[symbol] = absent
            if frame is not None:
                # The store is read-modify-write per symbol, so the writes stay serial here even though
                # the fetches did not (DL-Q6: two writers can publish a file one was still writing).
                stored[symbol] = store.append(symbol, frame)
    except Exception as exc:
        return {"stored": stored, "missing": missing, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "stored": stored,
        **({"missing": missing} if missing else {}),
        **({"newest_open_ms": spans} if spans else {}),
    }


def _merge(frame: pd.DataFrame, page: pd.DataFrame, columns: Any) -> pd.DataFrame:
    """Fold one endpoint's columns onto the bucket rows already collected for this symbol.

    Aligned on ``open_time``, which every page carries after `parse_rest_rows` converts the stamp -
    so an endpoint whose window is one bucket short contributes what it has and leaves the rest NaN,
    rather than shifting its values onto the wrong buckets.
    """
    indexed = page.set_index("open_time")
    out = frame.set_index("open_time")
    for column in columns:
        if column in indexed:
            out[column] = indexed[column].reindex(out.index)
    return out.reset_index()


#: Columns compared RELATIVELY, because REST publishes them to fewer digits than the archive keeps: four
#: decimals (1.3031) against six (1.302937).  Measured 2026-09-27 on 174k same-bucket pairs across the 17
#: symbols of the universe: median 1.1e-4 for the two account ratios and 1.2e-5 for the position ratio,
#: at most 3.9e-4.  1e-3 is the operator's ruling that day.  Open interest stays absolute at `tolerance`,
#: where 0 of 80,504 pairs differed.  The taker ratio is left out: aligned, 5.8% of its pairs exceed 1e-3.
RELATIVE_TOLERANCE: dict[str, float] = {
    "count_toptrader_long_short_ratio": 1e-3,
    "sum_toptrader_long_short_ratio": 1e-3,
    "count_long_short_ratio": 1e-3,
}


def metrics_parity(
    snapshot: pd.DataFrame,
    archive: pd.DataFrame,
    *,
    tolerance: float = 1e-6,
    columns: Sequence[str] | None = None,
) -> dict[str, Any]:
    """M-011: do the two sources agree on the buckets they share?

    The rate is ``None`` rather than 1.0 when nothing overlaps.  Zero disagreements out of zero
    comparisons is not agreement - reporting it as perfect is how a dead snapshot stream would look
    healthiest exactly while it stopped recording.

    ``through`` is the newest bucket the two share, because an agreement is only as recent as that.
    A dead ARCHIVE looks the same from here: from 2026-09-09 every call compared the buckets of
    2026-09-07, the last day anything had ingested, and answered rate 0.0.

    ``columns`` narrows the comparison to the ones a caller vouches for; unset, every shared column.
    """
    if snapshot.empty or archive.empty:
        return {"overlapping": 0, "differing": 0, "rate": None, "through": None}
    shared = [c for c in snapshot.columns if c in archive.columns and c not in ("open_time", "symbol")]
    compared = [c for c in shared if columns is None or c in columns]
    merged = snapshot.merge(archive, on="open_time", suffixes=("_snap", "_arch"))
    if merged.empty or not compared:
        return {"overlapping": 0, "differing": 0, "rate": None, "through": None}
    differing = pd.Series(False, index=merged.index)
    for column in compared:
        left, right = merged[f"{column}_snap"], merged[f"{column}_arch"]
        allowed = RELATIVE_TOLERANCE[column] * right.abs() if column in RELATIVE_TOLERANCE else tolerance
        differing |= ~((left - right).abs() <= allowed) & left.notna() & right.notna()
    count = int(differing.sum())
    return {
        "overlapping": len(merged),
        "differing": count,
        "rate": count / len(merged),
        "through": int(merged["open_time"].max()),
    }


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
