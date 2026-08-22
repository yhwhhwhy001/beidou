"""Remaining behavior coverage for expression validation and strategy risk state."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import beidou_research.mining.expression_ast as expr
from beidou_shared.types import StrategyId
from beidou_strategy.risk.manager import (
    CircuitBreakerReason,
    DrawdownMonitor,
    RiskBudget,
    StrategyRiskLevel,
    StrategyRiskManager,
    StrategyRiskState,
)


def _price() -> expr.Feature:
    return expr.Feature("close", expr.ExprType.PRICE)


def test_expression_helpers_and_leaf_error_contracts() -> None:
    with pytest.raises(expr.ExpressionError, match="未知表达式类型"):
        expr._parse_expr_type("NOT_A_TYPE")
    with pytest.raises(expr.ExpressionError, match="非法表达式字典"):
        expr.Expression.from_dict({})
    with pytest.raises(expr.ExpressionError, match="特征名"):
        expr.Feature("", expr.ExprType.PRICE)
    with pytest.raises(expr.ExpressionTypeError, match="非法 dtype"):
        expr.Constant(1.0, "SCALAR")
    with pytest.raises(expr.ExpressionTypeError, match="布尔常量"):
        expr.Constant(True)
    with pytest.raises(expr.ExpressionTypeError, match="数值常量"):
        expr.Constant(1.0, expr.ExprType.BOOLEAN)
    with pytest.raises(expr.ExpressionTypeError, match="特征类型"):
        expr.Feature("close", "PRICE")
    with pytest.raises(expr.ExpressionError, match="缺少特征"):
        _price().evaluate({})
    with pytest.raises(expr.ExpressionTypeError, match="必须为整数"):
        expr.Lag(_price(), True)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: expr.Diff(_price(), 0),
        lambda: expr.PctChange(_price(), 0),
        lambda: expr.RollingMean(_price(), 0),
        lambda: expr.RollingStd(_price(), 1),
        lambda: expr.RollingMedian(_price(), 0),
        lambda: expr.RollingMAD(_price(), 0),
        lambda: expr.RollingQuantile(_price(), 3, 2.0),
        lambda: expr.EMA(_price(), 0),
        lambda: expr.TsRank(_price(), 1),
        lambda: expr.ZScore(_price(), 1),
        lambda: expr.RobustZScore(_price(), 1),
    ],
)
def test_expression_window_validation(factory) -> None:
    with pytest.raises(expr.ExpressionTypeError):
        factory()


def test_expression_boolean_and_numeric_validation_matrix() -> None:
    boolean = expr.Constant(True, expr.ExprType.BOOLEAN)
    with pytest.raises(expr.ExpressionTypeError):
        expr.RollingMean(boolean, 2)
    with pytest.raises(expr.ExpressionTypeError):
        expr.RollingStd(boolean, 2)
    with pytest.raises(expr.ExpressionTypeError):
        expr.RollingMedian(boolean, 2)
    with pytest.raises(expr.ExpressionTypeError):
        expr.RollingMAD(boolean, 2)
    with pytest.raises(expr.ExpressionTypeError):
        expr.RollingQuantile(boolean, 2, 0.5)
    with pytest.raises(expr.ExpressionTypeError):
        expr.EMA(boolean, 2)
    with pytest.raises(expr.ExpressionTypeError):
        expr.TsRank(boolean, 2)
    with pytest.raises(expr.ExpressionTypeError):
        expr.ZScore(boolean, 2)
    with pytest.raises(expr.ExpressionTypeError):
        expr.RobustZScore(boolean, 2)
    with pytest.raises(expr.ExpressionTypeError):
        expr.CsRank(boolean)
    with pytest.raises(expr.ExpressionTypeError):
        expr.SafeDiv(boolean, expr.Constant(1.0))
    with pytest.raises(expr.ExpressionTypeError):
        expr.SignedLog1p(boolean)
    with pytest.raises(expr.ExpressionTypeError):
        expr.SignedSqrt(boolean)
    with pytest.raises(expr.ExpressionTypeError):
        expr.Clip(boolean, 0.0, 1.0)
    with pytest.raises(expr.ExpressionTypeError):
        expr.Residualize(_price(), (boolean,))
    with pytest.raises(expr.ExpressionTypeError):
        expr.Add(boolean, expr.Constant(1.0))
    with pytest.raises(expr.ExpressionTypeError):
        expr.Sub(boolean, expr.Constant(1.0))
    with pytest.raises(expr.ExpressionTypeError):
        expr.Mul(boolean, expr.Constant(1.0))


def test_expression_canonicalization_and_eval_edge_paths() -> None:
    close = _price()
    nested_add = expr.Add(expr.Add(close, expr.Constant(1.0)), expr.Add(expr.Constant(2.0), close))
    assert len(expr._flatten_add(nested_add)) == 4
    nested_mul = expr.Mul(expr.Mul(close, expr.Constant(2.0)), expr.Constant(3.0))
    assert len(expr._flatten_mul(nested_mul)) == 3
    assert expr.Sub(expr.Constant(0.0), close).canonicalize() == expr.Neg(close)
    assert expr.Sub(expr.Constant(5.0), expr.Constant(2.0)).canonicalize() == expr.Constant(3.0)
    assert expr.Add(expr.Constant(0.0), expr.Constant(0.0)).canonicalize() == expr.Constant(0.0)
    assert expr.Mul(expr.Constant(2.0), expr.Constant(3.0)).canonicalize() == expr.Constant(6.0)
    assert expr.Neg(expr.Constant(2.0)).canonicalize() == expr.Constant(-2.0)
    assert expr.Lt(expr.Constant(1.0), expr.Constant(2.0)).canonicalize() == expr.Constant(True, expr.ExprType.BOOLEAN)
    assert expr.Le(expr.Constant(1.0), expr.Constant(1.0)).canonicalize().value is True
    assert expr.Gt(expr.Constant(2.0), expr.Constant(1.0)).canonicalize().value is True
    assert expr.Ge(expr.Constant(2.0), expr.Constant(2.0)).canonicalize().value is True
    assert expr.Eq(expr.Constant(2.0), expr.Constant(2.0)).canonicalize().value is True
    assert expr.Ne(expr.Constant(2.0), expr.Constant(1.0)).canonicalize().value is True
    matrix = {"close": [[1.0, 2.0], [3.0, 4.0]]}
    assert expr.Neg(close).evaluate(matrix) == [[-1.0, -2.0], [-3.0, -4.0]]
    assert expr.Mul(close, expr.Constant(2.0)).evaluate(matrix) == [[2.0, 4.0], [6.0, 8.0]]


def test_residualize_empty_shape_and_singular_controls_are_observable() -> None:
    close = _price()
    assert expr.Residualize(close, ())._eval({"close": []}) == []
    assert expr.Residualize(close, (expr.Feature("volume", expr.ExprType.VOLUME),))._eval(
        {"close": [[1.0], [2.0]], "volume": [[1.0], [1.0]]}
    ) == [[1.0], [2.0]]
    result = expr.Residualize(close, (expr.Feature("volume", expr.ExprType.VOLUME),))._eval(
        {"close": [[1.0], [2.0], [3.0]], "volume": [[1.0], [1.0], [1.0]]}
    )
    assert result == [[1.0], [2.0], [3.0]]


def test_strategy_risk_budget_state_and_manager_boundaries(tmp_path) -> None:
    sid = StrategyId("risk")
    budget = RiskBudget(sid, max_drawdown_pct=10.0, max_daily_loss_pct=2.0, max_consecutive_losses=2)
    assert budget.compute_position_size(sid, 1000.0, 0.0, 99.0) == 0.0
    assert budget.compute_position_size(sid, 1000.0, 100.0, 100.0) == 0.0
    assert budget.compute_position_size(sid, 1000.0, 100.0, 90.0) > 0.0
    state = StrategyRiskState(
        sid, risk_level=StrategyRiskLevel.EXIT_ONLY, active_circuit_breakers=[CircuitBreakerReason.DRAWDOWN_LIMIT]
    )
    encoded = state.to_dict()
    encoded["risk_level"] = "NOT_A_LEVEL"
    restored = StrategyRiskState.from_dict(encoded)
    assert restored.risk_level is StrategyRiskLevel.NORMAL
    assert restored.active_circuit_breakers == [CircuitBreakerReason.DRAWDOWN_LIMIT]
    assert not state.is_trading_allowed() and not state.is_new_position_allowed()
    assert DrawdownMonitor.compute_drawdown(0.0, 0.0) == (0.0, 0.0)
    assert DrawdownMonitor.check_limit(0.0, 10.0)[1] is StrategyRiskLevel.NORMAL
    assert DrawdownMonitor.check_limit(3.0, 10.0)[1] is StrategyRiskLevel.CAUTION
    assert DrawdownMonitor.check_limit(5.0, 10.0)[1] is StrategyRiskLevel.REDUCED
    assert DrawdownMonitor.check_limit(7.0, 10.0)[1] is StrategyRiskLevel.EXIT_ONLY
    assert DrawdownMonitor.check_limit(10.0, 10.0)[1] is StrategyRiskLevel.LOCKED
    manager = StrategyRiskManager()
    assert manager.get_budget(sid) is None
    assert manager.compute_position_size(sid, 1000.0, 100.0, 90.0) == 0.0
    assert manager.update_equity(sid, 1000.0)["action"] == "NOOP"
    assert manager.record_trade(sid, -10.0, False)["action"] == "NOOP"
    assert manager.reset_circuit_breakers(sid) is False
    manager.set_budget(budget)
    assert manager.get_budget(sid) is budget
    assert manager.compute_position_size(sid, 1000.0, 100.0, 90.0) > 0.0
    assert manager.update_equity(sid, 1000.0)["action"] == "NOOP"
    manager._states[sid].last_updated = datetime.now(timezone.utc) - timedelta(days=1)
    degraded = manager.update_equity(sid, 890.0)
    assert degraded["action"] == "DEGRADE"
    manager.record_trade(sid, -20.0, False)
    manager.record_trade(sid, -20.0, False)
    assert manager.get_risk_level(sid) in {StrategyRiskLevel.LOCKED, StrategyRiskLevel.EXIT_ONLY}
    manager.update_sharpe(sid, -1.0)
    manager.reset_daily_pnl()
    assert manager.reset_circuit_breakers(sid, "operator") is True
    assert manager.is_trading_allowed(sid)

    class Store:
        def __init__(self):
            self.saved = []

        def save_strategy_risk_state(self, key, value):
            self.saved.append((key, value))

        def restore_strategy_risk_states(self):
            return {"risk": StrategyRiskState(sid).to_dict(), "bad": {"risk_level": "bad"}}

    store = Store()
    manager.save_state(store)
    assert store.saved
    manager.restore_state(store)
    assert manager.get_state(sid) is not None
    assert manager.strategy_count() == 1
    assert manager.get_all_states()[sid].strategy_id == sid
