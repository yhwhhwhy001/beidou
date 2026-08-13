"""Task 10: ExpressionComponent 通用表达式组件单元测试。"""

import asyncio
import concurrent.futures
from typing import Any

from beidou_core import expression_component
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


def test_history_window_capped_at_800() -> None:
    """窗口上限：连续喂 1000 根 close 后，四个历史 deque 均收在 _HISTORY_LIMIT(800)。"""
    comp = ExpressionComponent(factor_id="x", expression_string="close", role="ENTRY")

    async def feed() -> None:
        for i in range(1000):
            await comp.generate(_context(100.0 + i))

    asyncio.run(feed())
    assert len(comp._history) == 800
    assert len(comp._high_history) == 800
    assert len(comp._low_history) == 800
    assert len(comp._volume_history) == 800
    # 仅保留最近 800 根（丢弃最早 200 根）
    assert comp._history[0] == 100.0 + 200.0
    assert comp._history[-1] == 100.0 + 999.0


def test_generate_runs_in_event_loop_with_capped_history() -> None:
    """executor 路径：generate 在事件循环内正常求值（求值语义不变），历史仅含最近 800 根。"""
    comp = ExpressionComponent(factor_id="x", expression_string="diff(close, 5)", role="ENTRY")

    async def drive() -> AlphaSignal:
        signal: AlphaSignal | None = None
        price = 100.0
        for _ in range(1000):
            price *= 1.01
            signal = await comp.generate(_context(price))
        assert signal is not None
        return signal

    signal = asyncio.run(drive())
    assert signal.direction == SignalDirection.LONG
    assert 0.0 < signal.strength <= 1.0
    assert len(comp._history) == 800
    assert len(comp._value_history) == 100  # z 窗口不受历史窗口缩小影响
    assert comp._history[0] > 100.0  # 最早 200 根已被丢弃


def test_eval_executor_is_dedicated_pool_of_4() -> None:
    """求值走专用线程池：与 rest_client 的 asyncio.to_thread 默认池隔离（修复循环 90s 退化）。"""
    executor = expression_component._EVAL_EXECUTOR
    assert isinstance(executor, concurrent.futures.ThreadPoolExecutor)
    assert executor._max_workers == 4
    assert executor._thread_name_prefix == "expr-eval"
