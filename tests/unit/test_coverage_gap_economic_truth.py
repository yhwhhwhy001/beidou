"""Coverage gap tests for beidou_research.economic_truth.

Targets the fail-closed branches not exercised by the existing E0-E6 gate
suite: empty assessments, non-serializable evidence hashing, malformed
bool/number evidence coercion, each gate's missing-field and malformed-field
FAIL paths, and the defensive unknown-gate fallthrough.
"""

from __future__ import annotations

from beidou_research.economic_truth import (
    EconomicTruthAssessment,
    GateStatus,
    _bool_flag,
    _evaluate_single_gate,
    _finite_number,
    _gate_evidence_hash,
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


def test_overall_empty_results_not_evaluated() -> None:
    assert EconomicTruthAssessment(results=()).overall is GateStatus.NOT_EVALUATED


def test_gate_evidence_hash_falls_back_to_repr_for_circular() -> None:
    circular: dict = {}
    circular["self"] = circular
    digest = _gate_evidence_hash(circular)
    assert len(digest) == 64


def test_bool_flag_returns_none_for_missing_and_malformed() -> None:
    assert _bool_flag({"x": None}, "x") is None
    assert _bool_flag({"x": "yes"}, "x") is None
    assert _bool_flag({"x": True}, "x") is True


def test_finite_number_rejects_bad_values() -> None:
    assert _finite_number({"x": None}, "x") is None
    assert _finite_number({"x": "not-a-number"}, "x") is None
    assert _finite_number({"x": 3.0}, "x") == 3.0


def test_lookahead_audit_must_be_mapping() -> None:
    evidence = _full_pass_evidence()
    evidence["data"]["lookahead_audit"] = "not-a-dict"
    assessment = assess_economic_truth(evidence)
    assert assessment.results[0].status is GateStatus.FAIL
    assert "MALFORMED_LOOKAHEAD_AUDIT" in assessment.results[0].reasons


def test_e0_requires_market_data_assessment() -> None:
    evidence = _full_pass_evidence()
    del evidence["data"]["market_data_assessment"]

    assessment = assess_economic_truth(evidence)

    assert assessment.results[0].status is GateStatus.NOT_EVALUATED
    assert "MISSING_EVIDENCE:market_data_assessment" in assessment.results[0].reasons


def test_e0_rejects_malformed_market_data_assessment() -> None:
    evidence = _full_pass_evidence()
    evidence["data"]["market_data_assessment"] = "PASS"

    assessment = assess_economic_truth(evidence)

    assert assessment.results[0].status is GateStatus.FAIL
    assert "MARKET_DATA_ASSESSMENT_MALFORMED" in assessment.results[0].reasons


def test_lookahead_violations_non_int_counts_as_one() -> None:
    evidence = _full_pass_evidence()
    evidence["data"]["lookahead_audit"] = {"passed": True, "violations": "many"}
    assessment = assess_economic_truth(evidence)
    assert "LOOKAHEAD_VIOLATIONS:1" in assessment.results[0].reasons


def test_e1_missing_field_not_evaluated() -> None:
    evidence = _full_pass_evidence()
    del evidence["factor_sanity"]["non_constant"]
    assessment = assess_economic_truth(evidence)
    assert assessment.results[1].status is GateStatus.NOT_EVALUATED
    assert "MISSING_EVIDENCE:non_constant" in assessment.results[1].reasons


def test_e1_undeclared_missing_handling_fails() -> None:
    evidence = _full_pass_evidence()
    evidence["factor_sanity"]["missing_handling"] = ""
    assessment = assess_economic_truth(evidence)
    assert "MISSING_HANDLING_UNDECLARED" in assessment.results[1].reasons


def test_e2_missing_field_not_evaluated() -> None:
    evidence = _full_pass_evidence()
    del evidence["significance"]["adjusted_p_value"]
    assessment = assess_economic_truth(evidence)
    assert assessment.results[2].status is GateStatus.NOT_EVALUATED


def test_e2_not_corrected_fails() -> None:
    evidence = _full_pass_evidence()
    evidence["significance"]["multiple_testing_corrected"] = False
    assessment = assess_economic_truth(evidence)
    assert "MULTIPLE_TESTING_NOT_CORRECTED" in assessment.results[2].reasons


def test_e2_no_p_hacking_controls_fails() -> None:
    evidence = _full_pass_evidence()
    evidence["significance"]["p_hacking_controls"] = []
    assessment = assess_economic_truth(evidence)
    assert "NO_P_HACKING_CONTROLS" in assessment.results[2].reasons


def test_e2_invalid_alpha_fails() -> None:
    evidence = _full_pass_evidence()
    evidence["significance"]["alpha"] = 0.0
    assessment = assess_economic_truth(evidence)
    assert "INVALID_P_VALUE_OR_ALPHA" in assessment.results[2].reasons


def test_e3_missing_field_not_evaluated() -> None:
    evidence = _full_pass_evidence()
    del evidence["oos"]["cpcv"]
    assessment = assess_economic_truth(evidence)
    assert assessment.results[3].status is GateStatus.NOT_EVALUATED


def test_e4_missing_field_not_evaluated() -> None:
    evidence = _full_pass_evidence()
    del evidence["after_cost"]["fee_bps"]
    assessment = assess_economic_truth(evidence)
    assert assessment.results[4].status is GateStatus.NOT_EVALUATED


def test_e4_costs_not_subtracted_fails() -> None:
    evidence = _full_pass_evidence()
    evidence["after_cost"]["costs_subtracted"] = False
    assessment = assess_economic_truth(evidence)
    assert "COSTS_NOT_SUBTRACTED" in assessment.results[4].reasons


def test_e4_invalid_expected_return_fails() -> None:
    evidence = _full_pass_evidence()
    evidence["after_cost"]["expected_return_after_cost"] = "bad"
    assessment = assess_economic_truth(evidence)
    assert "INVALID_EXPECTED_RETURN" in assessment.results[4].reasons


def test_e4_invalid_cost_fails() -> None:
    evidence = _full_pass_evidence()
    evidence["after_cost"]["fee_bps"] = "bad"
    assessment = assess_economic_truth(evidence)
    assert "INVALID_COST:fee_bps" in assessment.results[4].reasons


def test_e5_missing_field_not_evaluated() -> None:
    evidence = _full_pass_evidence()
    del evidence["paper"]["behavior_match_ratio"]
    assessment = assess_economic_truth(evidence)
    assert assessment.results[5].status is GateStatus.NOT_EVALUATED


def test_e5_invalid_ratio_fails() -> None:
    evidence = _full_pass_evidence()
    evidence["paper"]["behavior_match_ratio"] = 1.5
    assessment = assess_economic_truth(evidence)
    assert "INVALID_BEHAVIOR_MATCH_RATIO" in assessment.results[5].reasons


def test_e5_behavior_mismatch_fails() -> None:
    evidence = _full_pass_evidence()
    evidence["paper"]["behavior_match_ratio"] = 0.5
    assessment = assess_economic_truth(evidence)
    assert "BEHAVIOR_MISMATCH:ratio=0.5" in assessment.results[5].reasons


def test_e6_missing_field_not_evaluated() -> None:
    evidence = _full_pass_evidence()
    del evidence["portfolio"]["incremental_risk_adjusted_return"]
    assessment = assess_economic_truth(evidence)
    assert assessment.results[6].status is GateStatus.NOT_EVALUATED


def test_e6_invalid_incremental_fails() -> None:
    evidence = _full_pass_evidence()
    evidence["portfolio"]["incremental_risk_adjusted_return"] = "bad"
    assessment = assess_economic_truth(evidence)
    assert "INVALID_INCREMENTAL_RETURN" in assessment.results[6].reasons


def test_single_gate_unknown_falls_back_not_evaluated() -> None:
    result = _evaluate_single_gate(object(), {})
    assert result.status is GateStatus.NOT_EVALUATED
    assert "UNKNOWN_GATE" in result.reasons
