"""Combinatorial purged cross-validation splits (López de Prado 2018, ch. 12)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from typing import Any

import numpy as np
import pandas as pd

from beidou_alpha.validation.metrics import sharpe


@dataclass(frozen=True)
class Split:
    train_index: np.ndarray
    test_index: np.ndarray
    test_groups: tuple[int, ...]


def cpcv_splits(
    n_bars: int, n_groups: int = 6, n_test_groups: int = 2, purge: int = 0, embargo: int = 0
) -> list[Split]:
    if n_groups < 2 or not 0 < n_test_groups < n_groups or n_bars < n_groups * 2:
        raise ValueError("invalid CPCV specification")
    bounds = np.linspace(0, n_bars, n_groups + 1, dtype=int)
    groups = [(int(bounds[i]), int(bounds[i + 1])) for i in range(n_groups)]
    splits: list[Split] = []
    for chosen in combinations(range(n_groups), n_test_groups):
        test_mask = np.zeros(n_bars, dtype=bool)
        blocked = np.zeros(n_bars, dtype=bool)
        for g in chosen:
            start, end = groups[g]
            test_mask[start:end] = True
            blocked[max(0, start - purge) : start] = True
            blocked[end : min(n_bars, end + embargo)] = True
        train_mask = ~test_mask & ~blocked
        splits.append(Split(np.flatnonzero(train_mask), np.flatnonzero(test_mask), tuple(chosen)))
    return splits


def cpcv_evaluate(
    net_returns_by_params: Mapping[str, pd.Series],
    splits: Sequence[Split],
    bars_per_year: float,
) -> dict[str, Any]:
    """For each split, pick the best parameters on train and record their test Sharpe."""
    keys = list(net_returns_by_params)
    if not keys:
        raise ValueError("no parameter sets supplied")
    oos: list[float] = []
    chosen_keys: list[str] = []
    for split in splits:
        best_key, best_score = keys[0], -np.inf
        for key in keys:
            values = net_returns_by_params[key].to_numpy(dtype=float)
            score = sharpe(values[split.train_index], bars_per_year)
            if score is not None and score > best_score:
                best_key, best_score = key, score
        test_values = net_returns_by_params[best_key].to_numpy(dtype=float)[split.test_index]
        test_sharpe = sharpe(test_values, bars_per_year)
        oos.append(float("nan") if test_sharpe is None else test_sharpe)
        chosen_keys.append(best_key)
    array = np.asarray(oos, dtype=float)
    finite = array[np.isfinite(array)]
    return {
        "paths": len(splits),
        "oos_sharpe_mean": float(finite.mean()) if finite.size else None,
        "oos_sharpe_q05": float(np.quantile(finite, 0.05)) if finite.size else None,
        "oos_sharpe_min": float(finite.min()) if finite.size else None,
        "fraction_negative": float(np.mean(finite < 0)) if finite.size else None,
        "chosen": chosen_keys,
    }
