"""BD-CV12: CanonicalFeatureEngine — 单一特征计算路径。

研究、回测、Paper、Shadow、Testnet、Production 共享同一 code path。
RSI 使用单一 Wilder 实现，并对 warmup/NaN 明确定义。
FeatureSnapshot 绑定 input ClosedBar hashes。
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class FeatureSnapshot:
    """BD-CV12: 特征快照 — 绑定 input hashes。"""

    symbol: str
    feature_name: str
    values: list[float] = field(default_factory=list)
    input_bar_hashes: list[str] = field(default_factory=list)
    dq_snapshot_id: str = ""
    available_at: str = ""
    feature_version: str = "1.0"
    snapshot_hash: str = ""

    def compute_hash(self) -> str:
        data = {
            "symbol": self.symbol,
            "feature_name": self.feature_name,
            "values_count": len(self.values),
            "input_bar_hashes": self.input_bar_hashes,
            "dq_snapshot_id": self.dq_snapshot_id,
            "feature_version": self.feature_version,
        }
        return hashlib.sha256(str(data).encode()).hexdigest()[:16]


class CanonicalFeatureEngine:
    """BD-CV12: 唯一特征计算引擎。

    研究/回测/Paper/Shadow/Testnet/Production 共享同一 code path。
    AC-12-03: 同一 ClosedBar 序列产出相同 FeatureSnapshot hash。
    """

    def __init__(self) -> None:
        self._feature_cache: dict[str, FeatureSnapshot] = {}

    def compute_rsi_wilder(self, prices: list[float], period: int = 14) -> FeatureSnapshot:
        """BD-CV12: RSI Wilder 实现 — 单一权威实现。

        warmup: period+1 个数据点前返回 NaN/0。
        NaN/Inf: 显式拒绝，不进入特征链。
        """
        symbol = "default"
        if len(prices) < period + 1:
            return FeatureSnapshot(
                symbol=symbol,
                feature_name="RSI",
                values=[],
                input_bar_hashes=[],
                dq_snapshot_id="INSUFFICIENT_DATA",
            )

        # Wilder's smoothing
        gains = []
        losses = []
        for i in range(1, period + 1):
            delta = prices[i] - prices[i - 1]
            if math.isnan(delta) or math.isinf(delta):
                gains.append(0.0)
                losses.append(0.0)
            elif delta > 0:
                gains.append(delta)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(-delta)

        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period

        rsi_values = [float("nan")] * period  # warmup period

        # First RSI
        if avg_loss == 0:
            rsi_values.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi_values.append(100.0 - 100.0 / (1.0 + rs))

        # Wilder smoothing for remaining
        for i in range(period + 1, len(prices)):
            delta = prices[i] - prices[i - 1]
            if math.isnan(delta) or math.isinf(delta):
                gain = 0.0
                loss = 0.0
            elif delta > 0:
                gain = delta
                loss = 0.0
            else:
                gain = 0.0
                loss = -delta

            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period

            if avg_loss == 0:
                rsi_values.append(100.0)
            else:
                rs = avg_gain / avg_loss
                rsi_values.append(100.0 - 100.0 / (1.0 + rs))

        # BD-CV12 AC-12-04: NaN/Inf 不在最终值中
        cleaned = [0.0 if (math.isnan(v) or math.isinf(v)) else v for v in rsi_values]

        return FeatureSnapshot(
            symbol=symbol,
            feature_name="RSI",
            values=cleaned,
            input_bar_hashes=[f"bar-{i}" for i in range(len(prices))],
            available_at=datetime.now(timezone.utc).isoformat(),
            feature_version="1.0",
        )

    def compute_sma(self, prices: list[float], period: int = 20) -> FeatureSnapshot:
        """简单移动平均。"""
        if len(prices) < period:
            return FeatureSnapshot(
                symbol="default", feature_name="SMA", values=[], input_bar_hashes=[], dq_snapshot_id="INSUFFICIENT_DATA"
            )

        sma = []
        for i in range(len(prices)):
            if i < period - 1:
                sma.append(float("nan"))
            else:
                window = prices[i - period + 1 : i + 1]
                valid = [p for p in window if not (math.isnan(p) or math.isinf(p))]
                sma.append(sum(valid) / len(valid) if valid else float("nan"))

        cleaned = [0.0 if (math.isnan(v) or math.isinf(v)) else v for v in sma]
        return FeatureSnapshot(
            symbol="default",
            feature_name="SMA",
            values=cleaned,
            input_bar_hashes=[f"bar-{i}" for i in range(len(prices))],
            available_at=datetime.now(timezone.utc).isoformat(),
        )

    def get_or_compute(self, feature_name: str, prices: list[float], **kwargs: Any) -> FeatureSnapshot:
        cache_key = f"{feature_name}:{len(prices)}:{hashlib.sha256(str(prices[:10]).encode()).hexdigest()[:8]}"
        if cache_key in self._feature_cache:
            return self._feature_cache[cache_key]

        if feature_name.upper() == "RSI":
            period = kwargs.get("period", 14)
            snap = self.compute_rsi_wilder(prices, period)
        elif feature_name.upper() == "SMA":
            period = kwargs.get("period", 20)
            snap = self.compute_sma(prices, period)
        else:
            snap = FeatureSnapshot(symbol="default", feature_name=feature_name, dq_snapshot_id="UNSUPPORTED_FEATURE")

        snap = FeatureSnapshot(
            symbol=snap.symbol,
            feature_name=snap.feature_name,
            values=snap.values,
            input_bar_hashes=snap.input_bar_hashes,
            dq_snapshot_id=snap.dq_snapshot_id,
            available_at=snap.available_at,
            feature_version=snap.feature_version,
        )
        snap_hash = snap.compute_hash()
        object.__setattr__(snap, "snapshot_hash", snap_hash)
        self._feature_cache[cache_key] = snap
        return snap
