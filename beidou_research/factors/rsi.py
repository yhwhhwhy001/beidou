"""
PKG06 (BDS-P0-006): RSI 指标修复 — 边界/性质测试与独立验证。

问题：原 RSI 实现（feed.py L835-839）中 gain/loss 序列长度不足 14
时使用不足长度的均值，且未处理全部上涨/下跌导致的极端情况。

修复：
1. 正确填充 gain/loss 序列（非对应方向填 0）
2. 不足最小期数时返回 NOT_VERIFIABLE 而非伪造值
3. 全涨/全跌场景正确处理（avg_loss=0 → RSI=100, avg_gain=0 → RSI=0）
4. NaN/Inf/极端序列的性质测试
5. 与权威实现（Wilder's RSI）的交叉验证契约
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


class RSIVerifiability(str, Enum):
    VERIFIED = "VERIFIED"
    NOT_VERIFIABLE = "NOT_VERIFIABLE"


@dataclass(frozen=True)
class RSIResult:
    """RSI 计算结果 — 包含可验证性状态。"""

    value: float
    avg_gain: float
    avg_loss: float
    n_periods_used: int
    verifiability: RSIVerifiability

    @property
    def is_valid(self) -> bool:
        return self.verifiability == RSIVerifiability.VERIFIED and math.isfinite(self.value)


def compute_rsi_wilder(
    prices: list[float],
    period: int = 14,
    min_periods: int = 15,  # 至少需要 period+1 个价格点
) -> RSIResult:
    """PKG06: Wilder's RSI 的正确实现。

    Wilder's RSI 使用 Wilder's smoothing（非 SMA），这是行业标准。

    Args:
        prices: 价格序列（收盘价）
        period: RSI 周期（默认 14）
        min_periods: 最少需要的价格点数（默认 period+1）

    Returns:
        RSIResult 包含 RSI 值、avg_gain、avg_loss 和可验证性状态

    RSI 公式:
        RSI = 100 - 100 / (1 + RS)
        RS = AvgGain / AvgLoss
        AvgGain = Wilder_smoothed(average of gains)
        AvgLoss = Wilder_smoothed(average of losses)
        Wilder smoothing: S_t = (prev_S * (period-1) + current_value) / period

    Edge cases:
        - 全部上涨 (avg_loss=0): RSI = 100
        - 全部下跌 (avg_gain=0): RSI = 0
        - 价格不变 (avg_gain=avg_loss=0): RSI = 50（中性）
        - 不足 min_periods: NOT_VERIFIABLE
    """
    n = len(prices)
    if n < min_periods:
        return RSIResult(
            value=50.0,
            avg_gain=0.0,
            avg_loss=0.0,
            n_periods_used=n,
            verifiability=RSIVerifiability.NOT_VERIFIABLE,
        )

    # Step 1: 计算价格变化
    # M03-R2（对抗审查反例）: 非有限/非正价格 → NOT_VERIFIABLE,绝不伪造
    # 0.0 变化后继续计算（旧实现对 NaN/负价序列照常返回 VERIFIED）。
    changes: list[float] = []
    for i in range(1, n):
        if not math.isfinite(prices[i]) or not math.isfinite(prices[i - 1]) or prices[i - 1] <= 0:
            return RSIResult(
                value=50.0,
                avg_gain=0.0,
                avg_loss=0.0,
                n_periods_used=n,
                verifiability=RSIVerifiability.NOT_VERIFIABLE,
            )
        changes.append(prices[i] - prices[i - 1])

    if len(changes) < period:
        return RSIResult(
            value=50.0,
            avg_gain=0.0,
            avg_loss=0.0,
            n_periods_used=len(changes),
            verifiability=RSIVerifiability.NOT_VERIFIABLE,
        )

    # Step 2: 初始 AvgGain / AvgLoss（简单均值）
    gains = [max(c, 0.0) for c in changes[:period]]
    losses = [abs(min(c, 0.0)) for c in changes[:period]]

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    # Step 3: Wilder smoothing for remaining periods
    for c in changes[period:]:
        gain = max(c, 0.0)
        loss = abs(min(c, 0.0))
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    # Step 4: Calculate RSI
    # M03-R2: 扁平序列（avg_gain==avg_loss==0）→ 中性 50,先于全涨/全跌
    # 判定（旧实现只有 avg_loss<1e-15 → 100 分支,扁平市场被误判超买）。
    if avg_loss < 1e-15 and avg_gain < 1e-15:
        rsi = 50.0
    elif avg_loss < 1e-15:
        # All gains, no losses → RSI = 100
        rsi = 100.0
    elif avg_gain < 1e-15:
        # All losses, no gains → RSI = 0
        rsi = 0.0
    else:
        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))

    # Clamp to valid range
    rsi = max(0.0, min(100.0, rsi))

    return RSIResult(
        value=round(rsi, 2),
        avg_gain=round(avg_gain, 6),
        avg_loss=round(avg_loss, 6),
        n_periods_used=len(changes),
        verifiability=RSIVerifiability.VERIFIED,
    )


def compute_rsi_simple(
    prices: list[float],
    period: int = 14,
) -> RSIResult:
    """PKG06: 简化版 RSI（SMA 平滑），供交叉验证对比。

    使用简单移动平均而非 Wilder smoothing。
    用于与 Wilder 版本交叉验证，确保两者在正常数据上高度一致。
    """
    n = len(prices)
    if n < period + 1:
        return RSIResult(
            value=50.0,
            avg_gain=0.0,
            avg_loss=0.0,
            n_periods_used=n,
            verifiability=RSIVerifiability.NOT_VERIFIABLE,
        )

    changes: list[float] = []
    for i in range(1, n):
        # M03-R2: 非有限/非正价格 → NOT_VERIFIABLE（与 Wilder 版一致）
        if not math.isfinite(prices[i]) or not math.isfinite(prices[i - 1]) or prices[i - 1] <= 0:
            return RSIResult(
                value=50.0,
                avg_gain=0.0,
                avg_loss=0.0,
                n_periods_used=n,
                verifiability=RSIVerifiability.NOT_VERIFIABLE,
            )
        changes.append(prices[i] - prices[i - 1])

    gains = [max(c, 0.0) for c in changes]
    losses = [abs(min(c, 0.0)) for c in changes]

    # Rolling window of last 'period' values
    recent_gains = gains[-period:]
    recent_losses = losses[-period:]

    avg_gain = sum(recent_gains) / period
    avg_loss = sum(recent_losses) / period

    # M03-R2: 扁平序列 → 中性 50（旧实现误判 100）
    if avg_loss < 1e-15 and avg_gain < 1e-15:
        rsi = 50.0
    elif avg_loss < 1e-15:
        rsi = 100.0
    elif avg_gain < 1e-15:
        rsi = 0.0
    else:
        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))

    return RSIResult(
        value=round(rsi, 2),
        avg_gain=round(avg_gain, 6),
        avg_loss=round(avg_loss, 6),
        n_periods_used=len(changes),
        verifiability=RSIVerifiability.VERIFIED,
    )


def cross_validate_rsi(prices: list[float], period: int = 14, tolerance: float = 1.0) -> dict:
    """PKG06: 两个 RSI 实现的交叉验证。

    Wilder 和 Simple 版本在正常数据上应高度一致（差异 < tolerance）。
    差异过大表明其中一个实现有 bug。

    Returns:
        dict: {wilder_rsi, simple_rsi, delta, is_consistent}
    """
    wilder = compute_rsi_wilder(prices, period)
    simple = compute_rsi_simple(prices, period)

    delta = abs(wilder.value - simple.value)
    is_consistent = delta <= tolerance

    return {
        "wilder_rsi": wilder.value,
        "simple_rsi": simple.value,
        "delta": round(delta, 2),
        "is_consistent": is_consistent,
        "both_verifiable": wilder.is_valid and simple.is_valid,
    }
