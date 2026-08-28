"""Canonical adaptive sizing authority for the Testnet verification path.

The legacy strategy-risk fields remain source-compatible, but every
executable Testnet quantity is produced by :func:`compute_adaptive_sizing`.
The function is deterministic, exchange-rule aware, and fail-closed when a
fact needed to form a venue order is missing.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any


def _hash_payload(payload: object) -> str:
    def normalize(value: object) -> object:
        if isinstance(value, float) and not math.isfinite(value):
            return str(value)
        if isinstance(value, dict):
            return {str(key): normalize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [normalize(item) for item in value]
        return value

    encoded = json.dumps(normalize(payload), sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _decimal(value: Any, *, default: Decimal | None = None) -> Decimal | None:
    if value in (None, ""):
        return default
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


@dataclass(frozen=True)
class SizingInput:
    """All facts consumed by the single executable sizing authority.

    The first eight fields are retained for compatibility with the existing
    strategy risk helper.  The remaining fields are required for an
    executable venue quantity; leaving them at their empty/zero defaults
    intentionally produces no executable quantity.
    """

    equity: float
    available_margin: float
    portfolio_risk_pct: float = 0.02
    volatility: float = 0.02  # annualized
    capacity_utilization: float = 0.5
    regime_confidence: float = 0.5
    funding_rate: float = 0.0
    liquidation_distance_pct: float = 0.10
    symbol_price: float = 0.0
    step_size: str = ""
    min_qty: str = ""
    min_notional: str = ""
    max_notional: float = 0.0
    max_leverage: float = 0.0
    signal_confidence: float = 0.5
    liquidity_score: float = 0.0
    drawdown_pct: float = 0.0
    stop_distance_pct: float = 0.0

    @property
    def input_hash(self) -> str:
        return _hash_payload(asdict(self))


@dataclass
class SizingDecision:
    """Deterministic sizing output, including the exact venue quantity."""

    leverage: float = 0.0
    position_size_pct: float = 0.0
    raw_quantity: str = ""
    final_quantity: str = ""
    notional: str = ""
    reason_vector: list[str] = field(default_factory=list)
    policy_hash: str = ""
    input_hash: str = ""
    output_hash: str = ""
    is_safe: bool = True

    def compute_output_hash(self) -> str:
        return _hash_payload(
            {
                "leverage": self.leverage,
                "position_size_pct": self.position_size_pct,
                "raw_quantity": self.raw_quantity,
                "final_quantity": self.final_quantity,
                "notional": self.notional,
                "reason_vector": self.reason_vector,
                "policy_hash": self.policy_hash,
                "input_hash": self.input_hash,
                "is_safe": self.is_safe,
            }
        )


# Explicit names used by the V4 package.  The compatibility aliases make the
# authority easy to find without creating a second implementation.
AdaptiveSizingInput = SizingInput
AdaptiveSizingDecision = SizingDecision


def _invalid_decision(inputs: SizingInput, reason: str) -> SizingDecision:
    decision = SizingDecision(
        reason_vector=[reason],
        input_hash=inputs.input_hash,
        is_safe=False,
    )
    decision.output_hash = decision.compute_output_hash()
    return decision


def _quantize_down(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def compute_adaptive_sizing(inputs: SizingInput, previous: SizingDecision | None = None) -> SizingDecision:
    """Compute one deterministic, cap-bounded sizing decision.

    When venue fields are supplied, ``final_quantity`` is the only quantity
    that may enter an ``OrderRequest``.  A quantity below min-notional or
    min-quantity is rejected rather than enlarged, and all risk-worsening
    inputs are monotone non-increasing.
    """

    reasons: list[str] = []
    is_safe = True
    numeric_inputs = (
        inputs.equity,
        inputs.available_margin,
        inputs.portfolio_risk_pct,
        inputs.volatility,
        inputs.capacity_utilization,
        inputs.regime_confidence,
        inputs.funding_rate,
        inputs.liquidation_distance_pct,
        inputs.symbol_price,
        inputs.max_notional,
        inputs.max_leverage,
        inputs.signal_confidence,
        inputs.liquidity_score,
        inputs.drawdown_pct,
        inputs.stop_distance_pct,
    )
    if not all(math.isfinite(float(value)) for value in numeric_inputs):
        return _invalid_decision(inputs, "INVALID_OR_UNKNOWN_INPUT")
    if inputs.equity <= 0 or inputs.available_margin < 0:
        return _invalid_decision(inputs, "INVALID_OR_UNKNOWN_INPUT")
    if inputs.portfolio_risk_pct <= 0:
        return _invalid_decision(inputs, "RISK_BUDGET_ZERO")
    if not 0.0 <= inputs.capacity_utilization <= 1.0:
        return _invalid_decision(inputs, "CAPACITY_INPUT_OUT_OF_RANGE")
    if not 0.0 <= inputs.regime_confidence <= 1.0 or not 0.0 <= inputs.signal_confidence <= 1.0:
        return _invalid_decision(inputs, "CONFIDENCE_INPUT_OUT_OF_RANGE")
    if not 0.0 <= inputs.liquidity_score <= 1.0:
        return _invalid_decision(inputs, "LIQUIDITY_INPUT_OUT_OF_RANGE")
    if inputs.drawdown_pct < 0 or inputs.drawdown_pct >= 1.0:
        return _invalid_decision(inputs, "DRAWDOWN_INPUT_OUT_OF_RANGE")

    venue_fields = (
        inputs.symbol_price,
        inputs.step_size,
        inputs.min_qty,
        inputs.min_notional,
        inputs.max_notional,
        inputs.max_leverage,
    )
    executable_fields_present = all(bool(value) for value in venue_fields)
    if any(bool(value) for value in venue_fields) and not executable_fields_present:
        return _invalid_decision(inputs, "VENUE_RULE_INPUT_INVALID")

    # Base leverage is the configured absolute ceiling scaled by the fraction
    # of equity currently available as margin.  A fully available account
    # therefore starts at the configured ceiling, while a constrained account
    # loses leverage before the monotone risk reductions below are applied.
    # The legacy, non-executable helper has no venue cap and retains its
    # original ratio-based behavior.
    available_ratio = inputs.available_margin / max(inputs.equity, 1.0)
    base_leverage = available_ratio * (inputs.max_leverage if executable_fields_present else 1.0)

    # Higher volatility, lower liquidity/capacity, lower confidence, funding
    # drag, drawdown, and a shorter liquidation distance can only reduce size.
    vol_scalar = max(0.1, min(1.0, 0.20 / max(inputs.volatility, 0.001)))
    if vol_scalar < 0.5:
        reasons.append(f"HIGH_VOL:volatility={inputs.volatility:.1%}")
        is_safe = False

    cap_scalar = max(0.1, min(1.0, inputs.capacity_utilization))
    if cap_scalar < 0.3:
        reasons.append(f"CAPACITY_LIMITED:{inputs.capacity_utilization:.1%}")
        is_safe = False

    liquidity_scalar = (
        (max(0.1, min(1.0, inputs.liquidity_score)) if inputs.liquidity_score else 0.1)
        if executable_fields_present
        else 1.0
    )
    if executable_fields_present and liquidity_scalar < 0.5:
        reasons.append(f"LOW_LIQUIDITY:{inputs.liquidity_score:.1%}")
        is_safe = False

    regime_scalar = max(0.25, min(1.0, inputs.regime_confidence))
    signal_scalar = max(0.0, min(1.0, inputs.signal_confidence)) if executable_fields_present else 1.0
    if regime_scalar < 0.5 or (executable_fields_present and signal_scalar < 0.5):
        reasons.append("LOW_CONFIDENCE")
    funding_penalty = 1.0
    if abs(inputs.funding_rate) > 0.001:
        funding_penalty = max(0.25, 1.0 - abs(inputs.funding_rate) * 10.0)
        reasons.append(f"FUNDING_PENALTY:{inputs.funding_rate:.4f}")
        is_safe = False

    liq_scalar = 1.0
    if inputs.liquidation_distance_pct < 0.20:
        liq_scalar = max(0.25, inputs.liquidation_distance_pct / 0.20)
        reasons.append(f"CLOSE_TO_LIQUIDATION:{inputs.liquidation_distance_pct:.1%}")
        is_safe = False

    drawdown_scalar = max(0.25, 1.0 - inputs.drawdown_pct)
    if inputs.drawdown_pct > 0:
        reasons.append(f"DRAWDOWN_REDUCTION:{inputs.drawdown_pct:.1%}")
        is_safe = False

    stop_scalar = 1.0
    if inputs.stop_distance_pct > 0:
        stop_scalar = max(0.25, min(1.0, 0.10 / inputs.stop_distance_pct))
        if stop_scalar < 1.0:
            reasons.append(f"STOP_RISK_REDUCTION:{inputs.stop_distance_pct:.1%}")
            is_safe = False

    adjusted_leverage = (
        base_leverage
        * vol_scalar
        * cap_scalar
        * liquidity_scalar
        * regime_scalar
        * signal_scalar
        * funding_penalty
        * liq_scalar
        * drawdown_scalar
        * stop_scalar
    )

    if previous is not None and previous.leverage > 0 and not is_safe and adjusted_leverage > previous.leverage:
        adjusted_leverage = previous.leverage
        reasons.append("MONOTONIC_CLAMP: risk worsening, sizing frozen at previous level")

    # The old helper had no venue facts.  Preserve its fractional result for
    # callers that only use the strategy-risk estimate.  Executable sizing
    # below uses an integer leverage because Binance's endpoint requires it.
    if executable_fields_present:
        max_leverage = float(inputs.max_leverage)
        leverage = float(min(max_leverage, max(0.0, math.floor(adjusted_leverage))))
    else:
        leverage = round(max(0.0, adjusted_leverage), 2)

    position_size_pct = min(max(0.0, adjusted_leverage * inputs.portfolio_risk_pct), 0.95)
    if executable_fields_present and leverage < 1.0:
        reasons.append("LEVERAGE_BELOW_VENUE_MINIMUM")
        decision = SizingDecision(
            leverage=0.0,
            position_size_pct=round(position_size_pct, 4),
            reason_vector=reasons,
            input_hash=inputs.input_hash,
            is_safe=False,
        )
        decision.output_hash = decision.compute_output_hash()
        return decision

    if not executable_fields_present:
        decision = SizingDecision(
            leverage=leverage,
            position_size_pct=round(position_size_pct, 4),
            reason_vector=reasons,
            input_hash=inputs.input_hash,
            is_safe=is_safe,
        )
        decision.output_hash = decision.compute_output_hash()
        return decision

    price = _decimal(inputs.symbol_price)
    step = _decimal(inputs.step_size)
    min_qty = _decimal(inputs.min_qty, default=Decimal("0"))
    min_notional = _decimal(inputs.min_notional, default=Decimal("0"))
    max_notional = _decimal(inputs.max_notional)
    if any(value is None for value in (price, step, min_qty, min_notional, max_notional)):
        return _invalid_decision(inputs, "VENUE_RULE_INPUT_INVALID")
    assert price is not None and step is not None and min_qty is not None and min_notional is not None
    assert max_notional is not None
    if price <= 0 or step <= 0 or min_qty <= 0 or min_notional <= 0 or max_notional <= 0:
        return _invalid_decision(inputs, "VENUE_RULE_INPUT_INVALID")

    target_notional = min(
        Decimal(str(inputs.equity)) * Decimal(str(position_size_pct)),
        Decimal(str(inputs.available_margin)) * Decimal(str(leverage)),
        max_notional,
    )
    raw_quantity_decimal = target_notional / price
    final_quantity_decimal = _quantize_down(raw_quantity_decimal, step)
    if final_quantity_decimal <= 0:
        reasons.append("QUANTITY_ROUNDS_TO_ZERO")
        is_safe = False
        final_quantity_decimal = Decimal("0")
    final_notional = final_quantity_decimal * price
    if final_quantity_decimal < min_qty:
        reasons.append("MIN_QTY_NOT_SATISFIED")
        is_safe = False
        final_quantity_decimal = Decimal("0")
        final_notional = Decimal("0")
    elif final_notional < min_notional:
        # Never increase an approved quantity merely to satisfy a venue
        # minimum; doing so could cross the local absolute cap.
        reasons.append("MIN_NOTIONAL_NOT_SATISFIED")
        is_safe = False
        final_quantity_decimal = Decimal("0")
        final_notional = Decimal("0")

    def fmt(value: Decimal) -> str:
        return format(value, "f").rstrip("0").rstrip(".") or "0"

    decision = SizingDecision(
        leverage=leverage,
        position_size_pct=round(position_size_pct, 4),
        raw_quantity=fmt(raw_quantity_decimal),
        final_quantity=fmt(final_quantity_decimal),
        notional=fmt(final_notional),
        reason_vector=reasons,
        input_hash=inputs.input_hash,
        is_safe=is_safe,
    )
    decision.output_hash = decision.compute_output_hash()
    return decision


__all__ = [
    "AdaptiveSizingDecision",
    "AdaptiveSizingInput",
    "SizingDecision",
    "SizingInput",
    "compute_adaptive_sizing",
]
