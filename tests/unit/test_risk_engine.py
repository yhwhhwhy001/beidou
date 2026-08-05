
"""PKG-20A/B/C: Risk Engine 测试。"""
from beidou_shared.types import (
    AccountRef, CorrelationId, InstrumentId, MonetaryValue, OrderId, Price,
    Quantity, RiskApprovalId, RiskDecision, RiskApprovalId, VenueId, AccountId,
)
from beidou_safety.risk import RiskRuleLevel, RiskCheckResult, PreRiskContext
from beidou_safety.risk.engine import (
    PreRiskCheckerImpl, RiskEngineImpl, RiskApprovalSignerImpl,
    RiskApprovalStateMachine, RiskSnapshot, PostRiskMonitor,
)

class TestPreRisk:
    def test_leverage_exceeded(self):
        checker = PreRiskCheckerImpl(max_leverage=3.0)
        ctx = PreRiskContext(
            account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
            instrument_id=InstrumentId("BTCUSDT"), order_quantity=Quantity(amount="10"),
            order_price=Price(amount="50000"), leverage=5.0,
            correlation_id=CorrelationId("test-001"),
        )
        import asyncio
        results = asyncio.run(checker.check(ctx))
        assert any(r.decision == RiskDecision.REJECTED for r in results)

    def test_normal_order_passes(self):
        checker = PreRiskCheckerImpl()
        ctx = PreRiskContext(
            account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
            instrument_id=InstrumentId("BTCUSDT"), order_quantity=Quantity(amount="0.1"),
            order_price=Price(amount="50000"), leverage=1.0,
            correlation_id=CorrelationId("test-002"),
        )
        import asyncio
        results = asyncio.run(checker.check(ctx))
        assert any(r.decision == RiskDecision.APPROVED for r in results)

class TestRiskApproval:
    def test_approve_and_verify(self):
        signer = RiskApprovalSignerImpl()
        aid = RiskApprovalId("approval-001")
        signer.sign(aid)
        import asyncio
        assert asyncio.run(signer.verify(aid))

    def test_revoke(self):
        signer = RiskApprovalSignerImpl()
        aid = RiskApprovalId("approval-002")
        signer.sign(aid)
        signer.revoke(aid)
        import asyncio
        assert not asyncio.run(signer.verify(aid))

    def test_state_machine(self):
        sm = RiskApprovalStateMachine()
        aid = RiskApprovalId("approval-003")
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
