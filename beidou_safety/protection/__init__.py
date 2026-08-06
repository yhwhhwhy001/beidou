"""止盈止损保护引擎 — 止损止盈计算、保护单生命周期管理。

止损类型: FIXED_PERCENT / ATR_BASED / VOLATILITY_BASED / TRAILING / SWING_STRUCTURE
止盈类型: FIXED_RR / MULTI_TARGET / TRAILING_TAKE_PROFIT

止盈止损执行由交易所 Algo Order API 原生处理，不再使用本地价格监控。
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
]
