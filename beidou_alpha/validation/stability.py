"""Robustness checks: time-split degradation, parameter neighbourhood, cost stress, regime split."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import numpy as np
import pandas as pd

from beidou_alpha.validation.metrics import sharpe


def time_split_sharpes(net: pd.Series, n_splits: int, bars_per_year: float) -> list[float | None]:
    if n_splits <= 0:
        raise ValueError("n_splits must be positive")
    pieces = np.array_split(net.to_numpy(dtype=float), n_splits)
    return [sharpe(piece, bars_per_year) for piece in pieces]


def parameter_neighborhood(
    evaluate: Callable[[Mapping[str, Any]], float | None],
    base_params: Mapping[str, Any],
    *,
    perturb_pct: float = 0.10,
    numeric_keys: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Evaluate ±perturb_pct around every numeric parameter; report the worst relative degradation."""
    base = evaluate(base_params)
    results: dict[str, dict[str, float | None]] = {}
    candidates = numeric_keys or tuple(base_params)
    keys = tuple(
        k
        for k in candidates
        if isinstance(base_params.get(k), int | float) and not isinstance(base_params.get(k), bool)
    )
    for key in keys:
        value = base_params[key]
        neighbours: dict[str, float | None] = {}
        for label, factor in (("down", 1.0 - perturb_pct), ("up", 1.0 + perturb_pct)):
            perturbed = dict(base_params)
            candidate = value * factor
            perturbed[key] = round(candidate) if isinstance(value, int) else candidate
            if perturbed[key] == value:
                perturbed[key] = value + (1 if label == "up" else -1) if isinstance(value, int) else candidate
            try:
                neighbours[label] = evaluate(perturbed)
            except ValueError:
                neighbours[label] = None
        results[key] = neighbours
    scores = [v for group in results.values() for v in group.values() if v is not None]
    worst = min(scores) if scores else None
    return {
        "base": base,
        "neighbours": results,
        "worst_neighbour": worst,
        "worst_degradation": (None if base is None or worst is None or base == 0 else (base - worst) / abs(base)),
    }


def cost_stress(net_by_multiplier: Mapping[float, pd.Series], bars_per_year: float) -> dict[str, float | None]:
    return {f"x{multiplier:g}": sharpe(series, bars_per_year) for multiplier, series in net_by_multiplier.items()}


def regime_split_sharpes(
    net: pd.Series, benchmark_vol: pd.Series, bars_per_year: float, n_buckets: int = 3
) -> dict[str, float | None]:
    """Sharpe by trailing benchmark-volatility tercile (low/mid/high)."""
    aligned = pd.concat([net, benchmark_vol], axis=1, join="inner").dropna()
    if len(aligned) < n_buckets * 10:
        return {}
    buckets = pd.qcut(aligned.iloc[:, 1].rank(method="first"), n_buckets, labels=False)
    labels = ["low", "mid", "high"] if n_buckets == 3 else [str(i) for i in range(n_buckets)]
    return {labels[int(b)]: sharpe(aligned.iloc[:, 0][buckets == b], bars_per_year) for b in sorted(set(buckets))}
