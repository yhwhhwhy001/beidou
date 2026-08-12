"""合约桥接方法集成测试。测试 engine.py 中的合同桥接方法。"""

from __future__ import annotations

from beidou_certification.contracts import CertificationGate, GateLevel, StagedCertification
from beidou_control.truth import RESUME_REQUIRED_EVIDENCE
from beidou_research.contracts import FactorEvidence, FactorLifecycleState
from beidou_safety.execution.contracts import (
    ExecutionCostSnapshot,
    Fill,
    OrderIdempotencyKey,
    PositionAggregate,
    TripleReconciliation,
)
from beidou_strategy.portfolio.contracts import (
    AdaptiveSizing,
    OptimizationResult,
    RiskApproval,
    RiskState,
    RiskStateAuthority,
)


class TestOrderIdempotencyKey:
    def test_different_keys_have_different_hashes(self):
        k1 = OrderIdempotencyKey(correlation_id="c1", client_order_id="o1", outbox_id="b1")
        k2 = OrderIdempotencyKey(correlation_id="c2", client_order_id="o1", outbox_id="b1")
        assert k1.compute_hash() != k2.compute_hash()

    def test_same_key_same_hash(self):
        k1 = OrderIdempotencyKey(correlation_id="a", client_order_id="b", outbox_id="c")
        k2 = OrderIdempotencyKey(correlation_id="a", client_order_id="b", outbox_id="c")
        assert k1.compute_hash() == k2.compute_hash()


class TestPositionAggregateReplay:
    def test_single_fill_correct_net(self):
        pa = PositionAggregate(symbol="BTCUSDT")
        fills = [Fill(fill_id="f1", symbol="BTCUSDT", side="BUY", quantity=1.0, price=50000.0)]
        result = pa.replay(fills)
        assert result.net_position == 1.0
        assert result.avg_entry_price == 50000.0

    def test_buy_sell_net_zero(self):
        pa = PositionAggregate(symbol="BTCUSDT")
        fills = [
            Fill(fill_id="f1", symbol="BTCUSDT", side="BUY", quantity=1.0, price=50000.0),
            Fill(fill_id="f2", symbol="BTCUSDT", side="SELL", quantity=1.0, price=51000.0),
        ]
        result = pa.replay(fills)
        assert result.net_position == 0.0

    def test_multiple_fills_with_commission(self):
        pa = PositionAggregate(symbol="BTCUSDT")
        fills = [
            Fill(fill_id="f1", symbol="BTCUSDT", side="BUY", quantity=1.0, price=50000.0, commission=5.0),
            Fill(fill_id="f2", symbol="BTCUSDT", side="BUY", quantity=0.5, price=50100.0, commission=2.5),
        ]
        result = pa.replay(fills)
        assert result.net_position == 1.5

    def test_deterministic_replay(self):
        pa = PositionAggregate(symbol="ETHUSDT", net_position=0.5, avg_entry_price=3000.0)
        fills = [Fill(fill_id="f1", symbol="ETHUSDT", side="SELL", quantity=0.3, price=3100.0)]
        r1 = pa.replay(fills)
        r2 = pa.replay(fills)
        assert r1.net_position == r2.net_position
        assert r1.avg_entry_price == r2.avg_entry_price


class TestTripleReconciliation:
    def test_detect_same_source_fraud_needs_three_sources(self):
        tr = TripleReconciliation()
        assert not tr.detect_same_source_fraud(["system", "system"])  # 同源
        assert not tr.detect_same_source_fraud(["system", "exchange"])  # 仅两个
        assert tr.detect_same_source_fraud(["system", "exchange", "event_stream"])  # 三个独立源

    def test_mismatch_tracking(self):
        tr = TripleReconciliation(
            venue_orders=5,
            local_orders=5,
            ledger_entries=5,
            is_matched=True,
            mismatches=[],
        )
        assert tr.is_matched

    def test_unmatched_with_mismatches(self):
        tr = TripleReconciliation(
            venue_orders=5,
            local_orders=4,
            ledger_entries=5,
            is_matched=False,
            mismatches=["position_mismatch"],
        )
        assert not tr.is_matched
        assert len(tr.mismatches) == 1


class TestExecutionCostSnapshot:
    def test_cannot_learn_without_fill(self):
        snap = ExecutionCostSnapshot(symbol="BTCUSDT", has_fill=False, has_fee=True)
        assert not snap.can_learn()

    def test_cannot_learn_without_fee(self):
        snap = ExecutionCostSnapshot(symbol="BTCUSDT", has_fill=True, has_fee=False)
        assert not snap.can_learn()

    def test_can_learn_with_fill_and_fee(self):
        snap = ExecutionCostSnapshot(symbol="BTCUSDT", has_fill=True, has_fee=True)
        assert snap.can_learn()


class TestStagedCertification:
    def test_empty_chain_is_complete(self):
        sc = StagedCertification()
        assert sc.is_chain_complete()

    def test_single_gate_is_complete(self):
        g0 = CertificationGate(gate_level=GateLevel.G0, certified=True)
        sc = StagedCertification(stages={"G0": g0}, current_gate="G0")
        assert sc.is_chain_complete()

    def test_cannot_skip_gates(self):
        g0 = CertificationGate(gate_level=GateLevel.G0, certified=True)
        g2 = CertificationGate(gate_level=GateLevel.G2, certified=True)
        sc = StagedCertification(stages={"G0": g0, "G2": g2})
        g0_can_skip = g0.can_skip_to(GateLevel.G2)
        assert not g0_can_skip  # 不可跳 G1
        assert set(sc.stages) == {"G0", "G2"}

    def test_production_parity_not_verified_without_g8(self):
        sc = StagedCertification()
        assert not sc.production_parity_verified()

    def test_g8_still_only_candidate(self):
        g8 = CertificationGate(gate_level=GateLevel.G8, certified=True)
        sc = StagedCertification(stages={"G8": g8})
        assert sc.production_parity_verified()
        # G8 = Mainnet Candidate, 不自动启用


class TestResumeEvidence:
    def test_all_required_evidence_present(self):
        assert len(RESUME_REQUIRED_EVIDENCE) == 4
        assert any("reconciliation" in e.lower() for e in RESUME_REQUIRED_EVIDENCE)
        assert any("protection" in e.lower() for e in RESUME_REQUIRED_EVIDENCE)
        assert any("risk" in e.lower() for e in RESUME_REQUIRED_EVIDENCE)
        assert any("truthsnapshot" in e.lower() for e in RESUME_REQUIRED_EVIDENCE)


class TestFactorEvidenceBridge:
    def test_nan_sharpe_cannot_promote(self):

        fe = FactorEvidence(factor_id="f1", sharpe=float("nan"))
        assert not fe.can_promote()

    def test_zero_sharpe_cannot_promote(self):
        fe = FactorEvidence(factor_id="f1", sharpe=0.0)
        assert not fe.can_promote()

    def test_positive_sharpe_with_evidence_can_promote(self):
        fe = FactorEvidence(factor_id="f1", sharpe=0.8, evidence_dag_hash="abc123")
        assert fe.can_promote()

    def test_without_evidence_hash_cannot_promote(self):
        fe = FactorEvidence(factor_id="f1", sharpe=0.8, evidence_dag_hash="")
        assert not fe.can_promote()

    def test_full_evidence_dag(self):
        fe = FactorEvidence(factor_id="f1", state=FactorLifecycleState.ACTIVE, sharpe=0.8, evidence_dag_hash="dag-001")
        dag = fe.full_evidence_dag()
        assert dag["factor_id"] == "f1"
        assert dag["state"] == "ACTIVE"


class TestRiskStateAuthority:
    def test_corrupt_cannot_create_intent(self):
        rsa = RiskStateAuthority(state=RiskState.CORRUPT)
        assert not rsa.can_create_intent()

    def test_critical_cannot_create_intent(self):
        rsa = RiskStateAuthority(state=RiskState.CRITICAL)
        assert not rsa.can_create_intent()

    def test_normal_with_valid_approval_can_create(self):
        from datetime import datetime, timedelta, timezone

        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        approval = RiskApproval(approval_id="a1", max_exposure=10000.0, is_expired=False, expires_at=future)
        rsa = RiskStateAuthority(state=RiskState.NORMAL, active_approvals=[approval])
        assert rsa.can_create_intent()

    def test_expired_approval_blocks_intent(self):
        approval = RiskApproval(approval_id="a1", is_expired=True)
        rsa = RiskStateAuthority(state=RiskState.NORMAL, active_approvals=[approval])
        assert not rsa.can_create_intent()


class TestAdaptiveSizingEdgeCases:
    def test_none_previous_no_increase(self):
        curr = AdaptiveSizing(risk_adjusted_leverage=5.0, is_safe=False)
        assert not curr.does_size_increase(None)

    def test_safe_unchanged(self):
        prev = AdaptiveSizing(risk_adjusted_leverage=1.0, is_safe=True)
        curr = AdaptiveSizing(risk_adjusted_leverage=1.0, is_safe=True)
        assert not curr.does_size_increase(prev)


class TestOptimizationResult:
    def test_empty_violations_is_constrained(self):
        opt = OptimizationResult(symbols=["BTC"], weights=[1.0])
        assert opt.is_constrained()

    def test_with_violations_not_constrained(self):
        opt = OptimizationResult(constraint_violations=["max_concentration"])
        assert not opt.is_constrained()
