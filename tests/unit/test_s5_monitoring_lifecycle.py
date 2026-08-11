"""
S5 (PKG22-26): 监控、生命周期、自愈与安全测试。

覆盖：
- PKG24 (BDS-P0-024): verify_recovery — 空不变量返回 False
- PKG24 (BDS-P1-044): HealthEvidence.is_healthy — freshness/checkpoint 参与
- PKG24 (BDS-P1-045): Lifecycle FSM 完整闭合
- PKG24 (BDS-P1-047): 重启计数器持久化接口
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from beidou_lifecycle.lifecycle import (
    HealthEvidence,
    ModuleLifecycle,
    ModuleState,
    VALID_TRANSITIONS,
)
from beidou_autonomy.mapek import MAPEKController


class TestVerifyRecovery:
    """PKG24 (BDS-P0-024): 恢复验证 — 空不变量返回 False。"""

    def test_empty_invariants_returns_false(self) -> None:
        controller = MAPEKController()
        result = controller.verify_recovery("test_module", {})
        assert result is False, "空不变量必须返回 False"

    def test_all_valid_returns_true(self) -> None:
        controller = MAPEKController()
        result = controller.verify_recovery("test", {"checkpoint_ok": True, "state_valid": True, "data_fresh": True})
        assert result is True

    def test_any_invalid_returns_false(self) -> None:
        controller = MAPEKController()
        result = controller.verify_recovery("test", {"a": True, "b": False, "c": True})
        assert result is False

    def test_single_invariant(self) -> None:
        controller = MAPEKController()
        assert controller.verify_recovery("test", {"ok": True}) is True
        assert controller.verify_recovery("test", {"ok": False}) is False


class TestHealthEvidence:
    """PKG24 (BDS-P1-044): 健康证据 — 完整健康契约。"""

    def _healthy_evidence(self) -> HealthEvidence:
        return HealthEvidence(
            module_name="test",
            state=ModuleState.ACTIVE,
            dependencies_healthy={"db": True, "ws": True},
            data_freshness_seconds={"market": 5.0, "account": 10.0},
            checkpoint_lag=50,
            invariants_valid=True,
            schema_version="v2.0",
            leadership_status="LEADER",
            active_incidents=[],
        )

    def test_healthy_when_all_ok(self) -> None:
        assert self._healthy_evidence().is_healthy()

    def test_stale_data_returns_unhealthy(self) -> None:
        evidence = replace(self._healthy_evidence(), data_freshness_seconds={"market": 200.0})
        assert not evidence.is_healthy()

    def test_checkpoint_lag_returns_unhealthy(self) -> None:
        evidence = replace(self._healthy_evidence(), checkpoint_lag=5000)
        assert not evidence.is_healthy()

    def test_missing_schema_returns_unhealthy(self) -> None:
        evidence = replace(self._healthy_evidence(), schema_version="")
        assert not evidence.is_healthy()

    def test_unknown_leadership_returns_unhealthy(self) -> None:
        evidence = replace(self._healthy_evidence(), leadership_status="UNKNOWN")
        assert not evidence.is_healthy()


class TestLifecycleFSM:
    """PKG24 (BDS-P1-045): 生命周期状态机完整闭合。"""

    def test_all_states_have_transitions(self) -> None:
        for state in ModuleState:
            assert state in VALID_TRANSITIONS, f"状态 {state} 缺少转换定义"

    def test_locked_is_terminal(self) -> None:
        assert VALID_TRANSITIONS[ModuleState.LOCKED] == set()

    def test_failed_can_recover(self) -> None:
        assert ModuleState.PROVISIONING in VALID_TRANSITIONS[ModuleState.FAILED]

    def test_valid_transition(self) -> None:
        lifecycle = ModuleLifecycle("test")
        lifecycle.state = ModuleState.PROVISIONING
        result = lifecycle.transition(ModuleState.BOOTSTRAPPING)
        assert result.value == "SUCCESS"
        assert lifecycle.state == ModuleState.BOOTSTRAPPING

    def test_invalid_transition_rejected(self) -> None:
        lifecycle = ModuleLifecycle("test")
        lifecycle.state = ModuleState.ACTIVE
        result = lifecycle.transition(ModuleState.PROVISIONING)
        assert result.value == "ERROR"
        assert lifecycle.state == ModuleState.ACTIVE


class TestRestartCounter:
    """PKG24 (BDS-P1-047): 重启计数器持久化接口。"""

    def test_get_restart_count_initial_zero(self) -> None:
        controller = MAPEKController()
        assert controller.get_restart_count("new_module") == 0

    def test_restore_and_get_count(self) -> None:
        controller = MAPEKController()
        controller.restore_restart_counter("module_a", 3)
        assert controller.get_restart_count("module_a") == 3

    def test_reset_counter(self) -> None:
        controller = MAPEKController()
        controller.restore_restart_counter("module_b", 5)
        controller.reset_restart_counter("module_b")
        assert controller.get_restart_count("module_b") == 0

    def test_restore_zero_does_nothing(self) -> None:
        controller = MAPEKController()
        controller.restore_restart_counter("module_c", 0)
        assert controller.get_restart_count("module_c") == 0
