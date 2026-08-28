"""Coverage gap tests for beidou_strategy.risk.adaptive_sizing_engine.

Targets the validation/rejection branches and low-confidence reduction paths
that the existing executable-sizing tests do not reach: non-finite decimal
normalization, range violations, liquidity/drawdown/stop reductions,
below-venue-minimum leverage, malformed venue rules, and the min-notional
rejection path (distinct from the already-covered min-quantity path).
"""

from __future__ import annotations

from decimal import Decimal

from beidou_strategy.risk.adaptive_sizing_engine import SizingInput, _decimal, compute_adaptive_sizing


def _input(**overrides: object) -> SizingInput:
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
        "max_notional": 100_000.0,
        "max_leverage": 3.0,
        "stop_distance_pct": 0.0,
        "drawdown_pct": 0.0,
        "funding_rate": 0.0,
    }
    values.update(overrides)
    return SizingInput(**values)


def test_decimal_helper_handles_empty_and_invalid() -> None:
    assert _decimal(None, default=Decimal("7")) == Decimal("7")
    assert _decimal("", default=Decimal("7")) == Decimal("7")
    assert _decimal("abc") is None


def test_equity_must_be_positive() -> None:
    assert compute_adaptive_sizing(_input(equity=0.0)).reason_vector == ["INVALID_OR_UNKNOWN_INPUT"]
    assert compute_adaptive_sizing(_input(available_margin=-1.0)).reason_vector == ["INVALID_OR_UNKNOWN_INPUT"]


def test_capacity_must_be_within_range() -> None:
    assert compute_adaptive_sizing(_input(capacity_utilization=1.5)).reason_vector == ["CAPACITY_INPUT_OUT_OF_RANGE"]


def test_confidence_must_be_within_range() -> None:
    assert compute_adaptive_sizing(_input(regime_confidence=1.5)).reason_vector == ["CONFIDENCE_INPUT_OUT_OF_RANGE"]
    assert compute_adaptive_sizing(_input(signal_confidence=-0.1)).reason_vector == ["CONFIDENCE_INPUT_OUT_OF_RANGE"]


def test_liquidity_must_be_within_range() -> None:
    assert compute_adaptive_sizing(_input(liquidity_score=1.5)).reason_vector == ["LIQUIDITY_INPUT_OUT_OF_RANGE"]


def test_drawdown_must_be_within_range() -> None:
    assert compute_adaptive_sizing(_input(drawdown_pct=1.0)).reason_vector == ["DRAWDOWN_INPUT_OUT_OF_RANGE"]


def test_low_liquidity_is_reduced() -> None:
    decision = compute_adaptive_sizing(_input(liquidity_score=0.1))
    assert "LOW_LIQUIDITY:10.0%" in decision.reason_vector
    assert decision.is_safe is False


def test_drawdown_triggers_reduction() -> None:
    decision = compute_adaptive_sizing(_input(drawdown_pct=0.2))
    assert "DRAWDOWN_REDUCTION:20.0%" in decision.reason_vector
    assert decision.is_safe is False


def test_wide_stop_triggers_reduction() -> None:
    decision = compute_adaptive_sizing(_input(stop_distance_pct=0.5))
    assert "STOP_RISK_REDUCTION:50.0%" in decision.reason_vector
    assert decision.is_safe is False


def test_leverage_below_venue_minimum_is_rejected() -> None:
    decision = compute_adaptive_sizing(_input(max_leverage=0.5))
    assert "LEVERAGE_BELOW_VENUE_MINIMUM" in decision.reason_vector
    assert decision.leverage == 0.0
    assert decision.final_quantity == ""


def test_malformed_venue_rule_is_rejected() -> None:
    decision = compute_adaptive_sizing(_input(step_size="abc"))
    assert decision.reason_vector == ["VENUE_RULE_INPUT_INVALID"]


def test_non_positive_venue_rule_is_rejected() -> None:
    decision = compute_adaptive_sizing(_input(step_size="0"))
    assert decision.reason_vector == ["VENUE_RULE_INPUT_INVALID"]


def test_min_notional_not_satisfied_is_rejected() -> None:
    decision = compute_adaptive_sizing(_input(symbol_price=1.0, min_notional="1000", max_notional=1_000.0))
    assert "MIN_NOTIONAL_NOT_SATISFIED" in decision.reason_vector
    assert "MIN_QTY_NOT_SATISFIED" not in decision.reason_vector
    assert decision.final_quantity == "0"
    assert decision.is_safe is False
