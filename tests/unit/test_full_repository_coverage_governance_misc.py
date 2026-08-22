"""Behavioral coverage for autonomy, lifecycle, ladder, and release boundaries."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from beidou_autonomy.mapek import Checkpoint, FaultFingerprint, MAPEKController, RecoveryAction, RecoveryResult
from beidou_autonomy.recovery_planner import (
    PersistedRecoveryState,
    RecoveryExecutor,
    RecoveryOrchestrator,
    RecoveryPlanner,
    RecoveryVerifier,
)
from beidou_delivery.release import ArtifactIdentity, MigrationCompatibility, ReleaseManager, ReleaseStatus
from beidou_lifecycle.capability import CapabilityRegistry, CompatibilityResult
from beidou_lifecycle.lifecycle import (
    DEGRADATION_FROM_STATE,
    DegradationLevel,
    HealthEvidence,
    ModuleLifecycle,
    ModuleState,
    StartupWorkflow,
)
from beidou_production.ladder import LadderLevel, ProductionLadder
from beidou_shared.types import GateResult, SchemaVersion


class _ControlPlane:
    def __init__(self, *, fail: bool = False) -> None:
        self.actions: list[object] = []
        self.fail = fail

    def get_status(self) -> str:
        return "NORMAL"

    def execute_action(self, action: object) -> None:
        if self.fail:
            raise RuntimeError("control unavailable")
        self.actions.append(action)


class _UnknownAction:
    value = "UNKNOWN"

    def __hash__(self) -> int:
        return hash(self.value)


def _checkpoint(valid: bool = True) -> Checkpoint:
    return Checkpoint("cp-1", "engine", {"position": 1}, 1, valid)


def _limits() -> dict[LadderLevel, float]:
    return {level: float(index * 1000) for index, level in enumerate(LadderLevel)}


def _artifact(signature: str | None = None) -> ArtifactIdentity:
    return ArtifactIdentity(
        artifact_id="artifact-1",
        commit_sha="commit-1",
        schema_versions={"order": "2.0"},
        policy_versions={"risk": "2.0"},
        model_versions={"alpha": "3.0"},
        rust_component_hashes={"core": "hash"},
        signature=signature,
    )


def test_mapek_recovery_actions_and_authority_fail_closed() -> None:
    controller = MAPEKController()
    empty = FaultFingerprint()
    assert empty.similarity(FaultFingerprint()) == 0.0
    close_but_below = FaultFingerprint(symptom_vector={"cpu": 0.9})
    controller.register_fingerprint(close_but_below)
    assert controller.decide_action({"cpu": 0.1}, "engine")[0] is RecoveryAction.LOCK

    assert controller.execute_recovery(RecoveryAction.NOOP, "engine") is RecoveryResult.SUCCESS
    assert controller.execute_recovery(RecoveryAction.LOCK, "engine") is RecoveryResult.DEGRADED
    assert controller.execute_recovery(RecoveryAction.RESTART_MODULE, "engine", _checkpoint()) is RecoveryResult.SUCCESS
    assert (
        controller.execute_recovery(RecoveryAction.RESTART_MODULE, "engine", _checkpoint(False))
        is RecoveryResult.PARTIAL
    )
    assert controller.execute_recovery(RecoveryAction.ROLLBACK_CHECKPOINT, "engine") is RecoveryResult.FAILED
    assert (
        controller.execute_recovery(RecoveryAction.ROLLBACK_CHECKPOINT, "engine", _checkpoint())
        is RecoveryResult.SUCCESS
    )
    assert controller.execute_recovery(RecoveryAction.DEGRADE_TO_NO_NEW_RISK, "engine") is RecoveryResult.DEGRADED
    assert controller.execute_recovery(RecoveryAction.DEGRADE_TO_EXIT_ONLY, "engine") is RecoveryResult.DEGRADED
    assert controller.execute_recovery(RecoveryAction.EMERGENCY_FLATTEN, "engine") is RecoveryResult.DEGRADED
    unknown = _UnknownAction()
    assert controller.execute_recovery(unknown, "engine") is RecoveryResult.FAILED  # type: ignore[arg-type]
    assert controller._recovery_evidence

    failing_plane = _ControlPlane(fail=True)
    assert (
        controller.execute_with_authority(RecoveryAction.LOCK, "engine", control_plane=failing_plane)
        is RecoveryResult.FAILED
    )
    assert (
        controller.execute_with_authority(RecoveryAction.RESTART_MODULE, "engine", _checkpoint())
        is RecoveryResult.SUCCESS
    )
    assert (
        controller.execute_with_authority(RecoveryAction.RESTART_MODULE, "engine", _checkpoint(False))
        is RecoveryResult.PARTIAL
    )
    assert controller.execute_with_authority(RecoveryAction.RESTART_MODULE, "engine") is RecoveryResult.FAILED
    assert controller.execute_with_authority(RecoveryAction.ROLLBACK_CHECKPOINT, "engine") is RecoveryResult.FAILED
    assert (
        controller.execute_with_authority(RecoveryAction.ROLLBACK_CHECKPOINT, "engine", _checkpoint())
        is RecoveryResult.SUCCESS
    )
    assert controller.execute_with_authority(RecoveryAction.NOOP, "engine") is RecoveryResult.SUCCESS
    plane = _ControlPlane()
    for action in (
        RecoveryAction.LOCK,
        RecoveryAction.DEGRADE_TO_NO_NEW_RISK,
        RecoveryAction.DEGRADE_TO_EXIT_ONLY,
        RecoveryAction.EMERGENCY_FLATTEN,
    ):
        assert controller.execute_with_authority(action, "engine", control_plane=plane) is RecoveryResult.DEGRADED
    assert len(plane.actions) == 4
    assert controller.execute_with_authority(_UnknownAction(), "engine") is RecoveryResult.FAILED  # type: ignore[arg-type]


def test_recovery_planner_and_orchestrator_success_branch(tmp_path: Path) -> None:
    controller = MAPEKController()
    fp = FaultFingerprint(
        symptom_vector={"crash": 1.0},
        effective_actions=[RecoveryAction.RESTART_MODULE],
        approved_runbook="runbook",
    )
    controller.register_fingerprint(fp)
    controller.restore_restart_counter("engine", 3)
    controller.decide_action = lambda _symptoms, _module: (RecoveryAction.RESTART_MODULE, "retry")  # type: ignore[method-assign]
    action, reason = RecoveryPlanner(controller).plan({"crash": 1.0}, "engine")
    assert action is RecoveryAction.LOCK
    assert reason.startswith("MAX_RESTARTS_EXCEEDED")

    fresh_controller = MAPEKController()
    fresh_controller.register_fingerprint(fp)
    state = PersistedRecoveryState(module_name="engine", _state_file=str(tmp_path / "recovery.json"))
    result, message = RecoveryOrchestrator(
        RecoveryPlanner(fresh_controller),
        RecoveryExecutor(fresh_controller),
        RecoveryVerifier(),
        state,
    ).recover({"crash": 1.0}, "engine", {"state": False}, _checkpoint(), _ControlPlane(), True)
    assert result is RecoveryResult.FAILED
    assert "VERIFY_FAILED:INVARIANTS_FAILED" in message

    failing_state = PersistedRecoveryState(module_name="engine", _state_file=str(tmp_path / "persist.json"))
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "beidou_autonomy.recovery_planner.os.replace", lambda *_args: (_ for _ in ()).throw(OSError("disk full"))
        )
        assert failing_state.save() is False


def test_capability_and_module_lifecycle_edges() -> None:
    registry = CapabilityRegistry()
    registry.register_capability("orders", SchemaVersion("2.0.0"), SchemaVersion("1.0.0"))
    assert registry.check_compatibility("orders", SchemaVersion("1.0.0")) is CompatibilityResult.BACKWARD_COMPATIBLE
    assert registry.check_compatibility("missing", SchemaVersion("1.0.0")) is CompatibilityResult.UNKNOWN
    assert registry.negotiate("orders", registry.get_provider_versions("orders")[0]).can_join_consumer_group()

    lifecycle = ModuleLifecycle("engine")
    lifecycle.state = ModuleState.ACTIVE
    assert lifecycle.get_degradation_level() is DEGRADATION_FROM_STATE[ModuleState.ACTIVE]
    assert lifecycle.resolve_degradation([]) is DegradationLevel.ACTIVE
    assert (
        lifecycle.resolve_degradation([DegradationLevel.NO_NEW_RISK, DegradationLevel.EXIT_ONLY])
        is DegradationLevel.EXIT_ONLY
    )
    assert lifecycle.latest_evidence() is None
    evidence = HealthEvidence(
        module_name="engine",
        state=ModuleState.PROVISIONING,
        dependencies_healthy={},
        data_freshness_seconds={},
        checkpoint_lag=0,
        invariants_valid=False,
        schema_version="1",
        leadership_status="STANDBY",
    )
    lifecycle.record_evidence(evidence)
    assert lifecycle.latest_evidence() is evidence
    assert lifecycle.should_restart_directly_to_active() is False
    unhealthy_dependency = HealthEvidence(
        module_name="engine",
        state=ModuleState.ACTIVE,
        dependencies_healthy={"database": False},
        data_freshness_seconds={},
        checkpoint_lag=0,
        invariants_valid=True,
        schema_version="1",
        leadership_status="LEADER",
    )
    assert unhealthy_dependency.is_healthy() is False

    workflow = StartupWorkflow()
    assert workflow.advance("TRADING_READY") is False
    assert workflow.advance("PROCESS_START") is True
    assert workflow.advance("PROCESS_START") is False
    for phase in StartupWorkflow.PHASES[1:]:
        assert workflow.advance(phase) is True
    assert workflow.is_trading_ready() is True


def test_production_ladder_configuration_and_degradation_edges() -> None:
    import beidou_production.ladder as ladder_module

    unverified = ProductionLadder()
    assert isinstance(unverified.can_promote_to(LadderLevel.L1_SHADOW), bool)
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(ladder_module, "CAPITAL_LIMITS_VERIFIED", False)
        monkeypatch.setattr(ladder_module, "LEVEL_CAPITAL_LIMITS", dict.fromkeys(LadderLevel, 0.0))
        blocked = ProductionLadder()
        assert blocked.can_promote_to(LadderLevel.L1_SHADOW) is False
    with pytest.raises(ValueError, match="explicitly define"):
        ProductionLadder({LadderLevel.L0_PAPER: 0.0})
    ladder = ProductionLadder(_limits(), max_drawdown_pct=10.0, max_incidents=2)
    assert ladder.current_capital_limit() == 0.0
    assert ladder.can_promote_to(LadderLevel.L2_CANARY) is False
    invalid = ladder.certify(LadderLevel.L2_CANARY, GateResult.PASS, [""])
    assert invalid.result is GateResult.UNVERIFIABLE
    l1 = ladder.certify(LadderLevel.L1_SHADOW, GateResult.PASS, ["g5.json"])
    assert l1.is_pass()
    l2 = ladder.certify(LadderLevel.L2_CANARY, GateResult.PASS, ["g6.json"])
    assert l2.is_pass()
    assert ladder.current_capital_limit() == 2000.0
    assert ladder.can_promote_to(LadderLevel.L1_SHADOW) is False
    assert ladder.should_degrade(0.0, 10.0, 0) is False
    assert ladder.should_degrade(0.0, None, 3) is True
    assert ladder.should_degrade(11.0, 10.0, 0) is True

    fallback, verified = ladder_module._load_capital_limits()
    assert isinstance(verified, bool)
    assert set(fallback) == set(LadderLevel)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "beidou_shared.config.ConfigProvider.load",
            lambda _self: SimpleNamespace(
                source="safety_only:missing",
                capital_ladder=SimpleNamespace(capital_limits={level.name: 0.0 for level in LadderLevel}),
            ),
        )
        safety_only, safety_verified = ladder_module._load_capital_limits()
        assert safety_verified is False
        assert set(safety_only) == set(LadderLevel)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "beidou_shared.config.ConfigProvider.load",
            lambda _self: SimpleNamespace(
                source="env-file:testnet",
                capital_ladder=SimpleNamespace(
                    capital_limits={
                        level.name: (-1.0 if level is LadderLevel.L2_CANARY else 1.0) for level in LadderLevel
                    }
                ),
            ),
        )
        invalid, invalid_verified = ladder_module._load_capital_limits()
        assert invalid_verified is False
        assert set(invalid) == set(LadderLevel)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "beidou_shared.config.ConfigProvider.load",
            lambda _self: SimpleNamespace(
                source="env-file:testnet",
                capital_ladder=SimpleNamespace(
                    capital_limits={level.name: float(index) for index, level in enumerate(LadderLevel)}
                ),
                production=SimpleNamespace(max_drawdown_pct=8.0, max_consecutive_losses=4),
            ),
        )
        loaded, loaded_verified = ladder_module._load_capital_limits()
        assert loaded_verified is True and loaded[LadderLevel.L2_CANARY] == 2.0
        configured = ProductionLadder(_limits())
        assert configured._max_drawdown_pct == 8.0
        assert configured._max_incidents == 4

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "beidou_shared.config.ConfigProvider.load",
            lambda _self: SimpleNamespace(
                source="env-file:testnet",
                capital_ladder=SimpleNamespace(
                    capital_limits={level.name: float(index) for index, level in enumerate(LadderLevel)}
                ),
                production=SimpleNamespace(max_drawdown_pct=8.0, max_consecutive_losses=4),
            ),
        )
        configured_defaults = ProductionLadder(_limits())
        assert configured_defaults._max_drawdown_pct == 8.0
        assert configured_defaults._max_incidents == 4

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "beidou_shared.config.ConfigProvider.load",
            lambda _self: (_ for _ in ()).throw(RuntimeError("config unavailable")),
        )
        blocked = ProductionLadder(_limits())
        assert blocked.should_degrade(0.0, 1.0, 0) is True


def test_release_manager_signature_migration_and_rollback_edges() -> None:
    manager = ReleaseManager()
    with pytest.raises(ValueError, match="Incompatible"):
        manager.register_build(_artifact(), MigrationCompatibility.INCOMPATIBLE)
    record = manager.register_build(_artifact(), MigrationCompatibility.FORWARD_BACKWARD)
    assert manager.promote_to_active("missing") is not None
    assert manager.promote_to_active(record.release_id).value == "ERROR"
    assert manager.sign_artifact(record.release_id, "sig").value == "SUCCESS"
    assert manager.promote_to_active(record.release_id).value == "ERROR"
    record.status = ReleaseStatus.TESTING
    assert manager.promote_to_active(record.release_id).value == "SUCCESS"
    record.migration_check = MigrationCompatibility.INCOMPATIBLE
    record.status = ReleaseStatus.TESTING
    assert manager.promote_to_active(record.release_id).value == "ERROR"
    second = manager.register_build(_artifact("sig2"), MigrationCompatibility.FORWARD_ONLY)
    second.status = ReleaseStatus.CANARY
    assert manager.promote_to_active(second.release_id).value == "SUCCESS"
    assert manager.get_active().release_id == second.release_id
    assert manager.rollback(record.release_id).value == "SUCCESS"
    assert manager.get_active().release_id == record.release_id
    assert manager.rollback("missing").value == "UNKNOWN"
    assert manager.sign_artifact("missing", "sig").value == "UNKNOWN"
