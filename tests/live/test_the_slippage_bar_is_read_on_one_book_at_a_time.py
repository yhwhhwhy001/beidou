"""M-Q08's 4 bps bar was being failed by a 1/3-fraction probe book on behalf of the main book.

On 2026-09-12 the daily report alerted `滑点 5.5 bps 高于假设的 4 bps` and had been doing so for two
days.  Split by the book that carries each symbol, the same 34 fills read:

    main-book-only names   19 fills   +0.80 bps   (mean +2.32, se 1.44)
    both books' names      15 fills  +16.18 bps   (mean +14.23, se 4.43, t = 2.31 vs the bar)

The combined 5.47 is a mixture that describes neither population, and its own error bar is 2.59 -
0.57 SE from the bar it is quoted as breaching.  Same defect as M-Q03's late share (restarts mixed
into scheduled cycles) and M-015's compression (two books summed before the spread was taken): an
aggregate over two populations, presented as a reading of one.

The gate is deliberately NOT re-pointed here.  Which book M-Q08 judges is an operator's ruling, and a
criterion that re-points itself the first time it fires is exactly what R10 forbids.  What these tests
hold is that the split and the error bar exist, that they are right, and that a record which cannot
name the books says so instead of inventing one population.
"""

from __future__ import annotations

from beidou_live.risk_budget import RiskBudgetParams, books_by_symbol, slippage_bps

HOUR = 3_600_000
NOW = 1_757_000_000_000
BOOKS = {"MAINONLYUSDT": frozenset({"main"}), "OVERLAIDUSDT": frozenset({"main", "flow_short"})}


def _fill(symbol: str, *, decision_close: float, avg_price: float, qty: float = 1.0) -> dict:
    return {
        "bar_open_ms": NOW - HOUR,
        "symbol": symbol,
        "avg_price": avg_price,
        "executed_qty": qty,
        "side": "BUY",
        "decision_close": decision_close,
    }


def _params(**kw: object) -> RiskBudgetParams:
    return RiskBudgetParams(min_slippage_fills=1, **kw)  # type: ignore[arg-type]


def test_the_two_populations_are_reported_separately() -> None:
    """One name at 1 bps and one at 21 bps must not both be reported as 11."""
    fills = [
        _fill("MAINONLYUSDT", decision_close=100.0, avg_price=100.01),
        _fill("OVERLAIDUSDT", decision_close=100.0, avg_price=100.21),
    ]
    result = slippage_bps(fills, _params(), latest_ms=NOW, books=BOOKS)
    groups = result["by_group"]
    assert abs(groups["main_only"]["value"] - 1.0) < 1e-6
    assert abs(groups["overlaid"]["value"] - 21.0) < 1e-6
    assert groups["main_only"]["fills"] == 1 and groups["overlaid"]["fills"] == 1
    # and the combined number, which is neither of them, is still what `inside` reads
    # (notional-weighted, so it lands a hair above the midpoint - the larger fill is the worse one)
    assert abs(result["value"] - 11.0) < 0.05
    assert result["inside"] is False


def test_a_record_that_cannot_name_the_books_reports_one_population_rather_than_guessing() -> None:
    """No `books` in the cycle -> no split.  Absent knowledge is not a reading (the house rule)."""
    fills = [_fill("MAINONLYUSDT", decision_close=100.0, avg_price=100.01)]
    assert slippage_bps(fills, _params(), latest_ms=NOW, books=None)["by_group"] == {}
    assert books_by_symbol({"contributions": {"tsmom": {"MAINONLYUSDT": 1.0}}}) == {}
    assert books_by_symbol(None) == {}


def test_books_by_symbol_reads_the_cycles_own_books_field() -> None:
    """`contributions` keys by strategy; only `books` says which book that strategy belongs to."""
    carried = books_by_symbol(
        {
            "books": {"tsmom": "main", "flow": "flow_short"},
            "contributions": {
                "tsmom": {"BTCUSDT": 1.0, "ENAUSDT": 1.0},
                "flow": {"ENAUSDT": -0.23, "TUTUSDT": -0.2, "ZEROUSDT": 0.0},
            },
        }
    )
    assert carried["BTCUSDT"] == frozenset({"main"})
    assert carried["ENAUSDT"] == frozenset({"main", "flow_short"})
    assert carried["TUTUSDT"] == frozenset({"flow_short"})
    assert "ZEROUSDT" not in carried  # a zero contribution is not a position in a book


def test_the_breach_carries_the_error_bar_that_decides_whether_it_is_a_reading() -> None:
    """Two fills 20 bps apart cannot tell 5.5 from 4.0, and the instrument must say so.

    `min_slippage_fills` counts fills and has never asked how much they disagree; the 2026-09-12
    reading had 34 of them and a 12.7 bps spread.
    """
    noisy = slippage_bps(
        [
            _fill("MAINONLYUSDT", decision_close=100.0, avg_price=100.16),
            _fill("MAINONLYUSDT", decision_close=100.0, avg_price=99.95),
        ],
        _params(),
        latest_ms=NOW,
        books=BOOKS,
    )
    assert noisy["se"] > 2.0
    assert noisy["decisive"] is False  # 5.5 bps with a 10 bps error bar is not a breach measurement

    tight = slippage_bps(
        [_fill("MAINONLYUSDT", decision_close=100.0, avg_price=100.2)] * 4,
        _params(),
        latest_ms=NOW,
        books=BOOKS,
    )
    assert tight["se"] == 0.0
    assert tight["decisive"] is True  # 20 bps against a 4 bps bar, with no disagreement at all


def test_the_worst_symbols_are_named_so_a_breach_can_be_chased() -> None:
    """The 2026-09-12 breach was four illiquid alts; a number alone could not have said that."""
    fills = [
        _fill("MAINONLYUSDT", decision_close=100.0, avg_price=100.01),
        _fill("OVERLAIDUSDT", decision_close=100.0, avg_price=100.5),
    ]
    worst = slippage_bps(fills, _params(), latest_ms=NOW, books=BOOKS)["worst_symbols"]
    assert worst[0]["symbol"] == "OVERLAIDUSDT"
    assert abs(worst[0]["value"] - 50.0) < 1e-6
