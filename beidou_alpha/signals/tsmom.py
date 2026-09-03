"""Multi-horizon time-series momentum (vectorised port of the legacy ``TrendAlpha`` formula).

score = clip( (0.40*momentum + 0.25*slope + 0.10*persistence*direction) / 0.75 , -1, 1 )

momentum_h    = tanh(ret_h / max(vol, return_scale))            (momentum_mode "fixed", the validated form)
              = tanh(ret_h / (return_scale * vol * sqrt(h)))     (momentum_mode "vol_scaled", a t-statistic form)
slope_h       = tanh((ret_h / h) / slope_scale)
momentum      = sum_h w_h * momentum_h / sum_h w_h   (likewise slope)
persistence   = |mean_h sign(momentum_h)|,  direction = sign(momentum) (+1 when 0)

``vol`` is the std of *one-bar* simple returns (``vol_window=None`` reproduces
the legacy expanding population std exactly, used by the August 2026 parity
test).  Honest note (E-043): in "fixed" mode that per-bar std is always far
below ``return_scale`` (0.5%-4% hourly vs 0.20), so ``max(vol, return_scale)``
is simply ``return_scale``; the arithmetic is kept verbatim because it is what
was validated, and ``vol_window`` then only sets the NaN warmup of the rolling
std.  "vol_scaled" is the pre-registered alternative in which ``vol`` matters:
``return_scale`` becomes the number of h-bar standard deviations that saturates
the score.  At weekly horizons the slope term is nearly inert (mean |contribution|
0.02 vs 0.32 for momentum); it is likewise kept for parity.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from beidou_alpha.features import cross_sectional_rank, realized_vol, returns
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
    # Optional funding-crowding modifier (carry's only plausible role after it failed standalone):
    # when a symbol's trailing funding sits in the extreme cross-sectional rank on the *same* side as the
    # momentum score, the position is crowded and the score is shrunk by ``crowding_penalty``.
    crowding_window: int = 0
    crowding_cut: float = 0.7
    crowding_penalty: float = 0.5
    momentum_mode: str = "fixed"  # fixed | vol_scaled (see module docstring)

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
        if self.crowding_window < 0 or not 0 < self.crowding_cut <= 1 or not 0 <= self.crowding_penalty <= 1:
            raise ValueError("invalid crowding parameters")
        if self.momentum_mode not in {"fixed", "vol_scaled"}:
            raise ValueError("momentum_mode must be 'fixed' or 'vol_scaled'")

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
        if p.momentum_mode == "vol_scaled":
            scale = (vol * math.sqrt(horizon) * p.return_scale).clip(lower=1e-12)
            momentum_h = _tanh(ret / scale)
        else:
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


def apply_crowding_modifier(score: pd.DataFrame, funding: pd.DataFrame | None, p: TsmomParams) -> pd.DataFrame:
    """Shrink same-direction scores where trailing funding is in the extreme cross-sectional rank."""
    if p.crowding_window <= 0 or funding is None or p.crowding_penalty <= 0:
        return score
    aligned = funding.reindex(index=score.index, columns=score.columns)
    trailing = aligned.rolling(p.crowding_window, min_periods=p.crowding_window).sum()
    observed = aligned.abs().rolling(p.crowding_window, min_periods=p.crowding_window).sum() > 0
    rank = cross_sectional_rank(trailing.where(observed)).fillna(0.0)
    crowded_long = (score > 0) & (rank >= p.crowding_cut)
    crowded_short = (score < 0) & (rank <= -p.crowding_cut)
    return score.mask(crowded_long | crowded_short, score * (1.0 - p.crowding_penalty))


def compute(panel: Panel, params: Mapping[str, Any]) -> pd.DataFrame:
    p = TsmomParams.from_mapping(params)
    return apply_crowding_modifier(tsmom_scores(panel.close, p), panel.funding, p)
