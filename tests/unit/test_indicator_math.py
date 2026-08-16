"""指标数学性质与交叉验证测试（M03 + M03-R2）。

M03-R2（对抗审查）: 旧测试直接从生产模块导入 _ema_series/_wilder_smooth_last
作"独立手算" —— 循环自引用,helper 有系统性 bug 时测试依然全绿。重写为:
- 参考实现在本文件内独立定义（不同代码结构,同一数学约定）
- 长序列收敛性断言（两种约定在 n 足够大时一致）
- RSI 权威实现契约测试（扁平→50、全跌→0、NaN→NOT_VERIFIABLE）
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from beidou_core.feed import MarketDataFeed
from beidou_research.factors.rsi import RSIVerifiability, compute_rsi_simple, compute_rsi_wilder


def _independent_ema_from_first(values: list[float], period: int) -> list[float]:
    """独立 EMA 实现（结构不同: 从第一个值起递归,无 seed 数组填充）。

    与生产 _ema_series（SMA seed + 填充）的约定不同,但在长序列上两者
    指数收敛 —— 用收敛性断言替代同源比较。
    """
    alpha = 2.0 / (period + 1)
    ema = values[0]
    out = [ema]
    for value in values[1:]:
        ema = alpha * value + (1 - alpha) * ema
        out.append(ema)
    return out


def _independent_wilder(values: list[float], period: int) -> float:
    """独立 Wilder 平滑（显式前值变量,无切片）—— 与生产同约定不同结构。"""
    if not values:
        return 0.0
    if len(values) <= period:
        return sum(values) / len(values)
    smoothed = sum(values[:period]) / period
    idx = period
    while idx < len(values):
        smoothed = (smoothed * (period - 1) + values[idx]) / period
        idx += 1
    return smoothed


def _bars(n: int = 120, seed: float = 100.0, drift: float = 0.5) -> tuple[list[dict], list[float]]:
    """构造确定性的 1h 闭合 bar 序列（OHLC 一致性保证）。"""
    now = datetime.now(timezone.utc)
    closes: list[float] = []
    price = seed
    for i in range(n):
        price = price * (1 + drift / 100 * (1 if i % 3 else -1)) + (i % 5)
        closes.append(price)
    bars = []
    for i, close in enumerate(closes):
        open_t = now - timedelta(hours=n - i)
        bar_open = closes[i - 1] if i else seed
        bars.append(
            {
                "open_time": open_t,
                "close_time": open_t + timedelta(hours=1),
                "open": bar_open,
                "high": max(bar_open, close) * 1.01,
                "low": min(bar_open, close) * 0.99,
                "close": close,
                "volume": 10.0,
                "is_closed": True,
            }
        )
    return bars, closes


# --- RSI 权威实现契约（M03-R2: 扁平/全跌/NaN 反例） ---


def test_rsi_flat_series_is_neutral_50() -> None:
    """M03-R2: 扁平市场（全同价格）→ 中性 50,不是超买 100。"""
    result = compute_rsi_wilder([100.0] * 25, period=14)
    assert result.is_valid
    assert result.value == 50.0
    assert compute_rsi_simple([100.0] * 25, period=14).value == 50.0


def test_rsi_all_down_is_0() -> None:
    """M03-R2: 全跌 → 0（旧测试名声称覆盖全跌但从未实现）。"""
    prices = [100.0 - i for i in range(25)]
    result = compute_rsi_wilder(prices, period=14)
    assert result.is_valid
    assert result.value == 0.0


def test_rsi_all_up_is_100() -> None:
    prices = [100.0 + i for i in range(25)]
    assert compute_rsi_wilder(prices, period=14).value == 100.0


def test_rsi_nan_price_is_not_verifiable() -> None:
    """M03-R2: 非有限价格 → NOT_VERIFIABLE,绝不伪造 VERIFIED。"""
    prices = [100.0 + i for i in range(25)]
    prices[10] = float("nan")
    result = compute_rsi_wilder(prices, period=14)
    assert result.verifiability is RSIVerifiability.NOT_VERIFIABLE


def test_rsi_non_positive_price_is_not_verifiable() -> None:
    prices = [100.0 + i for i in range(25)]
    prices[10] = -5.0
    assert compute_rsi_wilder(prices, period=14).verifiability is RSIVerifiability.NOT_VERIFIABLE


def test_rsi_insufficient_data_is_not_verifiable() -> None:
    result = compute_rsi_wilder([100.0, 101.0, 102.0], period=14)
    assert result.verifiability is RSIVerifiability.NOT_VERIFIABLE


# --- feed 指标与独立参考的收敛性验证 ---


def test_feed_rsi_matches_wilder_reference() -> None:
    bars, closes = _bars()
    feed = MarketDataFeed()
    features = feed._compute_kline_features("BTCUSDT", "1h", bars)
    reference = compute_rsi_wilder(closes, period=14)
    assert reference.is_valid
    assert abs(features["rsi_14"] - reference.value) <= 0.01


def test_feed_ema_converges_to_independent_implementation() -> None:
    """M03-R2: 生产 EMA 序列与独立实现（不同约定）在长序列上指数收敛。

    seed 效应衰减 ~(1-α)^(n-period);n=300 时 EMA26 衰减到 1e-9 量级。
    """
    bars, closes = _bars(n=300)
    feed = MarketDataFeed()
    features = feed._compute_kline_features("BTCUSDT", "1h", bars)
    independent_12 = _independent_ema_from_first(closes, 12)
    independent_26 = _independent_ema_from_first(closes, 26)
    assert abs(features["ema_12"] - independent_12[-1]) < 1e-6
    assert abs(features["ema_26"] - independent_26[-1]) < 1e-6


def test_feed_macd_signal_converges_to_independent_implementation() -> None:
    """M03-R2: MACD 线/信号与独立实现（from-first 约定）收敛一致（n=300）。"""
    bars, closes = _bars(n=300)
    feed = MarketDataFeed()
    features = feed._compute_kline_features("BTCUSDT", "1h", bars)
    ema12 = _independent_ema_from_first(closes, 12)
    ema26 = _independent_ema_from_first(closes, 26)
    macd_line = [a - b for a, b in zip(ema12, ema26, strict=True)]
    signal = _independent_ema_from_first(macd_line, 9)
    assert abs(features["macd"] - macd_line[-1]) < 1e-6
    assert abs(features["macd_signal"] - signal[-1]) < 1e-6


def test_feed_atr_matches_independent_wilder() -> None:
    """M03-R2: ATR 与独立 Wilder 实现（同约定不同结构）一致。"""
    bars, closes = _bars(n=120)
    feed = MarketDataFeed()
    features = feed._compute_kline_features("BTCUSDT", "1h", bars)
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    trs = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, len(bars))
    ]
    expected_atr = _independent_wilder(trs, 14)
    assert abs(features["atr_pct"] - expected_atr / closes[-1] * 100) < 1e-9


def test_annualization_scales_with_interval() -> None:
    """M03-F04: 年化波动率按 interval 换算 —— 比例等于 bars_per_year 开方之比。"""
    bars, _closes = _bars(n=40)
    feed = MarketDataFeed()
    f1h = feed._compute_kline_features("BTCUSDT", "1h", bars)["ann_volatility"]
    f4h = feed._compute_kline_features("BTCUSDT", "4h", bars)["ann_volatility"]
    f1d = feed._compute_kline_features("BTCUSDT", "1d", bars)["ann_volatility"]
    assert math.isclose(f1h / f4h, (8760 / 2190) ** 0.5, rel_tol=1e-9)
    assert math.isclose(f1h / f1d, (8760 / 365) ** 0.5, rel_tol=1e-9)
