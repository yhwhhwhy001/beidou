"""Cross-sectional momentum / relative strength (vectorised port of ``RelativeStrengthAlpha``).

relative = sum_h w_h * tanh((r_asset,h - r_bench,h) / score_scale) / sum_h w_h
rank     = cross-sectional rank of (r_asset,H - r_bench,H) at the primary (last) horizon, in [-1, 1]
score    = clip(0.75 * relative + 0.25 * rank, -1, 1)

The benchmark is the equal-weight cross-sectional mean, so the signal is
market-neutral by construction.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from beidou_alpha.features import apply_numpy, cross_sectional_rank, returns
from beidou_alpha.panel import Panel


@dataclass(frozen=True)
class XsmomParams:
    horizons: tuple[int, ...] = (24, 72, 168)
    horizon_weights: tuple[float, ...] = (0.2, 0.3, 0.5)
    score_scale: float = 0.08
    relative_weight: float = 0.75
    rank_weight: float = 0.25
    entry_threshold: float = 0.20
    min_symbols: int = 3

    def __post_init__(self) -> None:
        if len(self.horizons) != len(self.horizon_weights) or not self.horizons:
            raise ValueError("horizons and horizon_weights must align")
        if self.score_scale <= 0 or sum(self.horizon_weights) <= 0:
            raise ValueError("scales must be positive")
        if not 0 < self.entry_threshold <= 1:
            raise ValueError("entry_threshold must be in (0, 1]")

    @classmethod
    def from_mapping(cls, params: Mapping[str, Any]) -> XsmomParams:
        values = dict(params)
        for key in ("horizons", "horizon_weights"):
            if key in values:
                values[key] = tuple(values[key])
        return cls(**values)

    @property
    def warmup_bars(self) -> int:
        return max(self.horizons) + 1


def xsmom_scores(close: pd.DataFrame, params: XsmomParams | None = None) -> pd.DataFrame:
    p = params or XsmomParams()
    weight_total = float(sum(p.horizon_weights))
    relative = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    valid = pd.DataFrame(True, index=close.index, columns=close.columns)
    excess_primary: pd.DataFrame | None = None
    for horizon, weight in zip(p.horizons, p.horizon_weights, strict=True):
        ret = returns(close, horizon)
        benchmark = ret.mean(axis=1)
        excess = ret.sub(benchmark, axis=0)
        valid &= excess.notna()
        relative = relative + apply_numpy(excess / p.score_scale, np.tanh).fillna(0.0) * weight
        excess_primary = excess
    assert excess_primary is not None
    relative = relative / weight_total
    rank = cross_sectional_rank(excess_primary)
    enough = close.notna().sum(axis=1) >= p.min_symbols
    score = (p.relative_weight * relative + p.rank_weight * rank.fillna(0.0)) / (p.relative_weight + p.rank_weight)
    return score.clip(-1.0, 1.0).where(valid).where(enough, other=np.nan)


def compute(panel: Panel, params: Mapping[str, Any]) -> pd.DataFrame:
    return xsmom_scores(panel.close, XsmomParams.from_mapping(params))
