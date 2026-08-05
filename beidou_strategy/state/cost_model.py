"""手续费、滑点、冲击、资金费预测与容量模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from beidou_shared.types import (
    InstrumentId,
    OrderSide,
    Price,
    Quantity,
    VenueId,
    VenueInstrument,
)


@dataclass(frozen=True, slots=True)
class CostEstimate:
    instrument_id: InstrumentId
    venue_id: VenueId
    fee_rate_maker: float
    fee_rate_taker: float
    estimated_slippage_bps: float
    estimated_impact_bps: float
    total_fee_bps: float = 0.0
    funding_rate_prediction: float | None = None
    funding_rate_confidence: float = 0.0
    warnings: list[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def total_cost_bps(self, is_taker: bool = True) -> float:
        fee = self.fee_rate_taker if is_taker else self.fee_rate_maker
        return fee + self.estimated_slippage_bps + self.estimated_impact_bps

    def net_alpha_after_costs(self, raw_alpha_bps: float, is_taker: bool = True) -> float:
        return raw_alpha_bps - self.total_cost_bps(is_taker)


class CostModel:
    """交易成本模型。手续费、滑点、冲击、资金费。"""

    def __init__(self) -> None:
        self._fee_tiers: dict[str, dict[str, float]] = {}

    def set_fee_tier(self, venue_id: VenueId, tier_name: str, maker_bps: float, taker_bps: float) -> None:
        self._fee_tiers[str(venue_id)] = {"maker": maker_bps, "taker": taker_bps}

    def estimate_slippage(self, spread_bps: float, urgency: float = 0.5) -> float:
        return spread_bps * urgency

    def estimate_impact(self, order_size_notional: float, avg_daily_volume: float, volatility: float) -> float:
        if avg_daily_volume == 0:
            return float("inf")
        participation = order_size_notional / avg_daily_volume
        return participation * volatility * 10000  # in bps

    def estimate(
        self,
        instrument_id: InstrumentId,
        venue_id: VenueId,
        order_size_notional: float,
        avg_daily_volume: float,
        spread_bps: float,
        volatility: float,
        is_taker: bool = True,
    ) -> CostEstimate:
        """底层API — 以原始数值估算交易成本。"""
        fees = self._fee_tiers.get(str(venue_id), {"maker": 2.0, "taker": 4.0})
        slippage = self.estimate_slippage(spread_bps)
        impact = self.estimate_impact(order_size_notional, avg_daily_volume, volatility)
        total = (fees["taker"] if is_taker else fees["maker"]) + slippage + impact
        return CostEstimate(
            instrument_id=instrument_id,
            venue_id=venue_id,
            fee_rate_maker=fees["maker"],
            fee_rate_taker=fees["taker"],
            estimated_slippage_bps=slippage,
            estimated_impact_bps=impact,
            total_fee_bps=total,
        )

    def estimate_order(
        self,
        venue_instrument: VenueInstrument,
        quantity: Quantity,
        price: Price,
        side: OrderSide,
        urgency: float = 0.5,
        spread_bps: float = 5.0,
        avg_daily_volume: float = 1000000000.0,
        volatility: float = 0.02,
        is_taker: bool = True,
    ) -> CostEstimate:
        """高层级API — 使用领域对象便捷估算交易成本。"""
        order_size_notional = float(quantity.amount) * float(price.amount)
        fees = self._fee_tiers.get(str(venue_instrument.venue_id), {"maker": 2.0, "taker": 4.0})
        slippage = self.estimate_slippage(spread_bps, urgency)
        impact = self.estimate_impact(order_size_notional, avg_daily_volume, volatility)
        total = (fees["taker"] if is_taker else fees["maker"]) + slippage + impact

        warnings: list[str] = []
        funding_warning = self.funding_rate_warning(0.0001)
        if funding_warning != "NORMAL":
            warnings.append(f"funding_rate_{funding_warning}")

        return CostEstimate(
            instrument_id=venue_instrument.instrument_id,
            venue_id=venue_instrument.venue_id,
            fee_rate_maker=fees["maker"],
            fee_rate_taker=fees["taker"],
            estimated_slippage_bps=slippage,
            estimated_impact_bps=impact,
            total_fee_bps=total,
            warnings=warnings,
        )

    def funding_rate_warning(self, predicted_rate: float) -> str:
        """资金费是净收益和风险输入，不是机械加仓信号。"""
        if abs(predicted_rate) > 0.001:  # > 0.1%
            return "HIGH_FUNDING_RATE"
        if abs(predicted_rate) > 0.0005:  # > 0.05%
            return "ELEVATED_FUNDING_RATE"
        return "NORMAL"
