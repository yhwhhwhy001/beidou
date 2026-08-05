"""真实成本模型 — BD-06 items 2,3,4。

使用真实 fee tier、spread、L2 depth、参与率、latency、funding/basis。
保存预测、实际和残差。OOS 校准绑定模型版本和样本量。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class CostEstimate:
    """成本估计 — 完整分解。"""
    symbol: str
    side: str  # BUY / SELL
    quantity: float
    price: float
    notional: float = 0.0

    # Fee
    maker_fee_bps: float = 2.0
    taker_fee_bps: float = 4.0
    effective_fee_bps: float = 0.0

    # Spread
    spread_bps: float = 0.0
    half_spread_cost_bps: float = 0.0

    # Market impact (Almgren-Chriss linear approximation)
    participation_pct: float = 0.0
    impact_bps: float = 0.0

    # Funding
    funding_rate_bps: float = 0.0
    funding_cost_bps: float = 0.0

    # Total
    total_cost_bps: float = 0.0
    expected_return_bps: float = 0.0
    net_after_cost_bps: float = 0.0


@dataclass
class CostCalibration:
    """成本模型 OOS 校准。"""
    model_version: str
    sample_count: int
    mean_predicted_bps: float = 0.0
    mean_actual_bps: float = 0.0
    mean_residual_bps: float = 0.0
    rmse_bps: float = 0.0
    calibrated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    is_valid: bool = False


class RealCostModel:
    """真实成本模型。

    特性:
    - 使用真实的 fee tier（从 exchangeInfo 获取）
    - 基于 L2 order book depth 计算 market impact
    - 考虑 participation rate 和 time horizon
    - 包含 funding rate 成本
    - 预测-实际残差追踪和 OOS 校准
    """

    def __init__(self):
        self._fee_tiers: dict[str, tuple[float, float]] = {}  # venue → (maker_bps, taker_bps)
        self._predictions: list[tuple[CostEstimate, float]] = []  # (pred, actual_total_bps)
        self._calibration: CostCalibration | None = None

    def set_fee_tier(self, venue: str, maker_bps: float, taker_bps: float) -> None:
        self._fee_tiers[venue] = (maker_bps, taker_bps)

    def get_fee_tier(self, venue: str) -> tuple[float, float]:
        return self._fee_tiers.get(venue, (2.0, 4.0))

    def estimate(self, symbol: str, side: str, quantity: float, price: float,
                 spread_bps: float = 1.0, bid_depth: float = 0.0,
                 ask_depth: float = 0.0, participation_pct: float = 1.0,
                 funding_rate_bps: float = 0.0, venue: str = "BINANCE",
                 ) -> CostEstimate:
        """估计交易成本（全面分解）。"""
        notional = quantity * price
        maker_bps, taker_bps = self.get_fee_tier(venue)

        # Spread cost (crossing the spread)
        half_spread = spread_bps / 2

        # Market impact (linear approximation)
        # impact = participation_rate × volatility × sqrt(trade_size / ADV) × penalty_factor
        if bid_depth > 0 and ask_depth > 0:
            depth = bid_depth if side == "SELL" else ask_depth
            impact = participation_pct * 10000 * math.sqrt(quantity / max(depth, 0.001))
        else:
            # Fallback: use volatility-based estimation
            impact = participation_pct * spread_bps * 0.5

        # Funding cost (for perpetuals, holding cost)
        funding_cost = funding_rate_bps * (1.0 / 3)  # assume ~8h holding period

        total = half_spread + taker_fee_bps + impact + funding_cost

        return CostEstimate(
            symbol=symbol, side=side, quantity=quantity, price=price,
            notional=notional,
            maker_fee_bps=maker_bps, taker_fee_bps=taker_bps,
            effective_fee_bps=taker_fee_bps,
            spread_bps=spread_bps, half_spread_cost_bps=half_spread,
            participation_pct=participation_pct, impact_bps=impact,
            funding_rate_bps=funding_rate_bps, funding_cost_bps=funding_cost,
            total_cost_bps=total,
        )

    def record_actual(self, estimate: CostEstimate, actual_total_bps: float) -> None:
        """记录实际成本（用于校准）。"""
        self._predictions.append((estimate, actual_total_bps))

    def calibrate(self, model_version: str) -> CostCalibration:
        """OOS 校准：计算预测-实际残差分布。"""
        if len(self._predictions) < 20:
            return CostCalibration(model_version=model_version, sample_count=0)

        residuals = [pred.total_cost_bps - actual for pred, actual in self._predictions]
        n = len(residuals)
        mean_pred = sum(p.total_cost_bps for p, _ in self._predictions) / n
        mean_actual = sum(a for _, a in self._predictions) / n
        mean_residual = sum(residuals) / n
        rmse = math.sqrt(sum(r ** 2 for r in residuals) / n)

        is_valid = abs(mean_residual) < 5.0 and rmse < 10.0  # within 5bps bias, 10bps RMSE

        self._calibration = CostCalibration(
            model_version=model_version, sample_count=n,
            mean_predicted_bps=mean_pred, mean_actual_bps=mean_actual,
            mean_residual_bps=mean_residual, rmse_bps=rmse,
            is_valid=is_valid,
        )
        return self._calibration

    def get_calibration(self) -> CostCalibration | None:
        return self._calibration


@dataclass
class SignalConsistency:
    """BD-06 item 3: 信号一致性评估。"""
    agreement_ratio: float     # 同向信号占比
    correlation_discount: float  # 相关性折扣 (0-1)
    conflict_detected: bool
    filter_multiplier: float   # Filter 施加的乘数
    contribution_breakdown: dict[str, float]  # component_id → contribution
    final_direction: str
    final_strength: float
    reason: str
