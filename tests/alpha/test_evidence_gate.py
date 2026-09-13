"""The configuration that runs must be the configuration its cited evidence validated (round-6 audit gap).

The digest check proves the report was not edited.  It says nothing about the params sitting next to it
in the registry, so before this gate an operator could change a parameter and keep pointing at a report
that validated something else - the KILL-027 failure one level up.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from beidou_alpha.registry import StrategyEntry, evidence_params, evidence_problems, param_problems
from beidou_alpha.signals import get_signal
from beidou_live.composition import load_registry
from beidou_live.config import registry_evidence_problems
from beidou_shared.config import load_yaml

ROOT = Path(__file__).resolve().parents[2]
WEEKLY: dict[str, Any] = {"horizons": [168, 336, 720], "horizon_weights": [0.2, 0.3, 0.5], "entry_threshold": 0.2}


def canonical(strategy_id: str, params: dict[str, Any]) -> dict[str, Any]:
    return get_signal(strategy_id).canonical_params(params)


def _validation_report(params: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "validation", "verdict": "PASS", "best_params": params}


def test_defaults_do_not_count_as_a_difference() -> None:
    """A registry lists what the operator wrote; a report records every field.  Same config, different text."""
    entry = StrategyEntry("tsmom", params=WEEKLY)
    full = canonical("tsmom", WEEKLY)  # every TsmomParams field, defaults filled in
    assert set(full) > set(WEEKLY)
    assert param_problems(entry, _validation_report(full), canonical) == []
    assert param_problems(entry, _validation_report(dict(WEEKLY)), canonical) == []


def test_a_changed_parameter_is_named_with_both_values() -> None:
    entry = StrategyEntry("tsmom", params={**WEEKLY, "entry_threshold": 0.3})
    problems = param_problems(entry, _validation_report(canonical("tsmom", WEEKLY)), canonical)
    assert len(problems) == 1
    assert "entry_threshold" in problems[0] and "registry 0.3" in problems[0] and "evidence 0.2" in problems[0]
    # the modifier that caused KILL-027 is exactly this shape
    crowded = StrategyEntry("tsmom", params={**WEEKLY, "crowding_window": 72})
    assert "crowding_window" in param_problems(crowded, _validation_report(canonical("tsmom", WEEKLY)), canonical)[0]


def test_a_book_report_is_checked_against_its_sleeve_params() -> None:
    params = {"window": 168, "scale": 0.05, "short_gate": 0.3, "long_side": False}
    report = {"kind": "book", "book_verdict": "ACCEPT", "sleeve": {"strategy": "flow", "params": params}}
    assert evidence_params(report) == params
    assert param_problems(StrategyEntry("flow", params=params), report, canonical) == []
    changed = StrategyEntry("flow", params={**params, "short_gate": 0.5})
    assert "short_gate" in param_problems(changed, report, canonical)[0]


def test_a_report_without_parameters_is_a_problem_not_a_pass() -> None:
    assert evidence_params({"kind": "validation"}) is None
    problems = param_problems(StrategyEntry("tsmom", params=WEEKLY), {"kind": "validation"}, canonical)
    assert problems and "records no parameters" in problems[0]


def test_the_gate_only_runs_when_the_report_itself_is_sound() -> None:
    """A missing or tampered report is already a problem; do not also report a parameter mismatch against it."""
    entry = StrategyEntry("tsmom", params=WEEKLY, evidence={"report": "gone.json", "sha256": "a" * 64})
    problems = evidence_problems(
        entry,
        exists=lambda _: False,
        sha256_of=lambda _: "a" * 64,
        read_report=lambda _: _validation_report({"entry_threshold": 0.9}),
        canonical_params=canonical,
    )
    assert len(problems) == 1 and "report missing" in problems[0]


def test_evidence_problems_reports_a_param_drift_end_to_end() -> None:
    report = _validation_report(canonical("tsmom", WEEKLY))
    drifted = StrategyEntry(
        "tsmom",
        params={**WEEKLY, "return_scale": 0.3},
        evidence={"report": "r.json", "sha256": "b" * 64, "verdict": "PASS"},
    )
    problems = evidence_problems(
        drifted,
        exists=lambda _: True,
        sha256_of=lambda _: "b" * 64,
        read_report=lambda _: report,
        canonical_params=canonical,
    )
    assert len(problems) == 1 and "return_scale" in problems[0]
    # without the normaliser the gate stays silent: existing callers keep their behaviour
    assert (
        evidence_problems(drifted, exists=lambda _: True, sha256_of=lambda _: "b" * 64, read_report=lambda _: report)
        == []
    )


def test_the_shipped_registry_runs_what_its_evidence_validated() -> None:
    """The guard that matters: every enabled strategy on this branch, against the reports it cites.

    The profile is an argument here, and that is the point of this test rather than a detail of it.  This
    test carries the shipped pair's name but for a long time called the gate with the REGISTRY ALONE, so
    it only ever reached the evidence pointers and their digests; the portfolio-level comparison
    `registry_evidence_problems` grew later was never exercised from the one place named for it.

    2026-09-14 is what that cost.  `vol_target` 0.30 -> 0.60 (96d659ae) left the shipped profile
    disagreeing with the shipped registry - `tsmom: portfolio vol_target is 0.6 live but 0.3 in the cited
    evidence` - which `beidou_cli/live_cmd.py:301` turns into a refusal for any armed run.  This test
    stayed green.  What went red was a transaction drill in `tests/governance/`, whose entire message is
    `assert 'ROLLBACK' == 'APPLY'`: it names neither the file, nor the field, nor the loop that would
    refuse to start.  The pair is checked here, where the failure can say so.
    """
    problems = registry_evidence_problems(
        load_registry(ROOT / "config" / "alpha_registry.yaml"),
        load_yaml(ROOT / "config" / "live.demo.yaml"),
    )
    assert problems == [], problems


@pytest.mark.parametrize("signal_id", sorted(get_signal(s).id for s in ("tsmom", "xsmom", "carry", "flow")))
def test_every_signal_can_normalise_its_own_registry_params(signal_id: str) -> None:
    registry = load_registry(ROOT / "config" / "alpha_registry.yaml")
    entry = next(e for e in registry.strategies if e.id == signal_id)
    filled = canonical(signal_id, entry.params)
    assert set(filled) >= set(entry.params)
    for key, value in entry.params.items():
        assert filled[key] == value, key  # normalisation fills gaps, it never rewrites what was written


# --- D-029: a probe may cite a REJECT, but only when the registry acknowledges it -------------------


def _book_report(verdict: str = "REJECT") -> dict[str, Any]:
    return {
        "kind": "book",
        "book_verdict": verdict,
        "sleeve": {"strategy": "flow", "params": {"window": 168}},
        "fraction": 0.333333,
    }


def _probe(**overrides: Any) -> dict[str, Any]:
    block = {
        "accepted_by": "operator",
        "accepted_on": "2026-09-03",
        "accepted_despite": "REJECT",
        "reason": "bounded experiment; the robustness leg cannot validate this sleeve",
        "stop": {"window_days": 30, "max_loss": 0.01},
        "review_after_days": 30,
    }
    block.update(overrides)
    return block


def _flow(probe: dict[str, Any], verdict: str = "REJECT") -> StrategyEntry:
    return StrategyEntry(
        "flow",
        params={"window": 168},
        evidence={"report": "b.json", "sha256": "c" * 64, "verdict": verdict},
        book="flow_short",
        probe=probe,
    )


def _check(entry: StrategyEntry, report: dict[str, Any]) -> list[str]:
    return evidence_problems(
        entry,
        exists=lambda _: True,
        sha256_of=lambda _: "c" * 64,
        read_report=lambda _: report,
        book_fraction=0.333333,
        canonical_params=canonical,
    )


def test_an_acknowledged_reject_runs() -> None:
    assert _check(_flow(_probe()), _book_report()) == []


def test_an_unacknowledged_reject_is_refused() -> None:
    problems = _check(_flow(_probe(accepted_despite=None)), _book_report())
    assert problems and "accepted_despite: REJECT" in problems[0]
    # acknowledging a different verdict than the one cited does not count
    wrong = _check(_flow(_probe(accepted_despite="ACCEPT")), _book_report())
    assert wrong and "accepted_despite: REJECT" in wrong[0]


def test_a_reject_needs_a_reason_and_a_review_date() -> None:
    assert any("probe.reason" in p for p in _check(_flow(_probe(reason="  ")), _book_report()))
    assert any("review_after_days" in p for p in _check(_flow(_probe(review_after_days=None)), _book_report()))


def test_the_cited_report_must_carry_the_verdict_the_registry_claims() -> None:
    problems = _check(_flow(_probe()), _book_report("ACCEPT"))
    assert problems and "verdict REJECT" in problems[0]


def test_an_accept_still_needs_no_acknowledgement() -> None:
    plain = {k: v for k, v in _probe().items() if k != "accepted_despite"}
    assert _check(_flow(plain, verdict="ACCEPT"), _book_report("ACCEPT")) == []


def test_a_reject_may_not_run_as_the_main_book() -> None:
    main = StrategyEntry(
        "flow",
        params={"window": 168},
        evidence={"report": "b.json", "sha256": "c" * 64, "verdict": "REJECT"},
        probe=_probe(),
    )
    assert any("non-main probe book" in p for p in _check(main, _book_report()))
