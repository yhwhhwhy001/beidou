"""Order-flow imbalance: taker-buy share of quote volume, scaled by volume expansion (new).

imbalance = taker_buy_quote / quote_volume - 0.5  over ``window`` bars
score = clip(tanh(imbalance / scale) * min(1, vol_ratio), -1, 1)

Requires ``taker_buy_quote`` and ``quote_volume`` in the panel (present in the
official archives); returns NaN otherwise.  Optionally cross-sectionally
demeaned so the book is market-neutral.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from beidou_alpha.features import apply_numpy, taker_buy_ratio, volume_ratio
from beidou_alpha.panel import Panel


@dataclass(frozen=True)
class FlowParams:
    window: int = 24
    scale: float = 0.05
    volume_window: int = 48
    cross_sectional: bool = True
    entry_threshold: float = 0.20

    def __post_init__(self) -> None:
        if self.window <= 0 or self.scale <= 0 or self.volume_window < 2:
            raise ValueError("invalid flow parameters")
        if not 0 < self.entry_threshold <= 1:
            raise ValueError("entry_threshold must be in (0, 1]")

    @classmethod
    def from_mapping(cls, params: Mapping[str, Any]) -> FlowParams:
        return cls(**dict(params))

    @property
    def warmup_bars(self) -> int:
        return max(self.window, self.volume_window) + 1


def flow_scores(panel: Panel, params: FlowParams | None = None) -> pd.DataFrame:
    p = params or FlowParams()
    if panel.taker_buy_quote is None or panel.quote_volume is None:
        return pd.DataFrame(np.nan, index=panel.close.index, columns=panel.close.columns)
    imbalance = taker_buy_ratio(panel.taker_buy_quote, panel.quote_volume, p.window) - 0.5
    if p.cross_sectional:
        imbalance = imbalance.sub(imbalance.mean(axis=1), axis=0)
    expansion = volume_ratio(panel.volume, p.volume_window).clip(upper=1.0).fillna(1.0)
    score = apply_numpy(imbalance / p.scale, np.tanh) * expansion
    return score.clip(-1.0, 1.0).where(imbalance.notna())


def compute(panel: Panel, params: Mapping[str, Any]) -> pd.DataFrame:
    return flow_scores(panel, FlowParams.from_mapping(params))
