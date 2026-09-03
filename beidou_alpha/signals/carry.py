"""Funding-rate carry (new; E-024 showed the old system never used funding).

Perpetual longs pay positive funding to shorts.  Over a trailing window the
settled funding is summed; the score is ``-tanh(funding / scale)`` so high
funding pushes short and negative funding pushes long.  With
``cross_sectional=True`` the cross-sectional mean is removed first, giving a
market-neutral relative-carry book.  Symbols without funding data get NaN.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from beidou_alpha.features import apply_numpy
from beidou_alpha.panel import Panel


@dataclass(frozen=True)
class CarryParams:
    window_bars: int = 24
    scale: float = 0.0005
    cross_sectional: bool = True
    entry_threshold: float = 0.20

    def __post_init__(self) -> None:
        if self.window_bars <= 0 or self.scale <= 0:
            raise ValueError("window_bars and scale must be positive")
        if not 0 < self.entry_threshold <= 1:
            raise ValueError("entry_threshold must be in (0, 1]")

    @classmethod
    def from_mapping(cls, params: Mapping[str, Any]) -> CarryParams:
        return cls(**dict(params))

    @property
    def warmup_bars(self) -> int:
        return self.window_bars


def carry_scores(
    funding: pd.DataFrame | None, index: pd.Index, columns: pd.Index, params: CarryParams | None = None
) -> pd.DataFrame:
    p = params or CarryParams()
    if funding is None:
        return pd.DataFrame(np.nan, index=index, columns=columns)
    aligned = funding.reindex(index=index, columns=columns)
    trailing = aligned.rolling(p.window_bars, min_periods=p.window_bars).sum()
    observed = aligned.abs().rolling(p.window_bars, min_periods=p.window_bars).sum() > 0
    if p.cross_sectional:
        trailing = trailing.sub(trailing.mean(axis=1), axis=0)
    score = -apply_numpy(trailing / p.scale, np.tanh)
    return score.where(observed)


def compute(panel: Panel, params: Mapping[str, Any]) -> pd.DataFrame:
    return carry_scores(panel.funding, panel.close.index, panel.close.columns, CarryParams.from_mapping(params))
