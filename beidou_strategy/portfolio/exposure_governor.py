"""Market-state-aware exposure governor for Alpha V3."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from beidou_strategy.alpha.contracts import EnsembleForecast

from .contracts import ExposureTarget


def _finite(value: object) -> float | None:
    try:
        parsed = float(cast(Any, value))
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _clip(value: float, lower: float, upper: float) -> float:
    return min(upper, max(lower, value))


@dataclass(frozen=True, slots=True)
class ExposurePolicy:
    """Versioned exposure policy; values are inspectable policy parameters."""

    policy_version: str = "exposure-policy-v3-v1"
    beta_min: float = -0.75
    beta_max: float = 0.75
    gross_min: float = 0.0
    gross_max: float = 1.0
    net_min: float = -1.0
    net_max: float = 1.0
    base_beta: float = 0.35
    base_gross: float = 0.65
    base_net: float = 0.35
    target_volatility: float = 0.20
    edge_scale: float = 0.005

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise ValueError("exposure policy version is required")
        if self.beta_min > self.beta_max or self.gross_min > self.gross_max or self.net_min > self.net_max:
            raise ValueError("exposure policy bounds are invalid")
        if self.gross_min < 0 or self.base_gross < 0 or self.base_net < 0 or self.edge_scale <= 0:
            raise ValueError("exposure policy scales are invalid")
        if self.target_volatility < 0 or not all(
            math.isfinite(value)
            for value in (
                self.beta_min,
                self.beta_max,
                self.gross_min,
                self.gross_max,
                self.net_min,
                self.net_max,
                self.base_beta,
                self.base_gross,
                self.base_net,
                self.target_volatility,
                self.edge_scale,
            )
        ):
            raise ValueError("exposure policy values must be finite")


class ExposureGovernor:
    """Convert state, forecast and account facts into a bounded target."""

    def __init__(self, policy: ExposurePolicy | None = None) -> None:
        self.policy = policy or ExposurePolicy()

    @staticmethod
    def _value(source: object, name: str, default: object = None) -> object:
        if isinstance(source, dict):
            return source.get(name, default)
        return getattr(source, name, default)

    def compute(
        self,
        market_state: object,
        ensemble: EnsembleForecast,
        *,
        risk_budget: dict[str, float] | None = None,
        account_facts: dict[str, float] | None = None,
        timestamp: datetime | None = None,
    ) -> ExposureTarget:
        policy = self.policy
        now = timestamp or ensemble.timestamp
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        reasons: list[str] = []
        account = account_facts or {}
        equity = _finite(account.get("equity"))
        if equity is None or equity <= 0:
            return self._zero_target(now, ("ACCOUNT_EQUITY_UNKNOWN",))
        edge = ensemble.expected_return_after_cost
        gross_edge = ensemble.expected_return
        if (
            edge is None
            or gross_edge is None
            or not math.isfinite(edge)
            or not math.isfinite(gross_edge)
            or abs(edge) <= 1e-12
            or gross_edge * edge <= 0
        ):
            reason = "FORECAST_NOT_VERIFIABLE" if edge is None or gross_edge is None else "NO_POSITIVE_EDGE_AFTER_COST"
            return self._zero_target(now, (reason,))

        state_tradable = getattr(market_state, "is_tradable", None)
        if callable(state_tradable) and not state_tradable():
            return self._zero_target(now, ("STATE_NOT_TRADABLE",))
        direction = self._value(market_state, "direction", {})
        stress = self._value(market_state, "stress", {})
        quality = self._value(market_state, "quality", {})
        trend_probability = _finite(self._value(direction, "trend_probability", 0.0)) or 0.0
        stress_score = _finite(self._value(stress, "stress_score", 0.0)) or 0.0
        stress_level = str(self._value(stress, "level", "UNKNOWN")).upper()
        quality_tier = str(self._value(quality, "tier", "UNKNOWN")).upper()
        quality_scalar = {"GOOD": 1.0, "PASS": 1.0, "DEGRADED": 0.5}.get(quality_tier, 0.0)  # nosec B105 - quality score labels
        stress_scalar = 0.0 if stress_level in {"CRISIS", "UNKNOWN"} else _clip(1.0 - stress_score, 0.0, 1.0)
        if quality_scalar == 0.0:
            return self._zero_target(now, ("QUALITY_NOT_VERIFIABLE",))
        if stress_scalar < 1.0:
            reasons.append("STRESS_SCALAR_APPLIED")
        if quality_scalar < 1.0:
            reasons.append("QUALITY_SCALAR_APPLIED")

        risk_scale = _finite((risk_budget or {}).get("risk_scale", 1.0))
        gross_cap = _finite((risk_budget or {}).get("gross_max", policy.gross_max))
        if risk_scale is None or gross_cap is None or risk_scale < 0 or gross_cap < 0:
            return self._zero_target(now, ("RISK_BUDGET_UNKNOWN",))
        risk_scale = _clip(risk_scale, 0.0, 1.0)
        gross_cap = _clip(gross_cap, policy.gross_min, policy.gross_max)
        opportunity = _clip(abs(edge) / policy.edge_scale, 0.0, 1.0)
        forecast_confidence = _clip(1.0 - ensemble.uncertainty, 0.0, 1.0)
        target_gross = _clip(
            policy.base_gross * opportunity * quality_scalar * stress_scalar * risk_scale,
            policy.gross_min,
            gross_cap,
        )
        sign = 1.0 if gross_edge > 0 else -1.0
        target_net = sign * min(
            target_gross,
            policy.base_net
            * max(trend_probability, 0.0)
            * forecast_confidence
            * quality_scalar
            * stress_scalar
            * risk_scale,
        )
        beta_magnitude = (
            policy.base_beta * trend_probability * forecast_confidence * quality_scalar * stress_scalar * risk_scale
        )
        target_beta = _clip(sign * beta_magnitude, policy.beta_min, policy.beta_max)
        if target_gross == 0.0:
            reasons.append("NO_VERIFIABLE_OPPORTUNITY")
        else:
            reasons.append("FORECAST_EDGE_AFTER_COST")
        return ExposureTarget(
            target_beta=target_beta,
            beta_min=policy.beta_min,
            beta_max=policy.beta_max,
            target_gross=target_gross,
            gross_min=policy.gross_min,
            gross_max=gross_cap,
            target_net=target_net,
            net_min=policy.net_min,
            net_max=policy.net_max,
            target_volatility=policy.target_volatility,
            confidence=forecast_confidence * quality_scalar,
            reason_codes=tuple(reasons),
            policy_version=policy.policy_version,
            timestamp=now,
        )

    def _zero_target(self, timestamp: datetime, reasons: tuple[str, ...]) -> ExposureTarget:
        return ExposureTarget(
            target_beta=0.0,
            beta_min=self.policy.beta_min,
            beta_max=self.policy.beta_max,
            target_gross=self.policy.gross_min,
            gross_min=self.policy.gross_min,
            gross_max=self.policy.gross_max,
            target_net=0.0,
            net_min=self.policy.net_min,
            net_max=self.policy.net_max,
            target_volatility=self.policy.target_volatility,
            confidence=0.0,
            reason_codes=reasons,
            policy_version=self.policy.policy_version,
            timestamp=timestamp,
        )
