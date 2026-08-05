"""止盈止损保护引擎 — 动态止损、风险回报止盈、移动止损、保护单生命周期管理。

止损类型: FIXED_PERCENT / ATR_BASED / VOLATILITY_BASED / TRAILING / SWING_STRUCTURE
止盈类型: FIXED_RR / MULTI_TARGET / TRAILING_TAKE_PROFIT
"""

from .engine import (
    PositionProtection,
    ProtectionManager,
    ProtectionOrder,
    ProtectionStatus,
    StopLossCalculator,
    StopLossType,
    TakeProfitCalculator,
    TakeProfitType,
    TrailingStopUpdater,
)

__all__ = [
    "PositionProtection",
    "ProtectionManager",
    "ProtectionOrder",
    "ProtectionStatus",
    "StopLossCalculator",
    "StopLossType",
    "TakeProfitCalculator",
    "TakeProfitType",
    "TrailingStopUpdater",
]
