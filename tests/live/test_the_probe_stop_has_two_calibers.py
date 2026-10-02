"""The probe stop read a quantity 17x quieter than the one it was calibrated against; since 2026-10-03 it does not.

Operator ruling 2026-09-12 (option C of the EXP-G6′ close-out).  Measured, both of them:

    realised attributed income (what it read)    30-day sigma 0.137% of equity -> -2% is 14.6 sigma
    mark-to-market (what its evidence is in)     30-day sigma 3.239%           -> -2% is  0.62 sigma

Neither is the "-2 sigma" D-019 claimed, and they miss in opposite directions.  Realised income only
moves when a position closes, and the no-trade band held 197 symbol-cycles on the day this was
measured - a sleeve can bleed on the mark for a month while that series barely moves.

The gate moved with the 2026-10-03 batch (`probe-stop-caliber` in `governance/window_changes.yaml`, operator
ruling D1 of 2026-10-02): it reads the mark-to-market P&L, with `max_loss` re-derived on that caliber as the
empirical 2.28% quantile of the book's own 30-day P&L - main 5.1% on the pit panel at k 0.175.  The realised
reading stays in the row as `realised_would_stop`, gating nothing.  Caliber and threshold moved together: alone,
the caliber would have fired at once (flow's marked -3.197% against -2% on 09-12, falsifier F3).
"""

from __future__ import annotations

from beidou_live.probe import ProbeParams, marked_pnl, probe_status

HOUR = 3_600_000
BASE = 1_789_000_000_000


def _cycle(index: int, weights: dict[str, float] | None, closes: dict[str, float] | None) -> dict:
    row: dict = {"bar_open_ms": BASE + index * HOUR}
    if weights is not None:
        row["book_weights"] = {"flow_short": weights, "main": {"BTCUSDT": 1.0}}
    if closes is not None:
        row["closes"] = closes
    return row


def _now(index: int) -> int:
    return BASE + index * HOUR


def test_the_marked_pnl_is_last_bars_weight_times_this_bars_return() -> None:
    """w_{t-1} . r_t, which is what the backtest measures and what the evidence is in."""
    cycles = [
        _cycle(0, {"BTCUSDT": 0.5}, {"BTCUSDT": 100.0}),
        _cycle(1, {"BTCUSDT": 0.5}, {"BTCUSDT": 110.0}),  # +10% on a 0.5 weight -> +5%
    ]
    out = marked_pnl(cycles, "flow_short", window_days=30, now_ms=_now(1))
    assert abs(out["value"] - 0.05) < 1e-12
    assert out["bars"] == 1


def test_a_short_window_understates_the_loss_rather_than_refusing() -> None:
    """For a STOP that is the safe direction - it can delay a stop, never cause one."""
    cycles = [
        _cycle(0, {"BTCUSDT": 1.0}, {"BTCUSDT": 100.0}),
        _cycle(1, {"BTCUSDT": 1.0}, {"BTCUSDT": 90.0}),
    ]
    out = marked_pnl(cycles, "flow_short", window_days=30, now_ms=_now(1))
    assert out["value"] < 0 and out["bars"] == 1, "one bar of a thirty-day window is still a reading"


def test_a_record_without_the_two_fields_reads_as_unreadable_not_as_zero() -> None:
    """Every cycle written before 2026-09-12 is this case, and zero would be a quiet false OK."""
    out = marked_pnl([_cycle(0, None, None), _cycle(1, None, None)], "flow_short", window_days=30, now_ms=_now(1))
    assert out["value"] is None and out["bars"] == 0
    assert "book_weights" in out["why"]


def test_a_symbol_priced_on_only_one_side_makes_no_claim() -> None:
    """A missing price is not a zero return; it is no statement about that symbol."""
    cycles = [
        _cycle(0, {"BTCUSDT": 1.0, "GONEUSDT": 1.0}, {"BTCUSDT": 100.0, "GONEUSDT": 5.0}),
        _cycle(1, {"BTCUSDT": 1.0}, {"BTCUSDT": 101.0}),  # GONEUSDT delisted mid-window
    ]
    out = marked_pnl(cycles, "flow_short", window_days=30, now_ms=_now(1))
    assert abs(out["value"] - 0.01) < 1e-12, "only BTC's return may be counted"


def test_bars_outside_the_window_are_not_counted() -> None:
    cycles = [_cycle(i, {"BTCUSDT": 1.0}, {"BTCUSDT": 100.0 + i}) for i in range(5)]
    inside = marked_pnl(cycles, "flow_short", window_days=30, now_ms=_now(4))
    narrow = marked_pnl(cycles, "flow_short", window_days=30, now_ms=_now(4) - 2 * HOUR)
    assert inside["bars"] == 4 and narrow["bars"] == 2


def test_the_gate_reads_the_marked_caliber_and_says_what_the_realised_one_would_say() -> None:
    """The two disagreeing IS the finding, so both are reported - and since 2026-10-03 the marked one gates."""
    params = ProbeParams(book="flow_short", strategy="flow", window_days=30, max_loss=0.02)
    attribution = [
        {"bar_open_ms": BASE + HOUR, "basis": "net_exposure", "by_strategy": {"flow": -1.0}}
    ]  # -0.01% of equity
    cycles = [
        _cycle(0, {"BTCUSDT": 1.0}, {"BTCUSDT": 100.0}),
        _cycle(1, {"BTCUSDT": 1.0}, {"BTCUSDT": 96.0}),  # -4% marked, past the -2% threshold
    ]
    status = probe_status(params, attribution, equity=10_000.0, now_ms=_now(1), cycles=cycles)
    assert status["gate"] == "marked"
    assert status["stop"] is True and status["status"] == "STOP", "the marked reading crossed"
    assert status["realised_would_stop"] is False, "and the realised one, reported beside it, did not"
    assert status["marked_pnl_pct"] < -0.02


def test_a_realised_breach_alone_no_longer_stops_the_book_and_says_so() -> None:
    """The old gate's reading is kept, gating nothing: a -5% realised month with no marked reading reads OK."""
    params = ProbeParams(book="flow_short", strategy="flow", window_days=30, max_loss=0.02)
    attribution = [
        {"bar_open_ms": BASE + HOUR, "basis": "net_exposure", "by_strategy": {"flow": -500.0}}
    ]  # -5% of equity
    status = probe_status(params, attribution, equity=10_000.0, now_ms=_now(1), cycles=[])
    assert status["stop"] is False and status["status"] == "OK"
    assert status["realised_would_stop"] is True
    assert status["marked_pnl_pct"] is None, "no marked inputs is no claim of a loss - the safe direction for a stop"


def test_a_zero_threshold_does_not_fire_on_a_flat_month() -> None:
    """`max_loss` 0 means "any loss", and a reading of exactly zero is not a loss."""
    params = ProbeParams(book="flow_short", strategy="flow", window_days=30, max_loss=0.0)
    flat = [_cycle(0, {"BTCUSDT": 1.0}, {"BTCUSDT": 100.0}), _cycle(1, {"BTCUSDT": 1.0}, {"BTCUSDT": 100.0})]
    assert probe_status(params, [], equity=10_000.0, now_ms=_now(1), cycles=flat)["stop"] is False
    down = [_cycle(0, {"BTCUSDT": 1.0}, {"BTCUSDT": 100.0}), _cycle(1, {"BTCUSDT": 1.0}, {"BTCUSDT": 99.9})]
    assert probe_status(params, [], equity=10_000.0, now_ms=_now(1), cycles=down)["stop"] is True
