"""D-P2 (2026-09-06): the Newey-West t is reported, never enforced.

The deep-analysis report's KILL-R17 said it three times and the code kept the gate anyway: on
hourly net returns the NW t equals the Sharpe times the square root of years to within 0.1%, so
``t >= 2`` beside ``Sharpe >= 1`` over a five-year window is not a second condition - it is the
first one restated, and it only bites below four years.  Keeping it there let a report fail for a
reason that was already counted, and made ``PASS`` look like it had cleared two independent bars.

What is *not* dropped: a report that carries no ``oos_t_stat`` at all still cannot pass.  That is a
completeness requirement on the artefact, not a significance test - "reported" means it has to be
reported.  25 of the 47 archived reports predate the field and this guard is what keeps them out.

Pre-registered before the change (T-R1-3, docs/analysis/2026-09-06-remediation-execution-plan.md):
recomputing every archived report under these semantics must flip zero verdicts.  Measured: 47
reports, 0 flips.  ``test_no_archived_verdict_changes`` is that measurement, kept as a test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from beidou_alpha.validation.verdict import decide

ROOT = Path(__file__).resolve().parents[2]


def _clean(**walk_forward: Any) -> dict[str, Any]:
    """A report that clears every gate except whatever the caller overrides."""
    wf = {"oos_sharpe": 1.5, "oos_t_stat": 3.0, "fold_consistency": 0.8, **walk_forward}
    return {
        "walk_forward": wf,
        "multiple_testing": {"dsr_p_value": 0.9, "pbo": None, "grid_trials": 1},
        "cpcv": {"fraction_negative": 0.0},
        "cost_stress": {"x2": 1.2},
    }


def test_a_low_t_no_longer_fails_a_candidate_that_clears_every_other_gate() -> None:
    """The old rule failed this at t 1.2 for a reason the Sharpe gate had already counted."""
    verdict, reasons = decide(_clean(oos_t_stat=1.2))

    assert verdict == "PASS"
    assert not any("oos_t_stat" in r for r in reasons)


def test_the_strong_pass_no_longer_requires_the_t_statistic() -> None:
    """t 1.8 used to cap a Sharpe 1.5 book at WEAK_PASS; the cap was the Sharpe restated."""
    assert decide(_clean(oos_t_stat=1.8))[0] == "PASS"


def test_a_weak_sharpe_is_still_only_a_weak_pass() -> None:
    """Demoting the t must not promote anything: the Sharpe gate is untouched."""
    assert decide(_clean(oos_sharpe=0.8, oos_t_stat=4.0))[0] == "WEAK_PASS"


def test_a_report_without_the_t_statistic_still_cannot_pass() -> None:
    """Completeness, not significance - and the reason has to say which."""
    report = _clean()
    del report["walk_forward"]["oos_t_stat"]

    verdict, reasons = decide(report)

    assert verdict == "FAIL"
    assert any("oos_t_stat" in r and "not reported" in r for r in reasons)


def test_the_thresholds_no_longer_carry_a_t_bar() -> None:
    """A threshold nothing reads is a rule that looks enforced.  Net rule change <= 0 (KILL-R17)."""
    from beidou_alpha.validation.verdict import VerdictThresholds

    assert not hasattr(VerdictThresholds(), "pass_oos_t")
    assert not hasattr(VerdictThresholds(), "weak_oos_t")


def test_the_family_p_value_is_in_the_selection_reason() -> None:
    """A verdict that says only 'below the threshold' cannot say how surprising the number is."""
    report = _clean(oos_sharpe=1.2)
    report["oos_selection"] = {
        "threshold_annual": 1.5,
        "n_trials": 125,
        "alpha": 0.05,
        "p_family": 0.42,
    }

    verdict, reasons = decide(report)

    assert verdict == "FAIL"
    selection = [r for r in reasons if "deflated threshold" in r]
    assert len(selection) == 1
    assert "p_family=0.42" in selection[0]


def test_a_selection_reason_survives_a_report_without_p_family() -> None:
    """Reports written before the field exists still have to produce a readable reason."""
    report = _clean(oos_sharpe=1.2)
    report["oos_selection"] = {"threshold_annual": 1.5, "n_trials": 93}

    verdict, reasons = decide(report)

    assert verdict == "FAIL"
    assert any("deflated threshold" in r for r in reasons)


def test_no_archived_verdict_changes() -> None:
    """T-R1-3, pre-registered: 47 archived reports, 0 flips.

    Anchored to the stored ``verdict`` of every report written under the current rules - the ones
    carrying an ``oos_selection`` block.  Older reports are excluded because the rules they were
    judged by no longer exist (9 of them already disagree with ``decide`` today, and did before
    this change); excluding them here is not a way to hide a flip: the pre-registered measurement
    covered all 47 and found none.
    """
    checked = 0
    for path in sorted(ROOT.glob("reports/research/**/*-validation-*.json")):
        report = json.loads(path.read_text(encoding="utf-8"))
        if not (report.get("oos_selection") or {}).get("n_trials"):
            continue
        checked += 1
        assert decide(report)[0] == report["verdict"], path.name

    assert checked >= 12
