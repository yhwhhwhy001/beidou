"""策略层风险管理测试。回撤监控、风险预算、熔断器、仓位计算。"""
from __future__ import annotations

import pytest
from beidou_shared.types import StrategyId
from beidou_strategy.risk.manager import (
    StrategyRiskLevel, CircuitBreakerReason, RiskBudget, StrategyRiskState,
    DrawdownMonitor, StrategyRiskManager,
)


class TestDrawdownMonitor:
    """回撤监控器测试。"""

    def test_no_drawdown(self):
        dd, peak = DrawdownMonitor.compute_drawdown(110.0, 100.0)
        assert dd == 0.0
        assert peak == 110.0

    def test_drawdown_calculation(self):
        dd, peak = DrawdownMonitor.compute_drawdown(90.0, 100.0)
        assert dd == 10.0  # (100 - 90) / 100 * 100
        assert peak == 100.0

    def test_drawdown_severe(self):
        dd, peak = DrawdownMonitor.compute_drawdown(75.0, 100.0)
        assert dd == 25.0

    def test_check_limit_normal(self):
        ok, level = DrawdownMonitor.check_limit(5.0, 20.0)
        assert ok is True
        assert level == StrategyRiskLevel.NORMAL

    def test_check_limit_caution(self):
        ok, level = DrawdownMonitor.check_limit(8.0, 20.0)
        assert ok is True
        assert level == StrategyRiskLevel.CAUTION  # 8 >= 20*0.3=6

    def test_check_limit_reduced(self):
        ok, level = DrawdownMonitor.check_limit(12.0, 20.0)
        assert ok is True
        assert level == StrategyRiskLevel.REDUCED  # 12 >= 20*0.5=10

    def test_check_limit_exit_only(self):
        ok, level = DrawdownMonitor.check_limit(15.0, 20.0)
        assert ok is True
        assert level == StrategyRiskLevel.EXIT_ONLY  # 15 >= 20*0.7=14

    def test_check_limit_locked(self):
        ok, level = DrawdownMonitor.check_limit(22.0, 20.0)
        assert ok is False
        assert level == StrategyRiskLevel.LOCKED


class TestRiskBudget:
    """风险预算测试。"""

    def test_budget_creation(self):
        b = RiskBudget(
            strategy_id=StrategyId("s1"), max_drawdown_pct=15.0,
            max_daily_loss_pct=3.0, max_consecutive_losses=3,
            max_position_notional=50000.0, risk_per_trade_pct=1.0,
        )
        assert b.max_drawdown_pct == 15.0
        assert b.risk_per_trade_pct == 1.0

    def test_position_size_calculation(self):
        mgr = StrategyRiskManager()
        mgr.set_budget(RiskBudget(
            strategy_id=StrategyId("s1"), max_drawdown_pct=20.0,
            risk_per_trade_pct=1.0, max_position_notional=100000.0,
        ))
        # 10,000 equity * 1% risk = 100 risk per trade
        # Entry 50,000, SL 49,500 → risk per unit = 500
        # Size = 100 / 500 = 0.2 BTC
        size = mgr.compute_position_size(StrategyId("s1"), 10000.0, 50000.0, 49500.0)
        assert size == 0.2

    def test_position_size_capped_by_max_notional(self):
        mgr = StrategyRiskManager()
        mgr.set_budget(RiskBudget(
            strategy_id=StrategyId("s1"), max_drawdown_pct=20.0,
            risk_per_trade_pct=10.0, max_position_notional=5000.0,
        ))
        size = mgr.compute_position_size(StrategyId("s1"), 100000.0, 50000.0, 49000.0)
        # risk amount = 100000 * 10% = 10000
        # risk per unit = 1000
        # size = 10000/1000 = 10
        # max_size = 5000/50000 = 0.1
        assert size == 0.1  # capped by max_position_notional

    def test_no_budget_returns_zero(self):
        mgr = StrategyRiskManager()
        size = mgr.compute_position_size(StrategyId("unknown"), 10000.0, 50000.0, 49000.0)
        assert size == 0.0


class TestStrategyRiskManager:
    """策略风险管理器测试。"""

    def _setup_manager(self) -> tuple[StrategyRiskManager, StrategyId]:
        mgr = StrategyRiskManager()
        sid = StrategyId("test-strategy")
        mgr.set_budget(RiskBudget(
            strategy_id=sid, max_drawdown_pct=20.0,
            max_daily_loss_pct=5.0, max_consecutive_losses=5,
            max_position_notional=100000.0, risk_per_trade_pct=1.0,
            min_sharpe_rolling=0.0,
        ))
        return mgr, sid

    def test_initial_state_normal(self):
        mgr, sid = self._setup_manager()
        assert mgr.is_trading_allowed(sid)
        assert mgr.get_risk_level(sid) == StrategyRiskLevel.NORMAL

    def test_drawdown_degradation(self):
        mgr, sid = self._setup_manager()
        # 12% drawdown → REDUCED (>= 20%*0.5=10%, < 14%)
        state = mgr.get_state(sid)
        state.peak_equity = 100.0
        result = mgr.update_equity(sid, 88.0)
        assert result["action"] == "DEGRADE"
        assert mgr.get_risk_level(sid) == StrategyRiskLevel.REDUCED

    def test_drawdown_locked(self):
        mgr, sid = self._setup_manager()
        state = mgr.get_state(sid)
        state.peak_equity = 100.0
        result = mgr.update_equity(sid, 78.0)
        assert result["action"] == "DEGRADE"
        assert mgr.get_risk_level(sid) == StrategyRiskLevel.LOCKED  # 22% >= 20%

    def test_new_high_resets_peak(self):
        mgr, sid = self._setup_manager()
        state = mgr.get_state(sid)
        state.peak_equity = 100.0
        mgr.update_equity(sid, 120.0)
        assert mgr.get_state(sid).peak_equity == 120.0
        assert mgr.get_state(sid).current_drawdown_pct == 0.0

    def test_consecutive_losses(self):
        mgr, sid = self._setup_manager()
        state = mgr.get_state(sid)
        state.peak_equity = 100.0

        # 5 consecutive losses should trigger
        for i in range(5):
            result = mgr.record_trade(sid, pnl=-10.0, is_win=False)

        assert state.consecutive_losses == 5
        assert result["action"] == "DEGRADE"
        assert mgr.get_risk_level(sid) == StrategyRiskLevel.EXIT_ONLY

    def test_win_resets_consecutive(self):
        mgr, sid = self._setup_manager()
        state = mgr.get_state(sid)
        state.peak_equity = 100.0

        mgr.record_trade(sid, pnl=-10.0, is_win=False)
        mgr.record_trade(sid, pnl=-10.0, is_win=False)
        assert state.consecutive_losses == 2

        mgr.record_trade(sid, pnl=5.0, is_win=True)
        assert state.consecutive_losses == 0

    def test_daily_loss_limit(self):
        mgr, sid = self._setup_manager()
        state = mgr.get_state(sid)
        state.peak_equity = 1000.0
        # 6% daily loss > 5% limit
        result = mgr.record_trade(sid, pnl=-60.0, is_win=False)
        assert result["action"] == "DEGRADE"
        assert "DAILY_LOSS_LIMIT" in str(result["triggered"])

    def test_sharpe_degradation(self):
        mgr, sid = self._setup_manager()
        mgr.set_budget(RiskBudget(
            strategy_id=sid, max_drawdown_pct=20.0,
            min_sharpe_rolling=0.3,
        ))
        result = mgr.update_sharpe(sid, 0.1)
        assert result["action"] == "DEGRADE"

    def test_reset_circuit_breaker(self):
        mgr, sid = self._setup_manager()
        state = mgr.get_state(sid)
        state.peak_equity = 100.0
        mgr.update_equity(sid, 78.0)  # triggers LOCKED
        assert mgr.get_risk_level(sid) == StrategyRiskLevel.LOCKED

        assert mgr.reset_circuit_breakers(sid, "manual override")
        assert mgr.get_risk_level(sid) == StrategyRiskLevel.NORMAL

    def test_daily_reset(self):
        mgr, sid = self._setup_manager()
        state = mgr.get_state(sid)
        state.peak_equity = 1000.0
        state.daily_pnl = -200.0
        state.daily_loss_pct = 20.0

        mgr.reset_daily_pnl()
        assert state.daily_pnl == 0.0
        assert state.daily_loss_pct == 0.0

    def test_no_budget_no_trading(self):
        mgr = StrategyRiskManager()
        assert not mgr.is_trading_allowed(StrategyId("nonexistent"))

    def test_is_new_position_allowed(self):
        mgr, sid = self._setup_manager()
        state = mgr.get_state(sid)
        assert state.is_new_position_allowed()  # NORMAL

        state.risk_level = StrategyRiskLevel.CAUTION
        assert not state.is_new_position_allowed()

    def test_circuit_breaker_log(self):
        mgr, sid = self._setup_manager()
        state = mgr.get_state(sid)
        state.peak_equity = 100.0
        mgr.update_equity(sid, 78.0)  # LOCKED
        log = mgr.get_circuit_breaker_log()
        assert len(log) == 1
        assert log[0]["reason"] == "DRAWDOWN_LIMIT"
