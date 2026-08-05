"""策略层风险管理 — 回撤限制、风险预算、策略熔断、跨策略相关性风险。

与 beidou_safety 的区别:
  - beidou_safety: 交易安全（PreRisk/杠杆/仓位不变量），阻止非法订单
  - beidou_strategy.risk: 策略质量（回撤/Alpha衰减/风险预算），决定是否继续交易
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from beidou_shared.types import MonetaryValue, StrategyId, ResultStatus


_RISK_LEVEL_SEVERITY = {"NORMAL": 0, "CAUTION": 1, "REDUCED": 2, "EXIT_ONLY": 3, "LOCKED": 4}

class StrategyRiskLevel(str, Enum):
    NORMAL = "NORMAL"
    CAUTION = "CAUTION"
    REDUCED = "REDUCED"
    EXIT_ONLY = "EXIT_ONLY"
    LOCKED = "LOCKED"

    @property
    def severity(self) -> int:
        return _RISK_LEVEL_SEVERITY.get(self.value, 0)


class CircuitBreakerReason(str, Enum):
    DRAWDOWN_LIMIT = "DRAWDOWN_LIMIT"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    CONSECUTIVE_LOSSES = "CONSECUTIVE_LOSSES"
    SHARPE_DEGRADATION = "SHARPE_DEGRADATION"
    VOLATILITY_SPIKE = "VOLATILITY_SPIKE"
    CORRELATION_BREAKDOWN = "CORRELATION_BREAKDOWN"
    MAX_POSITION_COUNT = "MAX_POSITION_COUNT"


@dataclass
class RiskBudget:
    """策略风险预算 — 限制单策略可承担的风险量。"""
    strategy_id: StrategyId
    max_drawdown_pct: float = 20.0  # 最大回撤百分比
    max_daily_loss_pct: float = 5.0  # 单日最大亏损
    max_consecutive_losses: int = 5  # 最大连续亏损笔数
    max_position_notional: float = 100000.0  # 最大仓位名义价值
    max_leverage: float = 3.0  # 最大杠杆
    risk_per_trade_pct: float = 1.0  # 单笔风险占资金百分比
    min_sharpe_rolling: float = 0.0  # 滚动Sharpe下限
    allocation_pct: float = 100.0  # 资金分配比例
    parent_budget_id: str | None = None  # 从属的上级预算


@dataclass
class StrategyRiskState:
    """策略风险实时状态 — 动态更新的风险指标。"""
    strategy_id: StrategyId
    current_drawdown_pct: float = 0.0
    peak_equity: float = 0.0
    daily_pnl: float = 0.0
    daily_loss_pct: float = 0.0
    consecutive_losses: int = 0
    rolling_sharpe: float | None = None
    current_leverage: float = 0.0
    position_count: int = 0
    active_circuit_breakers: list[CircuitBreakerReason] = field(default_factory=list)
    risk_level: StrategyRiskLevel = StrategyRiskLevel.NORMAL
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def is_trading_allowed(self) -> bool:
        return self.risk_level in (StrategyRiskLevel.NORMAL, StrategyRiskLevel.CAUTION)

    def is_new_position_allowed(self) -> bool:
        return self.risk_level == StrategyRiskLevel.NORMAL


class DrawdownMonitor:
    """回撤监控器 — 跟踪峰值权益，检测回撤阈值。"""

    @staticmethod
    def compute_drawdown(current_equity: float, peak_equity: float) -> tuple[float, float]:
        """计算当前回撤百分比和新峰值。"""
        new_peak = max(current_equity, peak_equity)
        if new_peak == 0:
            return 0.0, new_peak
        dd_pct = (new_peak - current_equity) / new_peak * 100
        return dd_pct, new_peak

    @staticmethod
    def check_limit(drawdown_pct: float, max_drawdown_pct: float) -> tuple[bool, StrategyRiskLevel]:
        """检查回撤是否超过限制。"""
        if drawdown_pct >= max_drawdown_pct:
            return False, StrategyRiskLevel.LOCKED
        elif drawdown_pct >= max_drawdown_pct * 0.7:
            return True, StrategyRiskLevel.EXIT_ONLY
        elif drawdown_pct >= max_drawdown_pct * 0.5:
            return True, StrategyRiskLevel.REDUCED
        elif drawdown_pct >= max_drawdown_pct * 0.3:
            return True, StrategyRiskLevel.CAUTION
        return True, StrategyRiskLevel.NORMAL


class StrategyRiskManager:
    """策略风险管理器 — 统一管理所有策略的风险预算和状态。

    功能:
      1. 分配和管理策略风险预算
      2. 实时监控回撤、连续亏损、Sharpe衰减
      3. 自动触发策略熔断（锁仓/仅平仓/降仓）
      4. 跨策略相关性风险检测
    """

    def __init__(self) -> None:
        self._budgets: dict[StrategyId, RiskBudget] = {}
        self._states: dict[StrategyId, StrategyRiskState] = {}
        self._circuit_breaker_log: list[dict] = []

       # ---- 风险预算 ----

    def set_budget(self, budget: RiskBudget) -> None:
        self._budgets[budget.strategy_id] = budget
        if budget.strategy_id not in self._states:
            self._states[budget.strategy_id] = StrategyRiskState(
                strategy_id=budget.strategy_id,
            )

    def get_budget(self, strategy_id: StrategyId) -> RiskBudget | None:
        return self._budgets.get(strategy_id)

    def compute_position_size(self, strategy_id: StrategyId, account_equity: float, entry_price: float, stop_loss_price: float) -> float:
        """基于风险预算计算仓位大小。风险金额 = 账户权益 × risk_per_trade_pct%。"""
        budget = self._budgets.get(strategy_id)
        if budget is None or entry_price == 0:
            return 0.0
        risk_amount = account_equity * budget.risk_per_trade_pct / 100
        risk_per_unit = abs(entry_price - stop_loss_price)
        if risk_per_unit == 0:
            return 0.0
        size = risk_amount / risk_per_unit
        max_size = budget.max_position_notional / entry_price
        return min(size, max_size)

    # ---- 状态更新 ----

    def update_equity(self, strategy_id: StrategyId, current_equity: float) -> dict:
        """更新权益并检查回撤。返回触发的动作。"""
        state = self._states.get(strategy_id)
        budget = self._budgets.get(strategy_id)
        if state is None or budget is None:
            return {"action": "NOOP", "reason": "no budget configured"}

        # 更新峰值
        if current_equity > state.peak_equity:
            state.peak_equity = current_equity

        # 计算回撤
        dd_pct, new_peak = DrawdownMonitor.compute_drawdown(current_equity, state.peak_equity)
        state.current_drawdown_pct = dd_pct
        state.peak_equity = new_peak
        state.last_updated = datetime.now(timezone.utc)

        # 检查回撤限制
        trading_ok, new_level = DrawdownMonitor.check_limit(dd_pct, budget.max_drawdown_pct)
        old_level = state.risk_level

        if new_level != old_level and new_level.severity > old_level.severity:
            state.risk_level = new_level
            reason = CircuitBreakerReason.DRAWDOWN_LIMIT
            state.active_circuit_breakers.append(reason)
            self._circuit_breaker_log.append({
                "strategy_id": str(strategy_id),
                "reason": reason.value,
                "old_level": old_level.value,
                "new_level": new_level.value,
                "drawdown_pct": dd_pct,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            return {
                "action": "DEGRADE",
                "old_level": old_level.value,
                "new_level": new_level.value,
                "drawdown_pct": dd_pct,
                "reason": f"Drawdown {dd_pct:.1f}% >= {budget.max_drawdown_pct * 0.3:.1f}%",
            }

        return {"action": "NOOP", "drawdown_pct": dd_pct, "risk_level": new_level.value}

    def record_trade(self, strategy_id: StrategyId, pnl: float, is_win: bool) -> dict:
        """记录交易结果，检查连续亏损和单日亏损限制。"""
        state = self._states.get(strategy_id)
        budget = self._budgets.get(strategy_id)
        if state is None or budget is None:
            return {"action": "NOOP"}

        # 单日亏损累计
        state.daily_pnl += pnl
        if state.peak_equity > 0:
            state.daily_loss_pct = abs(min(0, state.daily_pnl)) / state.peak_equity * 100

        # 连续亏损
        if is_win:
            state.consecutive_losses = 0
        else:
            state.consecutive_losses += 1

        actions = []

        # 检查单日亏损限制
        if state.daily_loss_pct >= budget.max_daily_loss_pct:
            reason = CircuitBreakerReason.DAILY_LOSS_LIMIT
            if reason not in state.active_circuit_breakers:
                state.active_circuit_breakers.append(reason)
                state.risk_level = StrategyRiskLevel.LOCKED
                self._circuit_breaker_log.append({
                    "strategy_id": str(strategy_id),
                    "reason": reason.value,
                    "daily_loss_pct": state.daily_loss_pct,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                actions.append(f"DAILY_LOSS_LIMIT: {state.daily_loss_pct:.1f}% >= {budget.max_daily_loss_pct:.1f}% → LOCKED")

        # 检查连续亏损限制
        if state.consecutive_losses >= budget.max_consecutive_losses:
            reason = CircuitBreakerReason.CONSECUTIVE_LOSSES
            if reason not in state.active_circuit_breakers:
                state.active_circuit_breakers.append(reason)
                state.risk_level = StrategyRiskLevel.EXIT_ONLY
                self._circuit_breaker_log.append({
                    "strategy_id": str(strategy_id),
                    "reason": reason.value,
                    "consecutive_losses": state.consecutive_losses,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                actions.append(f"CONSECUTIVE_LOSSES: {state.consecutive_losses} >= {budget.max_consecutive_losses} → EXIT_ONLY")

        return {
            "action": "DEGRADE" if actions else "NOOP",
            "consecutive_losses": state.consecutive_losses,
            "daily_loss_pct": state.daily_loss_pct,
            "triggered": actions,
        }

    def update_sharpe(self, strategy_id: StrategyId, rolling_sharpe: float) -> dict:
        """检查 Sharpe 衰减。"""
        state = self._states.get(strategy_id)
        budget = self._budgets.get(strategy_id)
        if state is None or budget is None:
            return {"action": "NOOP"}

        state.rolling_sharpe = rolling_sharpe

        if rolling_sharpe < budget.min_sharpe_rolling:
            reason = CircuitBreakerReason.SHARPE_DEGRADATION
            if reason not in state.active_circuit_breakers:
                state.active_circuit_breakers.append(reason)
                state.risk_level = StrategyRiskLevel.EXIT_ONLY
                self._circuit_breaker_log.append({
                    "strategy_id": str(strategy_id),
                    "reason": reason.value,
                    "sharpe": rolling_sharpe,
                    "threshold": budget.min_sharpe_rolling,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                return {"action": "DEGRADE", "new_level": "EXIT_ONLY",
                        "reason": f"Sharpe {rolling_sharpe:.2f} < {budget.min_sharpe_rolling}"}

        return {"action": "NOOP"}

    # ---- 熔断器 ----

    def reset_circuit_breakers(self, strategy_id: StrategyId, reason: str = "") -> bool:
        """手动重置策略熔断器（需要人工审核后操作）。"""
        state = self._states.get(strategy_id)
        if state is None:
            return False
        state.active_circuit_breakers.clear()
        state.risk_level = StrategyRiskLevel.NORMAL
        state.consecutive_losses = 0
        state.daily_pnl = 0.0
        state.daily_loss_pct = 0.0
        self._circuit_breaker_log.append({
            "strategy_id": str(strategy_id),
            "reason": "MANUAL_RESET",
            "reset_reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        return True

    def reset_daily_pnl(self) -> None:
        """每日重置所有策略的单日 PnL。"""
        for state in self._states.values():
            state.daily_pnl = 0.0
            state.daily_loss_pct = 0.0

    # ---- 查询 ----

    def get_state(self, strategy_id: StrategyId) -> StrategyRiskState | None:
        return self._states.get(strategy_id)

    def get_risk_level(self, strategy_id: StrategyId) -> StrategyRiskLevel:
        state = self._states.get(strategy_id)
        return state.risk_level if state else StrategyRiskLevel.NORMAL

    def is_trading_allowed(self, strategy_id: StrategyId) -> bool:
        state = self._states.get(strategy_id)
        return state is not None and state.is_trading_allowed()

    def get_circuit_breaker_log(self) -> list[dict]:
        return list(self._circuit_breaker_log)

    def get_all_states(self) -> dict[StrategyId, StrategyRiskState]:
        return dict(self._states)

    def strategy_count(self) -> int:
        return len(self._budgets)
