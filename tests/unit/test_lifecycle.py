
"""PKG-02: Module Lifecycle 测试。状态机、降级传播、能力协商。"""
from beidou_lifecycle import (
    ModuleLifecycle, ModuleState, DegradationLevel,
    HealthEvidence, CapabilityRegistry, CapabilityVersion, CapabilityNegotiation,
    CompatibilityResult,
)
from beidou_shared.types import ResultStatus, SchemaVersion

class TestModuleStateMachine:
    def test_normal_boot_sequence(self):
        lm = ModuleLifecycle("test_module")
        assert lm.state == ModuleState.PROVISIONING
        assert lm.transition(ModuleState.BOOTSTRAPPING) == ResultStatus.SUCCESS
        assert lm.transition(ModuleState.WARMING) == ResultStatus.SUCCESS
        assert lm.transition(ModuleState.VALIDATING) == ResultStatus.SUCCESS
        assert lm.transition(ModuleState.ACTIVE) == ResultStatus.SUCCESS
        assert lm.state == ModuleState.ACTIVE

    def test_cannot_skip_states(self):
        lm = ModuleLifecycle("test")
        assert lm.transition(ModuleState.ACTIVE) == ResultStatus.ERROR
        assert lm.state == ModuleState.PROVISIONING

    def test_degradation_path(self):
        lm = ModuleLifecycle("test")
        for s in [ModuleState.BOOTSTRAPPING, ModuleState.WARMING, ModuleState.VALIDATING, ModuleState.ACTIVE]:
            lm.transition(s)
        assert lm.transition(ModuleState.DEGRADED) == ResultStatus.SUCCESS
        assert lm.transition(ModuleState.RECOVERING) == ResultStatus.SUCCESS
        assert lm.transition(ModuleState.VALIDATING) == ResultStatus.SUCCESS

    def test_locked_is_terminal(self):
        lm = ModuleLifecycle("test")
        for s in [ModuleState.BOOTSTRAPPING, ModuleState.WARMING, ModuleState.VALIDATING, ModuleState.ACTIVE]:
            lm.transition(s)
        lm.transition(ModuleState.LOCKED)
        for s in ModuleState:
            if s != ModuleState.LOCKED:
                assert lm.transition(s) == ResultStatus.ERROR

    def test_restart_not_directly_active(self):
        lm = ModuleLifecycle("test")
        assert not lm.should_restart_directly_to_active()

    def test_failed_can_reprovision(self):
        lm = ModuleLifecycle("test")
        lm.state = ModuleState.FAILED
        assert lm.transition(ModuleState.PROVISIONING) == ResultStatus.SUCCESS


class TestDegradation:
    def test_active_lowest_priority(self):
        lm = ModuleLifecycle("test")
        lm.state = ModuleState.ACTIVE
        assert lm.get_degradation_level() == DegradationLevel.ACTIVE

    def test_locked_highest_priority(self):
        lm = ModuleLifecycle("test")
        lm.state = ModuleState.LOCKED
        assert lm.get_degradation_level() == DegradationLevel.LOCKED

    def test_resolve_most_restrictive(self):
        lm = ModuleLifecycle("test")
        result = lm.resolve_degradation([DegradationLevel.ACTIVE, DegradationLevel.LOCKED, DegradationLevel.NO_NEW_RISK])
        assert result == DegradationLevel.LOCKED

    def test_resolve_empty_defaults_active(self):
        lm = ModuleLifecycle("test")
        result = lm.resolve_degradation([])
        assert result == DegradationLevel.ACTIVE


class TestHealthEvidence:
    def test_unhealthy_when_invariants_invalid(self):
        evidence = HealthEvidence(
            module_name="test", state=ModuleState.ACTIVE,
            dependencies_healthy={"db": True}, data_freshness_seconds={"orderbook": 0.1},
            checkpoint_lag=0, invariants_valid=False, schema_version="2.0.0",
            leadership_status="LEADER",
        )
        assert not evidence.is_healthy()

    def test_unhealthy_when_not_active(self):
        evidence = HealthEvidence(
            module_name="test", state=ModuleState.DEGRADED,
            dependencies_healthy={"db": True}, data_freshness_seconds={"orderbook": 0.1},
            checkpoint_lag=0, invariants_valid=True, schema_version="2.0.0",
            leadership_status="LEADER",
        )
        assert not evidence.is_healthy()

    def test_healthy_when_all_ok(self):
        evidence = HealthEvidence(
            module_name="test", state=ModuleState.ACTIVE,
            dependencies_healthy={"db": True}, data_freshness_seconds={"orderbook": 0.1},
            checkpoint_lag=0, invariants_valid=True, schema_version="2.0.0",
            leadership_status="LEADER",
        )
        assert evidence.is_healthy()

    def test_unhealthy_with_incidents(self):
        evidence = HealthEvidence(
            module_name="test", state=ModuleState.ACTIVE,
            dependencies_healthy={"db": True}, data_freshness_seconds={"orderbook": 0.1},
            checkpoint_lag=0, invariants_valid=True, schema_version="2.0.0",
            leadership_status="LEADER", active_incidents=["INC-001"],
        )
        assert not evidence.is_healthy()


class TestCapabilityNegotiation:
    def test_exact_match_compatible(self):
        registry = CapabilityRegistry()
        registry.register_provider("kafka", CapabilityVersion(
            capability_name="kafka", schema_version=SchemaVersion("2.0.0"),
            min_compatible_version=SchemaVersion("1.0.0"), contract_hash="abc123",
        ))
        consumer = CapabilityVersion("kafka", SchemaVersion("2.0.0"), SchemaVersion("1.0.0"), "abc123")
        negotiation = registry.negotiate("kafka", consumer)
        assert negotiation.result == CompatibilityResult.COMPATIBLE
        assert negotiation.can_join_consumer_group()

    def test_backward_compatible(self):
        registry = CapabilityRegistry()
        registry.register_provider("kafka", CapabilityVersion(
            capability_name="kafka", schema_version=SchemaVersion("3.0.0"),
            min_compatible_version=SchemaVersion("1.0.0"), contract_hash="def456",
        ))
        consumer = CapabilityVersion("kafka", SchemaVersion("2.0.0"), SchemaVersion("2.0.0"), "old")
        negotiation = registry.negotiate("kafka", consumer)
        assert negotiation.result in (CompatibilityResult.BACKWARD_COMPATIBLE, CompatibilityResult.COMPATIBLE)

    def test_incompatible(self):
        registry = CapabilityRegistry()
        registry.register_provider("kafka", CapabilityVersion(
            capability_name="kafka", schema_version=SchemaVersion("2.0.0"),
            min_compatible_version=SchemaVersion("2.0.0"), contract_hash="abc",
        ))
        consumer = CapabilityVersion("kafka", SchemaVersion("1.0.0"), SchemaVersion("1.0.0"), "old")
        negotiation = registry.negotiate("kafka", consumer)
        assert negotiation.can_join_consumer_group() is False

    def test_no_provider_unknown(self):
        registry = CapabilityRegistry()
        consumer = CapabilityVersion("unknown", SchemaVersion("1.0.0"), SchemaVersion("1.0.0"), "abc")
        negotiation = registry.negotiate("unknown", consumer)
        assert negotiation.result == CompatibilityResult.UNKNOWN
        assert not negotiation.can_join_consumer_group()
