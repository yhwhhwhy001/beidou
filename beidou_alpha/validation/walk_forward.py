"""Purged, embargoed walk-forward evaluation over a parameter grid.

Signals are causal, so each parameter set is backtested once over the whole
panel; folds then slice the resulting net-return series.  Parameter selection
uses only the training slice (purged by ``purge`` bars before the test block).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from beidou_alpha.validation.metrics import compound, max_drawdown, newey_west_tstat, sharpe


@dataclass(frozen=True)
class Fold:
    train_start: int
    train_end: int  # exclusive
    test_start: int
    test_end: int  # exclusive

    @property
    def train_slice(self) -> slice:
        return slice(self.train_start, self.train_end)

    @property
    def test_slice(self) -> slice:
        return slice(self.test_start, self.test_end)


def walk_forward_folds(
    n_bars: int,
    n_folds: int,
    *,
    min_train: int,
    purge: int = 0,
    embargo: int = 0,
    expanding: bool = True,
    train_window: int | None = None,
) -> list[Fold]:
    """Split ``[min_train, n_bars)`` into ``n_folds`` contiguous test blocks.

    ``purge`` bars immediately before each test block are dropped from training
    (labels overlapping the test period).  ``embargo`` is accepted for API
    symmetry with CPCV but has no effect here: in walk-forward the training
    window always precedes the test block, so no training sample follows it.
    """
    if n_folds <= 0 or min_train <= 0 or n_bars <= min_train:
        raise ValueError("invalid fold specification")
    test_total = n_bars - min_train
    block = test_total // n_folds
    if block <= 0:
        raise ValueError("not enough bars for the requested folds")
    folds: list[Fold] = []
    for k in range(n_folds):
        test_start = min_train + k * block
        test_end = n_bars if k == n_folds - 1 else test_start + block
        train_end = max(0, test_start - purge)
        train_start = 0 if expanding or train_window is None else max(0, train_end - train_window)
        if train_end - train_start < 2:
            raise ValueError("training window collapsed; reduce purge/embargo or folds")
        folds.append(Fold(train_start, train_end, test_start, test_end))
    return folds


@dataclass
class FoldOutcome:
    fold: Fold
    chosen_params: dict[str, Any]
    train_sharpe: float | None
    test_sharpe: float | None
    test_return: float
    test_drawdown: float


@dataclass
class WalkForwardResult:
    folds: list[FoldOutcome]
    oos_returns: pd.Series
    param_keys: list[str]
    is_sharpes: dict[str, list[float | None]] = field(default_factory=dict)
    oos_sharpes: dict[str, list[float | None]] = field(default_factory=dict)

    def summary(self, bars_per_year: float) -> dict[str, Any]:
        test_sharpes = [f.test_sharpe for f in self.folds if f.test_sharpe is not None]
        # D-020: the OOS mean return's Newey-West t-statistic is the verdict's significance test
        significance = newey_west_tstat(self.oos_returns)
        return {
            "folds": len(self.folds),
            "oos_sharpe": sharpe(self.oos_returns, bars_per_year),
            "oos_return": compound(self.oos_returns),
            "oos_max_drawdown": max_drawdown(self.oos_returns),
            "oos_bars": len(self.oos_returns),
            "oos_t_stat": significance["t_stat"],
            "oos_t_lags": significance["lags"],
            "fold_sharpes": [f.test_sharpe for f in self.folds],
            "fold_consistency": (float(np.mean([s > 0 for s in test_sharpes])) if test_sharpes else None),
            "chosen_params": [f.chosen_params for f in self.folds],
        }


def param_key(params: Mapping[str, Any]) -> str:
    return "|".join(f"{key}={params[key]}" for key in sorted(params))


def walk_forward_evaluate(
    net_returns_by_params: Mapping[str, pd.Series],
    params_by_key: Mapping[str, Mapping[str, Any]],
    folds: Sequence[Fold],
    bars_per_year: float,
    select: Callable[[pd.Series, float], float | None] = sharpe,
) -> WalkForwardResult:
    """Pick the best parameter set per fold on training data; concatenate its test returns."""
    keys = list(net_returns_by_params)
    if not keys:
        raise ValueError("no parameter sets supplied")
    length = len(next(iter(net_returns_by_params.values())))
    outcomes: list[FoldOutcome] = []
    oos_parts: list[pd.Series] = []
    is_table: dict[str, list[float | None]] = {key: [] for key in keys}
    oos_table: dict[str, list[float | None]] = {key: [] for key in keys}
    for fold in folds:
        if fold.test_end > length:
            raise ValueError("fold exceeds series length")
        best_key: str | None = None
        best_score = -np.inf
        for key in keys:
            series = net_returns_by_params[key]
            train_score = select(series.iloc[fold.train_slice], bars_per_year)
            test_score = select(series.iloc[fold.test_slice], bars_per_year)
            is_table[key].append(train_score)
            oos_table[key].append(test_score)
            if train_score is not None and train_score > best_score:
                best_score = train_score
                best_key = key
        chosen = best_key or keys[0]
        test_series = net_returns_by_params[chosen].iloc[fold.test_slice]
        oos_parts.append(test_series)
        outcomes.append(
            FoldOutcome(
                fold=fold,
                chosen_params=dict(params_by_key[chosen]),
                train_sharpe=None if best_key is None else float(best_score),
                test_sharpe=select(test_series, bars_per_year),
                test_return=compound(test_series),
                test_drawdown=max_drawdown(test_series),
            )
        )
    oos = pd.concat(oos_parts) if oos_parts else pd.Series(dtype=float)
    return WalkForwardResult(
        folds=outcomes, oos_returns=oos, param_keys=keys, is_sharpes=is_table, oos_sharpes=oos_table
    )
