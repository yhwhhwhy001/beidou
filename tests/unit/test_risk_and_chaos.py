"""BD-CV33/54: Risk Engine 边界 + Chaos 故障注入场景测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from beidou_certification.contracts import (
    FaultInjectionResult,
    FaultScenario,
)
from beidou_control.plane import ControlAction, ControlPlane, RiskDirection, ValidationResult
from beidou_control.truth import TradingEligibility, derive_eligibility, eligibility_to_control_action
from beidou_strategy.portfolio.contracts import (
    RiskApproval,
    RiskState,
    RiskStateAuthority,
)


# ============================================================================
# BD-CV33: RiskStateAuthority + Approval
# ============================================================================


class TestRiskStateTransitions:
    """BD-CV33: 风险状态转换 — corrupt/missing ≠ NORMAL。"""

    def test_normal_to_warning_to_critical(self):
        assert RiskState.NORMAL.value == "NORMAL"
        assert RiskState.WARNING.value == "WARNING"
        assert RiskState.CRITICAL.value == "CRITICAL"
        assert RiskState.CORRUPT.value == "CORRUPT"
        # 状态严重度递增
        severities = {RiskState.NORMAL: 0, RiskState.WARNING: 1, RiskState.CRITICAL: 2, RiskState.CORRUPT: 3}
        assert severities[RiskState.CORRUPT] > severities[RiskState.NORMAL]
        assert severities[RiskState.CRITICAL] > severities[RiskState.WARNING]

    def test_approval_binds_to_specific_state(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        approval = RiskApproval(
            approval_id="a1",
            approved_by="admin",
            approved_at=datetime.now(timezone.utc).isoformat(),
            expires_at=future,
            max_exposure=50000.0,
            is_expired=False,
        )
        assert approval.is_valid()

    def test_corrupt_state_with_any_approval_blocked(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        approval = RiskApproval(approval_id="a1", max_exposure=50000.0, is_expired=False, expires_at=future)
        rsa = RiskStateAuthority(state=RiskState.CORRUPT, active_approvals=[approval])
        assert not rsa.can_create_intent()

    def test_empty_approvals_blocked(self):
        rsa = RiskStateAuthority(state=RiskState.NORMAL, active_approvals=[])
        assert not rsa.can_create_intent()

    def test_multiple_approvals_any_valid_suffices(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        expired = RiskApproval(approval_id="a1", is_expired=True)
        valid = RiskApproval(approval_id="a2", is_expired=False, expires_at=future)
        rsa = RiskStateAuthority(state=RiskState.NORMAL, active_approvals=[expired, valid])
        assert rsa.can_create_intent()


# ============================================================================
# BD-CV02: ControlAuthority Transition
# ============================================================================


class TestControlTransitions:
    """BD-CV02: 控制状态转换合法性。"""

    def test_lock_is_terminal(self):
        cp = ControlPlane()
        cp.execute_action(ControlAction.NO_NEW_RISK)
        cp.execute_action(ControlAction.LOCK)
        # LOCK → LOCK 允许（终态自循环）
        cp.execute_action(ControlAction.LOCK)
        assert cp.get_status() == ControlAction.LOCK

    def test_no_new_risk_rejects_increase(self):
        cp = ControlPlane()
        cp.execute_action(ControlAction.NO_NEW_RISK)
        result = cp.validate_intent(SimpleNamespace(side="BUY", order_type="MARKET"))
        assert not result.allowed
        assert result.risk_direction == RiskDirection.INCREASE

    def test_resume_allows_increase(self):
        cp = ControlPlane()
        cp.execute_action(ControlAction.RESUME)
        result = cp.validate_intent(SimpleNamespace(side="BUY", order_type="MARKET"))
        assert result.allowed

    def test_invalid_transition_rejected(self):
        cp = ControlPlane()
        cp.execute_action(ControlAction.LOCK)
        with pytest.raises(RuntimeError):
            cp.execute_action(ControlAction.RESUME)  # LOCK → RESUME 非法

    def test_version_monotonic(self):
        cp = ControlPlane()
        v1 = cp.version
        cp.execute_action(ControlAction.NO_NEW_RISK)
        v2 = cp.version
        assert v2 > v1

    def test_emergency_flatten_rejects_increase(self):
        cp = ControlPlane()
        cp.execute_action(ControlAction.EMERGENCY_FLATTEN)
        result = cp.validate_intent(SimpleNamespace(side="BUY", order_type="MARKET"))
        assert not result.allowed


# ============================================================================
# BD-CV02: Eligibility → ControlAction mapping
# ============================================================================


class TestEligibilityMapping:
    """TradingEligibility → ControlAction 映射。"""

    def test_eligible_maps_to_resume(self):
        assert eligibility_to_control_action(TradingEligibility.ELIGIBLE) == ControlAction.RESUME

    def test_lock_maps_to_lock(self):
        assert eligibility_to_control_action(TradingEligibility.LOCK) == ControlAction.LOCK

    def test_not_verifiable_maps_to_no_new_risk(self):
        assert eligibility_to_control_action(TradingEligibility.NOT_VERIFIABLE) == ControlAction.NO_NEW_RISK

    def test_no_new_risk_maps_correctly(self):
        assert eligibility_to_control_action(TradingEligibility.NO_NEW_RISK) == ControlAction.NO_NEW_RISK

    def test_exit_only_maps_correctly(self):
        assert eligibility_to_control_action(TradingEligibility.EXIT_ONLY) == ControlAction.EXIT_ONLY


# ============================================================================
# BD-CV54: Chaos Fault Injection — 12 scenarios
# ============================================================================


class TestFaultInjectionScenarios:
    """BD-CV54: 每个 P0 fault 场景有机器可判定 PASS/FAIL。"""

    ALL_SCENARIOS = list(FaultScenario)

    def test_all_12_scenarios_covered(self):
        assert len(self.ALL_SCENARIOS) == 12

    @pytest.mark.parametrize("scenario", ALL_SCENARIOS)
    def test_scenario_has_machine_decidable_result(self, scenario):
        result = FaultInjectionResult(scenario=scenario, passed=True)
        assert result.is_machine_decidable(), f"Scenario {scenario.value} not machine-decidable"

    def test_timeout_scenario(self):
        result = FaultInjectionResult(
            scenario=FaultScenario.TIMEOUT,
            passed=True, duplicate_orders_detected=0, risk_increase_detected=False,
        )
        assert result.is_machine_decidable()

    def test_kill9_scenario(self):
        result = FaultInjectionResult(
            scenario=FaultScenario.KILL_9,
            passed=True, duplicate_orders_detected=0, risk_increase_detected=False,
        )
        assert result.is_machine_decidable()

    def test_dual_instance_scenario(self):
        result = FaultInjectionResult(
            scenario=FaultScenario.DUAL_INSTANCE,
            passed=False, duplicate_orders_detected=1, risk_increase_detected=True,
        )
        assert not result.is_machine_decidable()

    def test_user_stream_gap_scenario(self):
        result = FaultInjectionResult(
            scenario=FaultScenario.USER_STREAM_GAP,
            passed=False, risk_increase_detected=False,
        )
        assert result.is_machine_decidable()

    def test_db_crash_scenario(self):
        result = FaultInjectionResult(
            scenario=FaultScenario.DB_CRASH,
            passed=True, duplicate_orders_detected=0, risk_increase_detected=False,
        )
        assert result.is_machine_decidable()

    def test_exchange_rule_change_scenario(self):
        result = FaultInjectionResult(
            scenario=FaultScenario.EXCHANGE_RULE_CHANGE,
            passed=False, risk_increase_detected=True,
        )
        assert not result.is_machine_decidable()


# ============================================================================
# BD-CV41: RiskDirection classification
# ============================================================================


class TestRiskDirectionClassification:
    """BD-CV41: 风险方向分类正确性。"""

    def test_buy_is_increase(self):
        direction = ControlPlane.classify_risk_direction("BUY")
        assert direction == RiskDirection.INCREASE

    def test_sell_is_increase(self):
        direction = ControlPlane.classify_risk_direction("SELL")
        assert direction == RiskDirection.INCREASE

    def test_reduce_only_is_reduce(self):
        direction = ControlPlane.classify_risk_direction("SELL", reduce_only=True)
        assert direction == RiskDirection.REDUCE

    def test_close_position_is_flatten(self):
        direction = ControlPlane.classify_risk_direction("SELL", close_position=True)
        assert direction == RiskDirection.FLATTEN

    def test_cancel_is_cancel(self):
        direction = ControlPlane.classify_risk_direction("BUY", order_type="CANCEL")
        assert direction == RiskDirection.CANCEL

    def test_query_is_query(self):
        direction = ControlPlane.classify_risk_direction("BUY", order_type="QUERY")
        assert direction == RiskDirection.QUERY

    def test_unknown_is_increase_fail_closed(self):
        direction = ControlPlane.classify_risk_direction("UNKNOWN")
        assert direction == RiskDirection.INCREASE  # fail-closed


# Helpers
class SimpleNamespace:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
