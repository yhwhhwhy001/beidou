"""Order-flow imbalance: taker-buy share of quote volume, scaled by volume expansion (new).

imbalance = taker_buy_quote / quote_volume - 0.5  over ``window`` bars
score = clip(tanh(imbalance / scale) * min(1, vol_ratio), -1, 1)

Requires ``taker_buy_quote`` and ``quote_volume`` in the panel (present in the
official archives); returns NaN otherwise.  Optionally cross-sectionally
demeaned so the book is market-neutral.

Short-side trend gate (round 5).  Perp taker *selling* into a strongly rising
market is hedging and short-selling into strength, not informed flow: the
price is being driven by spot demand the perp tape cannot see, and those
shorts are what got squeezed in 2024-11 (XRP/ADA/SUI).  With ``short_gate``
> 0 a negative score is replaced by an explicit exit (0.0) while the symbol's
weekly-scale momentum score is >= ``short_gate``.  Longs against downtrends
are deliberately left alone: the symmetric gate destroyed the 2022 bear-market
folds, where dip-buying flow was the profitable leg.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from beidou_alpha.features import apply_numpy, taker_buy_ratio, volume_ratio
from beidou_alpha.panel import Panel
from beidou_alpha.signals.tsmom import TsmomParams, tsmom_scores


@dataclass(frozen=True)
class FlowParams:
    window: int = 24
    scale: float = 0.05
    volume_window: int = 48
    cross_sectional: bool = True
    entry_threshold: float = 0.20
    short_gate: float = 0.0  # 0 disables; shorts are dropped while momentum >= short_gate
    long_side: bool = True  # False = short-only book (the long leg's static-universe evidence was survivorship)
    gate_horizons: tuple[int, ...] = (168, 336, 720)
    gate_weights: tuple[float, ...] = (0.2, 0.3, 0.5)
    gate_return_scale: float = 0.20
    gate_vol_window: int | None = 400

    def __post_init__(self) -> None:
        if self.window <= 0 or self.scale <= 0 or self.volume_window < 2:
            raise ValueError("invalid flow parameters")
        if not 0 < self.entry_threshold <= 1:
            raise ValueError("entry_threshold must be in (0, 1]")
        if not 0 <= self.short_gate <= 1:
            raise ValueError("short_gate must be in [0, 1]")
        if self.short_gate > 0:
            self.gate_params()  # validates the momentum parameters eagerly

    @classmethod
    def from_mapping(cls, params: Mapping[str, Any]) -> FlowParams:
        values = dict(params)
        for key in ("gate_horizons", "gate_weights"):
            if key in values:
                values[key] = tuple(values[key])
        return cls(**values)

    def gate_params(self) -> TsmomParams:
        return TsmomParams(
            horizons=self.gate_horizons,
            horizon_weights=self.gate_weights,
            return_scale=self.gate_return_scale,
            vol_window=self.gate_vol_window,
        )

    @property
    def warmup_bars(self) -> int:
        base = max(self.window, self.volume_window) + 1
        if self.short_gate <= 0:
            return base
        return max(base, self.gate_params().warmup_bars)


def flow_scores(panel: Panel, params: FlowParams | None = None) -> pd.DataFrame:
    p = params or FlowParams()
    if panel.taker_buy_quote is None or panel.quote_volume is None:
        return pd.DataFrame(np.nan, index=panel.close.index, columns=panel.close.columns)
    imbalance = taker_buy_ratio(panel.taker_buy_quote, panel.quote_volume, p.window) - 0.5
    if p.cross_sectional:
        imbalance = imbalance.sub(imbalance.mean(axis=1), axis=0)
    expansion = volume_ratio(panel.volume, p.volume_window).clip(upper=1.0).fillna(1.0)
    score = apply_numpy(imbalance / p.scale, np.tanh) * expansion
    score = apply_short_gate(score.clip(-1.0, 1.0).where(imbalance.notna()), panel.close, p)
    if not p.long_side:
        score = score.mask(score > 0, 0.0)  # a buy signal closes a short but never opens a long
    return score


def apply_short_gate(score: pd.DataFrame, close: pd.DataFrame, p: FlowParams) -> pd.DataFrame:
    """Replace short scores by an explicit exit while weekly momentum is strongly positive."""
    if p.short_gate <= 0:
        return score
    momentum = tsmom_scores(close, p.gate_params()).reindex(index=score.index, columns=score.columns)
    blocked = (score < 0) & (momentum >= p.short_gate)
    return score.mask(blocked, 0.0)


def compute(panel: Panel, params: Mapping[str, Any]) -> pd.DataFrame:
    return flow_scores(panel, FlowParams.from_mapping(params))
