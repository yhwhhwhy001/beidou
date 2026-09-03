"""Vectorised feature primitives on wide frames (time x symbol).  All functions are causal."""

from __future__ import annotations

import math
from collections.abc import Callable

import numpy as np
import pandas as pd


def apply_numpy(frame: pd.DataFrame, function: Callable[[np.ndarray], np.ndarray]) -> pd.DataFrame:
    """Apply an elementwise numpy function and keep the frame's index/columns."""
    return pd.DataFrame(function(frame.to_numpy(dtype=float)), index=frame.index, columns=frame.columns)


def returns(close: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Simple return over ``horizon`` bars: close_t / close_{t-h} - 1."""
    return close / close.shift(horizon) - 1.0


def log_returns(close: pd.DataFrame, horizon: int = 1) -> pd.DataFrame:
    return apply_numpy(close / close.shift(horizon), np.log)


def realized_vol(close: pd.DataFrame, window: int | None = 48, ddof: int = 0) -> pd.DataFrame:
    """Std of 1-bar simple returns; ``window=None`` uses an expanding window (legacy TrendAlpha convention)."""
    simple = close.pct_change()
    if window is None:
        return simple.expanding().std(ddof=ddof)
    return simple.rolling(window, min_periods=max(2, window // 2)).std(ddof=ddof)


def ewm_vol(close: pd.DataFrame, halflife: int = 24) -> pd.DataFrame:
    simple = close.pct_change()
    return simple.pow(2).ewm(halflife=halflife, min_periods=halflife).mean().pow(0.5)


def annualize_vol(vol: pd.DataFrame, bars_per_year: float) -> pd.DataFrame:
    return vol * math.sqrt(bars_per_year)


def true_range(high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame) -> pd.DataFrame:
    previous_close = close.shift(1)
    ranges = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
        keys=["hl", "hc", "lc"],
    )
    return ranges.T.groupby(level=1).max().T


def atr(high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame, window: int = 14) -> pd.DataFrame:
    return true_range(high, low, close).rolling(window, min_periods=window).mean()


def donchian(high: pd.DataFrame, low: pd.DataFrame, window: int = 20) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Highest high / lowest low of the *previous* ``window`` bars (excludes the current bar)."""
    upper = high.shift(1).rolling(window, min_periods=window).max()
    lower = low.shift(1).rolling(window, min_periods=window).min()
    return upper, lower


def zscore(frame: pd.DataFrame, window: int, ddof: int = 0) -> pd.DataFrame:
    rolling = frame.rolling(window, min_periods=window)
    std = rolling.std(ddof=ddof)
    return (frame - rolling.mean()) / std.where(std > 0)


def robust_zscore(frame: pd.DataFrame, window: int) -> pd.DataFrame:
    """(x - median) / (1.4826 * MAD) over a rolling window."""
    rolling = frame.rolling(window, min_periods=window)
    median = rolling.median()
    mad = (frame - median).abs().rolling(window, min_periods=window).median() * 1.4826
    return (frame - median) / mad.where(mad > 0)


def cross_sectional_rank(frame: pd.DataFrame) -> pd.DataFrame:
    """Rank across symbols per bar, mapped to [-1, 1]; NaN stays NaN."""
    ranks = frame.rank(axis=1, method="average")
    count = frame.notna().sum(axis=1)
    scaled = (ranks.sub(1.0, axis=0)).div((count - 1.0).where(count > 1), axis=0)
    return scaled * 2.0 - 1.0


def cross_sectional_zscore(frame: pd.DataFrame) -> pd.DataFrame:
    mean = frame.mean(axis=1)
    std = frame.std(axis=1, ddof=0)
    return frame.sub(mean, axis=0).div(std.where(std > 0), axis=0)


def demean_cross_section(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.sub(frame.mean(axis=1), axis=0)


def rolling_beta(asset_returns: pd.DataFrame, benchmark_returns: pd.Series, window: int) -> pd.DataFrame:
    """OLS beta of each column on the benchmark over a rolling window."""
    bench = benchmark_returns.reindex(asset_returns.index)
    cov = asset_returns.rolling(window, min_periods=window).cov(bench)
    var = bench.rolling(window, min_periods=window).var()
    return cov.div(var.where(var > 0), axis=0)


def residual_returns(asset_returns: pd.DataFrame, benchmark_returns: pd.Series, beta: pd.DataFrame) -> pd.DataFrame:
    return asset_returns - beta.mul(benchmark_returns.reindex(asset_returns.index), axis=0)


def volume_ratio(volume: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """Current bar volume relative to the trailing mean of the previous ``window`` bars."""
    baseline = volume.shift(1).rolling(window, min_periods=window).mean()
    return volume / baseline.where(baseline > 0)


def taker_buy_ratio(taker_buy_quote: pd.DataFrame, quote_volume: pd.DataFrame, window: int = 1) -> pd.DataFrame:
    """Share of taker-buy quote volume, smoothed over ``window`` bars; 0.5 is balanced."""
    buys = taker_buy_quote.rolling(window, min_periods=window).sum()
    total = quote_volume.rolling(window, min_periods=window).sum()
    return buys / total.where(total > 0)


def funding_per_bar_to_8h(funding: pd.DataFrame, window_bars: int) -> pd.DataFrame:
    """Sum of settled funding over a trailing window (fraction), e.g. 24 bars of 1h = 3 settlements."""
    return funding.rolling(window_bars, min_periods=1).sum()


def clip_unit(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.clip(-1.0, 1.0)
