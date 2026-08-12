"""Small contract branches that must remain observable and fail closed."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest

from beidou_control.truth import TradingEligibility, TruthSnapshot, derive_eligibility
from beidou_data.universe_hysteresis import UniverseEntry, UniverseHysteresis
from beidou_launcher.readiness_gate import ReadinessGate, StartupPhase
from beidou_observability.monitoring.certification import (
    CERTIFICATION_SECONDS,
    CertificationState,
    load_certification,
)
from beidou_observability.monitoring.checks.protection import (
    ExchangeEconomicPosition,
    ProtectionFact,
    build_protection_check,
    verify_position_protection,
)
from beidou_observability.monitoring.contracts import CheckStatus
from beidou_observability.monitoring.fact_bus import FactBus, FactDomain, OperationalFact
from beidou_research.mining.evidence import EvidenceBundle
from beidou_research.mining.generators.interaction import InteractionConfig, InteractionGenerator
from beidou_research.mining.generators.template_grid import TemplateConfig, TemplateGridGenerator
from beidou_safety.risk.state_persistence import RiskSnapshot, RiskStatePersistence
from beidou_security.identity import Credential, CredentialType, KeyRotator
from beidou_shared.serde import DatetimeEncoder
from beidou_shared.types import ResultStatus, SchemaVersion, StrategyId, VenueId
from beidou_strategy.alpha import AlphaComponent, AlphaComponentType, AlphaGraph
from beidou_strategy.portfolio.simple_optimizer import OptimizerInput, optimize_portfolio
from beidou_strategy.state.cost_capacity_model import CapacityEstimate
from beidou_strategy.state.cost_learner import CostLearner, TrainingSample


class _AlphaComponent(AlphaComponent):
    async def generate(self, context):  # pragma: no cover - abstract test fixture
        raise NotImplementedError

    def validate(self) -> bool:
        return True


def _complete_evidence(**overrides) -> EvidenceBundle:
    values = {
        "bundle_id": "bundle-1",
        "candidate_id": "candidate-1",
        "factor_id": "factor-1",
        "factor_version": "1",
        "candidate_hash": "candidate-hash",
        "factor_code_hash": "factor-hash",
        "dataset_manifest_hash": "dataset-hash",
        "feature_manifest_hash": "feature-hash",
        "label_spec_hash": "label-hash",
        "cost_model_version": "cost-v1",
        "policy_version": "policy-v1",
        "gate_decision": "PASS",
        "artifact_hash": "sealed-hash",
    }
    values.update(overrides)
    return EvidenceBundle(**values)


def _truth_snapshot(**overrides) -> TruthSnapshot:
    now = time.time()
    values = {
        "market_hash": "h",
        "account_hash": "h",
        "order_hash": "h",
        "position_hash": "h",
        "ledger_hash": "h",
        "reconciliation_hash": "h",
        "protection_hash": "h",
        "risk_hash": "h",
        "config_hash": "h",
        "policy_hash": "h",
        "market_freshness": now,
        "account_freshness": now,
        "order_freshness": now,
        "position_freshness": now,
        "ledger_freshness": now,
        "reconciliation_freshness": now,
        "protection_freshness": now,
        "risk_freshness": now,
        "config_freshness": now,
        "policy_freshness": now,
        "reconciliation_status": "MATCHED",
        "protection_status": "ACTIVE",
        "risk_status": "NORMAL",
    }
    values.update(overrides)
    return TruthSnapshot(**values)


def test_template_grid_stops_at_configured_limit() -> None:
    templates = TemplateGridGenerator(TemplateConfig(max_combinations=1)).generate_templates()
    assert len(templates) == 1


@pytest.mark.parametrize(
    ("volatility", "holding_hours", "expected"),
    [(1.0, 4.0, "VOLATILITY"), (0.02, 100.0, "HOLDING_PERIOD")],
)
def test_capacity_reports_non_volume_limiting_factor(volatility, holding_hours, expected) -> None:
    estimate = CapacityEstimate.estimate(
        "BTCUSDT",
        daily_volume=1_000,
        orderbook_depth=100,
        volatility=volatility,
        holding_period_hours=holding_hours,
    )
    assert estimate.limiting_factor == expected


def test_alpha_dependency_property_and_unresolved_graph_fail_closed() -> None:
    component = _AlphaComponent(AlphaComponentType.ENTRY, "entry", SchemaVersion("1"))
    assert component.input_dependencies == []

    graph = AlphaGraph(StrategyId("strategy"))
    graph.add_component(component)
    graph._edges["entry"] = ["ghost"]
    with pytest.raises(ValueError, match="unresolved dependencies"):
        graph.topological_order()


def test_unknown_key_rotation_targets_fail_closed() -> None:
    rotator = KeyRotator()
    credential = Credential("new", CredentialType.TRADING, VenueId("BINANCE"))
    assert rotator.complete_rotation("missing", credential) is ResultStatus.UNKNOWN
    assert rotator.revoke("missing") is ResultStatus.UNKNOWN


def test_datetime_encoder_handles_datetime_and_rejects_unknown_object() -> None:
    assert json.dumps(datetime(2026, 1, 1, tzinfo=timezone.utc), cls=DatetimeEncoder) == '"2026-01-01T00:00:00+00:00"'
    with pytest.raises(TypeError):
        DatetimeEncoder().default(object())


def test_evidence_promotion_rejects_each_unsealed_failure_state() -> None:
    assert _complete_evidence(gate_decision="FAIL").can_promote() == (False, "gate_not_passed: FAIL")
    rejected = _complete_evidence(failure_reasons=["unstable"])
    assert rejected.can_promote() == (False, "has_failure_reasons: unstable")
    assert _complete_evidence(artifact_hash="").can_promote() == (False, "not_sealed")


def test_cost_learner_requires_enough_shadow_evidence_before_activation() -> None:
    learner = CostLearner()
    assert not learner.verify_in_shadow()
    learner.samples = [TrainingSample("BTCUSDT", "TWAP", 1, 1, 1, "normal", 0, 0, True, True) for _ in range(50)]
    assert not learner.verify_in_shadow()
    assert not learner.activate_production()


def test_universe_rejects_zero_capacity_and_resets_neutral_streaks() -> None:
    assert not UniverseEntry("BTCUSDT", funding_rate=0.01, open_interest=1, dq_ok=True).is_promotable()
    universe = UniverseHysteresis()
    universe.promotion_streaks["BTCUSDT"] = 2
    universe.demotion_streaks["BTCUSDT"] = 2
    universe.observe(UniverseEntry("BTCUSDT", funding_rate=0.01, open_interest=1, dq_ok=True, capacity_score=0.5))
    assert universe.promotion_streaks["BTCUSDT"] == 0
    assert universe.demotion_streaks["BTCUSDT"] == 0


def test_interaction_generator_defaults_and_hard_limits() -> None:
    assert InteractionGenerator().generate_interactions(None)
    bases = [f"factor-{index}" for index in range(25)]
    assert len(InteractionGenerator().generate_interactions(bases)) == 200
    assert len(InteractionGenerator(InteractionConfig(max_order=3)).generate_interactions(bases)) == 500


def test_risk_snapshot_without_hash_and_unwritable_state_fail_closed(tmp_path) -> None:
    assert not RiskSnapshot(aggregate_state="NORMAL").is_valid()
    persistence = RiskStatePersistence(_state_file=str(tmp_path))
    assert not persistence.save(RiskSnapshot(aggregate_state="NORMAL"))


def test_readiness_rejects_unstarted_and_incomplete_phase_sets() -> None:
    gate = ReadinessGate()
    assert not gate.complete_phase(StartupPhase.CONFIG, True)
    gate.start()
    assert not gate.can_resume()
    for phase in (StartupPhase.RECONCILIATION, StartupPhase.PROTECTION_VERIFY, StartupPhase.RISK_SNAPSHOT):
        gate.phases[phase].passed = True
    assert not gate.can_resume()


def test_fact_bus_isolates_bad_subscriber_and_can_clear(caplog) -> None:
    bus = FactBus()

    def broken(_fact):
        raise RuntimeError("secret should not leak")

    bus.subscribe("order", broken)
    bus.publish(OperationalFact("order", FactDomain.EXECUTION, {}))
    assert "RuntimeError" in caplog.text
    assert "secret should not leak" not in caplog.text
    bus.clear()
    assert bus.query() == []


def test_optimizer_scales_weights_to_leverage_limit() -> None:
    result = optimize_portfolio(
        OptimizerInput(
            symbols=["A", "B"],
            expected_returns=[1, 1],
            covariance_matrix=[[1, 0], [0, 1]],
            max_concentration=1,
            max_leverage=0.5,
        )
    )
    assert sum(abs(weight) for weight in result.weights) == pytest.approx(0.5)
    assert result.constraint_violations == ["LEVERAGE:1.00"]


def test_protection_check_reports_duplicate_and_ghost() -> None:
    common = {"position_key": "BTCUSDT", "status": "ACTIVE", "origin_trace_id": "trace", "strategy_id": "s"}
    protections = [
        ProtectionFact("sl-old", kind="SL", order_id="1", generation=1, **common),
        ProtectionFact("sl-new", kind="SL", order_id="2", generation=1, supersedes_order_id="sl-old", **common),
        ProtectionFact("tp-dup", kind="TP", order_id="3", generation=1, **common),
    ]
    result = verify_position_protection(ExchangeEconomicPosition("BTCUSDT", "0"), protections, mode="ONE_WAY")
    check = build_protection_check(result)
    assert result.duplicate_count == 1
    assert result.ghost_detected
    assert check.status is CheckStatus.FAIL
    assert "DUP(1)" in check.message and "GHOST" in check.message


def test_certification_status_and_naive_manifest_fail_closed(tmp_path) -> None:
    assert CertificationState("new").status == "NOT_VERIFIED"
    completed = CertificationState("old", started_at=time.time() - CERTIFICATION_SECONDS - 1)
    assert completed.status == "PASS"

    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"certification_id": "naive", "started_at_utc": "2026-01-01T00:00:00"}))
    assert load_certification(manifest) is None


def test_truth_snapshot_staleness_unknown_and_fallback_are_safe() -> None:
    assert _truth_snapshot(market_freshness=0).is_stale()
    assert _truth_snapshot(risk_status="UNKNOWN").has_unknown_components() == ["risk"]
    assert derive_eligibility(_truth_snapshot(risk_status="UNRECOGNIZED")) is TradingEligibility.NO_NEW_RISK
