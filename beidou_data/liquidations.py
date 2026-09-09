"""Public liquidation events: ONE convention, and coverage carried as data (#19 / RISK-G3).

Four things were measured against ``data.binance.vision`` on 2026-09-09.  Each one changes the code
below rather than decorating it.

**1. There is no USDⓈ-M liquidation archive.**  ``futures/um/monthly/liquidationSnapshot/`` and
``futures/um/daily/liquidationSnapshot/`` both list ZERO keys - the plan that scheduled this column
named the monthly path as its history source and that path does not exist.  The only
``liquidationSnapshot`` on the CDN is COIN-margined daily (``futures/cm/daily/liquidationSnapshot/``):
118 symbols, 2023-06-25 through 2024-10-14, discontinued.  So ``market`` is a parameter and ``um``
stays the default: this code is right the day Binance republishes, and until then it ingests nothing,
which every layer below reports as *absent* rather than as zero.

**2. The archive writes every row exactly twice.**  6,438 rows over 17 symbol-days (BTC/ETH/BNB/SOL/
ADA/DOGE/LTC, spanning both ends of the range including the first day and the last) fall into
exact-duplicate groups of size two, with ZERO singletons and ZERO odd-sized groups.  Summing the file
as it ships doubles every notional and nothing raises - the number just comes back exactly 2x, looking
perfectly normal.  That is this repository's recurring defect ("could not compute it, returned a
plausible number") in its purest form, so ``parse_archive_csv`` halves each group and refuses an odd
one instead of rounding.  Halving rather than ``drop_duplicates``: two genuinely simultaneous,
identical liquidations would ship as four rows and must come back as two, not one.

**3. Event time needs no offset, and that is a measured negative result.**  0 of 2,102 rows across six
symbol-days fell outside the UTC day their own file names, so ``time`` is the same instant the archive
partitions by and bar assignment is ``floor(time / interval)`` with no correction.  This check is here
because the metrics column failed it: archive ``create_time`` was one whole bucket earlier than the
REST ``timestamp``, and every piece of evidence carried five minutes of look-ahead until 2026-09-07
found it.  A clean result still has to be a measured one.

**4. ``side`` is the side of the ORDER, not of the position.**  A long is closed by SELLING, so
``SELL`` means a long was liquidated.  The inversion happens once, at the parse boundary, and the
order-side spelling never survives into a frame - the same discipline that makes ``metrics.py`` drop
``create_time``.  Getting this backwards flips the sign of every signal built on the column and would
not fail a single test that did not look for it.

The aggregates behave like ``volume``, not like open interest: they are a FLOW over the bar, complete
at the bar's close, which is the same instant the bar's own close price is known.  There is therefore
no shift to apply - and no shift to get wrong - but there is a boundary: the interval is half-open, so
an event landing exactly on a bar's close belongs to the NEXT bar.  Writing that ``<=`` would push
events into a bar that had already finished, which is a look-ahead of one whole bar.
"""

from __future__ import annotations

import io
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

DAY_MS = 86_400_000

# The daily archive's columns, in the order it writes them.  There is no `symbol` column - the symbol
# lives in the path - which is why `parse_archive_csv` has to be told which one it is reading.
ARCHIVE_COLUMNS = (
    "time",
    "side",
    "order_type",
    "time_in_force",
    "original_quantity",
    "price",
    "average_price",
    "order_status",
    "last_fill_quantity",
    "accumulated_fill_quantity",
)

# The canonical event.  `side` here is the LIQUIDATED POSITION ("long"/"short"), never the order side.
EVENT_COLUMNS = ("time", "symbol", "side", "price", "quantity")

AGGREGATE_COLUMNS = (
    "liq_notional_long",
    "liq_notional_short",
    "liq_count_long",
    "liq_count_short",
)

# A long position is closed by selling it.  See point 4 in the module docstring.
_LIQUIDATED_SIDE = {"SELL": "long", "BUY": "short"}


class LiquidationArchiveAnomaly(RuntimeError):
    """The archive contradicted a measured invariant.  Raised rather than worked around on purpose."""


def liquidated_side(order_side: str) -> str:
    """``SELL`` -> ``long``, ``BUY`` -> ``short``: the side of the POSITION that was liquidated."""
    try:
        return _LIQUIDATED_SIDE[str(order_side).strip().upper()]
    except KeyError:
        raise ValueError(f"unknown liquidation order side {order_side!r}; expected BUY or SELL") from None


def _events_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=list(EVENT_COLUMNS))
    frame["time"] = pd.to_numeric(frame["time"], errors="coerce").astype("int64")
    for column in ("symbol", "side"):
        frame[column] = frame[column].astype(str)
    for column in ("price", "quantity"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype(float)
    return frame.sort_values("time", ignore_index=True)


def empty_events() -> pd.DataFrame:
    """A well-typed frame with no events.

    Not the same object as "no data": a day that was fetched and contained no liquidations is stored
    AS this frame, and `LiquidationStore` records that it was fetched by the file existing at all.
    """
    return _events_frame([])


def collapse_archive_duplication(raw: pd.DataFrame) -> pd.DataFrame:
    """Undo the archive's exact doubling: every distinct row appears twice, so keep half of each group.

    Measured over 6,438 rows and 17 symbol-days: every exact-duplicate group has size two, none has
    size one, none is odd.  An odd group would mean the doubling is not what it was measured to be,
    and the two available responses are to raise or to round.  Rounding is how a 2x error becomes
    invisible, so this raises: `sync_liquidations` isolates failures per symbol, which turns a
    surprising file into a reported skip instead of a silently halved day.
    """
    if raw.empty:
        return raw
    counts = raw.groupby(list(raw.columns), sort=False, dropna=False).size()
    odd = counts[counts % 2 != 0]
    if len(odd):
        raise LiquidationArchiveAnomaly(
            f"{len(odd)} liquidation rows are not duplicated in pairs (measured invariant: every row "
            f"appears an even number of times); first offender: {odd.index[0]}"
        )
    halved = counts.index.repeat((counts // 2).to_numpy())
    return pd.DataFrame(list(halved), columns=list(raw.columns))


def parse_archive_csv(text: str, symbol: str) -> pd.DataFrame:
    """One day of the daily archive, de-doubled, in the canonical shape.

    ``average_price`` and ``accumulated_fill_quantity`` are the pair that describe what was actually
    liquidated: ``price`` is the order's IOC limit (63778.3 against an average fill of 63551.6 on a
    real row, so it overstates), and ``last_fill_quantity`` is only the final fill of an order that
    may have filled in several (66 of 76 on that same row).  ``order_status``/``order_type``/
    ``time_in_force`` were constant FILLED/LIMIT/IOC across every row measured and carry nothing, so
    they do not survive; ``accumulated_fill_quantity`` equalled ``original_quantity`` on all of them,
    which makes this choice conservative rather than load-bearing today.
    """
    raw = pd.read_csv(io.StringIO(text))
    if raw.empty:
        return empty_events()
    missing = [column for column in ("time", "side", "average_price", "accumulated_fill_quantity") if column not in raw]
    if missing:
        raise ValueError(f"liquidation archive csv is missing {missing}; got {list(raw.columns)}")
    single = collapse_archive_duplication(raw)
    return _events_frame(
        [
            {
                "time": int(row["time"]),
                "symbol": str(symbol),
                "side": liquidated_side(str(row["side"])),
                "price": row["average_price"],
                "quantity": row["accumulated_fill_quantity"],
            }
            for _, row in single.iterrows()
        ]
    )


def parse_stream_events(payloads: Iterable[Mapping[str, Any]]) -> pd.DataFrame:
    """``!forceOrder@arr`` messages in the SAME canonical shape as the archive.

    ``T`` (the order's trade time) rather than ``E`` (the instant the venue pushed the message): the
    archive partitions by the event's own time, so joining on push time would compare two different
    clocks - and the gap between them is a network property that changes with load.  ``z``/``ap``
    rather than ``q``/``p`` for the same reason ``parse_archive_csv`` takes the accumulated fill and
    the average price.

    The doubling of point 2 is NOT undone here, because it was never measured on the stream: the
    archive stopped 2024-10-14 and no stream sample overlaps it, so whether the stream doubles too is
    unknown.  Applying the archive's correction to the stream on the assumption that it behaves the
    same way would manufacture a 2x disagreement in the other direction out of a guess.
    """
    rows = []
    for payload in payloads:
        order = payload.get("o", payload)
        rows.append(
            {
                "time": int(order["T"]),
                "symbol": str(order["s"]),
                "side": liquidated_side(str(order["S"])),
                "price": order.get("ap"),
                "quantity": order.get("z"),
            }
        )
    return _events_frame(rows)


def usable_from_ms(bar_open_ms: int, interval_ms: int) -> int:
    """The earliest instant a bar's aggregate is complete: the bar's own close.

    Same arithmetic as `beidou_data.metrics.usable_from_ms` and for the same reason - the close is a
    property of the grid, while any allowance for how fast the venue publishes is a property of one
    day's measurement and would make the boundary quietly wrong the day the venue changes speed.
    """
    return int(bar_open_ms) + int(interval_ms)


def _bar_opens_ms(bars: pd.DatetimeIndex) -> np.ndarray:
    """A DatetimeIndex carries its own resolution in pandas 2.x, so state the unit, never assume it.

    `beidou_data.metrics.align_to_bars` learned this the expensive way: `view("int64") // 1_000_000`
    is right for nanoseconds and silently wrong for microseconds, and a `DatetimeIndex([Timestamp])`
    is microseconds - every bar came back NaN.
    """
    return np.asarray(pd.DatetimeIndex(bars).as_unit("ms").view("int64"), dtype="int64")


def aggregate_to_bars(
    events: pd.DataFrame,
    bars: pd.DatetimeIndex,
    *,
    interval_ms: int,
    covered: Sequence[bool] | np.ndarray,
    contract_size: float | None = None,
) -> pd.DataFrame:
    """Liquidation events summed onto bars, with "no data" and "no liquidations" kept apart.

    **The aggregation choice, which is a choice and not a default.**  Four columns: notional and count,
    each split by the side of the position that was liquidated.

    *Split by side* because a cascade is directional - forced selling and forced buying push price
    opposite ways - and a total throws away the only part a directional signal can use.  The total is
    recoverable by adding the two; the split is not recoverable from the total, so the pipeline stores
    the finer thing.

    *Both notional and count* because they fail differently and the pipeline should not pick which
    failure a future signal inherits.  Notional is comparable across symbols and across years but is
    dominated by a handful of whales; count is the robust version of the same event but treats a
    0.001 BTC liquidation and a 500 BTC one alike.

    *No ratio or imbalance column*, though that is what a signal will most likely want: a ratio cannot
    be taken apart into its components, the components can always form the ratio, and a ratio has a
    0/0 case that would have to be invented here - where nothing knows what it should mean - rather
    than in the node that does.

    **Missing is missing.**  ``covered`` says, per bar, whether the ingest actually holds the data.  A
    covered bar with no events is 0.0, because no liquidations happened; an uncovered bar is NaN,
    because nothing is known.  Collapsing those two into 0.0 would let a symbol that was never
    downloaded read as the calmest one in the universe.

    ``contract_size`` states which market's quantity convention applies, because getting it wrong is
    silent.  ``None`` is the linear/USDⓈ-M convention - quantity is in the base asset, so notional is
    ``price x quantity``.  A value is the inverse/coin-margined convention, where quantity is in
    CONTRACTS of fixed quote value (BTCUSD_PERP is 100 USD) and the price does not enter the notional
    at all.  Multiplying contracts by price would produce a number roughly 63,000x too large on the
    only archive that currently exists, and it would look like a notional.
    """
    bars = pd.DatetimeIndex(bars)
    covered_mask = np.asarray(covered, dtype=bool)
    if covered_mask.shape != (len(bars),):
        raise ValueError(
            f"coverage has {covered_mask.shape} entries for {len(bars)} bars; one flag per bar is required"
        )
    frame = pd.DataFrame(np.nan, index=bars, columns=list(AGGREGATE_COLUMNS), dtype=float)
    frame.loc[covered_mask, :] = 0.0
    if events.empty or len(bars) == 0:
        return frame

    opens = _bar_opens_ms(bars)
    times = events["time"].to_numpy(dtype="int64")
    position = np.searchsorted(opens, times, side="right") - 1
    safe = np.clip(position, 0, None)
    # The half-open boundary, and the ONE place it is decided.  `searchsorted(side="right")` picks the
    # last bar that had opened; this guard drops the event unless the bar had not yet CLOSED - the
    # array form of `usable_from_ms`.  Spelling it `<=` attaches an event landing exactly on a bar's
    # close to that bar, which had already finished: a whole bar of look-ahead.  On a contiguous index
    # the mistake hides, because the next bar would have taken the event anyway; it only becomes
    # visible across a GAP, which is also where an event outside every bar must be DROPPED rather than
    # attached to the nearest one - a gap is precisely where "nearest" invents a neighbour.
    inside = (position >= 0) & (times < opens[safe] + int(interval_ms))
    if bool(np.any(inside & ~covered_mask[safe])):
        raise ValueError(
            "events fall on bars the coverage record calls uncovered; the record and the data it "
            "describes disagree, which is the failure one-file-per-day exists to make impossible"
        )

    chosen = np.flatnonzero(inside)
    used = events.iloc[chosen]
    quantity = used["quantity"].to_numpy(dtype=float)
    notional = (
        quantity * float(contract_size) if contract_size is not None else used["price"].to_numpy(float) * quantity
    )
    sides = used["side"].to_numpy()
    target = position[chosen]

    for side in ("long", "short"):
        picked = sides == side
        totals = np.zeros(len(bars), dtype=float)
        counts = np.zeros(len(bars), dtype=float)
        np.add.at(totals, target[picked], notional[picked])
        np.add.at(counts, target[picked], 1.0)
        # NaN + 0.0 stays NaN, so an uncovered bar survives this addition untouched - which is why the
        # coverage baseline is applied before the events rather than after.
        frame[f"liq_notional_{side}"] = frame[f"liq_notional_{side}"].to_numpy() + totals
        frame[f"liq_count_{side}"] = frame[f"liq_count_{side}"].to_numpy() + counts
    return frame


class LiquidationStore:
    """One parquet per symbol-DAY, because the file's existence is the coverage record.

    Every other store here keys on a timestamp and lets ``max(open_time)`` be the watermark, which
    works when a row exists for every period.  Liquidations are events: a day with none is legitimate
    and indistinguishable, by its rows, from a day never fetched.  A cursor file could record the
    difference, and `metrics_archive.py` writes down why it does not have one - a cursor can disagree
    with the data it claims to describe, and then a resume silently skips a gap.  A file per day keeps
    the store as its own watermark AND makes absence expressible: an empty parquet is "fetched, no
    liquidations", a missing one is "never fetched", and no third place can drift from either.
    """

    def __init__(self, root: str | Path = ".beidou/data", *, market: str = "um") -> None:
        self._root = Path(root)
        self._market = market

    @property
    def directory(self) -> Path:
        return self._root / "liquidations" / self._market

    def path(self, symbol: str, day: str) -> Path:
        return self.directory / symbol / f"{day}.parquet"

    def has_day(self, symbol: str, day: str) -> bool:
        return self.path(symbol, day).exists()

    def symbols(self) -> list[str]:
        if not self.directory.exists():
            return []
        return sorted(p.name for p in self.directory.iterdir() if p.is_dir())

    def covered_days(self, symbol: str) -> list[str]:
        """The days actually fetched, empty ones included.  This IS the coverage record."""
        directory = self.directory / symbol
        if not directory.exists():
            return []
        return sorted(p.stem for p in directory.glob("*.parquet"))

    def write_day(self, symbol: str, day: str, frame: pd.DataFrame) -> int:
        """Store one fetched day.  An empty frame still writes a file - that is the whole design."""
        path = self.path(symbol, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        stored = frame.reindex(columns=list(EVENT_COLUMNS)) if not frame.empty else empty_events()
        tmp = path.with_suffix(".parquet.tmp")
        stored.to_parquet(tmp, index=False)
        tmp.replace(path)
        return len(stored)

    def load(self, symbol: str, start_ms: int | None = None, end_ms: int | None = None) -> pd.DataFrame:
        days = self.covered_days(symbol)
        if not days:
            return empty_events()
        frames = [pd.read_parquet(self.path(symbol, day)) for day in days]
        merged = pd.concat(frames, ignore_index=True) if frames else empty_events()
        if merged.empty:
            return empty_events()
        if start_ms is not None:
            merged = merged[merged["time"] >= int(start_ms)]
        if end_ms is not None:
            merged = merged[merged["time"] < int(end_ms)]
        return merged.sort_values("time", ignore_index=True)

    def coverage(self, symbol: str, bars: pd.DatetimeIndex, *, interval_ms: int) -> np.ndarray:
        """Per bar: is every UTC day the bar touches one we actually fetched?

        Every day, not merely the day the bar opens in, so a bar wider than the archive's own daily
        partition cannot be called covered on the strength of its first hour.
        """
        held = {int(pd.Timestamp(day, tz="UTC").value // 1_000_000 // DAY_MS) for day in self.covered_days(symbol)}
        opens = _bar_opens_ms(pd.DatetimeIndex(bars))
        first = opens // DAY_MS
        last = (opens + int(interval_ms) - 1) // DAY_MS
        return np.array(
            [all(day in held for day in range(int(a), int(b) + 1)) for a, b in zip(first, last, strict=True)],
            dtype=bool,
        )


def to_panel_columns(
    store: LiquidationStore,
    symbols: Sequence[str],
    bars: pd.DatetimeIndex,
    *,
    interval_ms: int,
    contract_size: float | None = None,
) -> dict[str, pd.DataFrame]:
    """The four columns as wide bars-x-symbols frames, ready for ``Panel.from_frames(metrics=...)``.

    They ride in the panel's ``metrics`` dict rather than in a field of their own: that dict is already
    defined as "one wide frame per column, already aligned to these bars", and `Panel.from_frames`
    already REFUSES a frame that is not on the bar index instead of helpfully reindexing it.  A new
    field would need its own copy of that refusal, and the copy is what drifts.
    """
    bars = pd.DatetimeIndex(bars)
    columns: dict[str, dict[str, pd.Series]] = {name: {} for name in AGGREGATE_COLUMNS}
    for symbol in symbols:
        aligned = aggregate_to_bars(
            store.load(symbol),
            bars,
            interval_ms=interval_ms,
            covered=store.coverage(symbol, bars, interval_ms=interval_ms),
            contract_size=contract_size,
        )
        for name in AGGREGATE_COLUMNS:
            columns[name][symbol] = aligned[name]
    return {name: pd.DataFrame(series, index=bars, columns=list(symbols)) for name, series in columns.items()}


def stream_archive_parity(
    stream: pd.DataFrame,
    archive: pd.DataFrame,
    bars: pd.DatetimeIndex,
    *,
    interval_ms: int,
    contract_size: float | None = None,
    tolerance: float = 1e-9,
) -> dict[str, Any]:
    """M-011 for this column: do the live stream and the archive agree on the bars they both cover?

    ``bars`` is what the caller says BOTH sources cover; the rate is ``None`` when that set is empty,
    because zero disagreements out of zero comparisons is not agreement - it is the shape a dead
    recorder wears while looking healthiest.

    Unlike `metrics_parity` this does NOT return ``None`` merely because one side is empty.  For a
    point-in-time metric an empty frame means "we never looked"; for an event stream, a covered bar
    with no events means zero liquidations, and zero against non-zero is a real disagreement rather
    than a missing comparison.

    On today's data this can only return ``None``.  The USDⓈ-M archive does not exist and the
    coin-margined one stopped on 2024-10-14, so no live stream recorded now can share a bar with it -
    the same-source obligation this column would have to pass is not merely unmet, it is unmeetable.
    """
    bars = pd.DatetimeIndex(bars)
    if len(bars) == 0:
        return {"overlapping": 0, "differing": 0, "rate": None}
    everywhere = np.ones(len(bars), dtype=bool)
    left = aggregate_to_bars(stream, bars, interval_ms=interval_ms, covered=everywhere, contract_size=contract_size)
    right = aggregate_to_bars(archive, bars, interval_ms=interval_ms, covered=everywhere, contract_size=contract_size)
    differing = pd.Series(False, index=bars)
    for column in AGGREGATE_COLUMNS:
        differing |= (left[column] - right[column]).abs() > tolerance
    count = int(differing.sum())
    return {"overlapping": len(bars), "differing": count, "rate": count / len(bars)}
