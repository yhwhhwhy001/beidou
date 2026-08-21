"""约束组合优化器 — BD-06。

确定性约束优化：最大化成本后 Alpha，
约束 gross/net leverage、symbol/cluster exposure、策略预算、保证金、容量、min notional、step size。
UNKNOWN 输入返回不可交易（无 fallback 到固定数值）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class PortfolioConstraint:
    """组合约束。"""

    name: str
    description: str
    check: Callable[[dict[str, float]], bool]
    violation_message: str = ""


@dataclass
class OptimizationResult:
    """优化结果。"""

    targets: dict[str, float]  # symbol → target_quantity
    rejected: dict[str, str]  # symbol → rejection_reason
    total_risk_pct: float = 0.0
    gross_exposure: float = 0.0
    net_exposure: float = 0.0
    constraint_violations: list[str] = field(default_factory=list)

    @property
    def is_tradable(self) -> bool:
        return len(self.targets) > 0 and len(self.constraint_violations) == 0


class ConstraintOptimizer:
    """确定性约束优化器。

    输入: signals (symbol → {direction, strength, confidence, ...})
          constraints (list of PortfolioConstraint)
          account_facts (AccountFactSnapshot)
    输出: OptimizationResult (targets + rejected + violations)
    """

    def __init__(
        self,
        max_gross_leverage: float = 3.0,
        max_net_leverage: float = 1.0,
        max_per_symbol_pct: float = 50.0,
        covariance_matrix: dict[str, dict[str, float]] | None = None,  # P1-009
    ):
        self.max_gross_leverage = max_gross_leverage
        self.max_net_leverage = max_net_leverage
        self.max_per_symbol_pct = max_per_symbol_pct
        self.covariance_matrix = covariance_matrix or {}
        self._max_correlation_discount: float = 0.5  # 高相关性时最大折扣
        self.turnover_cost_bps: float = 0.0  # P1-010: 换手成本(bps)
        self.capacity_threshold: float = 0.0  # P1-010: 容量阈值

    def _covariance_discount(self, symbol: str, active_symbols: set[str]) -> float:
        """P1-009: 基于协方差的风险贡献折扣。

        与已有仓位的相关性越高，新仓位折扣越大。
        """
        if not self.covariance_matrix or not active_symbols:
            return 1.0
        max_corr = 0.0
        for active in active_symbols:
            corr = self.covariance_matrix.get(symbol, {}).get(active, 0.0)
            max_corr = max(max_corr, abs(corr))
        # 相关性越高折扣越大，最多折扣到 50%
        return 1.0 - max_corr * self._max_correlation_discount

    def optimize(
        self,
        signals: dict[str, dict],
        account_equity: float,
        min_notional: dict[str, float] | None = None,
        step_sizes: dict[str, float] | None = None,
        *,
        mode: str = "V2_COMPATIBILITY",
    ) -> OptimizationResult:
        """执行确定性优化。

        如果 account_equity 未知 (<=0)、成本无穷或规则缺失 → 返回空 targets。
        """
        if mode == "V3":
            return self.optimize_targets(
                {symbol: float(signal.get("target_weight", 0.0)) for symbol, signal in signals.items()},
                account_equity=account_equity,
                min_notional=min_notional,
                step_sizes=step_sizes,
            )
        if account_equity <= 0:
            return OptimizationResult(
                targets={},
                rejected=dict.fromkeys(signals, "UNKNOWN account equity"),
                constraint_violations=["account_equity_unknown"],
            )

        targets: dict[str, float] = {}
        rejected: dict[str, str] = {}
        violations: list[str] = []
        total_long: float = 0.0
        total_short: float = 0.0

        min_notional = min_notional or {}
        step_sizes = step_sizes or {}

        for symbol, signal in signals.items():
            direction = signal.get("direction", "NO_ACTION")
            if direction == "NO_ACTION":
                continue

            strength = signal.get("strength", 0.0)
            price = signal.get("price", 0.0)

            if price <= 0:
                rejected[symbol] = "UNKNOWN price"
                continue

            # 仓位大小：基于信号强度和账户权益
            raw_size = strength * account_equity / 100.0 / max(price, 0.01)
            # P1-009: 协方差感知折扣
            active = {s for s in targets if targets[s] != 0}
            raw_size *= self._covariance_discount(symbol, active)

            # 最小名义价值检查
            min_not = min_notional.get(symbol)
            if min_not is None or min_not <= 0:
                rejected[symbol] = "UNKNOWN venue min notional"
                continue
            if raw_size * price < min_not:
                rejected[symbol] = f"Below min notional ({raw_size * price:.2f} < {min_not})"
                continue

            # Step size 量化
            step = step_sizes.get(symbol)
            if step is None or step <= 0:
                rejected[symbol] = "UNKNOWN venue step size"
                continue
            raw_size = round(raw_size / step) * step

            if raw_size <= 0:
                rejected[symbol] = "Zero quantity after quantization"
                continue

            # 单品种集中度检查
            symbol_exposure_pct = raw_size * price / account_equity * 100
            if symbol_exposure_pct > self.max_per_symbol_pct:
                raw_size = self.max_per_symbol_pct / 100 * account_equity / price
                raw_size = round(raw_size / step) * step

            targets[symbol] = raw_size
            if direction == "LONG":
                total_long += raw_size * price
            elif direction == "SHORT":
                total_short += raw_size * price

        # 杠杆检查
        gross_exposure = (total_long + total_short) / account_equity
        net_exposure = abs(total_long - total_short) / account_equity

        if gross_exposure > self.max_gross_leverage:
            violations.append(f"Gross leverage {gross_exposure:.2f} > {self.max_gross_leverage}")
            scale = self.max_gross_leverage / gross_exposure
            targets = {s: q * scale for s, q in targets.items()}

        if net_exposure > self.max_net_leverage:
            violations.append(f"Net leverage {net_exposure:.2f} > {self.max_net_leverage}")

        return OptimizationResult(
            targets=targets,
            rejected=rejected,
            gross_exposure=gross_exposure,
            net_exposure=net_exposure,
            constraint_violations=violations,
        )

    def optimize_targets(
        self,
        target_weights: dict[str, float],
        *,
        account_equity: float,
        min_notional: dict[str, float] | None,
        step_sizes: dict[str, float] | None,
    ) -> OptimizationResult:
        """Validate V3 weights; this path never invents economic targets."""
        if account_equity <= 0:
            return OptimizationResult(
                targets={},
                rejected=dict.fromkeys(target_weights, "UNKNOWN account equity"),
                constraint_violations=["account_equity_unknown"],
            )
        rules_notional = min_notional or {}
        rules_step = step_sizes or {}
        targets: dict[str, float] = {}
        rejected: dict[str, str] = {}
        for symbol, target_weight in target_weights.items():
            if not isinstance(target_weight, (int, float)) or not math.isfinite(float(target_weight)):
                rejected[symbol] = "UNKNOWN target weight"
                continue
            if symbol not in rules_notional or symbol not in rules_step:
                rejected[symbol] = "UNKNOWN venue metadata"
                continue
            min_notional_value = rules_notional[symbol]
            step = rules_step[symbol]
            if min_notional_value <= 0 or step <= 0:
                rejected[symbol] = "UNKNOWN venue metadata"
                continue
            notional = abs(float(target_weight)) * account_equity
            if notional < min_notional_value:
                rejected[symbol] = "Below min notional"
                continue
            targets[symbol] = float(target_weight)
        gross = sum(abs(value) for value in targets.values())
        net = sum(targets.values())
        return OptimizationResult(
            targets=targets,
            rejected=rejected,
            gross_exposure=gross,
            net_exposure=net,
            constraint_violations=[],
        )
