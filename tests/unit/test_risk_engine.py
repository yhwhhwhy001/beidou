"""PKG-20A/B/C: Risk Engine 测试。"""

import pytest

from beidou_safety.risk import PreRiskContext
from beidou_safety.risk.engine import (
    PostRiskMonitor,
    PreRiskCheckerImpl,
    RiskApprovalSignerImpl,
    RiskApprovalStateMachine,
)
from beidou_shared.types import (
    AccountId,
    AccountRef,
    CorrelationId,
    InstrumentId,
    Price,
    Quantity,
    RiskApprovalId,
    RiskDecision,
    VenueId,
)


@pytest.fixture(autouse=True)
def _set_signing_key(monkeypatch):
    """BD-P0-05: 测试用签名密钥。"""
    monkeypatch.setenv("BEIDOU_SIGNING_KEY", "test-signing-key-for-unit-tests")


class TestPreRisk:
    def test_leverage_exceeded(self):
        checker = PreRiskCheckerImpl(max_leverage=3.0)
        ctx = PreRiskContext(
            account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
            instrument_id=InstrumentId("BTCUSDT"),
            order_quantity=Quantity(amount="10"),
            order_price=Price(amount="50000"),
            leverage=5.0,
            correlation_id=CorrelationId("test-001"),
        )
        import asyncio

        results = asyncio.run(checker.check(ctx))
        assert any(r.decision == RiskDecision.REJECTED for r in results)

    def test_normal_order_passes(self):
        checker = PreRiskCheckerImpl()
        ctx = PreRiskContext(
            account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
            instrument_id=InstrumentId("BTCUSDT"),
            order_quantity=Quantity(amount="0.1"),
            order_price=Price(amount="50000"),
            leverage=1.0,
            correlation_id=CorrelationId("test-002"),
        )
        import asyncio

        results = asyncio.run(checker.check(ctx))
        assert any(r.decision == RiskDecision.APPROVED for r in results)


class TestRiskApproval:
    def _make_signer(self) -> RiskApprovalSignerImpl:
        """创建带测试密钥的签名器。"""
        return RiskApprovalSignerImpl(signing_key="test-signing-key-for-unit-tests")

    def test_sign_requires_key(self, monkeypatch):
        """无密钥时 sign() 必须抛出 SIGNING_UNAVAILABLE。"""
        monkeypatch.delenv("BEIDOU_SIGNING_KEY", raising=False)
        signer = RiskApprovalSignerImpl()  # no key
        aid = RiskApprovalId("approval-000")
        import pytest as _pytest

        with _pytest.raises(RuntimeError, match="SIGNING_UNAVAILABLE"):
            signer.sign(aid)

    def test_issue_requires_approved_risk(self):
        signer = self._make_signer()
        aid = RiskApprovalId("approval-000-approved-boundary")
        with pytest.raises(RuntimeError, match="RISK_NOT_APPROVED"):
            signer.issue_for_approved_risk(aid, risk_approved=False)

        signature = signer.issue_for_approved_risk(aid, risk_approved=True, nonce="nonce-approved-boundary")
        assert signature

    def test_verify_denied_without_signature(self):
        """无签名时 verify() 必须返回 False（不再支持无签名旁路）。"""
        signer = self._make_signer()
        aid = RiskApprovalId("approval-001")
        signer.sign(aid, nonce="nonce-001")
        import asyncio

        assert not asyncio.run(signer.verify(aid))  # no signature provided

    def test_approve_and_verify(self):
        """正常签名→验证流程。"""
        signer = self._make_signer()
        aid = RiskApprovalId("approval-002")
        sig = signer.sign(aid, nonce="nonce-002")
        import asyncio

        assert asyncio.run(signer.verify(aid, signature=sig, nonce="nonce-002"))

    def test_verify_wrong_signature(self):
        """错误签名必须被拒绝。"""
        signer = self._make_signer()
        aid = RiskApprovalId("approval-003")
        signer.sign(aid, nonce="nonce-003")
        import asyncio

        assert not asyncio.run(signer.verify(aid, signature="bad-signature", nonce="nonce-003"))

    def test_verify_replay_rejected(self):
        """nonce 重放必须被拒绝。"""
        signer = self._make_signer()
        aid = RiskApprovalId("approval-004")
        sig = signer.sign(aid, nonce="nonce-004")
        import asyncio

        assert asyncio.run(signer.verify(aid, signature=sig, nonce="nonce-004"))
        # 重放相同 nonce
        assert not asyncio.run(signer.verify(aid, signature=sig, nonce="nonce-004"))

    def test_preflight_verify_does_not_consume_nonce_before_final_send(self):
        """审批预检可重复；nonce 只在最终发送边界消费。"""
        signer = self._make_signer()
        aid = RiskApprovalId("approval-004-preflight")
        sig = signer.sign(aid, nonce="nonce-004-preflight")
        import asyncio

        assert asyncio.run(signer.verify(aid, signature=sig, nonce="nonce-004-preflight", consume_nonce=False))
        assert asyncio.run(signer.verify(aid, signature=sig, nonce="nonce-004-preflight"))
        assert not asyncio.run(signer.verify(aid, signature=sig, nonce="nonce-004-preflight"))

    def test_verify_tampered_payload_rejected(self):
        """篡改 payload 字段导致签名不匹配。"""
        signer = self._make_signer()
        aid = RiskApprovalId("approval-005")
        sig = signer.sign(aid, proposal_hash="hash-A", nonce="nonce-005")
        import asyncio

        # 验证时使用不同的 proposal_hash
        assert not asyncio.run(signer.verify(aid, signature=sig, proposal_hash="hash-B", nonce="nonce-005"))

    def test_revoke(self):
        """BD-T01: 吊销签名后验证应失败 — 基于签名撤销集，不再基于内存集合。"""
        signer = self._make_signer()
        aid = RiskApprovalId("approval-006")
        sig = signer.sign(aid, nonce="nonce-006")
        import asyncio

        # 吊销签名
        signer.revoke(sig)
        assert not asyncio.run(signer.verify(aid, signature=sig, nonce="nonce-006"))

    def test_signing_unavailable_verify_denied(self, monkeypatch):
        """密钥不可用时 verify() 一律返回 False。"""
        monkeypatch.delenv("BEIDOU_SIGNING_KEY", raising=False)
        signer = RiskApprovalSignerImpl()  # no key
        aid = RiskApprovalId("approval-007")
        import asyncio

        assert not signer.signing_available
        assert not asyncio.run(signer.verify(aid, signature="any-sig"))

    def test_state_machine(self):
        sm = RiskApprovalStateMachine()
        aid = RiskApprovalId("approval-008")
        assert sm.get(aid) == RiskDecision.PENDING
        sm.approve(aid)
        assert sm.get(aid) == RiskDecision.APPROVED


class TestPostRisk:
    def test_cannot_approve_orders(self):
        monitor = PostRiskMonitor()
        assert monitor.cannot_approve()

    def test_recommend_degradation_after_violations(self):
        monitor = PostRiskMonitor()
        for i in range(6):
            monitor.record_violation(f"violation {i}")
        assert monitor.recommend_degradation()
