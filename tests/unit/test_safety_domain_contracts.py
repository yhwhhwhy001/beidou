"""Contract tests for safety-domain modules that must remain replayable.

These are deterministic domain tests, not substitutes for the real venue,
PostgreSQL, or Testnet gates.  They exercise invariants that are otherwise
easy to leave unexecuted by the integration suite.
"""

from __future__ import annotations

import time
from dataclasses import replace

import pytest

from beidou_safety.execution.decision_persistence import (
    DataAuthority,
    DecisionSnapshot,
    DecisionStore,
    get_authority,
)
from beidou_safety.execution.order_machine import OrderAggregate, OrderState, OrderStateMachine
from beidou_safety.gate import GateCertificate
from beidou_safety.position.projection import FillEvent, PositionProjection, PositionSide
from beidou_safety.protection.exchange_protection import (
    ExchangeProtectionManager,
    ProtectionType,
)
from beidou_safety.protection.lifecycle import PositionAggregate as LifecyclePosition
from beidou_safety.protection.lifecycle import PositionManager
from beidou_safety.risk.approval import ApprovalSigner, PaperApprovalPort, SignedApproval
from beidou_safety.risk.liquidation import LiquidationCalculator
from beidou_shared.types import GateResult, InstrumentId, MonetaryValue, Price, Quantity, VenueId
from beidou_strategy.portfolio.constraints import ConstraintOptimizer
from beidou_strategy.state.real_cost_model import RealCostModel


def _decision() -> DecisionSnapshot:
    return DecisionSnapshot(
        decision_id="decision-1",
        strategy_id="strategy-1",
        instrument_id="BTCUSDT",
        venue_id="BINANCE",
        account_fact_version="acct-v1",
        policy_version="policy-v1",
        feature_snapshot_hash="features-hash",
        market_state={"regime": "TREND"},
        cost_estimate={"total_bps": 8.0},
        risk_snapshot={"max_leverage": 2.0},
        direction="LONG",
        target_quantity=0.1,
        approval_id="approval-1",
        approval_hash="approval-hash",
        correlation_id="corr-1",
        causation_id="cause-1",
    )


def _fill(
    trade_id: str,
    side: PositionSide,
    quantity: str,
    price: str,
    sequence: int,
    fee: str = "0",
) -> FillEvent:
    return FillEvent(
        fill_id=f"fill-{trade_id}",
        trade_id=trade_id,
        venue_id=VenueId("BINANCE"),
        instrument_id=InstrumentId("BTCUSDT"),
        side=side,
        quantity=Quantity(amount=quantity),
        price=Price(amount=price),
        fee=MonetaryValue(amount=fee),
        sequence=sequence,
    )


def test_decision_store_checksum_duplicate_and_authority() -> None:
    decision = _decision()
    store = DecisionStore()
    assert decision.checksum
    assert store.persist(decision) is True
    assert store.persist(decision) is False
    assert store.get_by_correlation("corr-1") == [decision]
    assert store.verify_all({"version": "acct-v1"}, {"version": "policy-v1"}, {}) == []
    assert store.verify_all({"version": "acct-v2"}, {"version": "policy-v1"}, {}) == ["decision-1"]
    assert store.integrity_check() is True
    assert get_authority("positions") is DataAuthority.EXCHANGE
    assert get_authority("ledger_entries") is DataAuthority.DERIVED
    assert get_authority("untrusted_input") is DataAuthority.NOT_VERIFIABLE


def test_decision_checksum_detects_mutation() -> None:
    decision = _decision()
    store = DecisionStore()
    assert store.persist(decision)
    object.__setattr__(decision, "direction", "SHORT")
    assert store.integrity_check() is False


def test_approval_sign_verify_expiry_and_paper_is_non_tradable(monkeypatch: pytest.MonkeyPatch) -> None:
    signer = ApprovalSigner("unit-test-signing-key")
    approval = signer.create_approval("a-1", "intent", "portfolio", "acct-v1", "policy-v1", ttl_seconds=60)
    assert approval.verify("unit-test-signing-key") is True
    assert signer.verify(approval) is True
    assert approval.verify("wrong-key") is False
    tampered = replace(approval, intent_hash="other-intent")
    assert tampered.verify("unit-test-signing-key") is False
    expired = replace(approval, expires_at=time.time() - 1)
    assert expired.is_expired() is True
    assert expired.verify("unit-test-signing-key") is False
    monkeypatch.delenv("BEIDOU_SIGNING_KEY", raising=False)
    with pytest.raises(ValueError):
        ApprovalSigner()
    paper = PaperApprovalPort().decide("snapshot-hash", "intent", "portfolio", "policy-v1")
    assert paper.mode == "PAPER"
    assert paper.non_tradable is True


def test_signed_approval_generation_is_part_of_signature() -> None:
    base = SignedApproval(
        approval_id="a-1",
        intent_hash="intent",
        portfolio_decision_hash="portfolio",
        account_fact_version="acct-v1",
        policy_version="policy-v1",
        signer="unit",
        issued_at=time.time(),
        expires_at=time.time() + 60,
        generation=1,
    )
    signed = base.sign("key")
    assert signed.verify("key") is True
    assert replace(signed, generation=2).verify("key") is False


def test_order_state_machine_transitions_fills_and_idempotency() -> None:
    order = OrderAggregate(
        order_id="o-1",
        client_order_id="c-1",
        instrument_id="BTCUSDT",
        venue_id="BINANCE",
        side="BUY",
        order_type="LIMIT",
        original_quantity=2.0,
        approval_id="approval-1",
    )
    machine = OrderStateMachine()
    assert machine.create(order) is order
    assert machine.create(order) is order
    assert order.transition(OrderState.SENT) is False
    assert order.transition(OrderState.READY) is True
    assert order.transition(OrderState.SENT) is True
    assert order.transition(OrderState.ACKED) is True
    assert order.apply_fill(0.5, 100.0, commission=0.1, trade_id="trade-1") is True
    assert order.state is OrderState.PARTIAL
    assert order.apply_fill(0.5, 120.0, commission=0.1, trade_id="trade-1") is False
    assert order.apply_fill(1.5, 110.0, commission=0.1, trade_id="trade-2") is True
    assert order.state is OrderState.FILLED
    assert order.is_terminal is True
    assert order.remaining_quantity == 0.0
    assert order.avg_fill_price == pytest.approx((50 + 165) / 2.0)
    assert machine.get_by_client_id("c-1") is order
    assert machine.active_orders() == []
    assert machine.total_slice_quantity("approval-1") == 2.0
    with pytest.raises(ValueError):
        machine.create(order)


def test_order_unknown_freezes_symbol_and_can_recover() -> None:
    order = OrderAggregate(
        order_id="o-unknown",
        client_order_id="c-unknown",
        instrument_id="ETHUSDT",
        venue_id="BINANCE",
        side="SELL",
        order_type="MARKET",
        original_quantity=1.0,
    )
    machine = OrderStateMachine()
    machine.create(order)
    assert order.transition(OrderState.READY)
    assert order.transition(OrderState.SENT)
    assert order.transition(OrderState.UNKNOWN)
    assert machine.has_unknown_for_symbol("ETHUSDT") is True
    assert order.transition(OrderState.ACKED)
    assert machine.has_unknown_for_symbol("ETHUSDT") is False


def test_exchange_protection_requires_exchange_ack_for_coverage() -> None:
    manager = ExchangeProtectionManager()
    long_orders = manager.create_for_position("pos-long", "BTCUSDT", "BINANCE", 100.0, 2.0, "LONG")
    short_orders = manager.create_for_position("pos-short", "ETHUSDT", "BINANCE", 100.0, 1.0, "SHORT")
    assert [item.order_type for item in long_orders] == [ProtectionType.STOP_MARKET, ProtectionType.TAKE_PROFIT_MARKET]
    assert long_orders[0].side == "SELL"
    assert short_orders[0].side == "BUY"
    assert manager.has_coverage("pos-long") is False
    assert manager.all_positions_covered() is False
    assert manager.ack_protection("missing", "venue-1") is False
    assert manager.ack_protection("sl-pos-long", "venue-sl") is True
    assert manager.has_coverage("pos-long") is True
    manager.adjust_for_partial_fill("pos-long", 0.75)
    assert manager._protections["sl-pos-long"].quantity == 0.75
    report = manager.coverage_report()
    assert report == {"total_positions": 2, "covered": 1, "uncovered": 1, "coverage_pct": 50.0}


def test_position_lifecycle_tracks_protection_and_realized_pnl() -> None:
    manager = PositionManager()
    long_pos = LifecyclePosition("p1", "BTCUSDT", "BINANCE", "LONG", 100.0, 2.0)
    manager.open(long_pos)
    assert manager.protection_coverage_pct() == 0.0
    assert manager.unprotected_positions() == [long_pos]
    long_pos.protect("sl-1", ["tp-1"])
    assert long_pos.is_protected is True
    assert manager.protection_coverage_pct() == 100.0
    long_pos.increase(1.0, 130.0)
    assert long_pos.entry_price == pytest.approx(110.0)
    assert manager.close_position("p1", 120.0) == pytest.approx(30.0)
    assert manager.all_open() == []
    short_pos = LifecyclePosition("p2", "ETHUSDT", "BINANCE", "SHORT", 100.0, 1.0)
    short_pos.reduce(1.0, 90.0)
    assert short_pos.realized_pnl == pytest.approx(10.0)


def test_position_projection_replay_reverse_and_reconcile() -> None:
    projection = PositionProjection()
    open_fill = _fill("t1", PositionSide.LONG, "2", "100", 1, "0.2")
    add_fill = _fill("t2", PositionSide.LONG, "1", "110", 2)
    close_fill = _fill("t3", PositionSide.SHORT, "1", "120", 3)
    reverse_fill = _fill("t4", PositionSide.SHORT, "3", "90", 4)
    assert projection.apply(open_fill) is True
    assert projection.apply(open_fill) is False
    assert projection.apply(add_fill) is True
    assert projection.apply(close_fill) is True
    assert projection.apply(reverse_fill) is True
    positions = projection.rebuild()
    position = positions["BTCUSDT:BINANCE"]
    assert position.side is PositionSide.SHORT
    assert position.quantity == pytest.approx(1.0)
    assert position.realized_pnl == pytest.approx(-10.0)
    assert position.total_fees == pytest.approx(0.2)
    assert position.is_reduce_only_safe(1.0) is True
    assert position.is_reduce_only_safe(1.01) is False
    assert projection.fill_count == 4
    assert projection.reconcile_against({"BTCUSDT:BINANCE": 1.0}) == (True, [])
    ok, diffs = projection.reconcile_against({"BTCUSDT:BINANCE": 2.0})
    assert ok is False
    assert diffs and "system=1.0" in diffs[0]


def test_liquidation_calculator_handles_long_short_invalid_and_critical() -> None:
    long_price = LiquidationCalculator.calculate_long_liquidation(100.0, 1.0, 2.0)
    short_price = LiquidationCalculator.calculate_short_liquidation(100.0, 1.0, 2.0)
    assert long_price == pytest.approx(50.2)
    assert short_price == pytest.approx(149.4)
    assert LiquidationCalculator.calculate_long_liquidation(0, 1, 2) == 0.0
    assert LiquidationCalculator.calculate_short_liquidation(0, 1, 2) == float("inf")
    risk = LiquidationCalculator.assess_risk("BTCUSDT", 51.0, 100.0, 1.0, 2.0)
    assert risk.initial_margin == pytest.approx(25.5)
    assert risk.margin_ratio == pytest.approx(0.008)
    assert risk.is_critical is True
    short_risk = LiquidationCalculator.assess_risk("BTCUSDT", 100.0, 100.0, 1.0, 2.0, side="SHORT")
    assert short_risk.liquidation_price == pytest.approx(short_price)


def test_constraint_optimizer_fail_closed_and_limits_exposure() -> None:
    optimizer = ConstraintOptimizer(max_gross_leverage=1.0, max_net_leverage=0.5, max_per_symbol_pct=50.0)
    signals = {
        "BTCUSDT": {"direction": "LONG", "strength": 1.0, "price": 100.0},
        "ETHUSDT": {"direction": "NO_ACTION", "strength": 1.0, "price": 100.0},
        "XRPUSDT": {"direction": "SHORT", "strength": 0.1, "price": 0.0},
    }
    unknown = optimizer.optimize(signals, 0.0)
    assert unknown.targets == {}
    assert unknown.is_tradable is False
    result = optimizer.optimize(signals, 1000.0, min_notional={"BTCUSDT": 5.0}, step_sizes={"BTCUSDT": 0.1})
    assert result.targets["BTCUSDT"] == pytest.approx(0.1)
    assert result.rejected["XRPUSDT"] == "UNKNOWN price"
    assert result.gross_exposure == pytest.approx(0.01)


def test_real_cost_model_records_and_calibrates_oos_samples() -> None:
    model = RealCostModel()
    model.set_fee_tier("BINANCE", 1.0, 3.0)
    estimate = model.estimate(
        "BTCUSDT",
        "BUY",
        1.0,
        100.0,
        spread_bps=2.0,
        bid_depth=20.0,
        ask_depth=10.0,
        participation_pct=0.1,
        funding_rate_bps=3.0,
    )
    assert estimate.notional == 100.0
    assert estimate.taker_fee_bps == 3.0
    assert estimate.impact_bps > 0.0
    assert estimate.total_cost_bps == pytest.approx(
        estimate.half_spread_cost_bps + estimate.taker_fee_bps + estimate.impact_bps + estimate.funding_cost_bps
    )
    assert model.calibrate("v1").sample_count == 0
    for _ in range(20):
        model.record_actual(estimate, estimate.total_cost_bps)
    calibration = model.calibrate("v1")
    assert calibration.sample_count == 20
    assert calibration.is_valid is True
    assert model.get_calibration() == calibration


def test_gate_certificate_defaults_to_independent_reviewer() -> None:
    certificate = GateCertificate("G7", GateResult.UNVERIFIABLE, evidence_paths=["evidence/g7.json"])
    assert certificate.gate_id == "G7"
    assert certificate.result is GateResult.UNVERIFIABLE
    assert certificate.reviewer == "independent_review"
