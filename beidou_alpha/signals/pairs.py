"""Vol-matched pairs: mutual nearest neighbours, refit on a prefix, traded on the spread's z-score.

Pre-registered in docs/RESEARCH_LOG.md (P28, 2026-09-09) before this file existed.

**Vol-matched, not OLS-beta, and the construction is why.**  Stage 1 of the portfolio is
``w1 = target * vol_target / asset_vol`` - each leg sized to the target vol STANDALONE - so a signal
emitting ``+1`` and ``-beta`` would have its beta divided away by the two legs' own volatilities.
Defining the spread on vol-normalised returns instead makes that division the correct normalisation:
``+s`` and ``-s`` become two legs of equal risk contribution, which IS a vol-matched pair.  The hedge
ratio therefore comes free from the construction rather than being fought with it.

**Mutual nearest neighbours.**  A one-sided "closest partner" makes one popular symbol everybody's
partner, and then the book is not a set of pairs but a single leveraged bet against that symbol.

**The refit sees a prefix and nothing else.**  Selecting pairs on the full sample is the classic way
pairs trading reads the future, and it is not a subtle effect: choosing, in 2026, the pair that
co-moved best over 2021-2026 and then "trading" it in 2021 is close to a look-ahead by construction.
Partner, correlation and spread scale are all frozen at the refit bar and held until the next one.

Three vocabulary items, as everywhere else: ``+-`` an actionable score, an explicit ``0.0`` when the
spread has closed (``scores_to_targets`` reads it as CLOSE), and a sub-threshold hold otherwise.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from beidou_alpha.panel import Panel

HOLD = 0.05


@dataclass(frozen=True)
class PairsParams:
    formation_bars: int = 720
    refit_bars: int = 720
    z_window: int = 168
    entry_z: float = 2.0
    exit_z: float = 0.5
    z_scale: float = 3.0
    min_corr: float = 0.5
    entry_threshold: float = 0.2

    def __post_init__(self) -> None:
        if self.formation_bars < 60 or self.refit_bars < 24 or self.z_window < 24:
            raise ValueError("pairs windows are too short to estimate anything")
        if not 0.0 < self.exit_z < self.entry_z:
            raise ValueError("exit_z must be positive and below entry_z")
        if self.z_scale <= 0 or not -1.0 <= self.min_corr <= 1.0:
            raise ValueError("invalid pairs parameters")
        if not 0 < self.entry_threshold <= 1:
            raise ValueError("entry_threshold must be in (0, 1]")

    @classmethod
    def from_mapping(cls, params: Mapping[str, Any]) -> PairsParams:
        return cls(**dict(params))

    @property
    def warmup_bars(self) -> int:
        return self.formation_bars + self.z_window + 1


def _mutual_pairs(window: pd.DataFrame, min_corr: float) -> list[tuple[int, int]]:
    """Indices of mutually-nearest columns above ``min_corr``, computed on this window alone.

    Mutual rather than one-sided: with a one-sided rule the market's most correlated name becomes
    everybody's partner and the book stops being pairs at all.
    """
    # Enough observations AND some variance: `np.corrcoef` divides by the standard deviation, so a
    # column that never moved in the formation window produces a NaN row and a divide warning, and a
    # NaN row's argmax is meaningless rather than merely noisy.  A symbol that did not move is also not
    # a pair candidate under any reading, so it is dropped here rather than masked afterwards.
    observed = window.notna().sum() >= max(2, len(window) // 2)
    varying = window.std() > 0
    columns = np.flatnonzero((observed & varying).to_numpy())
    if len(columns) < 2:
        return []
    matrix = np.corrcoef(np.nan_to_num(window.to_numpy(dtype=float)[:, columns], nan=0.0), rowvar=False)
    np.fill_diagonal(matrix, -np.inf)
    best = matrix.argmax(axis=1)
    out: list[tuple[int, int]] = []
    for i, j in enumerate(best):
        if i < j and best[j] == i and matrix[i, j] >= min_corr:
            out.append((int(columns[i]), int(columns[j])))
    return out


def pairs_scores(panel: Panel, params: PairsParams | None = None) -> pd.DataFrame:
    p = params or PairsParams()
    close = panel.close
    scores = pd.DataFrame(np.nan, index=close.index, columns=close.columns, dtype=float)
    positive = close.where(close > 0)
    returns = pd.DataFrame(
        np.log(positive.to_numpy(dtype=float)), index=positive.index, columns=positive.columns
    ).diff()
    # Vol-normalised returns: the unit the spread is defined in, and the unit stage 1 will size in.
    vol = returns.rolling(p.z_window, min_periods=p.z_window // 2).std()
    normalised = returns.div(vol.where(vol > 0))
    values = normalised.to_numpy(dtype=float)

    n = len(close)
    if n <= p.warmup_bars:
        return scores
    for refit in range(p.formation_bars, n, p.refit_bars):
        # Everything below is computed from bars strictly BEFORE `refit`, then held until the next one.
        window = normalised.iloc[refit - p.formation_bars : refit]
        pairs = _mutual_pairs(window, p.min_corr)
        if not pairs:
            continue
        stop = min(n, refit + p.refit_bars)
        for left, right in pairs:
            formation = values[refit - p.formation_bars : refit, [left, right]]
            spread_formation = np.nancumsum(formation[:, 0] - formation[:, 1])
            scale = float(np.nanstd(spread_formation[-p.z_window :]))
            if not np.isfinite(scale) or scale <= 0:
                continue
            anchor = float(spread_formation[-1])
            live = values[refit:stop, [left, right]]
            spread = anchor + np.nancumsum(np.nan_to_num(live[:, 0] - live[:, 1], nan=0.0))
            centre = float(np.nanmean(spread_formation[-p.z_window :]))
            z = (spread - centre) / scale
            leg = np.where(
                np.abs(z) >= p.entry_z,
                -np.clip(z / p.z_scale, -1.0, 1.0),
                np.where(np.abs(z) < p.exit_z, 0.0, HOLD * -np.sign(z)),
            )
            scores.iloc[refit:stop, left] = leg
            scores.iloc[refit:stop, right] = -leg
    return scores


def compute(panel: Panel, params: Mapping[str, Any]) -> pd.DataFrame:
    return pairs_scores(panel, PairsParams.from_mapping(params))
