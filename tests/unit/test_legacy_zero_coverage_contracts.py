from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from beidou_autonomy.mapek import (
    FaultFingerprint,
    MAPEKController,
    RecoveryAction,
    RecoveryResult,
)
from beidou_autonomy.recovery_planner import (
    PersistedRecoveryState,
    RecoveryExecutor,
    RecoveryOrchestrator,
    RecoveryPlanner,
    RecoveryVerifier,
)
from beidou_core.engine import _rotating_symbol_batch
from beidou_research.factors.revalidation_engine import (
    FactorState,
    revalidate_factor,
    transition_state,
)
from beidou_research.mining.orchestrator import (
    MiningOrchestrator,
    MiningRunConfig,
    MiningRunStatus,
)
from beidou_shared.contracts import ContractRegistration, ContractRegistry
from beidou_shared.envelope import EventEnvelope
from beidou_shared.serde import (
    create_metadata,
    deserialize_json,
    deserialize_msgpack,
    serialize_json,
    serialize_msgpack,
)
from beidou_shared.types import (
    ClockDomain,
    InstrumentId,
    SchemaVersion,
    VenueId,
)
from beidou_strategy.alpha.extensions import (
    FundingRateExtension,
    FundingRateSignal,
    LiquidationCascadeExtension,
    LiquidationCascadeRisk,
)


class _ControlPlane:
    def __init__(self) -> None:
        self.actions: list[object] = []

    def get_status(self) -> str:
        return "NORMAL"

    def execute_action(self, action: object) -> None:
        self.actions.append(action)


def _approved_recovery_controller() -> MAPEKController:
    controller = MAPEKController()
    controller.register_fingerprint(
        FaultFingerprint(
            symptom_vector={"crash": 1.0},
            effective_actions=[RecoveryAction.RESTART_MODULE],
            approved_runbook="approved-runbook",
        )
    )
    return controller


def test_recovery_orchestrator_counts_once_and_persists_atomically(tmp_path) -> None:
    controller = _approved_recovery_controller()
    checkpoint = controller.save_checkpoint("engine", {"sequence": 3}, True)
    state_file = tmp_path / "recovery.json"
    state = PersistedRecoveryState(module_name="engine", _state_file=str(state_file))
    orchestrator = RecoveryOrchestrator(
        planner=RecoveryPlanner(controller),
        executor=RecoveryExecutor(controller),
        verifier=RecoveryVerifier(),
        state=state,
    )

    result, reason = orchestrator.recover(
        {"crash": 1.0},
        "engine",
        {"position_reconciled": True},
        checkpoint,
        _ControlPlane(),
        truth_snapshot_fresh=True,
    )

    assert result is RecoveryResult.SUCCESS
    assert "RECOVERY_VERIFIED" in reason
    assert controller.get_restart_count("engine") == 1
    assert state.restart_count == 1
    assert state.timestamp > 0
    assert not (tmp_path / "recovery.json.tmp").exists()
    loaded = PersistedRecoveryState.load("engine", str(state_file))
    assert loaded.restart_count == 1
    assert loaded.checkpoint_id == checkpoint.checkpoint_id


def test_recovery_verifier_and_persistence_fail_closed(tmp_path) -> None:
    verifier = RecoveryVerifier()
    assert verifier.verify(RecoveryResult.SUCCESS, {}, True) == (False, "EMPTY_INVARIANTS")
    assert verifier.verify(RecoveryResult.SUCCESS, {"risk": False}, True)[0] is False
    assert verifier.verify(RecoveryResult.SUCCESS, {"risk": True}, False) == (
        False,
        "TRUTH_SNAPSHOT_STALE",
    )
    assert verifier.verify(RecoveryResult.DEGRADED, {"risk": True}, True) == (
        False,
        "RECOVERY_DEGRADED",
    )

    controller = _approved_recovery_controller()
    checkpoint = controller.save_checkpoint("engine", {}, True)
    state = PersistedRecoveryState(
        module_name="engine",
        _state_file=str(tmp_path / "missing" / "state.json"),
    )
    state.save = lambda: False  # type: ignore[method-assign]
    result, reason = RecoveryOrchestrator(
        RecoveryPlanner(controller), RecoveryExecutor(controller), verifier, state
    ).recover(
        {"crash": 1.0},
        "engine",
        {"risk": True},
        checkpoint,
        truth_snapshot_fresh=True,
    )
    assert result is RecoveryResult.FAILED
    assert "STATE_PERSISTENCE_FAILED" in reason


def test_recovery_state_corruption_and_bare_filename_are_handled(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    state = PersistedRecoveryState(module_name="engine", restart_count=2, _state_file="state.json")
    assert state.save()
    assert json.loads((tmp_path / "state.json").read_text())["restart_count"] == 2

    (tmp_path / "state.json").write_text("not-json")
    loaded = PersistedRecoveryState.load("engine", "state.json")
    assert loaded.restart_count == 0
    assert loaded._state_file == "state.json"

    missing = PersistedRecoveryState.load("engine", "custom-missing.json")
    assert missing._state_file == "custom-missing.json"


def test_factor_revalidation_requires_all_evidence_bindings() -> None:
    incomplete = revalidate_factor(
        "factor-1",
        1.2,
        evidence_dag_hash="dag",
        sample_count=200,
        cost_verified=True,
        capacity_verified=True,
        statistics_verified=True,
    )
    assert not incomplete.can_promote
    assert incomplete.state is FactorState.REVALIDATION_REQUIRED
    assert "MISSING_PROMOTION_HASH:code_hash" in incomplete.blocking_reasons

    complete = revalidate_factor(
        "factor-1",
        1.2,
        evidence_dag_hash="dag",
        sample_count=200,
        cost_verified=True,
        capacity_verified=True,
        statistics_verified=True,
        universe_hash="u",
        feature_hash="f",
        code_hash="c",
        config_hash="cfg",
        policy_hash="p",
    )
    assert complete.can_promote
    assert complete.state is FactorState.ACTIVE
    assert transition_state(FactorState.CANDIDATE, FactorState.ACTIVE)[0] is False
    assert transition_state(FactorState.CANDIDATE, FactorState.REVALIDATION_REQUIRED)[0] is True


@dataclass
class _Candidate:
    candidate_id: str
    score: float


def test_mining_orchestrator_enforces_budget_and_round_trips_checkpoint() -> None:
    checkpoints: list[dict] = []
    orchestrator = MiningOrchestrator(
        MiningRunConfig(
            run_id="",
            max_candidates=3,
            checkpoint_interval=1,
            generators=["good", "never_reached"],
        )
    )
    orchestrator.on_checkpoint(lambda state: checkpoints.append({"status": state.status.value}))
    candidates = orchestrator.generate_candidates(
        {
            "ignored": lambda: [_Candidate("ignored", 0.0)],
            "good": lambda: [_Candidate(str(index), float(index)) for index in range(5)],
            "never_reached": lambda: [_Candidate("extra", 9.0)],
        }
    )
    assert orchestrator.state.run_id.startswith("run-")
    assert len(candidates) == 3
    assert orchestrator.state.generator_states["good"]["truncated"] is True

    screened = orchestrator.screen_candidates(candidates, lambda item: (item.score > 0, "zero"))
    results = orchestrator.evaluate_candidates(screened, lambda item: (True, {"score": item.score}))
    assert len(results) == 2
    assert orchestrator.state.status is MiningRunStatus.COMPLETED
    assert orchestrator.get_failure_taxonomy() == {"zero": 1}
    assert checkpoints

    bundle = orchestrator.generate_evidence_bundle()
    assert bundle["bundle_hash"] == orchestrator.state.evidence_bundle_hash
    restored = MiningOrchestrator.from_checkpoint(orchestrator.to_checkpoint_dict())
    assert restored.state.status is MiningRunStatus.COMPLETED
    assert restored.state.last_checkpoint is not None
    assert restored.get_failure_taxonomy() == {"zero": 1}
    restored.cancel()
    assert restored.state.status is MiningRunStatus.CANCELLED


def test_mining_orchestrator_records_generator_and_checkpoint_errors() -> None:
    orchestrator = MiningOrchestrator(MiningRunConfig(max_candidates=2, checkpoint_interval=1, generators=["broken"]))
    assert (
        orchestrator.generate_candidates({"broken": lambda: (_ for _ in ()).throw(RuntimeError("generator failed"))})
        == []
    )
    assert orchestrator.state.generator_states["broken"]["status"] == "failed"

    orchestrator.on_checkpoint(lambda _state: (_ for _ in ()).throw(RuntimeError("disk full")))
    orchestrator.cancel()
    assert any("Checkpoint callback: disk full" in error for error in orchestrator.state.errors)

    with pytest.raises(ValueError, match="max_candidates"):
        MiningOrchestrator(MiningRunConfig(max_candidates=0))
    with pytest.raises(ValueError, match="checkpoint_interval"):
        MiningOrchestrator(MiningRunConfig(checkpoint_interval=0))


def test_contract_registry_uses_numeric_version_ordering() -> None:
    registry = ContractRegistry()
    for version in ("1.2", "1.10", "1.3"):
        registry.register(
            ContractRegistration(
                contract_name="OrderIntent",
                schema_version=SchemaVersion(version),
            )
        )

    assert registry.get_latest("OrderIntent").schema_version == "1.10"  # type: ignore[union-attr]
    assert registry.get_version("OrderIntent", SchemaVersion("1.3")) is not None
    assert registry.list_contracts()["OrderIntent"] == ["1.2", "1.3", "1.10"]
    assert registry.get_latest("missing") is None
    with pytest.raises(ValueError, match="already registered"):
        registry.register(ContractRegistration(contract_name="OrderIntent", schema_version=SchemaVersion("1.2")))


def test_event_envelope_json_msgpack_and_metadata_round_trip() -> None:
    envelope = EventEnvelope[dict[str, object]](
        source="unit-test",
        clock_domain=ClockDomain.REALTIME,
        schema_version=SchemaVersion("1.0"),
        event_time=datetime(2026, 8, 12, tzinfo=timezone.utc),
        payload={"price": 100.0},
        sequence_number=7,
    )

    assert deserialize_json(serialize_json(envelope))["payload"] == {"price": 100.0}
    assert deserialize_msgpack(serialize_msgpack(envelope))["sequence_number"] == 7
    metadata = create_metadata(envelope)
    assert metadata.correlation_id == envelope.correlation_id
    assert metadata.event_time == envelope.event_time


@pytest.mark.asyncio
async def test_contract_extensions_validate_inputs_and_expose_warning_only() -> None:
    venue = VenueId("BINANCE")
    instrument = InstrumentId("BTCUSDT-PERP")
    signal = FundingRateSignal(venue, instrument, 0.0001, 10.95, confidence=0.8)
    risk = LiquidationCascadeRisk(venue, instrument, 10.0, 20.0, 0.4)
    assert signal.confidence == 0.8
    assert risk.cascade_probability == 0.4

    funding = await FundingRateExtension().analyze(instrument, venue)
    liquidation = await LiquidationCascadeExtension().analyze(instrument, venue)
    assert funding["type"] == "funding_rate"
    assert liquidation["type"] == "liquidation_cascade"

    with pytest.raises(ValueError, match="confidence"):
        FundingRateSignal(venue, instrument, 0.0, 0.0, confidence=1.1)
    with pytest.raises(ValueError, match="notionals"):
        LiquidationCascadeRisk(venue, instrument, -1.0, 0.0, 0.2)
    with pytest.raises(ValueError, match="cascade_probability"):
        LiquidationCascadeRisk(venue, instrument, 0.0, 0.0, float("nan"))


def test_testnet_symbol_batch_rotates_across_full_universe() -> None:
    symbols = [f"S{index}" for index in range(25)]
    first, cursor = _rotating_symbol_batch(symbols, 0)
    second, cursor = _rotating_symbol_batch(symbols, cursor)
    third, cursor = _rotating_symbol_batch(symbols, cursor)

    assert first == symbols[:10]
    assert second == symbols[10:20]
    assert third == symbols[20:] + symbols[:5]
    assert cursor == 5
    assert _rotating_symbol_batch([], 9) == ([], 0)
    assert _rotating_symbol_batch(symbols, 9, 0) == ([], 0)
