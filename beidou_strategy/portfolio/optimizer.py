
"""组合优化、策略资本归属、冲突仲裁、自适应仓位与杠杆。"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from beidou_shared.types import InstrumentId, MonetaryValue, Quantity, StrategyId, VenueId
from beidou_strategy.portfolio import PortfolioTarget, PortfolioState, PositionOwnership

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

    def allocate_capital(self, strategies: list[StrategyId], total_capital: MonetaryValue, weights: dict[StrategyId, float] | None = None) -> dict[StrategyId, MonetaryValue]:
        if weights is None:
            w = 1.0 / len(strategies) if strategies else 0
            weights = {s: w for s in strategies}
        return {s: MonetaryValue(amount=str(float(total_capital.amount) * weights.get(s, 0))) for s in strategies}

    def resolve_conflicts(self, targets: list[PortfolioTarget]) -> tuple[list[PortfolioTarget], int]:
        """仲裁策略冲突。同一净仓位中不同策略的贡献、退出义务和接管规则。"""
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
            total_qty = sum(float(t.target_quantity.amount) for t in group)
            for t in group:
                resolved.append(PortfolioTarget(
                    strategy_id=t.strategy_id, instrument_id=t.instrument_id,
                    venue_id=t.venue_id, target_quantity=Quantity(amount=str(total_qty / len(group))),
                    target_notional=t.target_notional, capital_budget=t.capital_budget,
                    ownership=PositionOwnership.SHARED if len(group) > 1 else t.ownership,
                ))
        return resolved, conflicts

    def exit_protection(self, exiting_strategy: StrategyId, all_targets: list[PortfolioTarget]) -> list[PortfolioTarget]:
        """一个策略退出时，不得错误平掉其他策略仍需要的仓位。"""
        remaining = []
        for t in all_targets:
            if t.strategy_id == exiting_strategy:
                if t.ownership == PositionOwnership.SHARED:
                    # Shared position: transfer ownership instead of closing
                    if t.takeover_strategy:
                        remaining.append(PortfolioTarget(
                            strategy_id=t.takeover_strategy, instrument_id=t.instrument_id,
                            venue_id=t.venue_id, target_quantity=t.target_quantity,
                            target_notional=t.target_notional, capital_budget=t.capital_budget,
                            ownership=PositionOwnership.DELEGATED,
                        ))
                continue
            remaining.append(t)
        return remaining
