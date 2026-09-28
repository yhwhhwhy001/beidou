"""Cross-sectional low volatility (G12): long the calmest pool members, short the most volatile.

Pre-registered 2026-09-29 in docs/RESEARCH_LOG.md, 「预登记：横截面低波 xs_lowvol」, before any number existed.
The hypothesis: ranked by trailing realised vol inside the point-in-time pool, the calm names earn more per
unit of risk than the volatile ones.  Who would pay: buyers who pay up for lottery-like upside, or who cannot
lever the calm names - the two stories the equity literature tells (leverage constraints, lottery demand).
Leverage is cheap on perpetuals, which is the first reason it may not transfer.

    score = cross-sectional rank of -realized_vol(window) over the reference population, in [-1, 1]

+1 is the calmest member, -1 the most volatile.  A bar where fewer than ``min_symbols`` members have a vol
reading scores NaN (no action), as xsmom does.

The book the portfolio layer builds from these scores is NOT market-neutral: inverse-vol pricing (D-038
stage 1) scales the calm longs up and the volatile shorts down, so it runs net long.  That is the
pre-registration's first expected failure - stage 0, correlation with tsmom >= 0.5 - written down before
the correlation was measured.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from beidou_alpha.features import cross_sectional_rank, realized_vol, realized_vol_warmup, within_reference
from beidou_alpha.panel import Panel


@dataclass(frozen=True)
class XsLowvolParams:
    window: int = 720
    entry_threshold: float = 0.20
    min_symbols: int = 3

    def __post_init__(self) -> None:
        if self.window < 10:
            raise ValueError("window must be >= 10 bars")
        if not 0 < self.entry_threshold <= 1:
            raise ValueError("entry_threshold must be in (0, 1]")
        if self.min_symbols < 2:
            raise ValueError("a rank needs at least two members")

    @classmethod
    def from_mapping(cls, params: Mapping[str, Any]) -> XsLowvolParams:
        return cls(**dict(params))

    @property
    def warmup_bars(self) -> int:
        return realized_vol_warmup(self.window)


def xs_lowvol_scores(
    close: pd.DataFrame, params: XsLowvolParams | None = None, reference: pd.DataFrame | None = None
) -> pd.DataFrame:
    """``reference``: the cross-sectional population the rank is taken over (DL-Q1, D-042)."""
    p = params or XsLowvolParams()
    vol = realized_vol(close, p.window, ddof=0)
    enough = within_reference(vol, reference).notna().sum(axis=1) >= p.min_symbols
    return cross_sectional_rank(-vol, reference).where(enough, other=np.nan)


def compute(panel: Panel, params: Mapping[str, Any]) -> pd.DataFrame:
    return xs_lowvol_scores(panel.close, XsLowvolParams.from_mapping(params), panel.reference)
