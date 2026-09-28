"""Market-wide long/short timing: the whole basket, long or short, against the crowd's positioning.

Pre-registered 2026-09-28 (`docs/RESEARCH_LOG.md`, 「预登记：全市场多空比择时 lsr_timing」).  It is not the LS
leaf reopened.  The leaf's four positive shapes (#199) were REFUTED as a selection signal: all of their money
was in how the book's NET exposure moved with the market-wide long/short ratio, none in which names they
ranked.  #200 then split off the ten months of 2021 in which only BTC had a ratio, and the timing was still
there once the cross-section existed (t 3.23 against the 2.0 line).  This signal is that timing written as a
book of its own, with its own bucket and its own controls.

Per bar, over the population (`panel.reference`, the members at the time): each name that has a ratio is
read against its own trailing ``window``-bar mean - `lsr(window)`, the mining leaf's own definition, bit for
bit - and the equal-weighted mean of those readings is the market's deviation M.  The raw score is
``tanh(-M / scale)``: accounts more long than usual across the market is a short, more short than usual a
long.  The book flips only on a reading at least ``entry_threshold`` strong and otherwise keeps the side it
has, and every name in the population holds that same side, including names with no ratio of their own:
what is timed is the basket, not the names the ratio happens to cover.

Before the first strong reading there is no side and no position, but there is a decision: the score is the
raw reading itself, below the line by definition, so the model takes no action and a backtest holds nothing
from the first bar M exists.  (Below the model's line only while the entry carries this ``entry_threshold``:
research merges the signal's defaults into every entry, and a registry entry has to name it, as it has to
for every signal.)  A NaN there would read as warmup, and `research validate` scores every cell of
a grid on the range the cells share, so the cell whose first strong reading came last would cut every cell's
history down to its own - on the first synthetic trial of the pre-registration, a 2021-01 start became
2022-04.  An exact 0.0 is passed as NaN instead, because `scores_to_targets` reads it as an explicit exit.

**Only the side survives the construction.**  `beidou_alpha.portfolio.vol_targeted` sizes the whole book to
``vol_target``, so a score that is the same on every name has its size divided back out in stage 2 and what
reaches the weights is its sign - unless the score is small enough for ``max_scalar`` to cap the scalar:
about 0.03 on a dozen correlated names, a third on a single name.  A held score is at least
``entry_threshold``, so on any population of more than a couple of names the held side is the whole signal,
and the score keeps its size only because the model's own `scores_to_targets` pass reads it against the same
``entry_threshold``.

The side is held at the market level, here, rather than per name by the model's hold.  With a per-name hold a
name that joined the population between two strong readings would sit out until the next one, and in a
point-in-time universe the book would drift away from the basket it is meant to time.  Live, the request
window need not reach back to the last strong reading; the score is then the raw reading, below the line, the
model's per-name hold seeds from the previous cycle's targets (E-042), and only a name that joined in between
waits for the next strong reading.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from beidou_alpha.features import within_reference
from beidou_alpha.mining.expr import LongShortRatio
from beidou_alpha.panel import Panel


@dataclass(frozen=True)
class LsrTimingParams:
    window: int = 168
    scale: float = 1.0
    entry_threshold: float = 0.20

    def __post_init__(self) -> None:
        if self.window < 2:
            raise ValueError("window must be at least two bars")
        if not self.scale > 0:
            raise ValueError("scale must be positive")
        if not 0 < self.entry_threshold <= 1:
            raise ValueError("entry_threshold must be in (0, 1]")

    @classmethod
    def from_mapping(cls, params: Mapping[str, Any]) -> LsrTimingParams:
        return cls(**dict(params))

    @property
    def warmup_bars(self) -> int:
        return self.window


def market_deviation(panel: Panel, window: int) -> tuple[pd.Series, pd.Series]:
    """M per bar, the population mean of each name's `lsr(window)`, and how many names it averaged.

    NaN where no name in the population has a reading yet.  The leaf raises `ExprError` when the panel carries
    no `count_long_short_ratio` at all, so a run without the metrics store fails rather than scoring nothing.
    """
    readings = within_reference(LongShortRatio(window).evaluate(panel), panel.reference)
    return readings.mean(axis=1), readings.notna().sum(axis=1)


def held_side(raw: pd.Series, entry_threshold: float) -> pd.Series:
    """The last raw score at least ``entry_threshold`` strong, carried forward; NaN before the first one."""
    return raw.where(raw.abs() >= entry_threshold).ffill()


def lsr_timing_scores(panel: Panel, params: LsrTimingParams | None = None) -> pd.DataFrame:
    p = params or LsrTimingParams()
    deviation, _names = market_deviation(panel, p.window)
    raw = np.tanh(-deviation / p.scale)
    score = held_side(raw, p.entry_threshold).fillna(raw.mask(raw == 0.0))  # before the first strong reading: raw
    columns = panel.close.columns
    scores = pd.DataFrame(
        np.repeat(score.to_numpy()[:, None], len(columns), axis=1), index=score.index, columns=columns
    )
    return within_reference(scores, panel.reference)


def compute(panel: Panel, params: Mapping[str, Any]) -> pd.DataFrame:
    return lsr_timing_scores(panel, LsrTimingParams.from_mapping(params))
