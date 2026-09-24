"""Checklist 3.9: what closing each held position would take, read off the book the last cycle left.

`beidou_live.report_risk.liquidity_to_close` answers "how long, and at what cost" position by position.
These tests pin the three places it could be quietly wrong:

* WHICH BOOK.  Cycle rows carry no `positions`; the reading takes each `skipped` / `orders` entry's
  `current_notional` and applies the cycle's own fills.  The first test runs on the record's row for the
  2026-09-24T17:00Z bar, fields copied verbatim, because that row closed AKEUSDT: a reading that forgot
  the fills would list it as a position still to close.
* WHICH RULERS.  The hourly volume is `LiveEngine._liquidity`'s and the impact is `impact_costs`', each
  checked against the owner's own code rather than against a second derivation of the same formula.
* WHAT A FULL CLOSE IS.  One order for the whole position, whatever the cap - so the bars column is about
  pure reductions only.  Pinned against `plan_rebalance` on the reading's own numbers, since that sentence
  is what the report prints.
"""

from __future__ import annotations

import math
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd

from beidou_alpha.backtest import ImpactModel, asset_returns, impact_costs
from beidou_alpha.panel import Panel
from beidou_data.store import KlineStore
from beidou_live.composition import impact_model
from beidou_live.engine import LiveEngine
from beidou_live.rebalancer import RebalanceParams, plan_rebalance
from beidou_live.report_risk import LIQUIDITY_WINDOW_BARS, _liquidity_to_close_lines, liquidity_to_close
from beidou_live.reports import daily_alerts, daily_markdown, daily_payload
from beidou_live.state import StateStore
from beidou_shared.config import load_yaml
from beidou_shared.types import Position
from tests.fakes.fake_venue import DEFAULT_RULES

ROOT = Path(__file__).resolve().parents[2]
HOUR = 3_600_000

# `.beidou/live/cycles.jsonl`, the row for the 2026-09-24T17:00Z bar (line 574 of the copy taken at
# 2026-09-24T18:48Z), with the fields this reading reads copied verbatim.  It planned a single order -
# AKEUSDT, a full close - and held the other sixteen names inside the band.  Every `skipped` entry had reason
# NO_TRADE_BAND; its four numbers are listed in the row's order as (symbol, current_notional, delta_notional,
# threshold) and rebuilt under the row's own key names below.
BAND_HELD = (
    ("BTCUSDT", 1689.81946327836, 14.411307734314505, 675.927785311344),
    ("ETHUSDT", 1317.90918798396, 239.06763547081368, 527.163675193584),
    ("ZECUSDT", 591.11936838882, 3.970179301841199, 236.447747355528),
    ("SOLUSDT", 1261.4755747761, -44.06819644589541, 504.59022991044003),
    ("XRPUSDT", 857.4409340640001, -19.41861248008115, 342.97637362560005),
    ("HYPEUSDT", 1140.50482, -173.56223590824925, 456.20192800000007),
    ("DOGEUSDT", 837.2497500000001, -79.99418445039805, 334.89990000000006),
    ("BNBUSDT", 1424.8103289360001, 241.8861919889082, 569.9241315744001),
    ("NEARUSDT", 453.669, -86.24450000167752, 181.4676),
    ("UNIUSDT", 488.76599999999996, -91.92474012237074, 195.50639999999999),
    ("SUIUSDT", 484.3335, 135.22877111208794, 193.73340000000002),
    ("ENAUSDT", 337.39113936, 74.00637707577368, 134.956455744),
    ("LSKUSDT", 216.16685295000002, -8.715134265389793, 86.46674118000001),
    ("1000PEPEUSDT", 364.3229664, 95.78941535493209, 145.72918656),
    ("TRUMPUSDT", 628.3515, 14.83846456921242, 251.3406),
    ("ADAUSDT", 816.5598, -56.20553699838479, 326.62392),
)
RECORDED: dict[str, Any] = {
    "bar": "2026-09-24T17:00:00+00:00",
    "bar_open_ms": 1790269200000,
    "as_of_ms": 1790269200000,
    "at": "2026-09-24T18:00:30+00:00",
    "equity": 13084.90044379,
    "gross_before": 13025.38607271,
    "skip": False,
    "dry_run": False,
    "skipped": [
        {
            "current_notional": held,
            "delta_notional": delta,
            "reason": "NO_TRADE_BAND",
            "symbol": symbol,
            "threshold": band,
        }
        for symbol, held, delta, band in BAND_HELD
    ],
    "orders": [
        {
            "avg_price": 0.0392107,
            "client_order_id": "bd-1790269200000-AKEUSDT",
            "current_notional": 115.49588661,
            "error": "",
            "executed_qty": "2943",
            "note": "",
            "order_id": "322986694",
            "price": 0.03924427,
            "quantity": "2943",
            "reduce_only": True,
            "side": "SELL",
            "status": "FILLED",
            "symbol": "AKEUSDT",
            "target_notional": 0.0,
            "target_weight": 0.0,
            "venue_status": "FILLED",
        }
    ],
}

# The newest row before it that carries `construction_full` - the 2026-09-23T16:00Z bar, the restart that
# day - with its `rebalance` block verbatim.  The knobs come from here, not from the config file.
RECORDED_REBALANCE: dict[str, Any] = {
    "band_entry_multiple": 2.0,
    "exempt_crossings": True,
    "exempt_reductions": False,
    "flat_inside_band": True,
    "max_order_notional": None,
    "max_participation": 0.02,
    "no_trade_band": 0.005,
    "no_trade_rel_band": 0.4,
}
RESTART: dict[str, Any] = {
    "bar": "2026-09-23T16:00:00+00:00",
    "bar_open_ms": 1790179200000,
    "as_of_ms": 1790179200000,
    "equity": 12792.09564952,
    "gross_before": 10974.64239565,
    "skip": False,
    "dry_run": False,
    "construction_full": {"rebalance": RECORDED_REBALANCE},
}

# The same copy, line 221: the 2026-09-10T23:00Z bar, in the shape every row had before 2026-09-14T19:00Z -
# a band-held entry carries its delta and threshold but no `current_notional`.  Three of its eighteen
# entries, verbatim.
OLDER: dict[str, Any] = {
    "bar": "2026-09-10T23:00:00+00:00",
    "bar_open_ms": 1789081200000,
    "as_of_ms": 1789081200000,
    "equity": 10683.76934132,
    "gross_before": 6253.8046597,
    "skip": False,
    "dry_run": False,
    "orders": [],
    "skipped": [
        {
            "delta_notional": -16.650476488857294,
            "reason": "NO_TRADE_BAND",
            "symbol": "BTCUSDT",
            "threshold": 331.1607347217216,
        },
        {
            "delta_notional": -57.99035104795837,
            "reason": "NO_TRADE_BAND",
            "symbol": "ETHUSDT",
            "threshold": 261.2474720000001,
        },
        {
            "delta_notional": -14.395702158595725,
            "reason": "NO_TRADE_BAND",
            "symbol": "SOLUSDT",
            "threshold": 201.98995556352,
        },
    ],
}

DECISION = 1_790_269_200_000  # the recorded row's bar, reused for the synthetic books below


def _store(tmp_path: Path, rows: list[dict[str, Any]]) -> StateStore:
    store = StateStore(tmp_path / "live")
    for row in rows:
        store.append_cycle(row)
    return store


def _cycle(bar_ms: int, held: dict[str, float], **extra: Any) -> dict[str, Any]:
    """A traded cycle holding `held` inside the band, with the recorded row's field names."""
    return {
        "bar": pd.Timestamp(bar_ms, unit="ms", tz="UTC").isoformat(),
        "bar_open_ms": bar_ms,
        "as_of_ms": bar_ms,
        "equity": 10_000.0,
        "gross_before": sum(abs(value) for value in held.values()),
        "skip": False,
        "dry_run": False,
        "skipped": [
            {"current_notional": value, "delta_notional": 0.0, "reason": "NO_TRADE_BAND", "symbol": symbol}
            for symbol, value in held.items()
        ],
        "orders": [],
        **extra,
    }


def _klines(
    root: Path, symbol: str, *, last_ms: int, bars: int, quote: float, price: float = 100.0, seed: int | None = 7
) -> pd.DataFrame:
    """Hourly bars in the archive's own format, written through `KlineStore` - the store the reading loads.

    `seed=None` gives a flat tape: every bar trades exactly `quote`, so the hourly mean is known by hand.
    """
    rng = np.random.default_rng(seed)
    moves = np.zeros(bars) if seed is None else rng.normal(0.0, 0.01, bars)
    opens = price * np.exp(np.cumsum(moves))
    closes = opens * np.exp(np.zeros(bars) if seed is None else rng.normal(0.0, 0.01, bars))
    volumes = np.full(bars, quote) if seed is None else quote * (0.5 + rng.random(bars))
    open_time = last_ms - HOUR * np.arange(bars)[::-1]
    frame = pd.DataFrame(
        {
            "open_time": open_time.astype("int64"),
            "open": opens,
            "high": np.maximum(opens, closes),
            "low": np.minimum(opens, closes),
            "close": closes,
            "volume": volumes / closes,
            "close_time": (open_time + HOUR - 1).astype("int64"),
            "quote_volume": volumes,
            "trades": np.full(bars, 100, dtype="int64"),
            "taker_buy_base": volumes / closes / 2.0,
            "taker_buy_quote": volumes / 2.0,
        }
    )
    KlineStore(root).append(symbol, "1h", frame)
    return frame


def test_the_book_is_what_the_recorded_cycle_left_after_its_own_fills(tmp_path: Path) -> None:
    """Sixteen names held, AKEUSDT gone: the order that closed it is applied at the price the planner used."""
    store = _store(tmp_path, [RESTART, RECORDED])
    result = liquidity_to_close(store, "2026-09-24", root=tmp_path / "data")
    held = {entry["symbol"]: entry["current_notional"] for entry in RECORDED["skipped"]}

    assert result["enforced"] is True and result["bar"] == "2026-09-24T17:00:00+00:00"
    assert result["positions"] == 16 and "AKEUSDT" not in result["unpriced"]
    assert math.isclose(result["gross_u"], sum(held.values()), rel_tol=1e-12)
    # The row accounts for its whole book: its per-symbol notionals add up to its own `gross_before`.
    assert math.isclose(result["accounted_before_u"], RECORDED["gross_before"], rel_tol=1e-9)
    assert result["book_complete"] is True
    assert result["max_participation"] == 0.02 and result["exempt_reductions"] is False
    # No archive under this root: every position says why it has no reading, and nothing reads as free.
    assert result["rows"] == [] and set(result["unpriced"]) == set(held)
    assert result["impact_u"] is None and result["impact_bps"] is None and result["hardest"] == []


def test_the_hourly_volume_is_the_loops_and_the_impact_is_the_backtests(tmp_path: Path) -> None:
    """Each ruler against its owner's code, on one tape: `LiveEngine._liquidity` and `impact_costs`."""
    root = tmp_path / "data"
    # 800 bars ending one bar AFTER the decision, so the backtest has an execution bar to charge on and
    # the reading has to leave it out.
    frame = _klines(root, "ETHUSDT", last_ms=DECISION + HOUR, bars=800, quote=2.0e6)
    notional = -250_000.0  # a short: closing it is a buy, and the size is what the rulers see
    store = _store(
        tmp_path, [_cycle(DECISION, {"ETHUSDT": notional}, construction_full={"rebalance": RECORDED_REBALANCE})]
    )
    [row] = liquidity_to_close(store, "2026-09-24", root=root)["rows"]

    decided = frame[frame["open_time"] <= DECISION]
    engine = SimpleNamespace(config=SimpleNamespace(liquidity_window=LIQUIDITY_WINDOW_BARS))
    live = LiveEngine._liquidity(engine, {"ETHUSDT": decided})["ETHUSDT"]
    assert math.isclose(row["hourly_quote_volume_u"], live, rel_tol=1e-12)
    assert math.isclose(row["participation"], abs(notional) / live, rel_tol=1e-12)

    panel = Panel.from_frames({"ETHUSDT": frame}, interval="1h")
    execution = pd.Timestamp(DECISION + HOUR, unit="ms", tz="UTC")
    turnover = pd.DataFrame({"ETHUSDT": [1.0]}, index=pd.DatetimeIndex([execution]))
    rets = asset_returns(panel, "open_to_close")
    charged = impact_costs(turnover, rets, panel, ["ETHUSDT"], ImpactModel(capital=abs(notional)))
    assert math.isclose(row["impact_bps"], 1e4 * float(charged.loc[execution, "ETHUSDT"]), rel_tol=1e-9)
    assert math.isclose(row["impact_u"], abs(notional) * float(charged.loc[execution, "ETHUSDT"]), rel_tol=1e-9)
    assert row["volume_through_ms"] == DECISION and row["impact_bps"] > 0.0


def test_the_cap_stretches_a_reduction_over_bars_and_a_full_close_goes_out_whole(tmp_path: Path) -> None:
    """The report's two sentences, checked against `plan_rebalance` on the reading's own numbers.

    1,500 USDT of BTCUSDT against a flat 30,000 USDT hourly tape: participation 5%, 2.5 caps, so 3 bars for a
    reduction of the whole position.  The planner sends the full close as one order anyway.
    """
    root = tmp_path / "data"
    _klines(root, "BTCUSDT", last_ms=DECISION, bars=800, quote=30_000.0, price=60_000.0, seed=None)
    store = _store(
        tmp_path, [_cycle(DECISION, {"BTCUSDT": 1_500.0}, construction_full={"rebalance": RECORDED_REBALANCE})]
    )
    result = liquidity_to_close(store, "2026-09-24", root=root)
    [row] = result["rows"]
    assert math.isclose(row["participation"], 0.05, rel_tol=1e-12)
    assert row["bars_under_cap"] == 3 == result["slowest_bars_under_cap"]

    def plan(target: float) -> list[Any]:
        orders, _skipped = plan_rebalance(
            {"BTCUSDT": target},
            managed_symbols=["BTCUSDT"],
            equity=10_000.0,
            positions={"BTCUSDT": Position("BTCUSDT", 0.025, 60_000.0, 60_000.0)},
            prices={"BTCUSDT": 60_000.0},
            rules=DEFAULT_RULES,
            bar_open_ms=DECISION,
            params=RebalanceParams(max_participation=result["max_participation"]),
            liquidity={"BTCUSDT": row["hourly_quote_volume_u"]},
        )
        return orders

    [close] = plan(0.0)
    assert close.quantity == Decimal("0.025") and close.note == "", "a full close is one order for all of it"
    [cut] = plan(0.0001)
    assert cut.reduce_only and cut.note == "PARTICIPATION_CAPPED"
    assert cut.notional <= 0.02 * row["hourly_quote_volume_u"] + 1e-9, "a pure reduction is held to the cap"
    lines = _liquidity_to_close_lines(result)
    assert "只对纯减仓成立" in lines["纯减仓限速"] and "豁免完全平仓" in lines["完全平仓"]


def test_a_skip_is_walked_past_and_a_later_day_is_never_read(tmp_path: Path) -> None:
    """A guard skip places no order, so the book is the one before it; a past report never sees tomorrow."""
    day = DECISION - 17 * HOUR  # 2026-09-24T00:00Z
    rows = [
        _cycle(day + HOUR, {"BTCUSDT": 1_500.0}),
        {**_cycle(day + 2 * HOUR, {}), "skip": True, "gross_before": 1_510.0},
        _cycle(day + 25 * HOUR, {"BTCUSDT": 3_000.0}),
    ]
    store = _store(tmp_path, rows)
    today = liquidity_to_close(store, "2026-09-24", root=tmp_path / "data")
    assert today["bar"] == rows[0]["bar"] and math.isclose(today["gross_u"], 1_500.0)
    assert liquidity_to_close(store, "2026-09-25", root=tmp_path / "data")["bar"] == rows[2]["bar"]
    before = liquidity_to_close(store, "2026-09-23", root=tmp_path / "data")
    assert before["enforced"] is False and "2026-09-23" in before["why"]
    assert _liquidity_to_close_lines(before)["status"] == "n/a"


def test_a_row_that_names_positions_without_their_notional_is_not_read_as_flat(tmp_path: Path) -> None:
    """Zero positions counted is "not recorded" on an older row, and the page has to say which."""
    result = liquidity_to_close(_store(tmp_path, [OLDER]), "2026-09-10", root=tmp_path / "data")
    assert result["positions"] == 0 and result["accounted_before_u"] == 0.0 and result["book_complete"] is False
    lines = _liquidity_to_close_lines(result)
    assert "差 6,253.80 U" in lines["按币记录不全"]
    assert lines["最难平的三个（按完全平仓的参与率）"] == "读不出：这一行没有按币记下名义额" == lines["成交额截至"]


def test_what_cannot_be_read_says_why_instead_of_reading_zero(tmp_path: Path) -> None:
    """Too few bars for sigma, no archive at all, an archive that stops short, and no knobs on record."""
    root = tmp_path / "data"
    _klines(root, "ETHUSDT", last_ms=DECISION - 2 * HOUR, bars=100, quote=1.0e6)
    store = _store(tmp_path, [_cycle(DECISION, {"ETHUSDT": 5_000.0, "SOLUSDT": -800.0})])
    result = liquidity_to_close(store, "2026-09-24", root=root)
    [row] = result["rows"]
    assert row["participation"] is not None and row["sigma_daily"] is None and row["impact_bps"] is None
    assert result["unpriced"] == {"SOLUSDT": "no 1h archive"}
    assert result["volume_lag_bars"] == 2 and result["max_participation"] is None and row["bars_under_cap"] is None
    lines = _liquidity_to_close_lines(result)
    assert lines["SOLUSDT"] == "读不出：no 1h archive" and "冲击读不出" in lines["ETHUSDT"]
    assert "早 2 根" in lines["成交额截至"] and lines["纯减仓限速"].startswith("读不出")


def test_nothing_it_raises_reaches_the_rest_of_the_report(tmp_path: Path) -> None:
    """It gates nothing: a broken parquet costs one symbol its reading, and a broken row costs the block."""
    root = tmp_path / "data"
    _klines(root, "BTCUSDT", last_ms=DECISION, bars=800, quote=30_000.0, price=60_000.0, seed=None)
    broken = KlineStore(root).path("ETHUSDT", "1h")
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"")  # a zero-byte parquet raises ArrowInvalid, not FileNotFoundError
    store = _store(tmp_path, [_cycle(DECISION, {"BTCUSDT": 1_500.0, "ETHUSDT": 900.0})])
    result = liquidity_to_close(store, "2026-09-24", root=root)
    assert [row["symbol"] for row in result["rows"]] == ["BTCUSDT"]
    assert result["unpriced"]["ETHUSDT"].startswith("archive unreadable: ")

    garbled = _store(tmp_path / "garbled", [{**_cycle(DECISION, {}), "skipped": ["not an entry"]}])
    block = liquidity_to_close(garbled, "2026-09-24", root=root)
    assert block["enforced"] is False and block["why"].startswith("AttributeError")
    assert _liquidity_to_close_lines(block) == {"status": "n/a", "why": block["why"]}


def test_exempt_reductions_on_record_turns_the_bars_column_into_no_path(tmp_path: Path) -> None:
    """The knob is read off the record: flipped there, the report stops calling the column a reduction's."""
    root = tmp_path / "data"
    _klines(root, "BTCUSDT", last_ms=DECISION, bars=800, quote=30_000.0, price=60_000.0, seed=None)
    flipped = {**RECORDED_REBALANCE, "exempt_reductions": True}
    store = _store(tmp_path, [_cycle(DECISION, {"BTCUSDT": 1_500.0}, construction_full={"rebalance": flipped})])
    lines = _liquidity_to_close_lines(liquidity_to_close(store, "2026-09-24", root=root))
    assert lines["纯减仓限速"].startswith("exempt_reductions 开着")


def test_the_window_and_the_impact_model_are_the_shipped_ones() -> None:
    """The record carries neither, so they are constants here - held to the files the loop and research read."""
    profile = load_yaml(ROOT / "config" / "live.demo.yaml")
    assert int(profile["pool"]["liquidity_window"]) == LIQUIDITY_WINDOW_BARS
    shipped = impact_model(load_yaml(ROOT / "config" / "costs.yaml"))
    default = ImpactModel()
    assert (default.coefficient, default.adv_window, default.vol_window) == (
        shipped.coefficient,
        shipped.adv_window,
        shipped.vol_window,
    )


def test_the_daily_report_prints_it_under_margin_and_never_pages_on_it(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _klines(root, "BTCUSDT", last_ms=DECISION, bars=800, quote=30_000.0, price=60_000.0, seed=None)
    store = _store(
        tmp_path, [_cycle(DECISION, {"BTCUSDT": 1_500.0}, construction_full={"rebalance": RECORDED_REBALANCE})]
    )
    payload = daily_payload(store, "2026-09-24", data_root=root)
    assert payload["liquidity_to_close"]["enforced"] is True
    keys = list(payload)
    assert keys.index("liquidity_to_close") == keys.index("margin") + 1
    markdown = daily_markdown(payload)
    margin = markdown.index("## Margin and rejections (M-007)")
    here = markdown.index("## Liquidity to close (3.9, reported only)")
    assert margin < here < markdown.index("## Exits and pool (M-005 / M-006)")
    assert "| BTCUSDT | +1,500.00 U；参与率 5.0000%" in markdown
    without = {key: value for key, value in payload.items() if key != "liquidity_to_close"}
    assert daily_alerts(payload) == daily_alerts(without), "reported only: it must not reach alerts or notices"
