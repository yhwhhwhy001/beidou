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

from beidou_alpha.features import realized_vol, returns, robust_zscore
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
        return max(self.window, self.vol_window) + 1


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
