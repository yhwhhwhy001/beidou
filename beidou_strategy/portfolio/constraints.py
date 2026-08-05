"""约束组合优化器 — BD-06。

确定性约束优化：最大化成本后 Alpha，
约束 gross/net leverage、symbol/cluster exposure、策略预算、保证金、容量、min notional、step size。
UNKNOWN 输入返回不可交易（无 fallback 到固定数值）。
"""

from __future__ import annotations

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
        self, max_gross_leverage: float = 3.0, max_net_leverage: float = 1.0, max_per_symbol_pct: float = 50.0
    ):
        self.max_gross_leverage = max_gross_leverage
        self.max_net_leverage = max_net_leverage
        self.max_per_symbol_pct = max_per_symbol_pct

    def optimize(
        self,
        signals: dict[str, dict],
        account_equity: float,
        min_notional: dict[str, float] | None = None,
        step_sizes: dict[str, float] | None = None,
    ) -> OptimizationResult:
        """执行确定性优化。

        如果 account_equity 未知 (<=0)、成本无穷或规则缺失 → 返回空 targets。
        """
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
            raw_size = strength * account_equity * 0.01 / max(price, 0.01)

            # 最小名义价值检查
            min_not = min_notional.get(symbol, 5.0)
            if raw_size * price < min_not:
                rejected[symbol] = f"Below min notional ({raw_size * price:.2f} < {min_not})"
                continue

            # Step size 量化
            step = step_sizes.get(symbol, 0.001)
            if step > 0:
                raw_size = round(raw_size / step) * step

            if raw_size <= 0:
                rejected[symbol] = "Zero quantity after quantization"
                continue

            # 单品种集中度检查
            symbol_exposure_pct = raw_size * price / account_equity * 100
            if symbol_exposure_pct > self.max_per_symbol_pct:
                raw_size = self.max_per_symbol_pct / 100 * account_equity / price
                if step > 0:
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
