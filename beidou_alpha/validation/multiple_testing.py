"""Multiple-testing control: BH-FDR, Holm, Deflated Sharpe Ratio, Probability of Backtest Overfitting."""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import combinations
from typing import Any

import numpy as np

from beidou_alpha.validation.metrics import normal_cdf, normal_ppf

EULER_GAMMA = 0.5772156649015329


def benjamini_hochberg(p_values: list[float]) -> list[float]:
    """BH-adjusted p-values (monotone)."""
    n = len(p_values)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: p_values[i])
    adjusted = [0.0] * n
    running = 1.0
    for rank in range(n, 0, -1):
        index = order[rank - 1]
        running = min(running, p_values[index] * n / rank)
        adjusted[index] = min(1.0, running)
    return adjusted


def holm(p_values: list[float]) -> list[float]:
    """Holm step-down adjusted p-values (monotone)."""
    n = len(p_values)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: p_values[i])
    adjusted = [0.0] * n
    running = 0.0
    for k, index in enumerate(order):
        running = max(running, min(1.0, p_values[index] * (n - k)))
        adjusted[index] = running
    return adjusted


def expected_max_sharpe(n_trials: int, sharpe_variance: float) -> float:
    """E[max SR] under the null across ``n_trials`` trials with SR variance ``sharpe_variance`` (per period)."""
    if n_trials <= 1 or sharpe_variance <= 0:
        return 0.0
    std = math.sqrt(sharpe_variance)
    return std * (
        (1.0 - EULER_GAMMA) * normal_ppf(1.0 - 1.0 / n_trials)
        + EULER_GAMMA * normal_ppf(1.0 - 1.0 / (n_trials * math.e))
    )


@dataclass(frozen=True)
class DeflatedSharpe:
    sharpe_period: float
    benchmark_sharpe: float
    dsr: float
    p_value: float
    n_trials: int
    n_obs: int


def deflated_sharpe_ratio(
    sharpe_period: float,
    *,
    n_trials: int,
    sharpe_variance: float,
    n_obs: int,
    skewness: float = 0.0,
    kurtosis: float = 3.0,
) -> DeflatedSharpe:
    """Bailey & López de Prado (2014).  All Sharpe inputs are *per period* (not annualised)."""
    if n_obs < 3:
        return DeflatedSharpe(sharpe_period, 0.0, 0.0, 1.0, n_trials, n_obs)
    sr0 = expected_max_sharpe(n_trials, sharpe_variance)
    denominator = 1.0 - skewness * sharpe_period + (kurtosis - 1.0) / 4.0 * sharpe_period**2
    if denominator <= 0:
        return DeflatedSharpe(sharpe_period, sr0, 0.0, 1.0, n_trials, n_obs)
    z = (sharpe_period - sr0) * math.sqrt(n_obs - 1) / math.sqrt(denominator)
    dsr = normal_cdf(z)
    return DeflatedSharpe(sharpe_period, sr0, dsr, 1.0 - dsr, n_trials, n_obs)


def sampling_variance(sharpe_period: float, n_obs: int, skewness: float = 0.0, kurtosis: float = 3.0) -> float:
    """Var[SR-hat] of one per-period Sharpe estimate (Bailey & López de Prado 2012): the null's noise floor.

    The verdict deflates with the *pooled empirical* variance of all trials (the
    ledger).  When trials are heterogeneous that overstates the null; this
    sampling variance understates it.  Both are reported (``noise_null`` is
    informational only; D-024).
    """
    if n_obs < 2:
        return 0.0
    return max(0.0, (1.0 - skewness * sharpe_period + (kurtosis - 1.0) / 4.0 * sharpe_period**2) / (n_obs - 1))


def sharpe_per_period(returns: np.ndarray) -> float | None:
    values = np.asarray(returns, dtype=float)
    values = values[np.isfinite(values)]
    if values.size < 2:
        return None
    std = float(np.std(values, ddof=1))
    return float(np.mean(values) / std) if std > 0 else None


def moments(returns: np.ndarray) -> tuple[float, float]:
    """(skewness, kurtosis) with kurtosis of a normal = 3."""
    values = np.asarray(returns, dtype=float)
    values = values[np.isfinite(values)]
    if values.size < 4:
        return 0.0, 3.0
    centered = values - values.mean()
    variance = float(np.mean(centered**2))
    if variance <= 0:
        return 0.0, 3.0
    return float(np.mean(centered**3) / variance**1.5), float(np.mean(centered**4) / variance**2)


@dataclass(frozen=True)
class PBOResult:
    pbo: float
    n_combinations: int
    logits: tuple[float, ...]
    degradation_slope: float | None
    prob_oos_loss: float | None


def probability_of_backtest_overfitting(
    returns_matrix: np.ndarray, *, n_subsets: int = 16, max_combinations: int = 5000, seed: int = 0
) -> PBOResult:
    """CSCV (Bailey, Borwein, López de Prado & Zhu 2017) on a T x N matrix of per-period trial returns."""
    matrix = np.asarray(returns_matrix, dtype=float)
    if matrix.ndim != 2 or matrix.shape[1] < 2 or matrix.shape[0] < n_subsets * 2:
        raise ValueError("need a T x N matrix with N >= 2 and T >= 2 * n_subsets")
    if n_subsets % 2:
        raise ValueError("n_subsets must be even")
    n_obs, n_trials = matrix.shape
    bounds = np.linspace(0, n_obs, n_subsets + 1, dtype=int)
    subsets = [np.arange(bounds[i], bounds[i + 1]) for i in range(n_subsets)]
    all_combos = list(combinations(range(n_subsets), n_subsets // 2))
    if len(all_combos) > max_combinations:
        rng = np.random.default_rng(seed)
        picked = rng.choice(len(all_combos), size=max_combinations, replace=False)
        all_combos = [all_combos[i] for i in sorted(picked)]

    def perf(rows: np.ndarray) -> np.ndarray:
        block = matrix[rows]
        std = block.std(axis=0, ddof=1)
        mean = block.mean(axis=0)
        return np.where(std > 0, mean / np.where(std > 0, std, 1.0), -np.inf)

    logits: list[float] = []
    is_best: list[float] = []
    oos_best: list[float] = []
    for combo in all_combos:
        train_rows = np.concatenate([subsets[i] for i in combo])
        test_rows = np.concatenate([subsets[i] for i in range(n_subsets) if i not in combo])
        train_perf = perf(train_rows)
        test_perf = perf(test_rows)
        best = int(np.argmax(train_perf))
        rank = float(np.sum(test_perf <= test_perf[best]))
        omega = rank / (n_trials + 1.0)
        omega = min(max(omega, 1e-9), 1.0 - 1e-9)
        logits.append(math.log(omega / (1.0 - omega)))
        is_best.append(float(train_perf[best]))
        oos_best.append(float(test_perf[best]))
    logit_array = np.asarray(logits)
    pbo = float(np.mean(logit_array <= 0.0))
    slope: float | None = None
    if len(is_best) >= 2 and np.std(is_best) > 0:
        slope = float(np.polyfit(is_best, oos_best, 1)[0])
    prob_loss = float(np.mean(np.asarray(oos_best) < 0.0)) if oos_best else None
    return PBOResult(
        pbo=pbo, n_combinations=len(all_combos), logits=tuple(logits), degradation_slope=slope, prob_oos_loss=prob_loss
    )


def oos_selection_threshold(oos_returns: np.ndarray, *, n_trials: int, bars_per_year: float) -> dict[str, Any]:
    """D-027: the out-of-sample Sharpe a strategy must clear given how many configurations were tried.

    Walk-forward embeds the selection that happens *inside* a fold; nothing in D-020 is sensitive to the
    selection that happens *across rounds*, and the Newey-West t is not a second condition because on
    hourly returns it is the Sharpe times the square root of years to within 0.1%.  So the same deflation
    the DSR applies in sample is applied here to the OOS series instead, which penalises exactly the
    cross-round family selection and nothing else.

    The null is the sampling distribution of one OOS Sharpe estimate, not the ledger's pooled dispersion:
    that dispersion degenerated in both directions (near zero among near-duplicate trials, inflated by
    heterogeneous ones) and is what round 7 was called to fix.  The threshold is derived, never chosen.
    """
    values = np.asarray(oos_returns, dtype=float)
    values = values[np.isfinite(values)]
    n_obs = int(values.size)
    period = sharpe_per_period(values)
    if n_obs < 3 or period is None:
        return {"n_obs": n_obs, "n_trials": max(1, int(n_trials)), "variance": 0.0, "threshold_annual": None}
    skew, kurt = moments(values)
    variance = sampling_variance(period, n_obs, skew, kurt)
    threshold = expected_max_sharpe(max(1, int(n_trials)), variance)
    return {
        "n_obs": n_obs,
        "n_trials": max(1, int(n_trials)),
        "variance": variance,
        "threshold_annual": threshold * math.sqrt(bars_per_year),
        "oos_sharpe_annual": period * math.sqrt(bars_per_year),
    }


def multiple_testing_report(
    candidate_returns: np.ndarray,
    trial_returns_matrix: np.ndarray,
    *,
    bars_per_year: float,
    prior_trials: int = 0,
    pooled_n_trials: int | None = None,
    pooled_sharpe_variance: float | None = None,
) -> dict[str, Any]:
    """DSR for the chosen candidate against all trials, plus PBO over the trial matrix.

    ``prior_trials`` counts configurations of the same strategy that were
    already evaluated in earlier rounds on the same data.  They belong in the
    DSR denominator: a pass obtained by shrinking the grid after seeing the
    results is exactly the selection bias DSR exists to expose.
    """
    trial_sharpes = [sharpe_per_period(trial_returns_matrix[:, j]) for j in range(trial_returns_matrix.shape[1])]
    finite = [s for s in trial_sharpes if s is not None]
    grid_variance = float(np.var(finite, ddof=1)) if len(finite) >= 2 else 0.0
    sharpe_variance = pooled_sharpe_variance if pooled_sharpe_variance is not None else grid_variance
    n_trials = pooled_n_trials if pooled_n_trials is not None else (len(finite) or 1) + max(0, int(prior_trials))
    candidate_sharpe = sharpe_per_period(candidate_returns)
    skew, kurt = moments(candidate_returns)
    n_obs = int(np.isfinite(candidate_returns).sum())
    dsr = deflated_sharpe_ratio(
        candidate_sharpe or 0.0,
        n_trials=max(1, int(n_trials)),
        sharpe_variance=sharpe_variance,
        n_obs=n_obs,
        skewness=skew,
        kurtosis=kurt,
    )
    noise_variance = sampling_variance(candidate_sharpe or 0.0, n_obs, skew, kurt)
    noise = deflated_sharpe_ratio(
        candidate_sharpe or 0.0,
        n_trials=max(1, int(n_trials)),
        sharpe_variance=noise_variance,
        n_obs=n_obs,
        skewness=skew,
        kurtosis=kurt,
    )
    pbo = probability_of_backtest_overfitting(trial_returns_matrix) if trial_returns_matrix.shape[1] >= 2 else None
    return {
        "n_trials": max(1, int(n_trials)),
        "grid_trials": len(finite),
        "prior_trials": max(0, int(prior_trials)),
        "sharpe_variance_period": sharpe_variance,
        "candidate_sharpe_annual": None if candidate_sharpe is None else candidate_sharpe * math.sqrt(bars_per_year),
        "expected_max_sharpe_annual": dsr.benchmark_sharpe * math.sqrt(bars_per_year),
        "dsr": dsr.dsr,
        "dsr_p_value": dsr.p_value,
        "pbo": None if pbo is None else pbo.pbo,
        "pbo_combinations": None if pbo is None else pbo.n_combinations,
        "degradation_slope": None if pbo is None else pbo.degradation_slope,
        "prob_oos_loss": None if pbo is None else pbo.prob_oos_loss,
        "noise_null": {
            "sharpe_variance_period": noise_variance,
            "expected_max_sharpe_annual": noise.benchmark_sharpe * math.sqrt(bars_per_year),
            "dsr_p_value": noise.p_value,
        },
    }
