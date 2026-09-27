"""Funding carry held as a hedge: long spot, short the perpetual, equal notional (#14 / #39).

Pre-registered 2026-09-27 (`docs/RESEARCH_LOG.md`, 「预登记：资金费对冲书 carry_hedged」) after the
operator's named ruling of the same day reopened the family.  It is not `carry`: that one trades the
perpetual alone, so it earns or loses price direction and uses funding only as the signal.  Here the
two price legs cancel and what is left is the funding itself, minus the basis moving and the cost of
trading two legs.

The score is not a direction.  1.0 means "hold the hedge on this symbol", 0.0 "hold nothing", NaN
"cannot decide yet" (warm-up).  It is decided once a day, on the bar that opens at
``decision_hour_utc`` - 00:00 by default, so the decision is taken at 01:00 UTC with the 00:00
settlement already paid, and the bar the trade executes on holds no settlement.  D-034 books a
settlement on the bar that contains it; a book entering ON a settlement bar would be credited a
payment it never held, and one leaving on it would miss one it did.

A symbol qualifies when its spot leg has a price on the decision bar (same open time, never
forward-filled - DL-D5 note 7), its perpetual has traded for a whole look-back window, and its
trailing funding - summed over ``lookback_days`` worth of bars and divided by ``lookback_days`` - is
strictly above ``threshold_per_day`` (by more than ``FUNDING_TOLERANCE``).  Summing bars rather than
averaging settlements puts a symbol that settles every 8h, 4h or 1h on the same per-day scale.

Only `research validate` prices this book, through `beidou_alpha.hedged`: the live loop has no spot
leg, and `AlphaModel` refuses a hedged signal so no perp-only path can trade it by accident.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from beidou_alpha.panel import Panel, interval_seconds

HOURS_PER_DAY = 24
#: Per day, and far below the 1e-8 a settled rate is quoted to.  Many symbols sit EXACTLY on Binance's
#: 0.0001-per-8h interest term for weeks, so the 0.0003-per-day line is met to the last bit by a summed
#: window, and a bare `>` would let float rounding decide whether the symbol is held.
FUNDING_TOLERANCE = 1e-9


@dataclass(frozen=True)
class CarryHedgedParams:
    lookback_days: int = 30
    threshold_per_day: float = 0.0  # 0.0003 per day is Binance's 0.01% per 8h interest-rate term
    decision_hour_utc: int = 0

    def __post_init__(self) -> None:
        if self.lookback_days <= 0:
            raise ValueError("lookback_days must be positive")
        if not 0 <= self.decision_hour_utc < HOURS_PER_DAY:
            raise ValueError("decision_hour_utc must be in [0, 24)")
        if not math.isfinite(self.threshold_per_day):
            raise ValueError("threshold_per_day must be finite")

    @classmethod
    def from_mapping(cls, params: Mapping[str, Any]) -> CarryHedgedParams:
        return cls(**dict(params))

    @property
    def warmup_bars(self) -> int:
        """Hourly bars, like every other signal's warm-up; `carry_hedged_scores` sizes by the panel's own interval."""
        return self.lookback_days * HOURS_PER_DAY


def carry_hedged_scores(panel: Panel, params: CarryHedgedParams | None = None) -> pd.DataFrame:
    p = params or CarryHedgedParams()
    index, columns = panel.close.index, panel.close.columns
    spot_close = panel.spot_field("close")
    if panel.funding is None or spot_close is None:
        return pd.DataFrame(np.nan, index=index, columns=columns)
    window = p.lookback_days * max(1, round(86_400 / interval_seconds(panel.interval)))
    per_day = panel.funding.reindex(index=index, columns=columns).rolling(window, min_periods=window).sum()
    per_day = per_day / p.lookback_days
    # `funding` is zero-filled before a listing, so a young symbol's window would average zeros it never
    # paid.  A whole window of perpetual bars is required before its mean is read - and a price on the
    # decision bar itself, or a delisted perpetual would still hold L days of the funding it used to pay.
    listed = panel.close.notna() & (panel.close.notna().cumsum() >= window)
    priced = spot_close.reindex(index=index, columns=columns).notna()
    held = ((per_day > p.threshold_per_day + FUNDING_TOLERANCE) & listed & priced).astype(float)
    stamps = pd.DatetimeIndex(index)
    decided = (stamps.hour == p.decision_hour_utc) & (stamps.minute == 0) & (np.arange(len(index)) >= window - 1)
    held.loc[~decided, :] = np.nan
    return held.ffill()


def compute(panel: Panel, params: Mapping[str, Any]) -> pd.DataFrame:
    return carry_hedged_scores(panel, CarryHedgedParams.from_mapping(params))
