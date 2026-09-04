"""Promotion verdict from a validation report (pure rules; thresholds are explicit inputs).

D-020 (2026-09-04, operator decision): the verdict is out-of-sample first.  The walk-forward
OOS Sharpe and the Newey-West t-statistic of the OOS net returns decide PASS / WEAK_PASS;
fold consistency, CPCV negative-path share, PBO (for grids of >= 4) and the doubled-cost
Sharpe are hard gates.  The deflated Sharpe ratio stays in every report but no longer vetoes:
it haircuts the *in-sample* Sharpe for selection, while walk-forward selection already happens
inside the training folds.

D-028 (2026-09-04) closes what that left open.  The Newey-West t was never a second condition:
on hourly net returns it equals the Sharpe times the square root of years to within 0.1%, so over
a five-year window "t >= 2" is looser than the "Sharpe >= 1" beside it, and it only bites below
four years - it is a short-sample guard, and is documented as one.  The condition that is actually
sensitive to selection deflates the *out-of-sample* Sharpe against the number of distinct
configurations in the strategy's ledger, using the sampling distribution of an OOS Sharpe estimate
as the null rather than the ledger's pooled dispersion.  Reports written before this field exists
carry no ``oos_selection`` block and are not judged by it.  Before D-020 the DSR p-value (<= 0.05 / 0.10) was a veto, and its pooled
variance was degenerate in both directions (near-zero with near-duplicate trials, inflated by
legacy configurations); see docs/RESEARCH_LOG.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class VerdictThresholds:
    pass_oos_sharpe: float = 1.0
    weak_oos_sharpe: float = 0.5
    pass_oos_t: float = 2.0  # Newey-West t of the walk-forward OOS net returns
    weak_oos_t: float = 1.5
    min_fold_consistency: float = 0.6
    max_cpcv_negative: float = 0.10  # share of CPCV paths with a negative OOS Sharpe
    max_pbo: float = 0.30
    min_cost_stress_sharpe: float = 0.0
    min_trials_for_pbo: int = 4
    enforce_oos_selection: bool = True  # D-028: the OOS Sharpe must clear the deflated threshold


def decide(report: dict[str, Any], thresholds: VerdictThresholds | None = None) -> tuple[str, list[str]]:
    t = thresholds or VerdictThresholds()
    wf = report.get("walk_forward", {}) or {}
    mt = report.get("multiple_testing", {}) or {}
    cpcv = report.get("cpcv", {}) or {}
    stress = report.get("cost_stress", {}) or {}
    reasons: list[str] = []
    oos = wf.get("oos_sharpe")
    oos_t = wf.get("oos_t_stat")
    consistency = wf.get("fold_consistency")
    negative = cpcv.get("fraction_negative")
    pbo = mt.get("pbo")
    stress_x2 = stress.get("x2")
    if oos is None:
        return "FAIL", ["no out-of-sample Sharpe"]
    if oos < t.weak_oos_sharpe:
        reasons.append(f"oos_sharpe {oos:.2f} < {t.weak_oos_sharpe}")
    if oos_t is None or oos_t < t.weak_oos_t:
        reasons.append(f"oos_t_stat {oos_t} < {t.weak_oos_t}")
    selection = report.get("oos_selection") or {}
    threshold = selection.get("threshold_annual")
    if t.enforce_oos_selection and threshold is not None and oos < threshold:
        reasons.append(
            f"oos_sharpe {oos:.2f} < the deflated threshold {threshold:.2f} at {selection.get('n_trials')} trials"
        )
    if consistency is not None and consistency < t.min_fold_consistency:
        reasons.append(f"fold_consistency {consistency:.2f} < {t.min_fold_consistency}")
    if negative is not None and negative > t.max_cpcv_negative:
        reasons.append(f"cpcv fraction_negative {negative:.2f} > {t.max_cpcv_negative}")
    grid_trials = mt.get("grid_trials", mt.get("n_trials"))
    if pbo is not None and pbo > t.max_pbo:
        if grid_trials is not None and int(grid_trials) < t.min_trials_for_pbo:
            # CSCV needs several strategies to rank; with two configurations PBO is a coin flip that only
            # says the two trade places across sub-periods.  Reported, not enforced.
            pass
        else:
            reasons.append(f"pbo {pbo:.2f} > {t.max_pbo}")
    if stress_x2 is not None and stress_x2 < t.min_cost_stress_sharpe:
        reasons.append(f"cost stress x2 sharpe {stress_x2:.2f} < {t.min_cost_stress_sharpe}")
    if reasons:
        return "FAIL", reasons
    strong = oos >= t.pass_oos_sharpe and oos_t is not None and oos_t >= t.pass_oos_t
    return ("PASS" if strong else "WEAK_PASS"), []
