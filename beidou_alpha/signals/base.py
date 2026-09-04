"""Common signal contract and target semantics."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from beidou_alpha.panel import Panel

SignalFunction = Callable[[Panel, Mapping[str, Any]], pd.DataFrame]
WarmupFunction = Callable[[Mapping[str, Any]], int]
FundingPredicate = Callable[[Mapping[str, Any]], bool]


@dataclass(frozen=True)
class SignalSpec:
    id: str
    compute: SignalFunction
    default_params: dict[str, Any]
    description: str = ""
    warmup_bars: int = 0  # under the default params
    warmup: WarmupFunction | None = None  # under arbitrary (registry) params
    uses_funding: FundingPredicate | None = None  # does the signal read ``panel.funding`` under these params?

    def warmup_for(self, params: Mapping[str, Any]) -> int:
        """Bars of history the signal needs under *these* params, not under the defaults.

        The live loop sizes its request window from this number; deriving it
        from the defaults made a 720-bar horizon run on an 817-bar window (E-042).
        """
        if self.warmup is None:
            return self.warmup_bars
        return int(self.warmup(params))

    def needs_funding(self, params: Mapping[str, Any]) -> bool:
        """Whether the signal consumes funding-rate history under these params.

        The live loop must then fetch that history into the panel; a signal
        that silently gets ``funding=None`` trades a different configuration
        from the one that was validated (KILL-027).
        """
        return bool(self.uses_funding(params)) if self.uses_funding is not None else False


def scores_to_targets(
    scores: pd.DataFrame,
    entry_threshold: float,
    *,
    hold: bool = True,
    zero_is_exit: bool = True,
    initial: Mapping[str, float] | None = None,
) -> pd.DataFrame:
    """Turn raw scores into positions.

    ``|score| >= entry_threshold`` is an actionable forecast and becomes the
    target.  A sub-threshold non-zero score (or NaN) is *no action*: with
    ``hold=True`` (the correct semantics, D-005) the previous target is
    preserved; with ``hold=False`` the position is flattened.  An exact
    ``0.0`` is an explicit exit when ``zero_is_exit`` (mean-reversion style
    signals use it to leave a trade once the dislocation has closed).
    Trading the raw sub-threshold score was the E-022 turnover bug and is
    deliberately not offered.

    ``initial`` seeds the hold with the target held *before* the first row:
    the live loop passes the previous cycle's per-strategy targets so that a
    position survives a sub-threshold stretch longer than the request window,
    exactly as it does in a backtest over the full history (E-042).  Symbols
    without any score in the frame stay NaN (not tradable) whatever the seed.
    """
    if not 0 < entry_threshold <= 1:
        raise ValueError("entry_threshold must be in (0, 1]")
    actionable = scores.where(scores.abs() >= entry_threshold)
    if zero_is_exit:
        actionable = actionable.mask(scores == 0.0, 0.0)
    seen = scores.notna().cummax()  # warmup rows stay NaN so backtests start at the first real decision
    if not hold:
        return actionable.fillna(0.0).where(seen)
    if initial:
        seed = np.array([_seed_value(initial, str(column)) for column in actionable.columns], dtype=float)
        stacked = np.vstack([seed, actionable.to_numpy(dtype=float)])
        values = pd.DataFrame(stacked, columns=actionable.columns).ffill().to_numpy()[1:]
        filled = pd.DataFrame(values, index=actionable.index, columns=actionable.columns)
    else:
        filled = actionable.ffill()
    return filled.fillna(0.0).where(seen)


def _seed_value(initial: Mapping[str, float], symbol: str) -> float:
    value = initial.get(symbol)
    if value is None:
        return float("nan")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")
