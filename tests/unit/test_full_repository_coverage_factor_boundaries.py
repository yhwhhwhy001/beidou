"""Behavior-backed coverage for factor evidence and lifecycle boundaries."""

from __future__ import annotations

from dataclasses import replace

import pytest

from beidou_research.factors.factor import (
    FactorDefinition,
    FactorEvaluator,
    FactorLifecycle,
    FactorPerformance,
    FactorPromotionGate,
    FactorRecord,
    FactorRegistry,
    MarginalContribution,
    RegimeConditionalForecastEvidence,
    bridge_to_factor_evidence,
    build_regime_reliability,
)
from beidou_shared.types import SchemaVersion, VenueId


def _definition(factor_id: str = "factor-boundary") -> FactorDefinition:
    return FactorDefinition(
        factor_id=factor_id,
        name=factor_id,
        version=SchemaVersion("1.0.0"),
        description="boundary factor",
        author="test",
        category="momentum",
        universe=frozenset({VenueId("BINANCE")}),
        instrument_types=frozenset({"perpetual"}),
        economic_rationale="trend persistence",
        lookback_period="1h",
        rebalance_interval="5m",
    )


def _performance(*, sample_count: int = 500, icir: float = 0.5, **kwargs: object) -> FactorPerformance:
    return FactorPerformance(
        factor_id="factor-boundary",
        evaluation_period="2026-Q3",
        sample_count=sample_count,
        ic_mean=0.2,
        ic_std=0.1,
        icir=icir,
        rank_ic_mean=0.2,
        rank_ic_std=0.1,
        rank_icir=0.5,
        **kwargs,
    )


def _evidence(state: FactorLifecycle) -> list[str]:
    from beidou_research.factors.factor import PROMOTION_EVIDENCE_REQUIREMENTS

    return list(PROMOTION_EVIDENCE_REQUIREMENTS[state]["required_evidence"])


def test_regime_evidence_validation_and_duplicate_identity_are_fail_closed() -> None:
    for kwargs in (
        {"factor_id": "", "regime": "r"},
        {"factor_id": "f", "regime": "", "sample_count": 1},
        {"factor_id": "f", "regime": "r", "sample_count": -1},
        {"factor_id": "f", "regime": "r", "icir": float("nan")},
        {"factor_id": "f", "regime": "r", "reliability_prior": 2.0},
        {"factor_id": "f", "regime": "r", "capacity_score": -0.1},
        {"factor_id": "f", "regime": "r", "correlation_penalty": 2.0},
        {"factor_id": "f", "regime": "r", "cost_adjusted_ic": float("inf")},
    ):
        params = {"factor_id": "f", "regime": "r", "sample_count": 30, "icir": 0.5}
        params.update(kwargs)
        with pytest.raises(ValueError):
            RegimeConditionalForecastEvidence(**params)

    first = RegimeConditionalForecastEvidence("f", "risk-on", 0.5, 50)
    duplicate = replace(first)
    with pytest.raises(ValueError, match="duplicate"):
        build_regime_reliability([first, duplicate], regime="risk-on")
    assert first.reliability_score(min_samples=100) == 0.0
    assert first.reliability_score(icir_scale=0.0) == 0.0
    assert RegimeConditionalForecastEvidence("f", "risk-on", 0.5, 50, cost_adjusted_ic=-1).reliability_score() == 0.0


def test_factor_promotion_gate_rejects_invalid_provenance_bundles_and_metrics() -> None:
    gate = FactorPromotionGate()
    invalid = gate.validate_evidence("f", FactorLifecycle.IDEA, FactorLifecycle.ACTIVE)
    assert not invalid.approved
    no_requirements = gate.validate_evidence("f", FactorLifecycle.IDEA, FactorLifecycle.RESEARCH)
    assert not no_requirements.approved and "No evidence" in no_requirements.reason

    no_metrics = gate.validate_evidence(
        "f",
        FactorLifecycle.GENERATED,
        FactorLifecycle.SANITY_PASSED,
        evidence_ids=_evidence(FactorLifecycle.SANITY_PASSED),
        commit="c",
        dataset_hash="d",
        policy_version="p",
        falsifier="t",
    )
    assert not no_metrics.approved and "performance_required" in no_metrics.reason

    bad_metrics = gate.validate_evidence(
        "f",
        FactorLifecycle.GENERATED,
        FactorLifecycle.SANITY_PASSED,
        performance=_performance(),
        evidence_ids=_evidence(FactorLifecycle.SANITY_PASSED),
        commit="c",
        dataset_hash="d",
        policy_version="p",
        falsifier="t",
    )
    bad_metrics = gate.validate_evidence(
        "f",
        FactorLifecycle.GENERATED,
        FactorLifecycle.SANITY_PASSED,
        performance=replace(_performance(), ic_std="bad"),
        evidence_ids=_evidence(FactorLifecycle.SANITY_PASSED),
        commit="c",
        dataset_hash="d",
        policy_version="p",
        falsifier="t",
    )
    assert not bad_metrics.approved and "not numeric" in bad_metrics.reason

    low_samples = gate.validate_evidence(
        "f",
        FactorLifecycle.GENERATED,
        FactorLifecycle.SANITY_PASSED,
        performance=_performance(sample_count=1),
        evidence_ids=_evidence(FactorLifecycle.SANITY_PASSED),
        commit="c",
        dataset_hash="d",
        policy_version="p",
        falsifier="t",
    )
    assert not low_samples.approved and "Sample count" in low_samples.reason

    class BrokenBundle:
        artifact_hash = "artifact"

        def can_promote(self) -> tuple[bool, str]:
            raise RuntimeError("bundle unavailable")

    active_ids = _evidence(FactorLifecycle.ACTIVE)
    broken = gate.validate_evidence(
        "f",
        FactorLifecycle.CHALLENGER,
        FactorLifecycle.ACTIVE,
        performance=_performance(),
        evidence_ids=active_ids,
        factor_version="v",
        commit="c",
        dataset_hash="d",
        policy_version="p",
        falsifier="t",
        evidence_bundle=BrokenBundle(),
    )
    assert not broken.approved and "bundle_contract_error" in broken.reason

    class Bundle:
        artifact_hash = "artifact"
        factor_id = "wrong"
        factor_version = "wrong-version"
        dataset_manifest_hash = "wrong-dataset"
        policy_version = "wrong-policy"

        def can_promote(self) -> tuple[bool, str]:
            return True, "OK"

    mismatch = gate.validate_evidence(
        "f",
        FactorLifecycle.CHALLENGER,
        FactorLifecycle.ACTIVE,
        performance=_performance(),
        evidence_ids=active_ids,
        factor_version="v",
        commit="c",
        dataset_hash="d",
        policy_version="p",
        falsifier="t",
        evidence_bundle=Bundle(),
    )
    assert not mismatch.approved
    assert "factor_id mismatch" in mismatch.reason
    assert mismatch.evidence_artifact_hash == "artifact"


def test_factor_record_registry_and_evaluator_boundary_states() -> None:
    record = FactorRecord(_definition())
    assert record.has_authorized_active_evidence() is False
    record.lifecycle = FactorLifecycle.ACTIVE
    assert record.has_authorized_active_evidence() is False
    record.lifecycle = FactorLifecycle.SUSPENDED
    assert record.restart_as_challenger() is True
    record.lifecycle = FactorLifecycle.IDEA
    assert record.restart_as_challenger() is False

    reg = FactorRegistry()
    assert reg.get("missing") is None
    assert reg.evaluate("missing", _performance(), []) is False
    assert reg.promote_to_challenger("missing") is False
    assert reg.promote_to_active("missing") is False
    assert reg.degrade("missing", "x") is False
    assert reg.suspend("missing", "x") is False
    assert reg.retire("missing", "x") is False
    assert reg.restart_as_new_idea("missing", _definition("new")) is None
    assert reg.cross_venue_stability("missing", {}) == (False, "Factor not found")

    current = reg.register(_definition("registered"))
    assert reg.get_challengers() == []
    current.lifecycle = FactorLifecycle.CHALLENGER
    assert reg.promote_to_active("registered") is False
    current.promotion_history.append(
        SimplePromotionDecision()  # type: ignore[arg-type]
    )
    assert reg.promote_to_active("registered") is False
    current.lifecycle = FactorLifecycle.ACTIVE
    assert reg.degrade("registered", "x") is True
    assert reg.suspend("registered", "x") is True

    assert FactorEvaluator.compute_rank_ic([1.0], [1.0]) == 0.0
    assert FactorEvaluator.compute_rank_ic([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) == 0.0
    assert FactorEvaluator.compute_decile_spread([1.0] * 3, [1.0] * 3) == 0.0
    assert FactorEvaluator.compute_turnover([1.0], []) == 0.0
    assert FactorEvaluator.compute_turnover([1.0], [0.0]) == 0.0
    assert FactorEvaluator.compute_vif({}, "f") == 1.0
    assert FactorEvaluator.compute_vif({"f": {"g": 1.0}}, "f") == float("inf")
    mc = FactorEvaluator.compute_marginal_contribution(_performance(), [], {"factor-boundary": {"other": 0.1}})
    assert mc.diversification_benefit > 0


class SimplePromotionDecision:
    approved = True
    to_state = FactorLifecycle.ACTIVE


def test_factor_bridge_maps_known_and_unknown_states() -> None:
    assert bridge_to_factor_evidence("f", "ACTIVE", 1.0, "hash").state.value == "ACTIVE"
    assert bridge_to_factor_evidence("f", "UNKNOWN", 0.0).state.value == "CANDIDATE"


def test_factor_gate_promote_records_approved_transition() -> None:
    record = FactorRecord(_definition("promotable"), lifecycle=FactorLifecycle.GENERATED)
    gate = FactorPromotionGate()
    decision = gate.promote(
        record,
        FactorLifecycle.SANITY_PASSED,
        performance=_performance(),
        evidence_ids=_evidence(FactorLifecycle.SANITY_PASSED),
    )
    assert decision.approved is True
    assert record.lifecycle is FactorLifecycle.SANITY_PASSED
    assert record.promotion_history == [decision]


def test_factor_evaluator_icir_skips_non_numeric_values() -> None:
    assert FactorEvaluator.compute_icir([0.1, "not-a-number", 0.3]) == FactorEvaluator.compute_icir([0.1, 0.3])  # type: ignore[list-item]


def test_factor_registry_rejects_duplicate_and_adverse_evaluations() -> None:
    reg = FactorRegistry()
    definition = _definition("duplicate")
    reg.register(definition)
    with pytest.raises(ValueError, match="already registered"):
        reg.register(definition)

    low_cost = replace(_performance(), factor_id="duplicate", cost_adjusted_ic=0.0)
    assert reg.evaluate("duplicate", low_cost, []) is False
    adverse = MarginalContribution(
        factor_id="duplicate",
        existing_factor_ids=frozenset({"other"}),
        marginal_sharpe=0.0,
        marginal_ic=0.0,
        diversification_benefit=0.0,
        collinearity_vif=6.0,
    )
    assert reg.evaluate("duplicate", replace(_performance(), factor_id="duplicate"), [adverse]) is False


def test_factor_registry_compatibility_promotions_remain_fail_closed() -> None:
    reg = FactorRegistry()
    record = reg.register(_definition("compat"))
    assert reg.promote_to_challenger("compat") is False
    assert reg.degrade("compat", "not active") is False

    record.lifecycle = FactorLifecycle.PAPER_TRADING
    assert reg.promote_to_challenger("compat") is False


def test_factor_registry_cross_venue_boundaries_are_explicit() -> None:
    reg = FactorRegistry()
    reg.register(_definition("venue"))
    assert reg.cross_venue_stability("venue", {}) == (False, "No venue data")
    result = reg.cross_venue_stability(
        "venue",
        {VenueId("BINANCE"): 0.5, VenueId("OKX"): -0.5},
    )
    assert result == (False, "IC direction inconsistent across venues")
