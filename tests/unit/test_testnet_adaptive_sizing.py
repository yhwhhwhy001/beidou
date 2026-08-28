"""PKG-05 executable adaptive-sizing contracts."""

from __future__ import annotations

from beidou_strategy.risk.adaptive_sizing_engine import SizingInput, compute_adaptive_sizing


def _healthy_input(**overrides: object) -> SizingInput:
    values: dict[str, object] = {
        "equity": 1_000.0,
        "available_margin": 1_000.0,
        "volatility": 0.01,
        "capacity_utilization": 1.0,
        "regime_confidence": 1.0,
        "signal_confidence": 1.0,
        "liquidity_score": 1.0,
        "liquidation_distance_pct": 0.50,
        "symbol_price": 20_000.0,
        "step_size": "0.001",
        "min_qty": "0.001",
        "min_notional": "5",
        "max_notional": 25.0,
        "max_leverage": 3.0,
        "stop_distance_pct": 0.005,
    }
    values.update(overrides)
    return SizingInput(**values)


def test_healthy_executable_account_can_form_bounded_quantity() -> None:
    decision = compute_adaptive_sizing(_healthy_input())

    assert decision.is_safe is True
    assert decision.leverage >= 1.0
    assert decision.final_quantity == "0.001"
    assert decision.notional == "20"
    assert decision.output_hash


def test_executable_quantity_never_crosses_absolute_cap() -> None:
    decision = compute_adaptive_sizing(_healthy_input(max_notional=19.0))

    assert decision.is_safe is False
    assert float(decision.notional) <= 19.0
    assert decision.final_quantity == "0"


def test_executable_sizing_requires_complete_venue_minimum_rules() -> None:
    decision = compute_adaptive_sizing(_healthy_input(min_qty=""))

    assert decision.is_safe is False
    assert decision.final_quantity == ""
    assert decision.reason_vector == ["VENUE_RULE_INPUT_INVALID"]
