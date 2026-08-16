"""组合优化、策略资本归属、冲突仲裁、自适应仓位与杠杆。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from beidou_shared.types import MonetaryValue, Quantity, StrategyId
from beidou_strategy.portfolio import PortfolioTarget, PositionOwnership


@dataclass
class OptimizationResult:
    targets: list[PortfolioTarget]
    capital_allocated: dict[StrategyId, MonetaryValue] = field(default_factory=dict)
    conflicts_resolved: int = 0
    max_leverage: float = 1.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


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
