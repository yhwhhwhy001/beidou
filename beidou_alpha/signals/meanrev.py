"""Short-horizon robust mean reversion (vectorised port of ``MeanReversionEngine``).

z = (close - rolling median) / (1.4826 * rolling MAD) over ``window`` bars.
Enter against the dislocation when |z| >= z_entry (score = -clip(z / z_max)),
emit an explicit exit (0.0) once |z| < z_exit, hold in between (NaN).  A
trend gate blocks entries when the trailing move is a strong trend
(|ret_window| / (vol * sqrt(window)) > trend_gate_z), mirroring the legacy
"RANGING only" regime gate.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from beidou_alpha.features import realized_vol, realized_vol_warmup, returns, robust_zscore, robust_zscore_warmup
from beidou_alpha.panel import Panel


@dataclass(frozen=True)
class MeanrevParams:
    window: int = 48
    z_entry: float = 1.5
    z_exit: float = 0.5
    z_max: float = 5.0
    trend_gate_z: float = 2.0
    vol_window: int = 48
    entry_threshold: float = 0.20

    def __post_init__(self) -> None:
        if self.window < 10 or self.z_max <= 0 or self.z_entry <= self.z_exit or self.z_exit < 0:
            raise ValueError("invalid mean-reversion parameters")
        if not 0 < self.entry_threshold <= 1:
            raise ValueError("entry_threshold must be in (0, 1]")

    @classmethod
    def from_mapping(cls, params: Mapping[str, Any]) -> MeanrevParams:
        return cls(**dict(params))

    @property
    def warmup_bars(self) -> int:
        """The z-score dominates: its MAD is a rolling median of a rolling deviation, so it needs two windows.

        ``max(window, vol_window) + 1`` reported 49 for the defaults while the first non-NaN score is at
        bar 95.  The trend gate's own inputs (an h-bar return and ``realized_vol``) are cheaper but are
        kept explicit so a future parameter change cannot make one of them the binding term unnoticed.
        """
        return max(robust_zscore_warmup(self.window), self.window + 1, realized_vol_warmup(self.vol_window))


def meanrev_scores(close: pd.DataFrame, params: MeanrevParams | None = None) -> pd.DataFrame:
    p = params or MeanrevParams()
    z = robust_zscore(close, p.window)
    trailing = returns(close, p.window)
    vol = realized_vol(close, p.vol_window, ddof=0)
    trend_strength = (trailing / (vol * np.sqrt(p.window))).abs()
    entry = (z.abs() >= p.z_entry) & (trend_strength <= p.trend_gate_z)
    exit_zone = z.abs() < p.z_exit
    score = (-(z / p.z_max)).clip(-1.0, 1.0)
    result = score.where(entry)
    result = result.mask(exit_zone & z.notna(), 0.0)
    return result.where(z.notna())


def compute(panel: Panel, params: Mapping[str, Any]) -> pd.DataFrame:
    return meanrev_scores(panel.close, MeanrevParams.from_mapping(params))
