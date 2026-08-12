"""BD-CV02: TruthSnapshot 与 TradingEligibility 测试。"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from beidou_control.plane import ControlAction
from beidou_control.truth import (
    ELIGIBILITY_TO_CONTROL,
    RESUME_REQUIRED_EVIDENCE,
    TradingEligibility,
    TruthSnapshot,
    derive_eligibility,
    eligibility_to_control_action,
)


def _fresh_snapshot(**overrides) -> TruthSnapshot:
    """构造一个完整的新鲜快照。"""
    now = datetime.now(timezone.utc).timestamp()
    defaults = {
        "snapshot_id": "test-snap-001",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "market_hash": "abc123",
        "account_hash": "def456",
        "order_hash": "ghi789",
        "position_hash": "jkl012",
        "ledger_hash": "mno345",
        "reconciliation_hash": "pqr678",
        "protection_hash": "stu901",
        "risk_hash": "vwx234",
        "config_hash": "yza567",
        "policy_hash": "bcd890",
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
    defaults.update(overrides)
    return TruthSnapshot(**defaults)


class TestTruthSnapshot:
    """BD-CV02: TruthSnapshot 基础测试。"""

    def test_empty_snapshot_detected(self):
        snap = TruthSnapshot()
        assert snap.is_empty()

    def test_fresh_snapshot_not_empty(self):
        snap = _fresh_snapshot()
        assert not snap.is_empty()

    def test_stale_detection(self):
        old = time.time() - 400  # 400s ago
        snap = _fresh_snapshot(
            reconciliation_freshness=old,
            protection_freshness=old,
        )
        assert snap.is_stale(max_age_seconds=300.0)

    def test_fresh_snapshot_not_stale(self):
        snap = _fresh_snapshot()
        assert not snap.is_stale(max_age_seconds=300.0)

    def test_unknown_components(self):
        snap = _fresh_snapshot(reconciliation_status="UNKNOWN")
        assert "reconciliation" in snap.has_unknown_components()

    def test_hash_determinism(self):
        fixed_ts = 1755590400.0  # fixed timestamp
        snap1 = _fresh_snapshot(
            created_at="2026-08-12T00:00:00+00:00",
            market_freshness=fixed_ts,
            account_freshness=fixed_ts,
            order_freshness=fixed_ts,
            position_freshness=fixed_ts,
            ledger_freshness=fixed_ts,
            reconciliation_freshness=fixed_ts,
            protection_freshness=fixed_ts,
            risk_freshness=fixed_ts,
            config_freshness=fixed_ts,
            policy_freshness=fixed_ts,
        )
        snap2 = _fresh_snapshot(
            created_at="2026-08-12T00:00:00+00:00",
            market_freshness=fixed_ts,
            account_freshness=fixed_ts,
            order_freshness=fixed_ts,
            position_freshness=fixed_ts,
            ledger_freshness=fixed_ts,
            reconciliation_freshness=fixed_ts,
            protection_freshness=fixed_ts,
            risk_freshness=fixed_ts,
            config_freshness=fixed_ts,
            policy_freshness=fixed_ts,
        )
        assert snap1.compute_hash() == snap2.compute_hash()

    def test_hash_differs_with_changes(self):
        fixed_time = "2026-08-12T00:00:00+00:00"
        snap1 = _fresh_snapshot(created_at=fixed_time)
        snap2 = _fresh_snapshot(created_at=fixed_time, reconciliation_status="MISMATCHED")
        assert snap1.compute_hash() != snap2.compute_hash()

    def test_empty_has_zero_freshness(self):
        snap = TruthSnapshot()
        assert snap.market_freshness == 0.0
        assert snap.is_empty()


class TestTradingEligibility:
    """BD-CV02: TradingEligibility 推导测试。"""

    def test_all_fresh_eligible(self):
        snap = _fresh_snapshot()
        assert derive_eligibility(snap) == TradingEligibility.ELIGIBLE

    def test_empty_snapshot_not_eligible(self):
        """AC-02-02: 空快照不能得到 ELIGIBLE。"""
        snap = TruthSnapshot()
        assert derive_eligibility(snap) == TradingEligibility.NOT_VERIFIABLE

    def test_missing_any_required_component_hash_is_not_verifiable(self):
        snap = _fresh_snapshot(position_hash="")
        assert derive_eligibility(snap) == TradingEligibility.NOT_VERIFIABLE

    def test_stale_snapshot_not_eligible(self):
        """AC-02-02: 陈旧快照不能得到 ELIGIBLE。"""
        old = time.time() - 400
        snap = _fresh_snapshot(
            market_freshness=old,
            account_freshness=old,
        )
        assert derive_eligibility(snap) == TradingEligibility.NOT_VERIFIABLE

    def test_unknown_component_not_eligible(self):
        """UNKNOWN 映射为 NO_NEW_RISK。"""
        snap = _fresh_snapshot(reconciliation_status="UNKNOWN")
        assert derive_eligibility(snap) == TradingEligibility.NO_NEW_RISK

    def test_mismatched_reconciliation_not_eligible(self):
        snap = _fresh_snapshot(reconciliation_status="MISMATCHED")
        assert derive_eligibility(snap) == TradingEligibility.NO_NEW_RISK

    def test_protection_gap_not_eligible(self):
        snap = _fresh_snapshot(protection_status="GAP")
        assert derive_eligibility(snap) == TradingEligibility.NO_NEW_RISK

    def test_risk_critical_maps_to_lock(self):
        snap = _fresh_snapshot(risk_status="CRITICAL")
        assert derive_eligibility(snap) == TradingEligibility.LOCK

    def test_risk_warning_not_eligible(self):
        snap = _fresh_snapshot(risk_status="WARNING")
        assert derive_eligibility(snap) == TradingEligibility.NO_NEW_RISK

    def test_stale_not_verifiable(self):
        """AC-02-02: 陈旧快照不能得到 ELIGIBLE。"""
        old = time.time() - 600
        snap = _fresh_snapshot(
            reconciliation_freshness=old,
            protection_freshness=old,
            risk_freshness=old,
        )
        assert derive_eligibility(snap, max_age_seconds=300.0) == TradingEligibility.NOT_VERIFIABLE


class TestEligibilityToControlMapping:
    """Eligibility → ControlAction 映射测试。"""

    def test_eligible_maps_to_resume(self):
        assert eligibility_to_control_action(TradingEligibility.ELIGIBLE) == ControlAction.RESUME

    def test_not_verifiable_maps_to_no_new_risk(self):
        assert eligibility_to_control_action(TradingEligibility.NOT_VERIFIABLE) == ControlAction.NO_NEW_RISK

    def test_lock_maps_to_lock(self):
        assert eligibility_to_control_action(TradingEligibility.LOCK) == ControlAction.LOCK

    def test_all_eligibilities_have_control_mapping(self):
        for eligibility in TradingEligibility:
            action = ELIGIBILITY_TO_CONTROL.get(eligibility)
            assert action is not None, f"Missing mapping for {eligibility}"


class TestResumeRequiredEvidence:
    """BD-CV02 AC-02-04: RESUME 证据要求。"""

    def test_resume_evidence_list(self):
        assert len(RESUME_REQUIRED_EVIDENCE) == 4
        assert any("reconciliation" in e.lower() for e in RESUME_REQUIRED_EVIDENCE)
        assert any("protection" in e.lower() for e in RESUME_REQUIRED_EVIDENCE)
        assert any("risk" in e.lower() for e in RESUME_REQUIRED_EVIDENCE)
        assert any("truthsnapshot" in e.lower() for e in RESUME_REQUIRED_EVIDENCE)


class TestControlPlaneAuthorizeResume:
    """BD-CV02: ControlPlane.authorize_resume 测试。"""

    def test_authorize_resume_with_fresh_snapshot(self):
        from beidou_control.plane import ControlPlane

        cp = ControlPlane()
        snap = _fresh_snapshot()
        allowed, reason = cp.authorize_resume(snap)
        assert allowed, reason

    def test_reject_resume_with_stale_snapshot(self):
        from beidou_control.plane import ControlPlane

        cp = ControlPlane()
        old = time.time() - 400
        snap = _fresh_snapshot(
            reconciliation_freshness=old,
            protection_freshness=old,
            risk_freshness=old,
        )
        allowed, reason = cp.authorize_resume(snap)
        assert not allowed
        assert "stale" in reason.lower() or "not_verifiable" in reason.lower()

    def test_reject_resume_with_unknown_status(self):
        from beidou_control.plane import ControlPlane

        cp = ControlPlane()
        snap = _fresh_snapshot(reconciliation_status="UNKNOWN")
        allowed, reason = cp.authorize_resume(snap)
        assert not allowed
        # UNKNOWN maps to NO_NEW_RISK in derive_eligibility, which propagates to reason message
        assert "rejected" in reason.lower()

    def test_evaluate_eligibility_returns_same_as_derive(self):
        from beidou_control.plane import ControlPlane

        cp = ControlPlane()
        snap = _fresh_snapshot()
        eligibility = cp.evaluate_eligibility(snap)
        assert eligibility == TradingEligibility.ELIGIBLE
