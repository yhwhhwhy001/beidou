"""Behavioral coverage for previously unexercised safety-critical modules."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from beidou_certification.certificate_chain import CertificateChain, GateCertificate, GateResult
from beidou_chaos.process_fault_injector import ProcessFaultConfig, ProcessFaultInjector
from beidou_data.universe_hysteresis import UniverseEntry, UniverseHysteresis
from beidou_infra.atomic_persistence import (
    AtomicIntent,
    AtomicOrderAggregate,
    AtomicOutbox,
    AtomicPersistence,
    PersistStatus,
)
from beidou_infra.lease import FencingLease, LeaseManager, LeaseState
from beidou_safety.execution.double_entry_ledger import LedgerTransaction, TripleReconciliationResult
from beidou_safety.execution.execution_plan_engine import (
    BoundPlanSlice,
    ExecutionAlgorithm,
    ExecutionPlanEngine,
)
from beidou_safety.position.position_aggregate import (
    FillEvent,
    PositionAggregate,
    ProtectionAggregate,
    ProtectionOrder,
)
from beidou_strategy.kernel.typed_kernel import (
    FilterResult,
    KernelInput,
    StrategyAction,
    TypedStrategyKernel,
    get_typed_strategy_kernel,
    reset_kernel,
)
from beidou_strategy.risk.adaptive_sizing_engine import SizingDecision, SizingInput, compute_adaptive_sizing


def test_position_replay_prices_realized_pnl_and_reduce_only_bounds() -> None:
    fills = [
        FillEvent("f1", "BTCUSDT", "BUY", 1.0, 100.0),
        FillEvent("f2", "BTCUSDT", "BUY", 1.0, 120.0),
        FillEvent("f3", "BTCUSDT", "SELL", 0.5, 130.0),
    ]
    result = PositionAggregate(symbol="BTCUSDT").replay(fills)

    assert result.net_position == 1.5
    assert result.avg_entry_price == 110.0
    assert result.realized_pnl == 10.0
    assert result.version == 3
    assert result.is_reduce_only_compliant(FillEvent("r1", "BTCUSDT", "SELL", 1.5, 100.0))
    assert not result.is_reduce_only_compliant(FillEvent("r2", "BTCUSDT", "SELL", 1.6, 100.0))
    assert not result.is_reduce_only_compliant(FillEvent("r3", "BTCUSDT", "BUY", 0.1, 100.0))


def test_position_cross_zero_reprices_and_invalid_fills_fail() -> None:
    long = PositionAggregate(symbol="BTCUSDT").apply_fill(FillEvent("f1", "BTCUSDT", "BUY", 1.0, 100.0))
    crossed = long.apply_fill(FillEvent("f2", "BTCUSDT", "SELL", 2.0, 90.0))
    assert crossed.net_position == -1.0
    assert crossed.avg_entry_price == 90.0
    assert crossed.realized_pnl == -10.0

    with pytest.raises(ValueError, match="Symbol mismatch"):
        long.apply_fill(FillEvent("bad", "ETHUSDT", "SELL", 1.0, 100.0))
    with pytest.raises(ValueError, match="Invalid fill side"):
        long.apply_fill(FillEvent("bad", "BTCUSDT", "HOLD", 1.0, 100.0))
    with pytest.raises(ValueError, match="positive"):
        long.apply_fill(FillEvent("bad", "BTCUSDT", "SELL", 0.0, 100.0))


def test_protection_requires_explicit_quantity_and_venue_ack() -> None:
    pending = ProtectionOrder("sl-new", "BTCUSDT", "p1", kind="SL", status="PENDING")
    aggregate = ProtectionAggregate(symbol="BTCUSDT", position_qty=2.0, covered_qty=0.0, stop_loss=pending)
    assert aggregate.compute_coverage() == 0.0
    assert not aggregate.has_full_coverage()
    assert aggregate.replace_sl_atomic(pending) == "WAIT_FOR_ACK"

    acknowledged = ProtectionOrder(
        "sl-ack", "BTCUSDT", "p1", kind="SL", status="ACTIVE", venue_order_id="venue-2", is_active=True
    )
    aggregate.covered_qty = 2.0
    assert aggregate.replace_sl_atomic(acknowledged, "rules-1") == "NEW_SL"
    assert aggregate.compute_coverage() == 100.0
    assert aggregate.has_full_coverage()

    replacement = ProtectionOrder(
        "sl-next", "BTCUSDT", "p1", kind="SL", status="ACTIVE", venue_order_id="venue-3", is_active=True
    )
    assert aggregate.replace_sl_atomic(replacement, "rules-2") == "CANCEL:venue-2"
    flat = ProtectionAggregate(symbol="BTCUSDT", position_qty=0.0)
    assert flat.compute_coverage() == 100.0 and flat.has_full_coverage()


def _atomic_records() -> tuple[AtomicIntent, AtomicOutbox, AtomicOrderAggregate]:
    intent = AtomicIntent("intent-1", "client-1", "BTCUSDT", "BUY", "0.1", "LIMIT")
    outbox = AtomicOutbox("outbox-1", "")
    order = AtomicOrderAggregate("corr-1", "", "BTCUSDT")
    return intent, outbox, order


def test_atomic_persistence_is_stably_idempotent_and_recovers_unknown() -> None:
    store = AtomicPersistence()
    intent, outbox, order = _atomic_records()
    committed, message = store.atomic_create_intent(intent, outbox, order)
    assert committed and message == "COMMITTED:intent-1"
    assert intent.status == PersistStatus.COMMITTED
    assert store.query_by_client_id("client-1") is order
    assert store.mark_unknown("intent-1") == "UNKNOWN:client-1"
    assert store.recover_after_restart() == [intent]
    assert store.mark_unknown("missing") == "NOT_FOUND"
    assert store.query_by_client_id("missing") is None

    duplicate, duplicate_outbox, duplicate_order = _atomic_records()
    duplicate.created_at = "different-runtime-timestamp"
    accepted, duplicate_message = store.atomic_create_intent(duplicate, duplicate_outbox, duplicate_order)
    assert not accepted and duplicate_message == "DUPLICATE:intent-1:UNKNOWN"
    assert store.generate_client_order_id("BTCUSDT").startswith("bd-BTCUSDT-")


def test_lease_is_fail_closed_for_expiry_unknown_mode_and_generation() -> None:
    expired = FencingLease(ttl_seconds=1, acquired_at=time.monotonic() - 2)
    assert not expired.is_valid() and expired.state == LeaseState.EXPIRED
    expired.fence()
    assert expired.state == LeaseState.FENCED
    expired.revoke()
    assert expired.state == LeaseState.REVOKED

    manager = LeaseManager("paper")
    lease = manager.acquire(generation=3)
    assert lease.is_valid() and manager.renew()
    assert manager.is_current_generation(3)
    assert not manager.is_current_generation(2)
    manager.release()
    assert not manager.renew()

    unknown = LeaseManager("unsupported").acquire()
    assert unknown.state == LeaseState.UNKNOWN and not unknown.is_valid()


def test_redis_lease_contention_fences_instead_of_assuming_renewal(monkeypatch) -> None:
    class FakeRedis:
        def __init__(self, **kwargs):
            pass

        def set(self, *args, **kwargs):
            return False

    monkeypatch.setitem(sys.modules, "redis", SimpleNamespace(Redis=FakeRedis))
    lease = LeaseManager("redis").acquire()
    assert lease.state == LeaseState.FENCED


def test_certificate_chain_binds_fields_verifies_and_cascades(tmp_path: Path) -> None:
    chain = CertificateChain(_chain_file=str(tmp_path / "chain.json"))
    g4 = GateCertificate(
        gate="G4",
        subject="build",
        repo_sha="abc",
        lockfile_hash="lock",
        build_hash="build",
        config_hash="config",
        policy_hash="policy",
        artifact_hash="artifact",
        evidence_hash="evidence",
        expires_at=str(time.time() + 60),
        result=GateResult.PASS,
    )
    assert chain.issue(g4, "issuer-a") == (True, "ISSUED:G4")
    assert g4.is_valid()
    assert chain.verify("G4", "verifier-b") == GateResult.PASS
    assert chain.verify("G4", "issuer-a") == GateResult.NOT_VERIFIABLE
    assert chain.verify("missing", "verifier-b") == GateResult.NOT_VERIFIABLE
    original_signature = g4.signature
    g4.artifact_hash = "tampered"
    assert not g4.is_valid()
    g4.artifact_hash = "artifact"
    g4.signature = original_signature

    g5 = GateCertificate(gate="G5", result=GateResult.PASS)
    chain.issue(g5, "issuer-a")
    assert chain.revoke("G4", "evidence withdrawn") == ["G4", "G5"]
    assert chain.verify("G4", "verifier-b") == GateResult.REVOKED
    assert chain.revoke("missing", "none") == []
    assert chain.elapsed_time("G4") >= 0 and chain.elapsed_time("missing") == 0
    assert chain.save_chain()
    assert chain.load_chain()

    payload = json.loads((tmp_path / "chain.json").read_text())
    payload["G4"]["signature"] = "tampered"
    (tmp_path / "chain.json").write_text(json.dumps(payload))
    assert chain.load_chain()
    assert g4.result == GateResult.NOT_VERIFIABLE


def test_certificate_missing_issuer_expiry_and_missing_file_fail_closed(tmp_path: Path) -> None:
    chain = CertificateChain(_chain_file=str(tmp_path / "missing" / "chain.json"))
    assert chain.issue(GateCertificate(gate="G1"), "") == (False, "MISSING_ISSUER_KEY")
    assert not chain.load_chain()
    expired = GateCertificate(gate="G1", expires_at=str(time.time() - 1), signature="x")
    assert not expired.is_valid()
    malformed = GateCertificate(gate="G1", expires_at="not-a-time", signature="x")
    assert not malformed.is_valid()


def test_execution_plan_selection_binding_and_emergency_invariants() -> None:
    engine = ExecutionPlanEngine()
    assert engine.select_algorithm("BTCUSDT", 0) == ExecutionAlgorithm.NOT_EXECUTABLE
    assert engine.select_algorithm("BTCUSDT", 0.0001) == ExecutionAlgorithm.MARKET
    assert engine.select_algorithm("BTCUSDT", 1, spread_bps=2) == ExecutionAlgorithm.AGGRESSIVE_LIMIT
    assert engine.select_algorithm("BTCUSDT", 1, market_impact_bps=20) == ExecutionAlgorithm.TWAP
    assert engine.select_algorithm("BTCUSDT", 1, is_emergency=True) == ExecutionAlgorithm.EMERGENCY

    plan = engine.create_emergency_plan("BTCUSDT", 2.0, "ignored")
    assert len(plan) == 1 and plan[0].slice_hash == plan[0].compute_hash()
    assert engine.validate_emergency_plan(plan, 2.0)
    unsafe = [
        BoundPlanSlice.bind(
            slice_id="u",
            symbol="BTCUSDT",
            side="BUY",
            quantity="3",
            limit_price="0",
            order_type="MARKET",
            time_in_force="IOC",
            reduce_only=False,
        )
    ]
    assert not engine.validate_emergency_plan(unsafe, 2.0)
    assert engine.create_emergency_plan("BTCUSDT", 0, "SELL") == []
    assert engine.create_twap_plan("BTCUSDT", "HOLD", 1) == []
    assert len(engine.create_twap_plan("BTCUSDT", "BUY", 1, n_slices=4)) == 4


def test_double_entry_trade_funding_hash_and_three_source_reconciliation() -> None:
    buy = LedgerTransaction.record_trade("tx1", "BTCUSDT", "BUY", 2, 100, commission=1)
    sell = LedgerTransaction.record_trade("tx2", "BTCUSDT", "SELL", 2, 100)
    funding = LedgerTransaction.record_funding("tx3", "BTCUSDT", -2)
    assert buy.is_balanced() and sell.is_balanced() and funding.is_balanced()
    assert buy.compute_hash() == buy.compute_hash()
    assert LedgerTransaction("empty").is_balanced()

    matched = TripleReconciliationResult(is_matched=True, sources=["exchange", "local", "ledger"])
    assert matched.detect_same_source_fraud() and matched.can_pass()
    assert not TripleReconciliationResult(is_matched=False, sources=["exchange", "local", "ledger"]).can_pass()
    assert not TripleReconciliationResult(is_matched=True, mismatches=["qty"], sources=["e", "l", "d"]).can_pass()
    assert not TripleReconciliationResult(is_matched=True, sources=["same", "same", "same"]).can_pass()


def test_typed_kernel_paths_are_deterministic_and_rule_errors_fail_closed() -> None:
    inputs = KernelInput(symbol="BTCUSDT", market_data={"price": 100}, feature_hash="f", policy_hash="p")

    def entry(_):
        return SimpleNamespace(direction="LONG", confidence=0.8, target_exposure=0.4, reason="signal")

    kernel = TypedStrategyKernel()
    assert kernel.execute(inputs).action == StrategyAction.NO_ACTION
    kernel.register_entry(entry)
    kernel.register_filter(lambda *_: FilterResult.DEGRADE)
    output = kernel.execute(inputs)
    assert output.action == StrategyAction.DEGRADED
    assert output.confidence == 0.4 and output.target_exposure == 0.2
    assert output.output_hash == output.compute_output_hash()

    veto = TypedStrategyKernel()
    veto.register_entry(entry)
    veto.register_filter(lambda *_: FilterResult.VETO)
    assert veto.execute(inputs).action == StrategyAction.VETO

    exiting = TypedStrategyKernel()
    exiting.register_entry(entry)
    exiting.register_exit(lambda _: True)
    assert exiting.execute(inputs).direction == "FLAT"

    for phase in ("entry", "filter", "exit"):
        broken = TypedStrategyKernel()
        if phase != "entry":
            broken.register_entry(entry)
        getattr(broken, f"register_{phase}")(lambda *_: (_ for _ in ()).throw(RuntimeError("boom")))
        assert broken.execute(inputs).action == StrategyAction.NOT_VERIFIABLE

    reset_kernel()
    assert get_typed_strategy_kernel() is get_typed_strategy_kernel()
    reset_kernel()


def test_adaptive_sizing_fails_closed_and_is_monotonic() -> None:
    invalid = compute_adaptive_sizing(SizingInput(equity=float("nan"), available_margin=100))
    assert invalid.leverage == 0 and not invalid.is_safe
    zero = compute_adaptive_sizing(SizingInput(equity=100, available_margin=100, portfolio_risk_pct=0))
    assert zero.reason_vector == ["RISK_BUDGET_ZERO"]

    previous = SizingDecision(leverage=0.001, position_size_pct=0.0001)
    stressed = compute_adaptive_sizing(
        SizingInput(
            equity=100,
            available_margin=1000,
            volatility=1.0,
            capacity_utilization=0.1,
            regime_confidence=0.2,
            funding_rate=0.02,
            liquidation_distance_pct=0.01,
        ),
        previous,
    )
    assert stressed.leverage <= previous.leverage
    assert not stressed.is_safe
    assert any("MONOTONIC_CLAMP" in reason for reason in stressed.reason_vector)


def test_universe_hysteresis_requires_dwell_and_point_in_time_snapshot() -> None:
    universe = UniverseHysteresis(MIN_DWELL_PERIODS=2)
    good = UniverseEntry(
        "BTCUSDT",
        funding_rate=0.001,
        open_interest=100,
        dq_ok=True,
        capacity_score=0.8,
        observed_at="2026-01-01T00:00:00Z",
    )
    universe.observe(good)
    assert not universe.should_promote("BTCUSDT")
    universe.observe(good)
    assert universe.should_promote("BTCUSDT")
    assert universe.executable_symbols() == ["BTCUSDT"]
    assert universe.snapshot_for_backtest("2025-12-31T23:59:59Z") == []
    assert universe.snapshot_for_backtest("2026-01-02T00:00:00Z") == [good]

    bad = UniverseEntry("BTCUSDT", dq_ok=False, capacity_score=0.1, observed_at="2026-01-03T00:00:00Z")
    universe.observe(bad)
    universe.observe(bad)
    assert universe.should_demote("BTCUSDT")
    assert universe.executable_symbols() == []
    assert bad.exclude_reason == "UNIVERSE_DEMOTED"


def test_process_fault_injector_uses_safe_fencing_and_url_boundaries(tmp_path: Path) -> None:
    token = tmp_path / "fencing-token"
    injector = ProcessFaultInjector(ProcessFaultConfig(fencing_token_path=str(token)))
    assert not injector.inject_kill_9().passed
    assert injector.inject_db_crash().invariants_failed == ["DB_PID_REQUIRED"]
    first = injector.inject_dual_instance()
    assert first.passed and token.exists()
    assert not injector.inject_dual_instance().passed
    injector.release_fencing_token()
    assert not token.exists()
    unsafe = injector.inject_network_timeout("file:///etc/passwd")
    assert not unsafe.passed and unsafe.invariants_failed == ["UNSAFE_TESTNET_URL"]
