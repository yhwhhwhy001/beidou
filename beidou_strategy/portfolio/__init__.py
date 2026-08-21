"""组合优化与策略资本归属。多策略目标贡献、资本预算、退出义务和接管规则。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from beidou_shared.types import InstrumentId, MonetaryValue, Quantity, StrategyId, VenueId


class CapitalAllocationMode(str, Enum):
    EQUAL_WEIGHT = "EQUAL_WEIGHT"
    RISK_PARITY = "RISK_PARITY"
    KELLY_FRACTIONAL = "KELLY_FRACTIONAL"
    MANUAL = "MANUAL"
    ADAPTIVE = "ADAPTIVE"


class PositionOwnership(str, Enum):
    EXCLUSIVE = "EXCLUSIVE"
    SHARED = "SHARED"
    DELEGATED = "DELEGATED"


@dataclass(frozen=True, slots=True)
class PortfolioTarget:
    strategy_id: StrategyId
    instrument_id: InstrumentId
    venue_id: VenueId
    target_quantity: Quantity
    target_notional: MonetaryValue
    capital_budget: MonetaryValue
    max_leverage: float = 1.0
    ownership: PositionOwnership = PositionOwnership.EXCLUSIVE
    exit_obligation: bool = True
    takeover_strategy: StrategyId | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class PortfolioState:
    net_positions: dict[str, Quantity] = field(default_factory=dict)
    strategy_contributions: dict[StrategyId, list[PortfolioTarget]] = field(default_factory=dict)
    total_capital: MonetaryValue | None = None
    available_margin: MonetaryValue | None = None
    total_risk_score: float = 0.0


from .contracts import ExposureTarget, PortfolioOptimizationInput
from .exposure_governor import ExposureGovernor, ExposurePolicy
from .optimizer import ActiveOptimizationResult, ActivePortfolioOptimizer, ActivePortfolioPolicy, OptimizationResult
from .optimizer import PortfolioOptimizerImpl as PortfolioOptimizer

__all__ = [
    "ActiveOptimizationResult",
    "ActivePortfolioOptimizer",
    "ActivePortfolioPolicy",
    "CapitalAllocationMode",
    "ExposureGovernor",
    "ExposurePolicy",
    "ExposureTarget",
    "OptimizationResult",
    "PortfolioOptimizationInput",
    "PortfolioOptimizer",
    "PortfolioState",
    "PortfolioTarget",
    "PositionOwnership",
]
