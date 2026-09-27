"""Funding rows are read once the venue has published them, and credited to the book that paid them.

Measured 2026-09-27 (read-only, on the operator's Mac) from `attribution.jsonl` against the account's
own positions and demo funding rates, 2026-09-15T00Z to 09-27T00Z: in 36 ordinary settlements - the
income query 24-40 s after the hour - 507 FUNDING_FEE rows were due and 6 were recorded, all BTCUSDT,
all queried 24-28 s after.  The one cycle whose query ran an hour late (09-23T16:00, the cycle before
it died on a ProxyError) recorded 13 of 13, and the two early cycles that queried 175 s and 259 s after
a settlement (09-03T08, 09-04T08) recorded all 12 and all 10.  About -16.51 of -17.40 USDT went missing.

The code path makes the loss certain whatever the publication delay turns out to be: the venue stamps
the row at the settlement, `_ingest_income` asks for `[since, now]` and then moves `since` to `now`, so
a row stamped before `now` and published after the query is never asked for again.  That the delay IS
the cause is inferred from the pattern above; a keyed `incomeType=FUNDING_FEE` query would confirm it.

The venue below has the two properties that matter: a row carries the settlement's stamp and is visible
only from `published` on, and a query returns every visible row in its window, inclusive at both ends,
as often as it is asked.  Nothing is handed out once, so a row read twice is counted twice - which is
what these tests are looking for.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

import beidou_live.engine as live_engine
from beidou_alpha.panel import Panel
from beidou_live.attribution import attribute
from beidou_live.engine import LiveEngine
from beidou_live.state import LiveState, StateStore
from tests.fakes.fake_venue import FakeVenue
from tests.live.fakes import FakeClock, FakeMarketData
from tests.live.test_live_loop import _config, _model, _prices

HOUR = 3_600_000
SECOND = 1_000
SETTLE = 1_789_000_000_000 // (8 * HOUR) * (8 * HOUR)  # a real settlement hour: 00Z, 08Z or 16Z
QUERY = 27 * SECOND  # the live loop's median: the income query runs about 27 s after the hour
EQUITY = 10_000.0

# Three books, far enough apart that crediting a row to the wrong one cannot pass by accident.
BEFORE = {"tsmom": {"ETHUSDT": -1.0}}
HELD = {"tsmom": {"BTCUSDT": 1.0, "ETHUSDT": 1.0}, "flow": {"BTCUSDT": -0.5}}  # held AT the settlement
AFTER = {"tsmom": {"BTCUSDT": 1.0}}  # what the cycle 27 s after the settlement traded into


def _at(hour: int) -> int:
    """Venue time of the cycle that wakes ``hour`` hours after the settlement."""
    return SETTLE + hour * HOUR + QUERY


def _bar(hour: int) -> int:
    """The bar that cycle decides on: the one that closed at the top of its hour."""
    return SETTLE + (hour - 1) * HOUR


class PublishingVenue(FakeVenue):
    """`/fapi/v1/income` as the loop meets it: stamped at the event, visible from `published` on."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.now = 0
        self.ledger: list[dict[str, Any]] = []
        self.windows: list[tuple[int, int]] = []

    def venue_time_ms(self) -> int:
        return self.now

    def book(self, symbol: str, kind: str, amount: float, *, at: int, published: int | None = None) -> None:
        self.ledger.append(
            {"symbol": symbol, "incomeType": kind, "income": str(amount), "time": at, "published": published or at}
        )

    async def income(self, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        self.windows.append((int(start_ms), int(end_ms)))
        return [
            {key: value for key, value in row.items() if key != "published"}
            for row in self.ledger
            if start_ms <= row["time"] <= end_ms and row["published"] <= self.now
        ]


@pytest.fixture
def panel(august_panel: Panel) -> Panel:
    return august_panel


def _engine(tmp_path: Path, venue: FakeVenue, panel: Panel) -> LiveEngine:
    return LiveEngine(
        _config(tmp_path),
        model=_model(),
        market=FakeMarketData(panel, cursor=400),
        venue=venue,
        clock=FakeClock(SETTLE),
        store=StateStore(tmp_path / "live"),
    )


def _as_the_old_code_left_it(engine: LiveEngine, *, hour: int, book: dict[str, dict[str, float]]) -> None:
    """A state.json written before the lagged window existed: one watermark, no funding one."""
    engine.state = LiveState(
        last_income_ms=_at(hour), last_bar_ms=_bar(hour), last_contributions=book, cycles=100, restarts=58
    )


async def _cycle(engine: LiveEngine, venue: PublishingVenue, hour: int, book: dict[str, dict[str, float]]) -> None:
    """One cycle's income step at `_at(hour)`, then what `_finish_cycle` leaves behind for the next."""
    venue.now = _at(hour)
    await engine._ingest_income(_bar(hour), EQUITY)
    engine.state.last_bar_ms = _bar(hour)
    engine.state.last_contributions = book


def _funding(rows: list[dict[str, Any]], symbol: str | None = None) -> float:
    return sum(
        float(bucket.get("FUNDING_FEE", 0.0))
        for row in rows
        for name, bucket in (row.get("by_symbol") or {}).items()
        if symbol is None or name == symbol
    )


def _written(engine: LiveEngine) -> list[dict[str, Any]]:
    return engine.store.read_jsonl(engine.store.attribution_path)


async def test_a_settlement_published_after_the_query_is_read_once_by_the_next_cycle(
    panel: Panel, tmp_path: Path
) -> None:
    """The reproduction.  On the code before this fix the ETH row is never read and the total is -1.0."""
    venue = PublishingVenue(balance=EQUITY, prices=_prices(panel, 400))
    engine = _engine(tmp_path, venue, panel)
    _as_the_old_code_left_it(engine, hour=-2, book=BEFORE)
    # Stamped 5 ms after the hour.  BTC is visible 20 s later, before the 27 s query - the six rows the
    # old code did record looked like this.  ETH is visible 90 s later, after it - the 501 it did not.
    venue.book("BTCUSDT", "FUNDING_FEE", -1.0, at=SETTLE + 5, published=SETTLE + 20 * SECOND)
    venue.book("ETHUSDT", "FUNDING_FEE", -0.4, at=SETTLE + 5, published=SETTLE + 90 * SECOND)

    for hour, book in ((-1, HELD), (0, AFTER), (1, AFTER), (2, AFTER)):
        await _cycle(engine, venue, hour, book)

    written = _written(engine)
    assert _funding(written) == pytest.approx(-1.4), "both rows, each exactly once"
    carrying = [row for row in written if _funding([row])]
    assert len(carrying) == 1
    late = carrying[0]
    # Paid by HELD, the book of the cycle one before the one that queried 27 s after the hour, and
    # labelled with that cycle's bar - where the rows would have landed had they been visible in time.
    assert late["bar_open_ms"] == _bar(-1)
    assert late["by_strategy"] == pytest.approx({"tsmom": -2.0 - 0.4, "flow": 0.5 * 2.0})
    assert late["late_funding"] is True
    assert (late["since_ms"], late["until_ms"]) == (_at(0) - live_engine.FUNDING_SETTLE_LAG_MS, _at(0))
    # Funding carries no tradeId, so the D-032 split never consults the set and the row stays with the
    # book; `reconciled` says the classification is complete rather than that a userTrades call failed.
    assert late["foreign"] == {"by_symbol": {}, "total": 0, "rows": 0, "reconciled": True}


async def test_no_row_is_read_twice_across_many_cycles_starting_from_an_old_state(panel: Panel, tmp_path: Path) -> None:
    """Every row lands exactly once, under the bar of the book that held it, from the migration cycle on.

    One symbol per row, so each row's fate can be read off `by_symbol`.  The rows at the edges are the
    point: the old code's last window was `[.., since]` inclusive, and every funding window here is
    `(from, until]`, so a row stamped exactly on a boundary belongs to exactly one side of it.
    """
    lag = live_engine.FUNDING_SETTLE_LAG_MS
    venue = PublishingVenue(balance=EQUITY, prices=_prices(panel, 400))
    engine = _engine(tmp_path, venue, panel)
    _as_the_old_code_left_it(engine, hour=-2, book=BEFORE)
    migrated_from = _at(-2)
    # symbol: (kind, amount, stamped, published, read late).  "Late" is a row stamped before the `since`
    # of the cycle that reads it, i.e. paid by the book before that cycle's - its own record.
    rows: dict[str, tuple[str, float, int, int, bool]] = {
        "PREUSDT": ("FUNDING_FEE", -0.11, migrated_from - 300 * SECOND, migrated_from - 240 * SECOND, False),
        "EDGEUSDT": ("FUNDING_FEE", -0.12, migrated_from, migrated_from, False),  # the old window's inclusive end
        "S0FASTUSDT": ("FUNDING_FEE", -0.21, SETTLE + 3, SETTLE + 10 * SECOND, True),
        "S0SLOWUSDT": ("FUNDING_FEE", -0.22, SETTLE + 3, SETTLE + 300 * SECOND, True),
        "S1USDT": ("FUNDING_FEE", -0.31, SETTLE + HOUR + 4, SETTLE + HOUR + 60 * SECOND, True),
        "ONUUSDT": ("FUNDING_FEE", -0.41, _at(3) - lag, _at(3) - lag, False),  # exactly on a funding boundary
        "SINCEUSDT": ("FUNDING_FEE", -0.42, _at(3), _at(3), False),  # exactly on a cycle's `since`: its own book
        # The loop is down at hour 5, so the hour-5 settlement sits INSIDE the next cycle's window
        # (since = _at(4)) and is paid by the book that window already charges: the current-book path.
        "S5USDT": ("FUNDING_FEE", -0.51, SETTLE + 5 * HOUR + 2, SETTLE + 5 * HOUR + 120 * SECOND, False),
        "S8USDT": ("FUNDING_FEE", -0.81, SETTLE + 8 * HOUR + 2, SETTLE + 8 * HOUR + 30 * SECOND, True),
        "FEEUSDT": ("COMMISSION", -0.05, SETTLE + 1_800 * SECOND, SETTLE + 1_800 * SECOND, False),
        "PNLUSDT": ("REALIZED_PNL", 0.07, SETTLE + 7 * HOUR + 600 * SECOND, SETTLE + 7 * HOUR + 600 * SECOND, False),
    }
    for symbol, (kind, amount, stamped, published, _late) in rows.items():
        venue.book(symbol, kind, amount, at=stamped, published=published)
    books = [{"tsmom": dict.fromkeys(rows, 1.0), "flow": dict.fromkeys(rows, float(i))} for i in range(12)]
    ran = [hour for hour in range(-1, 11) if hour != 5]
    for hour in ran:
        await _cycle(engine, venue, hour, books[hour + 1])

    def label(stamped: int) -> int:
        """The bar of the last cycle that ran at or before the stamp: whose book held the position."""
        return _bar(max(hour for hour in [-2, *ran] if _at(hour) <= stamped))

    written = _written(engine)
    for symbol, (kind, amount, stamped, _published, late) in rows.items():
        landed = [row for row in written if symbol in (row.get("by_symbol") or {})]
        if stamped <= migrated_from:
            assert landed == [], f"{symbol}: the old code's windows already covered it"
            continue
        assert len(landed) == 1, f"{symbol} landed {len(landed)} times"
        assert landed[0]["by_symbol"][symbol][kind] == pytest.approx(amount)
        assert landed[0]["bar_open_ms"] == label(stamped), f"{symbol} credited to the wrong bar"
        assert bool(landed[0].get("late_funding")) is late, symbol
    s5 = next(row for row in written if "S5USDT" in (row.get("by_symbol") or {}))
    assert s5["since_ms"] == _at(4), "the current-book path lands on the normal record of the cycle after the gap"
    due = sum(row[1] for row in rows.values() if row[0] == "FUNDING_FEE" and row[2] > migrated_from)
    assert _funding(written) == pytest.approx(due), "and nothing else: the file's total is what was due"


async def test_income_that_is_not_funding_is_ingested_exactly_as_before(panel: Panel, tmp_path: Path) -> None:
    """Same window, same book, same label, same record - and the lagged query re-reading it changes nothing."""
    venue = PublishingVenue(balance=EQUITY, prices=_prices(panel, 400))
    engine = _engine(tmp_path, venue, panel)
    _as_the_old_code_left_it(engine, hour=0, book=HELD)
    engine.state.last_funding_ms = _at(0) - live_engine.FUNDING_SETTLE_LAG_MS
    engine.state.income_prev_contributions = BEFORE
    engine.state.income_prev_bar_ms = _bar(-1)
    # Already ingested by the previous cycle, and inside this cycle's lagged window: must not come back.
    venue.book("ETHUSDT", "COMMISSION", -9.0, at=_at(0) - 60 * SECOND)
    for kind, amount in (("REALIZED_PNL", 12.5), ("COMMISSION", -0.75)):
        venue.book("BTCUSDT", kind, amount, at=SETTLE + 1_200 * SECOND)
    venue.book("", "TRANSFER", 250.0, at=SETTLE + 2_400 * SECOND)
    venue.now = _at(1)
    main = await venue.income(_at(0), _at(1))  # what the one window of the old code returned
    venue.windows.clear()

    flows = await engine._ingest_income(_bar(1), EQUITY)

    written = _written(engine)
    expected = {"bar_open_ms": _bar(0), "since_ms": _at(0), "until_ms": _at(1)} | attribute(main, HELD, {"tsmom": 1.0})
    assert written == [{"at": written[0]["at"]} | expected]
    lag = live_engine.FUNDING_SETTLE_LAG_MS
    assert flows == {
        "total": 250.0,
        "rows": 1,
        "by_type": {"TRANSFER": 250.0},
        "rebaselined": True,
        "funding_window": {"since_ms": _at(0) - lag, "until_ms": _at(1) - lag, "rows": 0},
    }
    assert (engine.state.day_start_equity, engine.state.equity_hwm) == (EQUITY, EQUITY)
    assert venue.windows == [(_at(0), _at(1)), (_at(0) - lag, _at(1) - lag)], "the main window first, unchanged"
    assert engine.state.last_income_ms == _at(1)


async def test_a_watermark_ahead_of_the_clock_skips_both_windows_and_keeps_both(panel: Panel, tmp_path: Path) -> None:
    venue = PublishingVenue(balance=EQUITY, prices=_prices(panel, 400))
    engine = _engine(tmp_path, venue, panel)
    _as_the_old_code_left_it(engine, hour=1, book=AFTER)
    engine.state.last_funding_ms = _at(1) - live_engine.FUNDING_SETTLE_LAG_MS
    engine.state.income_prev_contributions = HELD
    engine.state.income_prev_bar_ms = _bar(0)
    venue.book("BTCUSDT", "FUNDING_FEE", -1.0, at=SETTLE + 5)
    venue.now = _at(1) - 3_612 * SECOND  # the 2026-09-04 shape: the clock moved backwards by an hour

    flows = await engine._ingest_income(_bar(1), EQUITY)

    assert flows["clock_skew_ms"] == 3_612 * SECOND
    assert venue.windows == [], "neither window is asked for"
    assert engine.state.last_income_ms == _at(1)
    assert engine.state.last_funding_ms == _at(1) - live_engine.FUNDING_SETTLE_LAG_MS
    assert (engine.state.income_prev_contributions, engine.state.income_prev_bar_ms) == (HELD, _bar(0))
    assert _written(engine) == []


async def test_a_lost_watermark_takes_the_funding_one_with_it(panel: Panel, tmp_path: Path) -> None:
    """A3: the main window restarts at now and says so; a funding watermark left behind must not
    reach back across the outage and credit all of it to the book held before it."""
    venue = PublishingVenue(balance=EQUITY, prices=_prices(panel, 400))
    engine = _engine(tmp_path, venue, panel)
    engine.state = LiveState(cycles=41, last_funding_ms=_at(-30), income_prev_contributions=HELD, income_prev_bar_ms=0)
    venue.book("BTCUSDT", "FUNDING_FEE", -1.0, at=SETTLE + 5)
    venue.now = _at(1)

    flows = await engine._ingest_income(_bar(1), EQUITY)

    assert flows["watermark_lost"]["lost"] is True
    assert engine.state.last_income_ms == engine.state.last_funding_ms == _at(1)
    assert _written(engine) == []


async def test_funding_paid_before_the_loop_held_a_book_is_not_credited_but_is_said(
    panel: Panel, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A first start: nothing held the position, as the main window already treats it - and a log line says so."""
    venue = PublishingVenue(balance=EQUITY, prices=_prices(panel, 400))
    engine = _engine(tmp_path, venue, panel)
    engine.state = LiveState(last_income_ms=SETTLE - 30 * 60 * SECOND)  # startup set it; no cycle has run
    venue.book("BTCUSDT", "FUNDING_FEE", -1.0, at=SETTLE + 5, published=SETTLE + 90 * SECOND)

    with caplog.at_level(logging.WARNING, logger="beidou_live.engine"):
        await _cycle(engine, venue, 0, HELD)
        await _cycle(engine, venue, 1, HELD)

    assert _written(engine) == []
    assert any("before this loop held a book" in record.getMessage() for record in caplog.records)
    assert engine.state.last_funding_ms == _at(1) - live_engine.FUNDING_SETTLE_LAG_MS


def test_an_old_state_file_loads_and_starts_the_funding_window_at_its_own_watermark(tmp_path: Path) -> None:
    """The keys this change adds are absent from every state.json written before it."""
    store = StateStore(tmp_path)
    store.state_path.write_text('{"cycles": 300, "last_income_ms": 1789000027000, "restarts": 58}', encoding="utf-8")
    state = store.load()
    assert state.last_funding_ms is None
    assert (state.income_prev_contributions, state.income_prev_bar_ms) == ({}, None)
