"""Drawdown throttle: scale gross exposure down as equity falls from its high-water mark (D-015).

scalar(dd) = 1                          for dd <= start
           = floor + (1 - floor) * (stop - dd) / (stop - start)   between
           = floor                      for dd >= stop

One scalar multiplies the whole weight row, so the book's shape (and the
vol-targeting that produced it) is untouched; only its size changes.  The
live loop applies the same function to venue equity against a persisted
high-water mark.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class DrawdownThrottleParams:
    start: float = 0.05
    stop: float = 0.20
    floor: float = 0.25
    enabled: bool = False

    def __post_init__(self) -> None:
        if not 0 <= self.start < self.stop or not 0 < self.floor <= 1:
            raise ValueError("need 0 <= start < stop and 0 < floor <= 1")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> DrawdownThrottleParams:
        known = {key: payload[key] for key in cls.__dataclass_fields__ if key in payload}
        return cls(**known)


def drawdown_scalar(drawdown: float, params: DrawdownThrottleParams) -> float:
    """``drawdown`` is a non-negative fraction below the high-water mark."""
    if not params.enabled:
        return 1.0
    dd = max(0.0, float(drawdown))
    if dd <= params.start:
        return 1.0
    if dd >= params.stop:
        return params.floor
    return params.floor + (1.0 - params.floor) * (params.stop - dd) / (params.stop - params.start)


def apply_drawdown_throttle(
    weights: pd.DataFrame, portfolio_returns: pd.Series, params: DrawdownThrottleParams
) -> tuple[pd.DataFrame, pd.Series]:
    """Scale decision-time weights by the throttle implied by the *throttled* equity path.

    ``portfolio_returns[t]`` is the unthrottled net return realised on the bar
    *after* decision ``t-1`` (i.e. aligned to decision index, shifted by one
    execution bar in the caller's frame); the throttled path is
    ``equity_t = equity_{t-1} * (1 + scalar_{t-1} * r_t)``.  Returns the scaled
    weights and the per-bar scalar series.
    """
    if not params.enabled:
        return weights.copy(), pd.Series(1.0, index=weights.index)
    rets = portfolio_returns.reindex(weights.index).fillna(0.0).to_numpy(dtype=float)
    scalars = np.ones(len(rets))
    equity = 1.0
    peak = 1.0
    scalar = 1.0
    for t in range(len(rets)):
        equity *= 1.0 + scalar * rets[t]
        peak = max(peak, equity)
        scalar = drawdown_scalar(1.0 - equity / peak, params)
        scalars[t] = scalar
    series = pd.Series(scalars, index=weights.index)
    return weights.mul(series, axis=0), series
