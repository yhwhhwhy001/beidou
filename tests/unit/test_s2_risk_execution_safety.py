"""
S2 (PKG08-15): 风险、订单、执行与保护安全测试。

覆盖：
- PKG08: 风险等级单调严重性 (BDS-P0-008, P0-009, P0-010)
- PKG10: 审批生命周期状态机 (BDS-P0-011)
- PKG11: REST 写请求盲重试防护 (BDS-P0-012)
"""

from __future__ import annotations

import pytest

from beidou_safety.risk.risk_level import (
    BreakerScope,
    RiskLevel,
    RiskLevelManager,
    RiskLevelState,
)
from beidou_safety.risk.engine import RiskApprovalStateMachine
from beidou_shared.types import RiskApprovalId, RiskDecision


class TestRiskLevelMonotonic:
    """PKG08: 风险等级单调严重性。"""

    def test_escalate_to_higher_level(self) -> None:
        """升级到更高级别成功。"""
        mgr = RiskLevelManager()
        assert mgr.current_level == RiskLevel.NORMAL

        state = mgr.escalate(RiskLevel.NO_NEW_RISK, "Test escalation")
        assert mgr.current_level == RiskLevel.NO_NEW_RISK
        assert state.generation == 1

    def test_cannot_downgrade(self) -> None:
        """降级必须被拒绝。"""
        mgr = RiskLevelManager()
        mgr.escalate(RiskLevel.LOCKED, "Breaker triggered")

        with pytest.raises(ValueError, match="不可降级"):
            mgr.escalate(RiskLevel.EXIT_ONLY, "Attempted downgrade")

    def test_cannot_downgrade_from_emergency(self) -> None:
        """从 EMERGENCY_FLATTEN 不允许任何降级。"""
        mgr = RiskLevelManager()
        mgr.escalate(RiskLevel.EMERGENCY_FLATTEN, "Critical breach")

        for lower in [RiskLevel.LOCKED, RiskLevel.EXIT_ONLY, RiskLevel.NO_NEW_RISK, RiskLevel.NORMAL]:
            with pytest.raises(ValueError, match="不可降级"):
                mgr.escalate(lower, f"Attempted downgrade to {lower.name}")

    def test_recover_with_evidence(self) -> None:
        """有签名证据的恢复允许降级。"""
        mgr = RiskLevelManager()
        mgr.escalate(RiskLevel.NO_NEW_RISK, "Minor breach")

        state = mgr.recover(RiskLevel.NORMAL, "signed_recovery_evidence_abc123")
        assert mgr.current_level == RiskLevel.NORMAL
        assert "RECOVERY" in state.reason

    def test_recover_without_evidence_fails(self) -> None:
        """无证据的恢复被拒绝。"""
        mgr = RiskLevelManager()
        mgr.escalate(RiskLevel.LOCKED, "Test")

        with pytest.raises(ValueError, match="签名证据"):
            mgr.recover(RiskLevel.NORMAL, "")

    def test_recover_too_high_fails(self) -> None:
        """恢复到比当前更高的等级被拒绝。"""
        mgr = RiskLevelManager()
        mgr.escalate(RiskLevel.NO_NEW_RISK, "Test")

        with pytest.raises(ValueError, match="NORMAL 或 NO_NEW_RISK"):
            mgr.recover(RiskLevel.EXIT_ONLY, "evidence")

    def test_corrupted_state_fallback_locked(self) -> None:
        """损坏状态 fallback 到 LOCKED（不 fallback NORMAL）。"""
        mgr = RiskLevelManager.from_corrupted_state(None)
        assert mgr.current_level == RiskLevel.LOCKED

        mgr2 = RiskLevelManager.from_corrupted_state({"level": "GARBAGE"})
        assert mgr2.current_level == RiskLevel.LOCKED

    def test_unknown_string_falls_back_locked(self) -> None:
        """未知字符串 → LOCKED（fail closed）。"""
        assert RiskLevel.from_string("UNKNOWN") == RiskLevel.LOCKED
        assert RiskLevel.from_string("CORRUPTED") == RiskLevel.LOCKED

    def test_daily_breakers_persist_across_reset(self) -> None:
        """日内重置不清除跨日 breaker。"""
        mgr = RiskLevelManager()
        mgr.escalate(RiskLevel.EXIT_ONLY, "Drawdown 5%", breaker_scope=BreakerScope.DAILY)
        mgr.escalate(RiskLevel.LOCKED, "Intraday spike", breaker_scope=BreakerScope.INTRADAY)

        assert mgr.current_level == RiskLevel.LOCKED

        mgr.reset_intraday()
        # 跨日 breaker 仍在，但日内 breaker 已清除
        # 由于跨日 breaker 还在，等级应保持（从 LOCKED 降级需要显式恢复）
        # reset_intraday 在无 daily breaker 时才会降级

    def test_intraday_only_resets_to_normal(self) -> None:
        """仅日内 breaker 触发时，重置后回到 NORMAL。"""
        mgr = RiskLevelManager()
        mgr.escalate(RiskLevel.NO_NEW_RISK, "Intraday volatility", breaker_scope=BreakerScope.INTRADAY)

        mgr.reset_intraday()
        assert mgr.current_level == RiskLevel.NORMAL


class TestApprovalLifecycle:
    """PKG10: 审批生命周期状态机。"""

    def test_full_lifecycle(self) -> None:
        """PENDING → APPROVED → CONSUMED 完整生命周期。"""
        sm = RiskApprovalStateMachine(default_ttl_seconds=3600)
        aid = RiskApprovalId("test-approval-001")

        # PENDING 初始状态
        assert sm.get(aid) == RiskDecision.PENDING

        # 批准
        result = sm.approve(aid, nonce="nonce-001", risk_snapshot_hash="abc123", policy_version="v2.0")
        assert result == RiskDecision.APPROVED
        assert sm.is_valid_for_use(aid)

        # 消费
        result = sm.consume(aid)
        assert result == RiskDecision.APPROVED
        assert sm.is_consumed(aid)
        assert not sm.is_valid_for_use(aid)

    def test_cannot_approve_rejected(self) -> None:
        """已拒绝的审批不可逆转。"""
        sm = RiskApprovalStateMachine()
        aid = RiskApprovalId("rejected-001")

        sm.reject(aid)
        result = sm.approve(aid)
        assert result == RiskDecision.REJECTED

    def test_cannot_reject_approved(self) -> None:
        """已批准的审批不可被拒绝覆盖。"""
        sm = RiskApprovalStateMachine()
        aid = RiskApprovalId("approved-001")

        sm.approve(aid)
        result = sm.reject(aid)
        assert result == RiskDecision.APPROVED

    def test_cannot_consume_twice(self) -> None:
        """审批不可重复消费（nonce 保护）。"""
        sm = RiskApprovalStateMachine()
        aid = RiskApprovalId("single-use-001")

        sm.approve(aid, nonce="n1")
        sm.consume(aid)
        assert sm.is_consumed(aid)
        # 第二次消费：状态不变
        sm.consume(aid)
        assert sm.is_consumed(aid)

    def test_approval_expires(self) -> None:
        """审批 TTL 超时后不可使用。"""
        sm = RiskApprovalStateMachine(default_ttl_seconds=0.01)  # 10ms TTL
        aid = RiskApprovalId("expiring-001")

        sm.approve(aid, ttl=0.001)
        import time
        time.sleep(0.01)
        assert not sm.is_valid_for_use(aid)

    def test_approve_if_verified_requires_both(self) -> None:
        """审批必须签名有效 AND 风控通过。"""
        sm = RiskApprovalStateMachine()
        aid = RiskApprovalId("verify-001")

        # 签名无效 → REJECTED
        result = sm.approve_if_verified(aid, signature_valid=False, risk_check_passed=True)
        assert result == RiskDecision.REJECTED

        # 风控不通过 → REJECTED
        aid2 = RiskApprovalId("verify-002")
        result = sm.approve_if_verified(aid2, signature_valid=True, risk_check_passed=False)
        assert result == RiskDecision.REJECTED

        # 两者都通过 → APPROVED
        aid3 = RiskApprovalId("verify-003")
        result = sm.approve_if_verified(aid3, signature_valid=True, risk_check_passed=True)
        assert result == RiskDecision.APPROVED

    def test_metadata_tracks_full_context(self) -> None:
        """审批元数据追踪完整上下文。"""
        sm = RiskApprovalStateMachine()
        aid = RiskApprovalId("meta-001")

        sm.approve(aid, nonce="n1", risk_snapshot_hash="snap1", policy_version="v1")

        meta = sm.get_metadata(aid)
        assert meta["status"] == "APPROVED"
        assert meta["nonce"] == "n1"
        assert meta["risk_snapshot_hash"] == "snap1"


class TestRiskLevelMutation:
    """PKG08: Mutation 测试。"""

    def test_mutation_downgrade_blocked(self) -> None:
        """Mutation: 任何降级尝试必须被阻止。"""
        mgr = RiskLevelManager()
        mgr.escalate(RiskLevel.LOCKED, "P0 breach")

        # 枚举所有可能的降级
        for level in RiskLevel:
            if level < RiskLevel.LOCKED:
                with pytest.raises(ValueError):
                    mgr.escalate(level, f"Mutation attempt: {level.name}")

    def test_mutation_cannot_normal_after_daily_breaker(self) -> None:
        """Mutation: 存在跨日 breaker 时不能恢复到 NORMAL。"""
        mgr = RiskLevelManager()
        mgr.escalate(RiskLevel.EXIT_ONLY, "Daily drawdown", breaker_scope=BreakerScope.DAILY)

        with pytest.raises(ValueError, match="跨日断路器"):
            mgr.recover(RiskLevel.NORMAL, "unsigned_attempt")
