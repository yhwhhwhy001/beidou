"""组合优化、策略资本归属、冲突仲裁、自适应仓位与杠杆。"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, cast

from beidou_shared.types import MonetaryValue, Quantity, StrategyId
from beidou_strategy.alpha.contracts import EnsembleForecast
from beidou_strategy.portfolio import PortfolioTarget, PositionOwnership
from beidou_strategy.portfolio.contracts import PortfolioOptimizationInput


@dataclass
class OptimizationResult:
    targets: list[PortfolioTarget]
    capital_allocated: dict[StrategyId, MonetaryValue] = field(default_factory=dict)
    conflicts_resolved: int = 0
    max_leverage: float = 1.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def signed_exposure_math(exposures: Mapping[str, float]) -> tuple[float, float]:
    """Return gross and signed net exposure without collapsing the two."""
    values = [float(value) for value in exposures.values()]
    if any(not math.isfinite(value) for value in values):
        raise ValueError("exposures must be finite")
    return sum(abs(value) for value in values), sum(values)


@dataclass(frozen=True, slots=True)
class ActivePortfolioPolicy:
    policy_version: str = "portfolio-optimizer-v3-policy-v1"
    lambda_risk: float = 1.0
    lambda_turnover: float = 0.1
    lambda_cost: float = 1.0
    lambda_beta: float = 1.0
    max_symbol_weight: float = 0.80
    max_turnover: float = 1.0
    numerical_epsilon: float = 1e-12

    def __post_init__(self) -> None:
        values = (
            self.lambda_risk,
            self.lambda_turnover,
            self.lambda_cost,
            self.lambda_beta,
            self.max_symbol_weight,
            self.max_turnover,
            self.numerical_epsilon,
        )
        if not self.policy_version.strip() or any(not math.isfinite(value) or value < 0 for value in values):
            raise ValueError("invalid active portfolio policy")
        if self.numerical_epsilon == 0 or self.max_symbol_weight == 0:
            raise ValueError("active portfolio policy bounds must be positive")


@dataclass(frozen=True, slots=True)
class ActiveOptimizationResult:
    weights: dict[str, float]
    rejected: dict[str, str]
    gross_exposure: float
    net_exposure: float
    portfolio_beta: float
    objective: float | None
    target_hash: str
    output_hash: str
    constraint_violations: tuple[str, ...] = ()

    @property
    def is_tradable(self) -> bool:
        return bool(self.weights) and not self.constraint_violations and not self.rejected


class PortfolioOptimizerImpl:
    """组合优化器实现。策略资本归属、冲突仲裁。"""

    def __init__(self, max_total_leverage: float = 3.0) -> None:
        self.max_total_leverage = max_total_leverage

    def _enforce_leverage_constraint(self, targets: list[PortfolioTarget]) -> list[PortfolioTarget]:
        """P1-008: 强制杠杆约束 — 总杠杆超限时等比缩放。"""
        total_notional = sum(float(t.target_notional.amount) for t in targets if t.target_notional is not None)
        total_capital = sum(float(t.capital_budget.amount) for t in targets if t.capital_budget is not None)
        if total_capital <= 0:
            return targets
        current_leverage = total_notional / total_capital
        if current_leverage <= self.max_total_leverage:
            return targets
        # 等比缩放
        scale = self.max_total_leverage / current_leverage
        return [
            PortfolioTarget(
                strategy_id=t.strategy_id,
                instrument_id=t.instrument_id,
                venue_id=t.venue_id,
                target_notional=MonetaryValue(amount=str(float(t.target_notional.amount) * scale)),
                target_quantity=Quantity(amount=str(float(t.target_quantity.amount) * scale)),
                capital_budget=t.capital_budget,
                ownership=t.ownership,
            )
            for t in targets
        ]

    def allocate_capital(
        self, strategies: list[StrategyId], total_capital: MonetaryValue, weights: dict[StrategyId, float] | None = None
    ) -> dict[StrategyId, MonetaryValue]:
        if weights is None:
            w = 1.0 / len(strategies) if strategies else 0
            weights = dict.fromkeys(strategies, w)
        return {s: MonetaryValue(amount=str(float(total_capital.amount) * weights.get(s, 0))) for s in strategies}

    def resolve_conflicts(self, targets: list[PortfolioTarget]) -> tuple[list[PortfolioTarget], int]:
        """仲裁策略冲突 — PKG18 (BDS-P0-007)。

        修复前: total_qty / len(group) 可能改变策略方向（多空抵消→归零→方向丢失）。
        修复后: 保持每个策略的原始方向、数量和归属，仅标记冲突和共享所有权。
        组合层输出 target，不重写策略原始 proposal。

        冲突仲裁规则:
        1. 同方向: 保留每个策略已完成资本预算后的目标，不二次缩放
        2. 反方向: 不净额抵消 — 各自保留原始目标，标记 SHARED
        3. 始终保留 owner/generation/attribution
        """
        resolved: list[PortfolioTarget] = []
        conflicts = 0
        seen: dict[str, list[PortfolioTarget]] = {}
        for t in targets:
            key = f"{t.venue_id}:{t.instrument_id}"
            if key not in seen:
                seen[key] = []
            seen[key].append(t)

        for key, group in seen.items():
            if len(group) == 1:
                resolved.extend(group)
                continue
            conflicts += 1

            # PKG18: 按方向分组，不净额抵消
            long_targets = [t for t in group if float(t.target_quantity.amount) > 0]
            short_targets = [t for t in group if float(t.target_quantity.amount) < 0]

            # 资本预算已经体现在各策略 target 中；组合层只标记共享
            # ownership，不得在此处二次缩放或净额化策略原意。
            for direction_group in (long_targets, short_targets):
                if not direction_group:
                    continue

                for t in direction_group:
                    resolved.append(
                        PortfolioTarget(
                            strategy_id=t.strategy_id,
                            instrument_id=t.instrument_id,
                            venue_id=t.venue_id,
                            target_quantity=t.target_quantity,  # 保留原始数量和方向
                            target_notional=t.target_notional,
                            capital_budget=t.capital_budget,
                            ownership=PositionOwnership.SHARED,
                        )
                    )

        # P1-008: 强制杠杆硬约束
        resolved = self._enforce_leverage_constraint(resolved)
        return resolved, conflicts

    def exit_protection(
        self, exiting_strategy: StrategyId, all_targets: list[PortfolioTarget]
    ) -> list[PortfolioTarget]:
        """一个策略退出时，不得错误平掉其他策略仍需要的仓位。"""
        remaining = []
        for t in all_targets:
            if t.strategy_id == exiting_strategy:
                if t.ownership == PositionOwnership.SHARED:
                    # Shared position: transfer ownership instead of closing
                    if t.takeover_strategy:
                        remaining.append(
                            PortfolioTarget(
                                strategy_id=t.takeover_strategy,
                                instrument_id=t.instrument_id,
                                venue_id=t.venue_id,
                                target_quantity=t.target_quantity,
                                target_notional=t.target_notional,
                                capital_budget=t.capital_budget,
                                ownership=PositionOwnership.DELEGATED,
                            )
                        )
                continue
            remaining.append(t)
        return remaining


class ActivePortfolioOptimizer:
    """V3 optimizer for notional-fraction targets.

    This is intentionally separate from ``PortfolioOptimizerImpl``'s legacy
    strategy-ownership resolver.  It never derives a target from ``strength``
    or a fixed account fraction; the target comes from ``ExposureTarget`` and
    the expected-return/covariance objective.
    """

    def __init__(self, policy: ActivePortfolioPolicy | None = None) -> None:
        self.policy = policy or ActivePortfolioPolicy()

    def optimize(self, inputs: PortfolioOptimizationInput) -> ActiveOptimizationResult:
        accepted: dict[str, EnsembleForecast] = {}
        rejected: dict[str, str] = {}
        for symbol in sorted(inputs.forecasts):
            candidate = inputs.forecasts[symbol]
            if not isinstance(candidate, EnsembleForecast):
                rejected[symbol] = "FORECAST_CONTRACT_UNKNOWN"
                continue
            if candidate.expected_return_after_cost is None or not math.isfinite(candidate.expected_return_after_cost):
                rejected[symbol] = "FORECAST_NOT_VERIFIABLE"
                continue
            required_rules = (
                inputs.min_notional,
                inputs.step_sizes,
                inputs.tick_sizes,
            )
            if any(symbol not in rules for rules in required_rules):
                rejected[symbol] = "EXCHANGE_RULES_UNKNOWN"
                continue
            try:
                min_notional = float(inputs.min_notional[symbol])
                step_size = float(inputs.step_sizes[symbol])
                tick_size = float(inputs.tick_sizes[symbol])
            except (TypeError, ValueError, OverflowError):
                rejected[symbol] = "EXCHANGE_RULES_UNKNOWN"
                continue
            if (
                min_notional <= 0
                or step_size <= 0
                or tick_size <= 0
                or not all(math.isfinite(value) for value in (min_notional, step_size, tick_size))
            ):
                rejected[symbol] = "EXCHANGE_RULES_UNKNOWN"
                continue
            if (
                symbol not in inputs.asset_betas
                or symbol not in inputs.liquidity_limits
                or symbol not in inputs.capacity_limits
            ):
                rejected[symbol] = "RISK_OR_CAPACITY_UNKNOWN"
                continue
            if any(symbol not in costs for costs in (inputs.fee_costs, inputs.slippage_costs, inputs.funding_costs)):
                rejected[symbol] = "COST_MODEL_UNKNOWN"
                continue
            beta = _finite(inputs.asset_betas[symbol])
            liquidity = _finite(inputs.liquidity_limits[symbol])
            capacity = _finite(inputs.capacity_limits[symbol])
            fee = _finite(inputs.fee_costs[symbol])
            slippage = _finite(inputs.slippage_costs[symbol])
            funding = _finite(inputs.funding_costs[symbol])
            if fee is None or slippage is None or funding is None or fee < 0 or slippage < 0 or funding < 0:
                rejected[symbol] = "COST_MODEL_UNKNOWN"
                continue
            if beta is None or liquidity is None or capacity is None or liquidity <= 0 or capacity <= 0:
                rejected[symbol] = "RISK_OR_CAPACITY_UNKNOWN"
                continue
            accepted[symbol] = candidate

        if not accepted:
            return self._result({}, rejected, inputs, objective=None, violations=())

        raw_scores: dict[str, float] = {}
        for symbol, forecast in accepted.items():
            volatility = forecast.expected_volatility
            if volatility is None or volatility <= 0.0:
                volatility = inputs.exposure_target.target_volatility
            quality = max(0.0, min(1.0, 1.0 - forecast.uncertainty))
            after_cost = forecast.expected_return_after_cost
            if after_cost is None:
                raise ValueError("accepted forecast unexpectedly lost after-cost return")
            score = after_cost / max(volatility, self.policy.numerical_epsilon)
            raw_scores[symbol] = score * quality
        total_score = sum(abs(score) for score in raw_scores.values())
        if total_score <= self.policy.numerical_epsilon:
            return self._result(
                {}, {**rejected, **dict.fromkeys(accepted, "NO_VERIFIABLE_EDGE")}, inputs, objective=None, violations=()
            )

        target = inputs.exposure_target
        weights = {symbol: score / total_score * target.target_gross for symbol, score in raw_scores.items()}
        weights = self._project(weights, inputs)
        gross, net = signed_exposure_math(weights)
        portfolio_beta = sum(weights[symbol] * float(inputs.asset_betas[symbol]) for symbol in weights)
        turnover = sum(
            abs(weights.get(symbol, 0.0) - float(inputs.current_weights.get(symbol, 0.0))) for symbol in weights
        )
        violations: list[str] = []
        if (
            gross < target.gross_min - self.policy.numerical_epsilon
            or gross > target.gross_max + self.policy.numerical_epsilon
        ):
            violations.append("GROSS_TARGET_OUT_OF_BOUNDS")
        if net < target.net_min - self.policy.numerical_epsilon or net > target.net_max + self.policy.numerical_epsilon:
            violations.append("NET_TARGET_OUT_OF_BOUNDS")
        if (
            portfolio_beta < target.beta_min - self.policy.numerical_epsilon
            or portfolio_beta > target.beta_max + self.policy.numerical_epsilon
        ):
            violations.append("BETA_TARGET_OUT_OF_BOUNDS")
        if turnover > self.policy.max_turnover + self.policy.numerical_epsilon:
            violations.append("TURNOVER_LIMIT")
        objective = self._objective(weights, inputs, portfolio_beta, turnover)
        return self._result(weights, rejected, inputs, objective=objective, violations=tuple(violations))

    def _project(self, weights: dict[str, float], inputs: PortfolioOptimizationInput) -> dict[str, float]:
        target = inputs.exposure_target
        bounded = {
            symbol: max(
                -min(
                    self.policy.max_symbol_weight,
                    float(inputs.liquidity_limits[symbol]),
                    float(inputs.capacity_limits[symbol]),
                ),
                min(
                    self.policy.max_symbol_weight,
                    float(inputs.liquidity_limits[symbol]),
                    float(inputs.capacity_limits[symbol]),
                    value,
                ),
            )
            for symbol, value in weights.items()
        }
        gross, _net = signed_exposure_math(bounded)
        if gross > target.gross_max:
            scale = target.gross_max / gross
            bounded = {symbol: value * scale for symbol, value in bounded.items()}
        if bounded:
            net_error = target.target_net - signed_exposure_math(bounded)[1]
            per_symbol = net_error / len(bounded)
            bounded = {symbol: value + per_symbol for symbol, value in bounded.items()}
        beta = sum(value * float(inputs.asset_betas[symbol]) for symbol, value in bounded.items())
        beta_error = target.target_beta - beta
        beta_denominator = sum(float(inputs.asset_betas[symbol]) ** 2 for symbol in bounded)
        if beta_denominator > self.policy.numerical_epsilon:
            bounded = {
                symbol: value + beta_error * float(inputs.asset_betas[symbol]) / beta_denominator
                for symbol, value in bounded.items()
            }
        bounded = {
            symbol: max(
                -min(
                    self.policy.max_symbol_weight,
                    float(inputs.liquidity_limits[symbol]),
                    float(inputs.capacity_limits[symbol]),
                ),
                min(
                    self.policy.max_symbol_weight,
                    float(inputs.liquidity_limits[symbol]),
                    float(inputs.capacity_limits[symbol]),
                    value,
                ),
            )
            for symbol, value in bounded.items()
        }
        gross, _net = signed_exposure_math(bounded)
        if gross > target.gross_max:
            scale = target.gross_max / gross
            bounded = {symbol: value * scale for symbol, value in bounded.items()}
        return {symbol: value for symbol, value in bounded.items() if abs(value) > self.policy.numerical_epsilon}

    def _objective(
        self,
        weights: Mapping[str, float],
        inputs: PortfolioOptimizationInput,
        portfolio_beta: float,
        turnover: float,
    ) -> float:
        expected = sum(
            weight * self._required_after_cost(inputs.forecasts[symbol]) for symbol, weight in weights.items()
        )
        risk = 0.0
        for left, left_weight in weights.items():
            for right, right_weight in weights.items():
                risk += left_weight * right_weight * float(inputs.covariance.get(left, {}).get(right, 0.0))
        cost = sum(
            abs(weight)
            * (inputs.fee_costs[symbol] + inputs.slippage_costs[symbol] + inputs.funding_costs[symbol])
            / 10000.0
            for symbol, weight in weights.items()
        )
        beta_error = portfolio_beta - inputs.exposure_target.target_beta
        return (
            expected
            - self.policy.lambda_risk * risk
            - self.policy.lambda_turnover * turnover
            - self.policy.lambda_cost * cost
            - self.policy.lambda_beta * beta_error * beta_error
        )

    @staticmethod
    def _required_after_cost(forecast: EnsembleForecast) -> float:
        value = forecast.expected_return_after_cost
        if value is None or not math.isfinite(value):
            raise ValueError("forecast after-cost return is unknown")
        return value

    def _result(
        self,
        weights: dict[str, float],
        rejected: dict[str, str],
        inputs: PortfolioOptimizationInput,
        *,
        objective: float | None,
        violations: tuple[str, ...],
    ) -> ActiveOptimizationResult:
        gross, net = signed_exposure_math(weights)
        portfolio_beta = sum(weights[symbol] * float(inputs.asset_betas[symbol]) for symbol in weights)
        target_payload = {
            "policy_version": inputs.policy_version,
            "exposure_policy_version": inputs.exposure_target.policy_version,
            "target_beta": inputs.exposure_target.target_beta,
            "target_gross": inputs.exposure_target.target_gross,
            "target_net": inputs.exposure_target.target_net,
        }
        target_hash = hashlib.sha256(
            json.dumps(target_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:16]
        output_payload = {
            "weights": weights,
            "gross": gross,
            "net": net,
            "beta": portfolio_beta,
            "target_hash": target_hash,
        }
        output_hash = hashlib.sha256(
            json.dumps(output_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:16]
        return ActiveOptimizationResult(
            weights=weights,
            rejected=rejected,
            gross_exposure=gross,
            net_exposure=net,
            portfolio_beta=portfolio_beta,
            objective=objective,
            target_hash=target_hash,
            output_hash=output_hash,
            constraint_violations=violations,
        )


def _finite(value: object) -> float | None:
    try:
        parsed = float(cast(Any, value))
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None
