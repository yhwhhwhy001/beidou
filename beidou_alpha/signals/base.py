"""Common signal contract and target semantics."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import pandas as pd

from beidou_alpha.panel import Panel

SignalFunction = Callable[[Panel, Mapping[str, Any]], pd.DataFrame]


@dataclass(frozen=True)
class SignalSpec:
    id: str
    compute: SignalFunction
    default_params: dict[str, Any]
    description: str = ""
    warmup_bars: int = 0


def scores_to_targets(scores: pd.DataFrame, entry_threshold: float, *, hold: bool = True) -> pd.DataFrame:
    """Turn raw scores into positions.

    ``|score| >= entry_threshold`` is an actionable forecast and becomes the
    target.  Anything below the threshold is *no action*: with ``hold=True``
    (the correct semantics, D-005) the previous target is preserved; with
    ``hold=False`` the position is flattened.  ``hold=None`` is not offered —
    trading the raw sub-threshold score was the E-022 turnover bug.
    """
    if not 0 < entry_threshold <= 1:
        raise ValueError("entry_threshold must be in (0, 1]")
    actionable = scores.where(scores.abs() >= entry_threshold)
    seen = scores.notna().cummax()  # warmup rows stay NaN so backtests start at the first real decision
    filled = actionable.ffill() if hold else actionable
    return filled.fillna(0.0).where(seen)
