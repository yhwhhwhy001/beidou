"""The instrument the 2026-09-08 ruling owes.

Audit question 3 asked whether the backtest's equity and the live loop's equity are the same quantity.
They are not: 52% of live equity is non-USDT collateral and the backtest models none, so every weight
is a fraction of a number that moves with BTC.  The operator ruled on 2026-09-08 that the denominator
stays total equity - it is the venue's own margin basis, and under cross margin the collateral really
does absorb losses - which makes the pro-cyclical amplifier a NAMED accepted risk.

D.8 asks that an accepted residual risk carry a compensating control, and this repository's own
recurring lesson is blunter: a threshold nothing measures is just a sentence.  So the amplifier gets an
instrument.  It measures and changes nothing, and it must never page: it is a standing fact about the
account, not an event.

The property that matters most is the last one.  Reporting the repriced share is safe; subtracting it
would silently produce the USDT-denominator book the operator did not choose.
"""

from __future__ import annotations

from typing import Any

from beidou_live.risk_budget import collateral_drift


def _cycle(ms: int, equity: float, usdt: float) -> dict[str, Any]:
    return {
        "bar_open_ms": ms,
        "equity": equity,
        "collateral": {
            "equity": equity,
            "usdt_equity": usdt,
            "collateral": equity - usdt,
            "share": (equity - usdt) / equity,
        },
    }


def test_it_splits_the_equity_move_into_trading_and_repricing() -> None:
    """The live shape: most of the move was the collateral, and the report has to be able to say so."""
    rows = [_cycle(1_000, 10_964.21, 5_264.90), _cycle(2_000, 10_904.10, 5_216.70)]
    attribution = [{"bar_open_ms": 1_500, "total": -16.19}]
    drift = collateral_drift(rows, attribution)

    assert drift["enforced"]
    assert round(drift["equity_change"], 2) == -60.11
    assert round(drift["attributed_pnl"], 2) == -16.19
    assert round(drift["collateral_repricing"], 2) == -43.92
    assert 0.72 < drift["repricing_share"] < 0.74


def test_attribution_before_the_window_is_not_counted() -> None:
    """Otherwise a long attribution history would be charged against a short collateral window."""
    rows = [_cycle(5_000, 100.0, 50.0), _cycle(6_000, 90.0, 50.0)]
    stale = [{"bar_open_ms": 1, "total": -999.0}, {"bar_open_ms": 5_500, "total": -4.0}]
    assert collateral_drift(rows, stale)["attributed_pnl"] == -4.0


def test_a_flat_window_reports_no_share_rather_than_zero() -> None:
    """Zero would read as "none of it was collateral", which is the opposite of what it means."""
    rows = [_cycle(1, 100.0, 50.0), _cycle(2, 100.0, 40.0)]
    assert collateral_drift(rows, [])["repricing_share"] is None


def test_it_refuses_rather_than_inventing_a_number_when_the_field_is_too_new() -> None:
    """The same refusal shape as the rest of this module: a window too short says so."""
    assert not collateral_drift([], [])["enforced"]
    assert not collateral_drift([_cycle(1, 100.0, 50.0)], [])["enforced"]
    assert "needs 2 cycles" in collateral_drift([{"bar_open_ms": 1}], [])["reason"]


def test_it_is_reported_and_never_subtracted() -> None:
    """Subtracting it would produce the USDT-denominator book the operator did not choose.

    Checked by shape rather than by grepping the caller: the returned mapping carries no field that a
    sizing path could read as a corrected equity, and the raw equity change is present unmodified.
    """
    rows = [_cycle(1, 200.0, 100.0), _cycle(2, 150.0, 100.0)]
    drift = collateral_drift(rows, [{"bar_open_ms": 1, "total": -10.0}])
    assert drift["equity_change"] == -50.0
    assert not any(key.endswith("adjusted_equity") or key == "equity" for key in drift)
