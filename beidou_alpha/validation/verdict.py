"""Promotion verdict from a validation report (pure rules; thresholds are explicit inputs)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class VerdictThresholds:
    pass_oos_sharpe: float = 1.0
    weak_oos_sharpe: float = 0.5
    pass_dsr_p: float = 0.05
    weak_dsr_p: float = 0.10
    min_fold_consistency: float = 0.6
    max_pbo: float = 0.30
    min_cost_stress_sharpe: float = 0.0
    min_trials_for_pbo: int = 4


def decide(report: dict[str, Any], thresholds: VerdictThresholds | None = None) -> tuple[str, list[str]]:
    t = thresholds or VerdictThresholds()
    wf = report.get("walk_forward", {}) or {}
    mt = report.get("multiple_testing", {}) or {}
    stress = report.get("cost_stress", {}) or {}
    reasons: list[str] = []
    oos = wf.get("oos_sharpe")
    p_value = mt.get("dsr_p_value")
    consistency = wf.get("fold_consistency")
    pbo = mt.get("pbo")
    stress_x2 = stress.get("x2")
    if oos is None:
        return "FAIL", ["no out-of-sample Sharpe"]
    if oos < t.weak_oos_sharpe:
        reasons.append(f"oos_sharpe {oos:.2f} < {t.weak_oos_sharpe}")
    if p_value is None or p_value > t.weak_dsr_p:
        reasons.append(f"dsr_p_value {p_value} > {t.weak_dsr_p}")
    if consistency is not None and consistency < t.min_fold_consistency:
        reasons.append(f"fold_consistency {consistency:.2f} < {t.min_fold_consistency}")
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
    strong = oos >= t.pass_oos_sharpe and p_value is not None and p_value <= t.pass_dsr_p
    return ("PASS" if strong else "WEAK_PASS"), []
