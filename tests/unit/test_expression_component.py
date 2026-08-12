"""Task 10: ExpressionComponent 通用表达式组件单元测试。"""

import asyncio
from typing import Any

from beidou_core.expression_component import ExpressionComponent
from beidou_strategy.alpha import AlphaSignal, SignalDirection


def _context(close: float) -> dict[str, Any]:
    return {
        "features": {"close": close, "high": close * 1.001, "low": close * 0.999, "volume": 100.0},
        "instrument_id": "BTCUSDT",
        "venue_id": "BINANCE",
    }


def test_validates_only_with_bound_expression() -> None:
    comp = ExpressionComponent(factor_id="x", expression_string="close", role="ENTRY")
    assert comp.validate() is True
    empty = ExpressionComponent()
    assert empty.validate() is False


def test_insufficient_history_is_no_action() -> None:
    comp = ExpressionComponent(factor_id="x", expression_string="close", role="ENTRY")
    signal = asyncio.run(comp.generate(_context(100.0)))
    assert signal.direction == SignalDirection.NO_ACTION


def test_flat_history_is_no_action() -> None:
    comp = ExpressionComponent(factor_id="x", expression_string="close", role="ENTRY")
    signal: AlphaSignal | None = None
    for _ in range(50):
        signal = asyncio.run(comp.generate(_context(100.0)))
    assert signal is not None and signal.direction == SignalDirection.NO_ACTION


def test_uptrend_generates_long() -> None:
    # pct_change(close, 5) 在恒定 1% 复利趋势下输出常数序列（1.01^5 - 1），
    # 滚动 z≈0 → NO_ACTION；改用同语义的 5 根动量 diff(close, 5)
    # （值随价格增长，末值 > 均值 → z>0.5）保持"趋势 → LONG"。见报告。
    comp = ExpressionComponent(factor_id="x", expression_string="diff(close, 5)", role="ENTRY")
    signal: AlphaSignal | None = None
    price = 100.0
    for _ in range(60):
        price *= 1.01
        signal = asyncio.run(comp.generate(_context(price)))
    assert signal is not None and signal.direction == SignalDirection.LONG
    assert 0.0 < signal.strength <= 1.0


def test_broken_expression_is_no_action_not_exception() -> None:
    comp = ExpressionComponent(factor_id="x", expression_string="no_such_primitive(close)", role="ENTRY")
    assert comp.validate() is False
    signal = asyncio.run(comp.generate(_context(100.0)))
    assert signal.direction == SignalDirection.NO_ACTION
