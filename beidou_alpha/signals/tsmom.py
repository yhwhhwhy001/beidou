"""Multi-horizon time-series momentum (vectorised port of the legacy ``TrendAlpha`` formula).

score = clip( (0.40*momentum + 0.25*slope + 0.10*persistence*direction) / 0.75 , -1, 1 )

momentum_h    = tanh(ret_h / max(vol, return_scale))
slope_h       = tanh((ret_h / h) / slope_scale)
momentum      = sum_h w_h * momentum_h / sum_h w_h   (likewise slope)
persistence   = |mean_h sign(momentum_h)|,  direction = sign(momentum) (+1 when 0)

``vol_window=None`` reproduces the legacy expanding population std exactly
(used by the August 2026 parity test); a rolling window is the default for
live use so old regimes do not dominate the scale forever.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from beidou_alpha.features import realized_vol, returns
from beidou_alpha.panel import Panel


@dataclass(frozen=True)
class TsmomParams:
    horizons: tuple[int, ...] = (5, 20, 50)
    horizon_weights: tuple[float, ...] = (0.2, 0.3, 0.5)
    return_weight: float = 0.40
    slope_weight: float = 0.25
    persistence_weight: float = 0.10
    return_scale: float = 0.20
    slope_scale: float = 0.01
    entry_threshold: float = 0.20
    vol_window: int | None = 200

    def __post_init__(self) -> None:
        if len(self.horizons) != len(self.horizon_weights) or not self.horizons:
            raise ValueError("horizons and horizon_weights must align")
        if any(h <= 0 for h in self.horizons) or any(w < 0 for w in self.horizon_weights):
            raise ValueError("horizons must be positive and weights non-negative")
        if sum(self.horizon_weights) <= 0:
            raise ValueError("horizon_weights must have positive mass")
        if self.return_scale <= 0 or self.slope_scale <= 0:
            raise ValueError("scales must be positive")
        if not 0 < self.entry_threshold <= 1:
            raise ValueError("entry_threshold must be in (0, 1]")

    @classmethod
    def from_mapping(cls, params: Mapping[str, Any]) -> TsmomParams:
        values = dict(params)
        for key in ("horizons", "horizon_weights"):
            if key in values:
                values[key] = tuple(values[key])
        return cls(**values)

    @property
    def warmup_bars(self) -> int:
        return max(self.horizons) + 1


def _tanh(frame: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(np.tanh(frame.to_numpy(dtype=float)), index=frame.index, columns=frame.columns)


def tsmom_scores(close: pd.DataFrame, params: TsmomParams | None = None) -> pd.DataFrame:
    p = params or TsmomParams()
    vol = realized_vol(close, window=p.vol_window, ddof=0)
    denominator = vol.clip(lower=p.return_scale).clip(lower=1e-12)
    weight_total = float(sum(p.horizon_weights))
    momentum_sum = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    slope_sum = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    sign_sum = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    valid = pd.DataFrame(True, index=close.index, columns=close.columns)
    for horizon, weight in zip(p.horizons, p.horizon_weights, strict=True):
        ret = returns(close, horizon)
        momentum_h = _tanh(ret / denominator)
        slope_h = _tanh((ret / horizon) / p.slope_scale)
        valid &= momentum_h.notna()
        momentum_sum = momentum_sum + momentum_h.fillna(0.0) * weight
        slope_sum = slope_sum + slope_h.fillna(0.0) * weight
        sign_sum = sign_sum + momentum_h.fillna(0.0).apply(np.sign)
    momentum = momentum_sum / weight_total
    slope = slope_sum / weight_total
    persistence = (sign_sum / len(p.horizons)).abs()
    direction = pd.DataFrame(np.where(momentum >= 0, 1.0, -1.0), index=close.index, columns=close.columns)
    component_weight = p.return_weight + p.slope_weight + p.persistence_weight
    score = (
        momentum * p.return_weight + slope * p.slope_weight + persistence * direction * p.persistence_weight
    ) / component_weight
    return score.clip(-1.0, 1.0).where(valid)


def compute(panel: Panel, params: Mapping[str, Any]) -> pd.DataFrame:
    return tsmom_scores(panel.close, TsmomParams.from_mapping(params))
