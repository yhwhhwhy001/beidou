"""Multiple-testing control: BH-FDR, Holm, Deflated Sharpe Ratio, Probability of Backtest Overfitting."""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import combinations
from typing import Any

import numpy as np

from beidou_alpha.validation.metrics import normal_cdf, normal_ppf

EULER_GAMMA = 0.5772156649015329

# The gate `oos_selection_threshold` currently applies, carried in every report it writes and checked
# by `verdict.decide`.  A threshold is a number produced by a rule; when the rule changes, reports
# already on disk keep the old number and nothing in them says so.  That happened on 2026-09-06:
# KILL-Q3 replaced `expected_max_sharpe` with `max_sharpe_quantile` three hours after the report the
# registry cites was written, and its stored 1.1446 is the retired gate's answer (the current one
# gives 1.4684 on the same inputs).  The name travels with the number so the comparison can refuse.
SELECTION_GATE = "max_sharpe_quantile"


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


def max_sharpe_quantile(n_trials: int, sharpe_variance: float, alpha: float = 0.05) -> float:
    """The ``1 - alpha`` quantile of the maximum of ``n_trials`` null Sharpes (per period).

    Solves ``Phi(x)^N = 1 - alpha``: the level below which the best of N pure-noise trials
    stays with probability ``1 - alpha``.  This is the object a *gate* needs.  Its sibling
    ``expected_max_sharpe`` returns E[max], which is what the DSR needs as a benchmark and is
    the wrong thing to compare a candidate against: a single null exceeds the expectation of
    the maximum roughly half the time, so a gate set there admits noise at about one half.
    """
    if n_trials <= 1 or sharpe_variance <= 0 or not 0.0 < alpha < 1.0:
        return 0.0
    return math.sqrt(sharpe_variance) * normal_ppf((1.0 - alpha) ** (1.0 / n_trials))


def effective_trials(returns_matrix: np.ndarray) -> float:
    """How many *independent* trials a correlated family of trials is worth (Li & Ji 2005).

    Reported, never substituted into the gate.  The ledger's trials are near-duplicates - 24 exact
    copies and a long tail of one-parameter variations - so an N_eff estimated here would be smaller
    than the ledger count and would therefore *lower* the bar.  Lowering a bar on an estimator that
    has never been validated against this ledger is the failure this round exists to prevent, so the
    number is published beside the gate and read by a person, not by ``oos_selection_threshold``.

    Li & Ji sum ``f(|lambda|) = I(|lambda| >= 1) + frac(|lambda|)`` over the correlation matrix's
    eigenvalues: 40 independent columns give ~40, 40 copies of one column give ~1.  Columns with no
    variance carry no information about correlation and are counted as themselves.
    """
    values = np.asarray(returns_matrix, dtype=float)
    if values.ndim != 2 or values.shape[1] == 0:
        return 0.0
    n_trials = values.shape[1]
    if n_trials == 1 or values.shape[0] < 2:
        return float(n_trials)
    std = values.std(axis=0, ddof=1)
    live = std > 0
    if not live.any():
        return float(n_trials)  # nothing varies: no shared structure to collapse
    correlation = np.corrcoef(values[:, live], rowvar=False)
    return effective_trials_from_correlation(correlation, dead=n_trials - int(live.sum()))


def _li_ji_count(eigenvalues: np.ndarray) -> float:
    """Li & Ji's ``f(lambda) = I(lambda >= 1) + frac(lambda)``, summed, with its integer edge closed.

    That ``frac`` makes ``f`` discontinuous at every integer from 2 up: approaching 40 from below, ``f``
    tends to 1 + 1 = 2; AT 40 it is 1 + 0 = 1.  The formula is not wrong - a real correlation matrix
    does not put an eigenvalue on an integer - but a wall of identical columns puts one exactly there,
    and that is the degenerate case this estimator is most often asked about, the one its own docstring
    promises ("40 copies of one column give ~1").

    2026-09-16: that promise broke on CI and held on this laptop, same commit, same seed.  The true
    eigenvalue of 40 identical columns is exactly 40; `eigvalsh` returns 40.000000000000014 here and a
    hair BELOW 40 on `ubuntu-latest`, and `floor` turns that hair into a whole unit - the estimator read
    1.0000000000001767 locally against 2.0000000000001084 there.  Nothing about the search changed.  It
    reported twice the independence because a BLAS rounded the other way, on a number the gate publishes
    for a person to read.

    So snap an eigenvalue onto its nearest integer when it sits within `eigvalsh`'s own error bar,
    ``n * eps * ||A||_2`` - the standard bound for a symmetric eigensolver, 3.6e-13 for that 40x40 case
    against the 1.4e-14 actually observed.  Two things make this a fix rather than a fudge: the snap is
    far below any spacing this estimator can resolve, and ``f`` is CONTINUOUS at lambda = 1 (both sides
    give 1), so the only values it can move are ones already sitting on a discontinuity.
    """
    if eigenvalues.size == 0:
        return 0.0
    nearest = np.round(eigenvalues)
    tolerance = eigenvalues.size * np.finfo(float).eps * max(float(eigenvalues.max()), 1.0)
    snapped = np.where(np.abs(eigenvalues - nearest) <= tolerance, nearest, eigenvalues)
    return float(np.sum((snapped >= 1.0).astype(float) + (snapped - np.floor(snapped))))


def effective_trials_from_correlation(correlation: np.ndarray, *, dead: int = 0) -> float:
    """Li & Ji's count taken from the correlation matrix itself - the estimator's one implementation.

    Split out 2026-09-14 (Q4b) so an artefact can persist the matrix and get the same number back.  The
    45-minute `--measure` run before it wrote one scalar and discarded the structure behind it, which
    made validating the estimator cost the 45 minutes again - a defect in the artefact, not a limit of
    the estimator, and the same failure `all_trials` names one floor down: a number nobody can see is a
    number nobody can argue with.  A number that cannot be recomputed from the artefact reporting it is
    that, one step on.

    ``dead`` is the count of columns with no variance.  They contribute nothing to a correlation and
    are counted as themselves, which is why they are passed in rather than inferred - a matrix cannot
    tell how many columns were dropped before it was built.
    """
    matrix = np.atleast_2d(np.asarray(correlation, dtype=float))
    n_live = matrix.shape[0]
    if n_live == 0:
        return 0.0 if dead == 0 else float(dead)
    if not np.all(np.isfinite(matrix)):
        return float(n_live + dead)
    counted = _li_ji_count(np.abs(np.linalg.eigvalsh(matrix)))
    return min(max(counted, 1.0), float(n_live)) + float(dead)


def family_p_value(sharpe_period: float, n_trials: int, sharpe_variance: float) -> float | None:
    """P(the best of ``n_trials`` nulls reaches ``sharpe_period``) - the gate's own p-value."""
    if sharpe_variance <= 0:
        return None
    trials = max(1, int(n_trials))
    return float(1.0 - normal_cdf(sharpe_period / math.sqrt(sharpe_variance)) ** trials)


def oos_selection_threshold(
    oos_returns: np.ndarray, *, n_trials: int, bars_per_year: float, alpha: float = 0.05
) -> dict[str, Any]:
    """D-028: the out-of-sample Sharpe a strategy must clear given how many configurations were tried.

    Walk-forward embeds the selection that happens *inside* a fold; nothing in D-020 is sensitive to the
    selection that happens *across rounds*, and the Newey-West t is not a second condition because on
    hourly returns it is the Sharpe times the square root of years to within 0.1%.  So the same deflation
    the DSR applies in sample is applied here to the OOS series instead, which penalises exactly the
    cross-round family selection and nothing else.

    The null is the sampling distribution of one OOS Sharpe estimate, not the ledger's pooled dispersion:
    that dispersion degenerated in both directions (near zero among near-duplicate trials, inflated by
    heterogeneous ones) and is what round 7 was called to fix.  The threshold is derived, never chosen.

    2026-09-06 (KILL-Q3 / F2): the threshold is the ``1 - alpha`` **quantile** of the null's maximum,
    not its **expectation**.  It had been ``expected_max_sharpe``, and a single noise curve exceeds
    E[max] roughly half the time - measured on the shipped report's own null, that gate admitted pure
    noise at 43.5%, which is no gate at all for the Sharpe 1.0-1.4 candidates the pipeline now has to
    tell apart.  ``expected_max_sharpe`` is untouched: the DSR wants the expectation as a benchmark.

    ``n_trials`` is the ledger count, and it is deliberately NOT reduced to an effective number of
    independent trials.  The ledger holds near-duplicate configurations, so the true N_eff is smaller
    and this gate is therefore **conservative** - it asks for more than the evidence strictly requires.
    That is the safe direction to be wrong in, and estimating N_eff needs a correlation structure this
    function is not given; it is recorded as owed rather than guessed.
    """
    values = np.asarray(oos_returns, dtype=float)
    values = values[np.isfinite(values)]
    n_obs = int(values.size)
    period = sharpe_per_period(values)
    trials = max(1, int(n_trials))
    if n_obs < 3 or period is None:
        return {
            "n_obs": n_obs,
            "n_trials": trials,
            "alpha": alpha,
            "gate": SELECTION_GATE,
            "variance": 0.0,
            "threshold_annual": None,
            "expected_max_annual": None,
            "p_family": None,
        }
    skew, kurt = moments(values)
    variance = sampling_variance(period, n_obs, skew, kurt)
    scale = math.sqrt(bars_per_year)
    return {
        "n_obs": n_obs,
        "n_trials": trials,
        "alpha": alpha,
        # WHICH rule produced `threshold_annual`.  Without it a stored number outlives the rule that
        # made it and `decide` compares against a gate nobody can name (KILL-Q3's leftover).
        "gate": SELECTION_GATE,
        "variance": variance,
        "threshold_annual": max_sharpe_quantile(trials, variance, alpha) * scale,
        # reported beside the gate so the change from E[max] to the quantile stays visible in the artefact
        "expected_max_annual": expected_max_sharpe(trials, variance) * scale,
        "p_family": family_p_value(period, trials, variance),
        "oos_sharpe_annual": period * scale,
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
        # Reported, never used as a denominator (DL-R3).  Scoped to this run's grid because the
        # ledger stores Sharpes, not return series - a ledger-wide N_eff is not computable from
        # anything this function is handed, which is the concrete reason KILL-R25 stays owed.
        "grid_effective_trials": effective_trials(trial_returns_matrix),
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
