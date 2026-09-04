"""P13 monitoring: the ladder fires on its thresholds, and refuses to invent a number it cannot compute."""

from __future__ import annotations

from typing import Any

import pytest

from beidou_live.risk_budget import (
    RiskBudgetParams,
    drawdown_state,
    guard_firings,
    realised_vol,
    risk_budget_status,
    slippage_bps,
)

HOUR_MS = 3_600_000


def _cycle(index: int, equity: float, **extra: Any) -> dict[str, Any]:
    return {
        "bar_open_ms": 1_700_000_000_000 + index * HOUR_MS,
        "at": f"2026-09-04T{index % 24:02d}:00:00+00:00",
        "equity": equity,
        "construction": extra.pop("construction", "aaa"),
        "guard_reasons": extra.pop("guard_reasons", []),
        **extra,
    }


def test_the_ladder_fires_at_its_two_thresholds_and_names_the_action() -> None:
    params = RiskBudgetParams()
    flat = [_cycle(i, 100.0) for i in range(5)]
    assert drawdown_state(flat, params)["action"] is None
    assert drawdown_state([*flat, _cycle(5, 70.0)], params)["action"] is None  # -30%, inside
    stepped = drawdown_state([*flat, _cycle(5, 64.0)], params)  # -36%
    assert stepped["action"] == "vol_target -> 0.225"
    rolled = drawdown_state([*flat, _cycle(5, 49.0)], params)  # -51%
    assert rolled["action"] == "vol_target -> 0.15"


def test_a_rebaselined_cycle_resets_the_high_water_mark() -> None:
    """The demo account resets, and a reset arrives as a TRANSFER row rather than as a loss."""
    params = RiskBudgetParams()
    rows = [_cycle(0, 10_000.0), _cycle(1, 10_000.0)]
    rows.append(_cycle(2, 1_000.0, external_flows={"rebaselined": True}))
    rows.append(_cycle(3, 990.0))
    state = drawdown_state(rows, params)
    assert state["peak"] == pytest.approx(1_000.0)
    assert state["value"] == pytest.approx(0.01)


def test_volatility_refuses_to_answer_on_too_few_bars_or_a_mixed_construction() -> None:
    params = RiskBudgetParams(min_vol_bars=10)
    short = [_cycle(i, 100.0 + i) for i in range(5)]
    thin = realised_vol(short, params)
    assert thin["value"] is None and not thin["enforced"] and "needs 10" in thin["why"]

    mixed = [_cycle(i, 100.0 + i, construction="aaa" if i < 10 else "bbb") for i in range(30)]
    straddled = realised_vol(mixed, params)
    assert straddled["value"] is None and "2 constructions" in straddled["why"]


def _wiggle(step: float, bars: int = 40) -> list[dict[str, Any]]:
    equity, rows = 100.0, []
    for i in range(bars):
        equity *= (1.0 + step) if i % 2 else (1.0 - step)
        rows.append(_cycle(i, equity))
    return rows


def test_the_band_is_calibrated_around_the_target_it_monitors() -> None:
    """+/-0.3% an hour is 28% annualised, which is what a 0.30 vol target should realise: inside."""
    params = RiskBudgetParams(min_vol_bars=10, vol_band=(0.26, 0.38))
    out = realised_vol(_wiggle(0.003), params)
    assert out["enforced"] and out["value"] == pytest.approx(0.284, abs=0.01) and out["inside"]


def test_volatility_outside_the_band_is_reported_as_such() -> None:
    params = RiskBudgetParams(min_vol_bars=10, vol_band=(0.26, 0.38))
    hot = realised_vol(_wiggle(0.006), params)  # ~57% annualised
    assert hot["enforced"] and not hot["inside"]
    cold = realised_vol(_wiggle(0.001), params)  # ~9% annualised
    assert cold["enforced"] and not cold["inside"]


def test_slippage_is_notional_weighted_and_signed_by_side() -> None:
    params = RiskBudgetParams(min_slippage_fills=2)
    trades = [
        # buying above the reference is adverse; selling below it is too
        {"bar_open_ms": 1_700_000_000_000, "side": "BUY", "price": 100.0, "avg_price": 100.1, "executed_qty": "1"},
        {"bar_open_ms": 1_700_000_000_000, "side": "SELL", "price": 100.0, "avg_price": 99.9, "executed_qty": "1"},
    ]
    out = slippage_bps(trades, params, latest_ms=1_700_000_000_000)
    assert out["enforced"] and out["value"] == pytest.approx(10.0, abs=0.05)
    # a favourable buy pulls it back below the limit
    trades[0]["avg_price"] = 99.9
    assert slippage_bps(trades, params, latest_ms=1_700_000_000_000)["value"] == pytest.approx(0.0, abs=0.05)


def test_a_thin_trade_log_reports_why_instead_of_zero_bps() -> None:
    out = slippage_bps([], RiskBudgetParams(), latest_ms=1_700_000_000_000)
    assert out["value"] is None and not out["enforced"] and "0 fills" in out["why"]


def test_guard_firings_are_counted_but_never_alert() -> None:
    params = RiskBudgetParams()
    rows = [
        _cycle(0, 100.0, guard_reasons=["DAILY_LOSS_PAUSE"]),
        _cycle(1, 100.0, guard_reasons=["GROSS_CAPPED", "DAILY_LOSS_PAUSE"]),
        _cycle(2, 100.0),
    ]
    counts = guard_firings(rows, params)
    assert counts["daily_loss_pause_bars"] == 2 and counts["gross_capped_bars"] == 1
    assert risk_budget_status(rows, [], params)["status"] == "OK"


def test_status_collects_every_breached_reason() -> None:
    params = RiskBudgetParams(min_vol_bars=10, min_slippage_fills=1)
    equity, rows = 100.0, []
    for i in range(40):
        equity *= 1.02 if i % 2 else 0.94  # violent, and ends deep below the peak
        rows.append(_cycle(i, equity))
    trades = [{"bar_open_ms": rows[-1]["bar_open_ms"], "side": "BUY", "price": 100.0, "avg_price": 101.0,
               "executed_qty": "1"}]
    out = risk_budget_status(rows, trades, params)
    assert out["status"] == "ALERT"
    assert any("drawdown" in r for r in out["reasons"])
    assert any("realised vol" in r for r in out["reasons"])
    assert any("slippage" in r for r in out["reasons"])


def test_params_come_from_the_profile_block() -> None:
    params = RiskBudgetParams.from_mapping({"deescalate_at": 0.30, "vol_band": [0.2, 0.5], "unknown_key": 1})
    assert params.deescalate_at == 0.30 and params.vol_band == (0.2, 0.5)
    with pytest.raises(ValueError, match="deescalate_at"):
        RiskBudgetParams(deescalate_at=0.6, rollback_at=0.5)
