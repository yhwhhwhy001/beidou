"""自适应止盈止损策略 — 基于 ATR、波动率、点差、价格档位的动态保护计算。"""

from .adaptive import (
    AdaptiveProtectionCalculator,
    AdaptiveProtectionConfig,
    MarketRegime,
    PriceTier,
    VolatilityRegime,
)

__all__ = [
    "AdaptiveProtectionCalculator",
    "AdaptiveProtectionConfig",
    "MarketRegime",
    "PriceTier",
    "VolatilityRegime",
]
