"""策略层风险管理 — 回撤限制、风险预算、策略熔断、跨策略相关性风险。

与 beidou_safety 的区别:
  - beidou_safety/risk: 交易安全（PreRisk/杠杆/仓位不变量），阻止非法订单
  - beidou_strategy/risk: 策略质量（回撤/Alpha衰减/风险预算），决定是否继续交易
"""
from .manager import (
    StrategyRiskLevel,
    CircuitBreakerReason,
    RiskBudget,
    StrategyRiskState,
    DrawdownMonitor,
    StrategyRiskManager,
)

__all__ = [
    "StrategyRiskLevel",
    "CircuitBreakerReason",
    "RiskBudget",
    "StrategyRiskState",
    "DrawdownMonitor",
    "StrategyRiskManager",
]
