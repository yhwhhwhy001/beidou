"""Binance's INDEX price beside the perpetual's own price, under a declared event-time contract (#29).

The index is the multi-exchange spot composite the venue computes mark price and funding against.  It
is not the same number as `beidou_data.spot`, and the difference is the reason this exists: spot is
Binance's own book, the index is the market's, so `spot / index - 1` is a venue dislocation and
`close / index - 1` is the basis the funding mechanism actually prices.

Everything below was MEASURED against the venue on 2026-09-09, because DL-D2 is the record of what
inferring costs.  The order is the one that lesson forces - contract first, downloader second.

1.  THE OFFSET IS ZERO, AND THAT HAD TO BE MEASURED.  Archive and REST agree on 24/24 buckets of
    BTCUSDT, ETHUSDT and SOLUSDT for 2026-09-05 when `open_time` is joined to `open_time` directly,
    and on 0/23 at one bucket either way.  Metrics' answer was minus five minutes; spot's was zero;
    this one is zero.  Three feeds, two answers, and no way to tell which from the naming - which is
    the whole argument for `verify_stamp_offset` over a comment.

2.  FOUR COLUMNS CANNOT CARRY THE EVIDENCE, AND THEY ARE THE ONES A SCHEMA WOULD KEEP.  The archive
    writes `volume`, `quote_volume`, `taker_buy_volume` and `taker_buy_quote_volume` as literal zero on
    every index row - an index has no trades.  Measured: those columns match 23/23 at BOTH rival
    offsets.  A verification that included them would find its rivals unrefuted and answer
    UNVERIFIABLE; a check that folded columns into a row verdict would instead read perfect agreement
    at every offset and call the contract confirmed.  So `INDEX_VALUE_COLUMNS` is the four PRICE
    columns and nothing else, and the zeros are dropped at parse time rather than filtered later.

3.  THE ARCHIVE HEADER IS NOT CONSISTENT ACROSS YEARS.  2019-12-23, 2024-06-15, 2025-01-15 and
    2026-08-01 ship a header row; 2021-06-19 does not.  `pd.read_csv` defaults to `header=0`, so on the
    2021 file it takes the first BAR as the column names and returns 23 rows whose columns are called
    "1624060800000", "35818.15879182"...  That is not a crash, it is a silently short day with
    unusable names, and it would land in the middle of a backfill.  `parse_index_archive_csv` decides
    by looking at whether the first field is a number.

4.  UNITS: MILLISECONDS, ALL THE WAY BACK.  The spot archive switched to microseconds at 2025-01
    (DL-D5 note 5); the futures INDEX archive did not - 2019-12-23 through 2026-08-01 are all 13-digit
    millisecond stamps.  So the microsecond normalisation in `klines_to_frame` is not exercised here,
    and this note exists so a later reader does not conclude from spot's note that all archives moved.

5.  NO MAPPING FILE AND NO MULTIPLIER, AND THAT IS MEASURED TOO.  All 528 TRADING USDT perpetuals have
    `symbol == pair`, so there is nothing to resolve; and the index is published in the CONTRACT's unit,
    not the token's - 1000PEPEUSDT's index closed at 0.00367048 against a perp close of 0.0036717.
    Spot needed `spot_map.json`, three candidate names and a multiplier for six symbols precisely
    because the spot venue quotes the TOKEN.  The index does not, so none of that machinery is
    reproduced here.  Do not add it back by analogy.

6.  COVERAGE IS TOTAL, WHICH SPOT'S IS NOT.  28 of 28 sampled perpetuals - including 1000PEPEUSDT,
    1000SATSUSDT, 1MBABYDOGEUSDT and XMRUSDT - have a 2026-09-05 daily index archive.  Spot has a leg
    for 362 of 528.  XMRUSDT is the sharp case: its SPOT listing has been halted since 2024-02-20, so a
    spot basis reads a dead price, while its index keeps tracking the live market.

7.  REST IS NOT A 30-DAY WINDOW.  `/fapi/v1/indexPriceKlines` serves 2019-12-23 onward, so unlike
    metrics - where research must eat the archive because live cannot see back - either source can
    cover any period.  The archive is a bulk convenience, and the two remain independent renderings,
    which is what makes note 1 a real comparison rather than a round trip.

8.  WHY THE CANONICAL FRAME IS STILL A VALID INPUT TO THE CHECK.  `verify_stamp_offset` warns that
    handing it canonical frames is circular, and for metrics it is: that parser CONVERTS the stamp, so
    a converted frame has the answer baked in.  This parser renames value columns and does not touch
    the stamp - `open_time` leaves exactly as each source wrote it - so both frames still carry their
    own source's raw stamp and the comparison is the real one.  If Binance ever moves either stamp, the
    verification FAILs; nothing here would hide it.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from typing import Any

import pandas as pd

from beidou_data.alignment import (
    CONTRACTS,
    EventTimeContract,
    Stamp,
    Verification,
    admits_live_signal,
    verify_stamp_offset,
)
from beidou_data.binance_public import PublicClient
from beidou_data.metrics import period_ms as interval_ms

INDEX_KLINES_PATH = "/fapi/v1/indexPriceKlines"
INDEX_ARCHIVE_MARKET = "futures/um"

# The raw kline field names, and the namespaced names they become.  Namespaced because `CONTRACTS` is a
# FLAT dict keyed by bare column name: registering "close" there would claim the word for every frame in
# the system and hand the index contract to the perpetual's own close.  A feed that adds columns to a
# global registry has to name them as if the registry were global, because it is.
INDEX_PRICE_FIELDS: tuple[str, ...] = ("open", "high", "low", "close")
INDEX_VALUE_COLUMNS: tuple[str, ...] = tuple(f"index_{field}" for field in INDEX_PRICE_FIELDS)

# Dropped at parse time, never carried.  An index has no trades, so these are literally zero on every
# row - and a zero column agrees with itself at every offset (note 2).  Named rather than merely
# omitted, so the next reader learns they were considered and why they lost.
INDEX_DEGENERATE_FIELDS: tuple[str, ...] = ("volume", "quote_volume", "taker_buy_volume", "taker_buy_quote_volume")

INDEX_ARCHIVE_COLUMNS: tuple[str, ...] = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "count",
    "taker_buy_volume",
    "taker_buy_quote_volume",
    "ignore",
)


def index_contract(interval: str = "1h") -> EventTimeContract:
    """The index-price contract at one bar size.

    Both stamps are `open_time` with offset 0 - the measured answer, not the assumed one (note 1) - so
    `stamp_offset_ms` is zero and the naive join happens to be correct here.  That coincidence is
    exactly why the rivals matter: `rival_rest_offsets` dedupes the naive hypothesis against the
    declared one and is left with one bucket either side, which is the off-by-one this feed could
    actually suffer.  A sample that cannot refute those two answers UNVERIFIABLE.

    `available_offset_ms` is one period, i.e. the bucket's close, for `usable_from_ms`'s reason: what
    must be TRUE is that the bucket has finished, and the venue's few seconds of publication delay is an
    operational fact that belongs in a health report rather than in a correctness boundary.
    """
    period = interval_ms(interval)
    return EventTimeContract(
        name=f"index_price[{interval}]",
        period_ms=period,
        archive=Stamp("open_time", 0, "the bucket OPEN"),
        rest=Stamp("open_time", 0, "the bucket OPEN"),
        available_offset_ms=period,
        measured=(
            "2026-09-09 against the venue: the data.binance.vision daily index archive and "
            "/fapi/v1/indexPriceKlines agree on 24/24 buckets of BTCUSDT, ETHUSDT and SOLUSDT for "
            "2026-09-05 at a stamp offset of 0, and on 0/23 at one bucket either way.  The four "
            "volume/taker columns are identically zero and match 23/23 at BOTH rivals, which is why "
            "only the price columns are declared."
        ),
    )


INDEX_PRICE = index_contract("1h")

# Registered from THIS module rather than written into `alignment.py`, and the direction of failure is
# the argument.  A column absent from `CONTRACTS` is refused by `contract_for`, so forgetting this
# import can only ever make the gate stricter - never admit a column whose offset nobody declared.  The
# reverse arrangement (alignment importing every feed) would both invert that and make the contract
# module depend on everything it governs.
CONTRACTS.update(dict.fromkeys(INDEX_VALUE_COLUMNS, INDEX_PRICE))


def index_archive_path(pair: str, interval: str, day: str, market: str = INDEX_ARCHIVE_MARKET) -> str:
    """The daily index-price archive key.  `pair`, not `symbol` - see `IndexPriceClient.klines`."""
    return f"/data/{market}/daily/indexPriceKlines/{pair}/{interval}/{pair}-{interval}-{day}.zip"


def _canonical(frame: pd.DataFrame) -> pd.DataFrame:
    """Price columns renamed to their namespaced form; `open_time` passed through UNCHANGED (note 8).

    `close_time` is CARRIED, and it is the one field here that is load-bearing rather than descriptive.
    The venue serves the bucket in progress: at 2026-09-09T09:19Z the newest 1h index bucket came back
    with `count` 2364 of an eventual 3600, and its `close` was the index price at that moment rather
    than at the bucket's close.  Dropping `close_time` would leave a caller no way to tell that row from
    a finished one, and `binance_public.drop_unclosed` - which is exactly the filter for it - keys on
    precisely this column.  The archive never contains a partial bucket; REST always can.
    """
    out = pd.DataFrame({"open_time": pd.to_numeric(frame["open_time"], errors="coerce").astype("int64")})
    out["close_time"] = pd.to_numeric(frame["close_time"], errors="coerce").astype("int64")
    for field, column in zip(INDEX_PRICE_FIELDS, INDEX_VALUE_COLUMNS, strict=True):
        out[column] = pd.to_numeric(frame[field], errors="coerce").astype(float)
    return out.sort_values("open_time", ignore_index=True).drop_duplicates("open_time", keep="last")


def parse_index_archive_csv(text: str) -> pd.DataFrame:
    """One day of the daily index archive, header row or not (note 3).

    The header is detected rather than configured because it varies BY FILE within one feed, so any
    single setting is wrong for part of the history.  Deciding from the first field - a bar's
    `open_time` is a number, the word "open_time" is not - is the only test that does not need a table
    of which years shipped which.
    """
    stripped = text.strip()
    if not stripped:
        return _canonical(pd.DataFrame(columns=list(INDEX_ARCHIVE_COLUMNS)))
    first_field = stripped.splitlines()[0].split(",")[0].strip()
    headerless = first_field.replace(".", "", 1).isdigit()
    frame = (
        pd.read_csv(io.StringIO(stripped), header=None, names=list(INDEX_ARCHIVE_COLUMNS))
        if headerless
        else pd.read_csv(io.StringIO(stripped))
    )
    missing = [column for column in ("open_time", "close_time", *INDEX_PRICE_FIELDS) if column not in frame.columns]
    if missing:
        raise ValueError(f"index archive csv is missing {missing}; got {list(frame.columns)}")
    return _canonical(frame)


def parse_index_rest_rows(rows: Sequence[Sequence[Any]]) -> pd.DataFrame:
    """A `/fapi/v1/indexPriceKlines` page in the same canonical shape, stamp untouched."""
    if not rows:
        return _canonical(pd.DataFrame(columns=list(INDEX_ARCHIVE_COLUMNS)))
    frame = pd.DataFrame([list(row[:12]) for row in rows], columns=list(INDEX_ARCHIVE_COLUMNS))
    return _canonical(frame)


def verify_index_contract(
    archive: pd.DataFrame, rest: pd.DataFrame, *, interval: str = "1h", min_overlap: int = 12
) -> Verification:
    """Hold the two sources against the declared offset, and against the two it must refute.

    `value_columns` is pinned to the price columns rather than left to default over the frame, which is
    note 2 made operative: defaulting would sweep the identically-zero volume columns back in and turn a
    real PASS into UNVERIFIABLE the moment somebody stopped dropping them at parse time.
    """
    return verify_stamp_offset(
        index_contract(interval),
        archive,
        rest,
        value_columns=INDEX_VALUE_COLUMNS,
        min_overlap=min_overlap,
    )


def index_columns_for_live(verification: Verification | None) -> tuple[tuple[str, ...], dict[str, str]]:
    """RISK-G3's gate for this feed: the columns a live signal may read, and why each other is refused.

    The first production caller of `admits_live_signal`.  It is a function rather than a constant
    because the answer depends on a MEASUREMENT that is re-run against the venue, not on the shape of
    the code: the same four columns are admitted today and refused tomorrow if the venue moves a stamp
    and the verification comes back FAIL.  Refusals are returned with their reasons for the reason
    `admits_live_signal` returns one - a governance report has to print why a candidate is sitting in
    `booked`, and a caller that re-derives the reason is the second copy that starts to disagree.
    """
    admitted: list[str] = []
    refused: dict[str, str] = {}
    for column in INDEX_VALUE_COLUMNS:
        allowed, reason = admits_live_signal(column, verification)
        if allowed:
            admitted.append(column)
        else:
            refused[column] = reason
    return tuple(admitted), refused


def align_index_to_perp_bars(index: pd.DataFrame, bars: pd.DatetimeIndex) -> pd.DataFrame:
    """The index bar with the SAME open time as each perp bar; otherwise NaN.

    Causality.  Index and perpetual are published on one grid (`open_time % 3_600_000 == 0` on both),
    so the index bar opening at t closes at the instant the perp bar opening at t closes.  A decision
    taken on the perp bar's close may read it and learns nothing the perp close does not already carry:
    this adds no lag of its own and inherits whatever execution lag the panel applies to `close`.

    Same-bar-or-NaN, with no fill in either direction - DL-D5 note 7's rule, kept for a DIFFERENT
    reason than spot's.  Spot needed it because a halted listing (XMRUSDT, dead since 2024-02-20) keeps
    answering with a two-year-old price, and `metrics.align_to_bars`'s "latest bucket that had closed"
    would have priced a +324% basis off it.  The index has no halted-listing failure - it tracks the
    market, not one book - so the hazard here is the plainer one: a gap in the archive filled forward
    becomes a stale basis that no other series can contradict, and a hole that stays a hole is the only
    version a reader can audit.  Reindexing is therefore the whole operation.
    """
    if index.empty:
        return pd.DataFrame(index=bars, columns=list(INDEX_VALUE_COLUMNS), dtype=float)
    stamps = pd.DatetimeIndex(pd.to_datetime(index["open_time"].astype("int64").to_numpy(), unit="ms", utc=True))
    columns = [column for column in INDEX_VALUE_COLUMNS if column in index.columns]
    indexed = index[columns].set_axis(stamps, axis=0).astype(float)
    indexed = indexed[~indexed.index.duplicated(keep="last")].sort_index()
    return indexed.reindex(bars).reindex(columns=list(INDEX_VALUE_COLUMNS))


class IndexPriceClient(PublicClient):
    """The futures public client pointed at the index-price klines path.

    A subclass for `SpotClient`'s reason - retry, backoff and frame conversion are the same venue
    behaviour under a different path - plus one override the spot client did not need: this endpoint
    keys on `pair`, and answers HTTP 400 `-1102 Mandatory parameter 'pair' was not sent` to the
    `symbol` the base class would send.  Measured, not read: `?symbol=BTCUSDT` returns that error today.
    """

    _klines_path = INDEX_KLINES_PATH
    # Page size is the futures 1500; unlike the spot venue (DL-D5 note 8) this path does not truncate to
    # a smaller page, so the inherited limit is correct and `klines_range` pages to the end.

    def klines(
        self,
        symbol: str,
        interval: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """Index klines for `symbol`, sent as `pair`.

        The argument keeps the base class's name so `klines_range` - which calls `self.klines(symbol,
        ...)` - pages this endpoint unchanged.  Renaming it here would fork the pagination loop, and
        that loop is where `SpotClient` found its silent truncation; one copy of it is the point.  All
        528 TRADING USDT perpetuals have `symbol == pair` (note 5), so the two names denote the same
        string today and the parameter, not the value, is what has to change.
        """
        page = self._page_limit if limit is None else min(limit, self._page_limit)
        params: dict[str, Any] = {"pair": symbol, "interval": interval, "limit": page}
        if start_ms is not None:
            params["startTime"] = int(start_ms)
        if end_ms is not None:
            params["endTime"] = int(end_ms)
        rows = self.get(self._klines_path, params)
        assert isinstance(rows, list)
        return parse_index_rest_rows(rows)
