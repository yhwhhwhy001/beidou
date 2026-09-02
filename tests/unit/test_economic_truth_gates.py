"""E0–E6 Economic Truth gates: fail-closed evidence contracts (V4.0 §C)."""

from __future__ import annotations

import pytest

from beidou_research.economic_truth import (
    EconomicTruthAssessment,
    GateStatus,
    TruthGate,
    assess_economic_truth,
)


def _full_pass_evidence() -> dict:
    return {
        "data": {
            "pit_manifest_hash": "pit-hash",
            "closed_bar_manifest_hash": "closed-bar-hash",
            "lineage_hash": "lineage-hash",
            "lookahead_audit": {"passed": True, "violations": 0},
            "market_data_assessment": {
                "status": "PASS",
                "manifest_hash": "a" * 64,
                "schema_version": "2.0",
                "source_class": "OFFICIAL_PUBLIC_ARCHIVE",
                "environment": "PUBLIC_READ_ONLY",
                "intended_use": "ECONOMIC_RESEARCH",
                "reasons": [],
            },
        },
        "factor_sanity": {
            "non_constant": True,
            "finite": True,
            "direction_plausible": True,
            "missing_handling": "forward-fill-then-flag",
            "sensitivity_passed": True,
        },
        "significance": {
            "multiple_testing_corrected": True,
            "adjusted_p_value": 0.01,
            "alpha": 0.05,
            "p_hacking_controls": ["preregistered", "family-wise-adjusted"],
        },
        "oos": {
            "purged_wfo": True,
            "cpcv": True,
            "embargo": True,
            "regime_perturbation": True,
            "parameter_perturbation": True,
        },
        "after_cost": {
            "costs_subtracted": True,
            "expected_return_after_cost": 0.02,
            "fee_bps": 4.0,
            "funding_bps": 1.0,
            "spread_bps": 2.0,
            "slippage_bps": 3.0,
        },
        "paper": {
            "behavior_match_ratio": 0.95,
            "synthetic_promotion": False,
        },
        "portfolio": {
            "incremental_risk_adjusted_return": 0.01,
        },
    }


def _statuses(assessment: EconomicTruthAssessment) -> dict[TruthGate, GateStatus]:
    return {result.gate: result.status for result in assessment.results}


def test_empty_evidence_is_not_evaluated_everywhere() -> None:
    assessment = assess_economic_truth()
    assert assessment.overall is GateStatus.NOT_EVALUATED
    assert all(status is GateStatus.NOT_EVALUATED for status in _statuses(assessment).values())
    assert assessment.results[0].gate is TruthGate.E0
    # later gates are blocked by their immediate predecessor, forming a
    # NOT_EVALUATED chain down the ladder.
    assert "PREREQUISITE_DATA_TRUSTED_NOT_EVALUATED" in assessment.results[1].reasons[0]
    assert "PREREQUISITE_FACTOR_SANITY_NOT_EVALUATED" in assessment.results[2].reasons[0]


def test_full_evidence_passes_all_gates() -> None:
    assessment = assess_economic_truth(_full_pass_evidence())
    assert assessment.overall is GateStatus.PASS
    assert all(status is GateStatus.PASS for status in _statuses(assessment).values())


def test_lookahead_violation_fails_e0_and_blocks_later_gates() -> None:
    evidence = _full_pass_evidence()
    evidence["data"]["lookahead_audit"] = {"passed": False, "violations": 3}
    assessment = assess_economic_truth(evidence)
    statuses = _statuses(assessment)
    assert statuses[TruthGate.E0] is GateStatus.FAIL
    assert "LOOKAHEAD_VIOLATIONS:3" in assessment.results[0].reasons
    assert assessment.overall is GateStatus.FAIL
    assert statuses[TruthGate.E1] is GateStatus.NOT_EVALUATED
    assert "PREREQUISITE_DATA_TRUSTED_FAIL" in assessment.results[1].reasons[0]
    for gate in (TruthGate.E2, TruthGate.E3, TruthGate.E4, TruthGate.E5, TruthGate.E6):
        assert statuses[gate] is GateStatus.NOT_EVALUATED


def test_demo_market_data_assessment_fails_e0() -> None:
    evidence = _full_pass_evidence()
    evidence["data"]["market_data_assessment"] = {
        "status": "FAIL",
        "manifest_hash": "b" * 64,
        "schema_version": "2.0",
        "source_class": "EXCHANGE_DEMO_API",
        "environment": "DEMO",
        "intended_use": "EXECUTION_ONLY",
        "reasons": ["SOURCE_NOT_RESEARCH_ELIGIBLE:EXCHANGE_DEMO_API"],
    }

    assessment = assess_economic_truth(evidence)

    assert _statuses(assessment)[TruthGate.E0] is GateStatus.FAIL
    assert "MARKET_DATA_NOT_TRUSTED" in assessment.results[0].reasons


def test_after_cost_negative_fails_e4() -> None:
    evidence = _full_pass_evidence()
    evidence["after_cost"]["expected_return_after_cost"] = -0.01
    assessment = assess_economic_truth(evidence)
    statuses = _statuses(assessment)
    assert statuses[TruthGate.E4] is GateStatus.FAIL
    assert statuses[TruthGate.E5] is GateStatus.NOT_EVALUATED
    assert "PREREQUISITE_AFTER_COST_POSITIVE_FAIL" in assessment.results[5].reasons[0]
    assert assessment.overall is GateStatus.FAIL


def test_synthetic_promotion_fails_e5() -> None:
    evidence = _full_pass_evidence()
    evidence["paper"]["synthetic_promotion"] = True
    assessment = assess_economic_truth(evidence)
    assert _statuses(assessment)[TruthGate.E5] is GateStatus.FAIL
    assert "SYNTHETIC_PROMOTION" in assessment.results[5].reasons


def test_negative_incremental_return_fails_e6() -> None:
    evidence = _full_pass_evidence()
    evidence["portfolio"]["incremental_risk_adjusted_return"] = -0.001
    assessment = assess_economic_truth(evidence)
    assert _statuses(assessment)[TruthGate.E6] is GateStatus.FAIL
    assert assessment.overall is GateStatus.FAIL


def test_malformed_evidence_is_never_trusted() -> None:
    assessment = assess_economic_truth(
        {
            "factor_sanity": {"non_constant": "yes", "finite": 1},
            "significance": {"adjusted_p_value": "not-a-number"},
        }
    )
    # E0 missing first, so everything stays fail-closed NOT_EVALUATED.
    assert assessment.overall is GateStatus.NOT_EVALUATED
    assert all(status is GateStatus.NOT_EVALUATED for status in _statuses(assessment).values())


def test_to_dict_roundtrip_shape() -> None:
    payload = assess_economic_truth(_full_pass_evidence()).to_dict()
    assert payload["overall"] == "PASS"
    assert len(payload["gates"]) == 7
    assert payload["gates"][0] == {
        "gate": "DATA_TRUSTED",
        "status": "PASS",
        "reasons": [],
        "evidence_hash": payload["gates"][0]["evidence_hash"],
    }


GATE_INDEX = {gate: index for index, gate in enumerate(TruthGate)}


@pytest.mark.parametrize("broken_gate", [TruthGate.E1, TruthGate.E2, TruthGate.E3])
def test_each_gate_failure_blocks_every_later_gate(broken_gate: TruthGate) -> None:
    evidence = _full_pass_evidence()
    if broken_gate is TruthGate.E1:
        evidence["factor_sanity"]["non_constant"] = False
    elif broken_gate is TruthGate.E2:
        evidence["significance"]["adjusted_p_value"] = 0.5
    else:
        evidence["oos"]["purged_wfo"] = False
    assessment = assess_economic_truth(evidence)
    statuses = _statuses(assessment)
    assert statuses[broken_gate] is GateStatus.FAIL
    broken_index = GATE_INDEX[broken_gate]
    for later in GATE_INDEX:
        if GATE_INDEX[later] <= broken_index:
            continue
        assert statuses[later] is GateStatus.NOT_EVALUATED
