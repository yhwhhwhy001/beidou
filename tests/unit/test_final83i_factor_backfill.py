"""final83i 回归:因子历史回填 — 重启后免 60 分钟预热。

ExpressionComponent 首轮求值前从注入的异步 K 线源重建价格/因子值历史;
回填失败不抛异常且每 (symbol, timeframe) 至多尝试一次;与当前闭合 bar
重合的尾部去重;feed.async_get_klines_raw 只保留闭合 bar 且按 open_time
升序。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from beidou_core.expression_component import ExpressionComponent
from beidou_core.feed import MarketDataFeed, MarketDataUnknownError
from beidou_strategy.alpha import SignalDirection


def _bars(n: int = 150, *, start_close: float = 1.0, step: float = 0.01) -> list[dict]:
    """单调上升的闭合 bar 序列(最后收盘显著高于均值 → z 强正)。"""
    t0 = datetime(2026, 8, 18, 8, 0, tzinfo=timezone.utc)
    bars = []
    for i in range(n):
        close = start_close + step * i
        bars.append(
            {
                "open_time": t0 + timedelta(minutes=i),
                "close_time": t0 + timedelta(minutes=i + 1) - timedelta(milliseconds=1),
                "open": close - step / 2,
                "high": close + step / 2,
                "low": close - step,
                "close": close,
                "volume": 100.0,
                "quote_volume": 100.0 * close,
                "trades": 1,
                "is_closed": True,
            }
        )
    return bars


def _component() -> ExpressionComponent:
    return ExpressionComponent(factor_id="f1", expression_string="close", role="ENTRY")


@pytest.mark.asyncio
async def test_backfill_seeds_history_and_signals_immediately() -> None:
    """回填后首轮求值即可出信号(重启免预热)。"""
    bars = _bars()
    comp = _component()
    comp.set_backfill_source(AsyncMock(return_value=bars))
    context = {
        "features": {"close": bars[-1]["close"], "high": bars[-1]["high"], "low": bars[-1]["low"], "volume": 100.0},
        "instrument_id": "AVAXUSDT",
        "timeframe": "1m",
    }
    signal = await comp.generate(context)

    assert signal.direction == SignalDirection.LONG
    assert signal.strength > 0
    # 回填源只调用一次(幂等)
    comp._backfill_source.assert_awaited_once()
    # 尾部去重:当前闭合 bar 与回填尾部重合,不双计
    hist = comp._histories["1m"]
    assert len(hist["close"]) == len(bars)
    # 第二次调用(新 bar)正常追加
    next_close = bars[-1]["close"] + 0.01
    signal2 = await comp.generate({**context, "features": {**context["features"], "close": next_close}})
    assert len(hist["close"]) == len(bars) + 1
    assert comp._backfill_source.await_count == 1


@pytest.mark.asyncio
async def test_backfill_failure_is_silent_and_not_retried() -> None:
    """回填异常不抛出;同一 (symbol,tf) 不重试;继续实时积累。"""
    source = AsyncMock(side_effect=RuntimeError("boom"))
    comp = _component()
    comp.set_backfill_source(source)
    context = {
        "features": {"close": 1.0, "high": 1.0, "low": 1.0, "volume": 1.0},
        "instrument_id": "AVAXUSDT",
        "timeframe": "1m",
    }
    for _ in range(3):
        signal = await comp.generate(context)
        assert signal.direction == SignalDirection.NO_ACTION
    assert source.await_count == 1
    hist = comp._histories["1m"]
    assert len(hist["close"]) == 3  # 实时积累照常


@pytest.mark.asyncio
async def test_backfill_skipped_when_history_already_sufficient() -> None:
    """历史已足够时不调用回填源。"""
    source = AsyncMock()
    comp = _component()
    comp.set_backfill_source(source)
    bars = _bars(60)
    hist = comp._hist_for("1m")
    for b in bars:
        hist["close"].append(b["close"])
        hist["high"].append(b["high"])
        hist["low"].append(b["low"])
        hist["volume"].append(b["volume"])
    context = {
        "features": {"close": 2.0, "high": 2.0, "low": 2.0, "volume": 1.0},
        "instrument_id": "AVAXUSDT",
        "timeframe": "1m",
    }
    await comp.generate(context)
    source.assert_not_awaited()


@pytest.mark.asyncio
async def test_feed_raw_klines_closed_only_sorted() -> None:
    """async_get_klines_raw 过滤形成中 bar,按 open_time 升序返回。

    时间基准取当前分钟(避免固定墙钟时间随日期推移而失效):
    前 3 根为已闭合 bar,当前分钟为形成中 bar。
    """
    import time as _time

    feed = MarketDataFeed.__new__(MarketDataFeed)
    _minute_ms = 60_000
    _now_ms = int(_time.time() * 1000)
    _cur_min = (_now_ms // _minute_ms) * _minute_ms
    _open_closed = [_cur_min - 3 * _minute_ms, _cur_min - 2 * _minute_ms, _cur_min - _minute_ms]
    raw = [
        [t, "1", "2", "0.5", "1.5", "10", t + 59_000, "15", 3, "15", "0", "0", "1"]
        for t in _open_closed
    ]
    raw.append(
        [_cur_min, "2.5", "3.5", "2", "3", "10", _cur_min + _minute_ms, "30", 3, "30", "0", "0", "1"]  # 形成中
    )
    feed._api_async = AsyncMock(return_value=raw)
    rows = await feed.async_get_klines_raw("AVAXUSDT", "1m", 100)
    expected = [
        datetime.fromtimestamp(t / 1000, tz=timezone.utc)
        for t in _open_closed
    ]
    assert len(rows) == 3
    assert [r["open_time"] for r in rows] == expected
    assert all(r["is_closed"] for r in rows)


@pytest.mark.asyncio
async def test_feed_raw_klines_error_response_raises() -> None:
    feed = MarketDataFeed.__new__(MarketDataFeed)
    feed._api_async = AsyncMock(return_value={"error": -1, "msg": "boom"})
    with pytest.raises(MarketDataUnknownError):
        await feed.async_get_klines_raw("AVAXUSDT")
