"""清算价格与风险距离计算 — BD-07。

计算实际 leverage、initial/maintenance margin、liquidation distance、保护覆盖。
所有计算结果进入风险审批链。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LiquidationRisk:
    """清算风险评估。"""
    symbol: str
    current_price: float
    liquidation_price: float
    distance_pct: float          # 距离清算的百分比
    leverage: float
    initial_margin: float
    maintenance_margin: float
    margin_ratio: float          # maintenance / initial
    is_critical: bool            # 距离 < 5%
    position_size: float
    notional: float


class LiquidationCalculator:
    """清算距离计算器。"""

    # Binance USDⓈ-M 默认维持保证金率
    DEFAULT_MAINTENANCE_MARGIN_RATE = 0.004  # 0.4%
    DEFAULT_INITIAL_MARGIN_RATE = 0.01       # 1% (100x max leverage)

    @staticmethod
    def calculate_long_liquidation(
        entry_price: float, quantity: float, leverage: float,
        maintenance_rate: float | None = None,
    ) -> float:
        """计算多头清算价格。"""
        if quantity <= 0 or entry_price <= 0 or leverage <= 0:
            return 0.0
        mmr = maintenance_rate or LiquidationCalculator.DEFAULT_MAINTENANCE_MARGIN_RATE
        # liq_price = entry_price * (1 - 1/leverage + mmr)
        bankruptcy_price = entry_price * (1 - 1.0 / leverage)
        liquidation_price = bankruptcy_price * (1 + mmr)
        return round(liquidation_price, 8)

    @staticmethod
    def calculate_short_liquidation(
        entry_price: float, quantity: float, leverage: float,
        maintenance_rate: float | None = None,
    ) -> float:
        """计算空头清算价格。"""
        if quantity <= 0 or entry_price <= 0 or leverage <= 0:
            return float("inf")
        mmr = maintenance_rate or LiquidationCalculator.DEFAULT_MAINTENANCE_MARGIN_RATE
        bankruptcy_price = entry_price * (1 + 1.0 / leverage)
        liquidation_price = bankruptcy_price * (1 - mmr)
        return round(liquidation_price, 8)

    @staticmethod
    def assess_risk(symbol: str, current_price: float, entry_price: float,
                    quantity: float, leverage: float, side: str = "LONG") -> LiquidationRisk:
        """评估清算风险。"""
        if side.upper() == "SHORT":
            liq_price = LiquidationCalculator.calculate_short_liquidation(
                entry_price, quantity, leverage)
        else:
            liq_price = LiquidationCalculator.calculate_long_liquidation(
                entry_price, quantity, leverage)

        notional = quantity * current_price
        initial_margin = notional / leverage
        maintenance_margin = notional * LiquidationCalculator.DEFAULT_MAINTENANCE_MARGIN_RATE
        margin_ratio = maintenance_margin / initial_margin if initial_margin > 0 else 1.0

        if current_price > 0:
            distance_pct = abs(current_price - liq_price) / current_price * 100
        else:
            distance_pct = 0.0

        return LiquidationRisk(
            symbol=symbol,
            current_price=current_price,
            liquidation_price=liq_price,
            distance_pct=round(distance_pct, 2),
            leverage=leverage,
            initial_margin=round(initial_margin, 2),
            maintenance_margin=round(maintenance_margin, 2),
            margin_ratio=round(margin_ratio, 4),
            is_critical=distance_pct < 5.0,
            position_size=quantity,
            notional=round(notional, 2),
        )
