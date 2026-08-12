"""Behavioral tests for previously uncovered runtime and certification contracts."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from beidou_certification.signing import CertificateStore, ReleaseArtifact, SignedCertificate
from beidou_certification.staged_ladder import (
    GATE_ORDER,
    GateCertificate,
    GateLevel,
    StagedCertificationLadder,
)
from beidou_infra.event_store import (
    ChecksumMismatchError,
    ConcurrencyConflictError,
    DomainEvent,
    EventStore,
)
from beidou_launcher.readiness_gate import ReadinessGate, StartupPhase
from beidou_observability.monitoring.certification import (
    CERTIFICATION_SECONDS,
    CertificationState,
    create_certification,
    load_certification,
)
from beidou_observability.monitoring.p0_immediate_trigger import (
    ImmediateTrigger,
    MonitorHeartbeat,
    P0ImmediateTriggerSystem,
    TriggerSeverity,
    TriggerStatus,
)
from beidou_safety.execution.order_state_machine import OrderState, OrderStateMachine
from beidou_safety.risk.state_persistence import RiskSnapshot, RiskStatePersistence
from beidou_strategy.portfolio.hedge_netting import (
    Direction,
    PositionMode,
    bind_portfolio_target,
    compute_hedge_netting,
    compute_oneway_netting,
)
from beidou_strategy.portfolio.simple_optimizer import OptimizerInput, equal_weight_portfolio, optimize_portfolio
from beidou_strategy.state.cost_capacity_model import CapacityEstimate, CostComponents, RealizedExecutionCost
from beidou_strategy.state.cost_learner import CostLearner, TrainingSample


def test_readiness_gate_requires_ordered_complete_phases() -> None:
    gate = ReadinessGate()
    assert gate.elapsed_seconds() == 0
    gate.start()
    assert gate.current_phase() == StartupPhase.CONFIG
    assert not gate.complete_phase(StartupPhase.RECONCILIATION, True, "out of order")
    assert not gate.complete_phase(StartupPhase.CONFIG, False, "invalid")
    assert gate.current_phase() == StartupPhase.CONFIG
    for phase in list(StartupPhase):
        assert gate.complete_phase(phase, True, phase.value)
    assert gate.is_ready() and gate.can_resume()
    assert gate.current_phase() == StartupPhase.READINESS
    assert gate.elapsed_seconds() >= 0


def test_order_state_machine_rejects_illegal_terminal_recovery_and_handles_unknown() -> None:
    machine = OrderStateMachine("order-1", "client-1", "BTCUSDT")
    assert machine.transition(OrderState.CREATED) == (True, "NO_CHANGE")
    assert machine.transition(OrderState.QUEUED)[0]
    assert machine.handle_unknown() == "QUERY_BY_CLIENT_ID:client-1"
    assert machine.state == OrderState.UNKNOWN
    assert machine.transition(OrderState.FILLED)[0]
    assert machine.is_terminal()
    transitioned, message = machine.transition(OrderState.QUEUED)
    assert not transitioned and message.startswith("INVALID_TRANSITION")
    assert machine.handle_unknown().startswith("NO_QUERY:INVALID_TRANSITION")
    assert machine.compute_idempotency_key() == machine.compute_idempotency_key()


def _risk_snapshot(expires_at: datetime) -> RiskSnapshot:
    return RiskSnapshot(
        snapshot_id="risk-1",
        input_fact_hashes={"positions": "p"},
        rule_outcomes={"leverage": "PASS"},
        aggregate_state="NORMAL",
        reason="fresh reconciliation",
        expires_at=expires_at.isoformat(),
        policy_version="policy-1",
        config_version="config-1",
    )


def test_risk_snapshot_round_trip_preserves_hash_inputs_and_fails_closed(tmp_path: Path) -> None:
    persistence = RiskStatePersistence(str(tmp_path / "risk.json"))
    assert persistence.load().reason == "NO_PERSISTED_STATE"
    snapshot = _risk_snapshot(datetime.now(timezone.utc) + timedelta(minutes=5))
    assert persistence.save(snapshot)
    loaded = persistence.load()
    assert loaded.is_valid() and not loaded.is_expired()
    assert loaded.input_fact_hashes == snapshot.input_fact_hashes
    assert loaded.rule_outcomes == snapshot.rule_outcomes
    assert loaded.config_version == "config-1"

    payload = json.loads((tmp_path / "risk.json").read_text())
    payload["reason"] = "tampered"
    (tmp_path / "risk.json").write_text(json.dumps(payload))
    assert persistence.load().reason == "PERSISTED_STATE_INVALID_OR_EXPIRED"
    (tmp_path / "risk.json").write_text("not-json")
    assert persistence.load().reason == "STATE_FILE_CORRUPTED"
    assert persistence.reset_requires_new_snapshot().aggregate_state == "CORRUPT"


def test_risk_snapshot_invalid_and_expired_states() -> None:
    assert not RiskSnapshot().is_valid()
    assert RiskSnapshot(expires_at="").is_expired()
    assert RiskSnapshot(expires_at="not-a-date").is_expired()
    naive = _risk_snapshot(datetime.now(timezone.utc).replace(tzinfo=None))
    naive.snapshot_hash = naive.compute_hash()
    assert naive.is_expired()


def test_event_store_append_replay_correlation_conflict_and_tamper() -> None:
    store = EventStore()
    later = DomainEvent("stream", "Order", 2, "ACK", {"status": "NEW"}, correlation_id="corr")
    earlier = DomainEvent("stream", "Order", 1, "CREATED", {"qty": 1}, correlation_id="corr")
    assert store.append(later) and store.append(earlier)
    assert [event.sequence for event in store.get_stream("stream")] == [1, 2]
    assert store.get_by_correlation("corr") == [later, earlier]
    assert store.replay("Order", "stream") == [earlier, later]
    assert store.verify_integrity() == []
    with pytest.raises(ConcurrencyConflictError):
        store.append(earlier)
    tampered = replace(earlier, stream_id="other", checksum="invalid")
    with pytest.raises(ChecksumMismatchError):
        store.append(tampered)


def test_hedge_oneway_netting_and_bound_target_sign_invariants() -> None:
    hedge = compute_hedge_netting({"BTC": 2}, {"BTC": 1, "ETH": 3})
    assert hedge["BTC"].mode == PositionMode.HEDGE
    assert hedge["BTC"].net_position == 1 and hedge["BTC"].gross_exposure == 3
    one_way = compute_oneway_netting({"BTC": -2, "ETH": 1})
    assert one_way["BTC"].short_exposure == 2 and one_way["ETH"].long_exposure == 1

    target = bind_portfolio_target(
        "BTC", -2, -1, Direction.SHORT, "universe", "rules", "decision", "policy", leverage=2
    )
    assert target.delta == -1 and target.margin_used == 1 and target.target_hash
    with pytest.raises(ValueError, match="SHORT"):
        bind_portfolio_target("BTC", 1, 0, Direction.SHORT)
    with pytest.raises(ValueError, match="LONG"):
        bind_portfolio_target("BTC", 0, 0, Direction.LONG)
    with pytest.raises(ValueError, match="FLAT"):
        bind_portfolio_target("BTC", 1, 0, Direction.FLAT)
    with pytest.raises(ValueError, match="leverage"):
        bind_portfolio_target("BTC", 1, 0, Direction.LONG, leverage=0)


def test_simple_optimizer_reports_fallback_constraints_and_shadow_prices() -> None:
    assert not equal_weight_portfolio([]).feasible
    assert equal_weight_portfolio(["a", "b"]).weights == [0.5, 0.5]
    assert optimize_portfolio(OptimizerInput([], [])).infeasibility_reason == "NO_SYMBOLS"
    assert optimize_portfolio(OptimizerInput(["a"], [])).infeasibility_reason == "DIM_MISMATCH"
    assert optimize_portfolio(OptimizerInput(["a"], [float("nan")])).infeasibility_reason == "NAN_INF_RETURNS"
    fallback = optimize_portfolio(OptimizerInput(["a", "b"], [1, 2], covariance_matrix=None))
    assert fallback.diagnostics["method"] == "equal_weight"
    malformed = optimize_portfolio(OptimizerInput(["a", "b"], [1, 2], covariance_matrix=[[1], [0, 1]]))
    assert malformed.diagnostics["method"] == "equal_weight"

    constrained = optimize_portfolio(
        OptimizerInput(
            ["a", "b"],
            [10, 1],
            covariance_matrix=[[1, 0], [0, 1]],
            current_weights=[0, 0],
            max_concentration=0.25,
            max_turnover=0.1,
        )
    )
    assert constrained.feasible
    assert any(item.startswith("CONCENTRATION:a") for item in constrained.constraint_violations)
    assert any(item.startswith("TURNOVER") for item in constrained.constraint_violations)
    assert constrained.shadow_prices["a"] > 0


def _signed_cert(cert_id: str = "cert-1") -> SignedCertificate:
    return SignedCertificate(cert_id, "G1", "beidou", "commit", "bundle", "")


def test_certificate_store_requires_key_verifies_revocation_and_never_fakes_s3(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="SIGNING_KEY"):
        CertificateStore("").issue(_signed_cert())
    store = CertificateStore("secret-key")
    cert = store.issue(_signed_cert())
    assert cert.verify("secret-key") and not cert.verify("wrong")
    assert store.get("cert-1") is cert
    assert store.verify_all() == {"cert-1": True}
    assert not store.revoke("missing", "none")
    assert store.revoke("cert-1", "evidence invalid")
    assert store.is_revoked("cert-1") and not cert.verify("secret-key")
    assert store.verify_all() == {"cert-1": False}
    assert not store.export_to_s3("bucket", "https://minio.invalid")

    artifact_file = tmp_path / "artifact.txt"
    artifact_file.write_text("artifact")
    artifact = ReleaseArtifact("1.0", "commit", sbom_path=str(artifact_file))
    assert artifact.compute_artifact_hash() == artifact.compute_artifact_hash()
    assert artifact._file_hash("missing") == "MISSING"


def _gate_certificate(gate: GateLevel, elapsed: float = 0) -> GateCertificate:
    return GateCertificate(
        gate=gate,
        evidence_hash=f"evidence-{gate.value}",
        commit_family=["commit"],
        elapsed_seconds=elapsed,
    )


def test_staged_ladder_rejects_skip_mismatch_missing_evidence_and_short_duration() -> None:
    ladder = StagedCertificationLadder()
    assert not ladder.certify(GateLevel.G2, _gate_certificate(GateLevel.G2))[0]
    assert not ladder.certify(GateLevel.G0, _gate_certificate(GateLevel.G1))[0]
    assert not ladder.certify(GateLevel.G0, GateCertificate(gate=GateLevel.G0))[0]
    for gate in GATE_ORDER[:6]:
        assert ladder.certify(gate, _gate_certificate(gate))[0]
    assert not ladder.certify(GateLevel.G6, _gate_certificate(GateLevel.G6, elapsed=1))[0]
    assert ladder.certify(GateLevel.G6, _gate_certificate(GateLevel.G6, elapsed=72 * 3600))[0]
    assert not ladder.certify(GateLevel.G7, _gate_certificate(GateLevel.G7, elapsed=1))[0]
    assert ladder.certify(GateLevel.G7, _gate_certificate(GateLevel.G7, elapsed=30 * 24 * 3600))[0]
    assert ladder.certify(GateLevel.G8, _gate_certificate(GateLevel.G8))[0]
    assert ladder.chain_complete() and ladder.is_g8_mainnet_candidate()
    assert not ladder.can_activate_mainnet()
    ladder.start_gate_timer(GateLevel.G8)
    assert ladder.update_elapsed(GateLevel.G8) >= 0
    ladder.record_incident("P1")
    ladder.record_incident("P0")
    assert not ladder.certificates[GateLevel.G8].certified


def test_staged_ladder_blocks_safety_evidence_and_handles_missing_timer() -> None:
    ladder = StagedCertificationLadder()
    unsafe = _gate_certificate(GateLevel.G0)
    unsafe.p0_incidents = 1
    assert ladder.certify(GateLevel.G0, unsafe) == (False, "BLOCKING_SAFETY_EVIDENCE")
    assert ladder.update_elapsed(GateLevel.G0) == 0
    ladder.record_incident("P0")


def test_immediate_trigger_aggregation_audit_and_heartbeat(monkeypatch) -> None:
    system = P0ImmediateTriggerSystem()
    assert system.check_condition("order.UNKNOWN", True, "timeout") == TriggerStatus.TRIGGERED
    assert system.any_p0_triggered() and system.aggregate_state() == "RED"
    assert system.check_condition("order.UNKNOWN", False) == TriggerStatus.CLEAR
    system.triggers["p1"] = ImmediateTrigger("p1", TriggerSeverity.P1, TriggerStatus.TRIGGERED)
    assert system.aggregate_state() == "RED"
    system.triggers["p1"].status = TriggerStatus.CLEAR
    assert system.aggregate_state() == "GREEN"
    assert system.should_audit() and not system.should_audit()

    heartbeat = MonitorHeartbeat()
    assert not heartbeat.is_alive()
    heartbeat.record()
    assert heartbeat.is_alive()
    heartbeat.last_heartbeat_monotonic -= 31
    assert not heartbeat.is_alive(timeout_seconds=30)


def test_certification_manifest_round_trip_preserves_elapsed_time_and_status(tmp_path: Path) -> None:
    state = CertificationState(
        "cert-old",
        started_at=time.time() - CERTIFICATION_SECONDS - 10,
        evidence_dir=tmp_path,
    )
    state.heartbeat()
    state.record_restart("deploy")
    state.record_incident("P1", "warning")
    assert state.is_complete and state.status == "CONDITIONAL"
    path = state.save_manifest()
    loaded = load_certification(path)
    assert loaded is not None
    assert loaded.started_at == pytest.approx(state.started_at, abs=0.001)
    assert loaded.checks_completed == 1 and loaded.restarts == 1
    assert loaded.status == "CONDITIONAL"
    assert state.finalize()["status"] == "CONDITIONAL"
    state.record_incident("P0", "critical")
    assert state.status == "FAIL"
    assert load_certification(tmp_path / "missing.json") is None
    invalid = tmp_path / "invalid.json"
    invalid.write_text("not-json")
    assert load_certification(invalid) is None
    assert create_certification("explicit").certification_id == "explicit"


def test_cost_capacity_and_learning_require_real_fills_and_shadow_evidence() -> None:
    assert CostComponents(commission_bps=1, slippage_bps=2, spread_bps=3, latency_bps=4).compute_total() == 10
    unknown = CapacityEstimate.estimate("BTC", 0, 0)
    assert unknown.is_stale and unknown.limiting_factor == "UNKNOWN_INPUTS"
    depth_limited = CapacityEstimate.estimate("BTC", 1_000_000, 100, participation_rate=0.1)
    volume_limited = CapacityEstimate.estimate("BTC", 1_000, 1_000, participation_rate=0.01)
    assert depth_limited.limiting_factor == "ORDERBOOK_DEPTH"
    assert volume_limited.limiting_factor == "DAILY_VOLUME"
    assert not depth_limited.is_constant(volume_limited)

    no_fill = RealizedExecutionCost("BTC", predicted_cost_bps=99)
    assert no_fill.compute_realized() == 0 and not no_fill.can_learn()
    realized = RealizedExecutionCost(
        "BTC", 100, 101, 102, commission_bps=1, funding_cost_bps=0.5, has_fill=True, has_fee=True
    )
    assert realized.compute_realized() > 1 and realized.can_learn()

    learner = CostLearner()
    assert learner.best_algorithm() == "TWAP"
    rejected = TrainingSample("BTC", "TWAP", 2, 3, 1, "BULL", -3, 1)
    assert not learner.add_sample(rejected)
    for i in range(50):
        assert learner.add_sample(TrainingSample("BTC", "POV", 1, 1, 1, "BULL", 1, i, True, True))
        learner.add_sample(TrainingSample("BTC", "TWAP", 3, 3, 1, "BULL", -1, i, True, True))
    assert learner.best_algorithm() == "POV"
    assert learner.verify_in_shadow() and learner.activate_production()
    assert learner.predicted_cost_bps("BTC", "POV") == 1
    assert learner.predicted_cost_bps("ETH", "POV") == 1
    assert learner.predicted_cost_bps("ETH", "MISSING") == 5
