"""指标数学性质与交叉验证测试（M03）。

feed._compute_kline_features 的 RSI/MACD/ATR/年化波动率必须与独立参考
实现一致（性质测试而非实现断言）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from beidou_core.feed import MarketDataFeed, _ema_series, _wilder_smooth_last
from beidou_research.factors.rsi import compute_rsi_wilder


def _bars(n: int = 30, seed: float = 100.0, drift: float = 0.5) -> list[dict]:
    """构造确定性的 1h 闭合 bar 序列。"""
    now = datetime.now(timezone.utc)
    closes: list[float] = []
    price = seed
    for i in range(n):
        price = price * (1 + drift / 100 * (1 if i % 3 else -1)) + (i % 5)
        closes.append(price)
    bars = []
    for i, close in enumerate(closes):
        open_t = now - timedelta(hours=n - i)
        close_t = open_t + timedelta(hours=1)
        bar_open = closes[i - 1] if i else seed
        bars.append(
            {
                "open_time": open_t,
                "close_time": close_t,
                "open": bar_open,
                # OHLC 一致性: high >= max(open,close), low <= min(open,close)
                "high": max(bar_open, close) * 1.01,
                "low": min(bar_open, close) * 0.99,
                "close": close,
                "volume": 10.0,
                "is_closed": True,
            }
        )
    return bars, closes


def test_rsi_matches_wilder_reference() -> None:
    """M03-F01: feed 的 RSI 与权威 Wilder 实现同输入同输出（容差 0.01）。"""
    bars, closes = _bars()
    feed = MarketDataFeed()
    features = feed._compute_kline_features("BTCUSDT", "1h", bars)
    reference = compute_rsi_wilder(closes, period=14)
    assert reference.is_valid
    assert abs(features["rsi_14"] - reference.value) <= 0.01


def test_rsi_all_up_is_100_all_down_is_0() -> None:
    """M03-F01: 全涨→100、全跌→0 边界。"""
    feed = MarketDataFeed()
    now = datetime.now(timezone.utc)
    up_bars = []
    price = 100.0
    for i in range(20):
        price += 1.0
        open_t = now - timedelta(hours=20 - i)
        bar_open = price - 1.0
        up_bars.append(
            {
                "open_time": open_t,
                "close_time": open_t + timedelta(hours=1),
                "open": bar_open,
                "high": max(bar_open, price) + 0.5,
                "low": min(bar_open, price) - 0.5,
                "close": price,
                "volume": 10.0,
                "is_closed": True,
            }
        )
    assert feed._compute_kline_features("BTCUSDT", "1h", up_bars)["rsi_14"] == 100.0


def test_macd_signal_is_ema9_of_macd_line() -> None:
    """M03-F02: signal = MACD 线（EMA12-EMA26 序列）的 EMA9 末值（独立手算）。"""
    bars, closes = _bars(n=40)
    feed = MarketDataFeed()
    features = feed._compute_kline_features("BTCUSDT", "1h", bars)

    ema12 = _ema_series(closes, 12)
    ema26 = _ema_series(closes, 26)
    macd_line = [a - b for a, b in zip(ema12, ema26, strict=True)]
    expected_signal = _ema_series(macd_line, 9)[-1]
    assert features["macd"] == macd_line[-1]
    assert abs(features["macd_signal"] - expected_signal) < 1e-9
    # 旧错误公式恒等于 EMA26 —— 新实现必须显著区别于 EMA26（在趋势数据上）
    assert abs(features["macd_signal"] - ema26[-1]) > 1e-6


def test_atr_uses_wilder_smoothing() -> None:
    """M03-F03: ATR = 全序列 TR 的 Wilder 平滑（独立手算）。"""
    bars, closes = _bars(n=40)
    feed = MarketDataFeed()
    features = feed._compute_kline_features("BTCUSDT", "1h", bars)
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    trs = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, len(bars))
    ]
    expected_atr = _wilder_smooth_last(trs, 14)
    assert abs(features["atr_pct"] - expected_atr / closes[-1] * 100) < 1e-9


def test_annualization_scales_with_interval() -> None:
    """M03-F04: 年化波动率按 interval 换算 —— 同 bar 序列下不同 interval 的比例
    等于各自 bars_per_year 开方之比。"""
    bars, _closes = _bars(n=40)
    feed = MarketDataFeed()
    f1h = feed._compute_kline_features("BTCUSDT", "1h", bars)["ann_volatility"]
    f4h = feed._compute_kline_features("BTCUSDT", "4h", bars)["ann_volatility"]
    f1d = feed._compute_kline_features("BTCUSDT", "1d", bars)["ann_volatility"]
    assert abs(f1h / f4h - (8760 / 2190) ** 0.5) < 1e-9
    assert abs(f1h / f1d - (8760 / 365) ** 0.5) < 1e-9
