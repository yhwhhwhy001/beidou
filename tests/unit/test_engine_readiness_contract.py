"""Readiness contracts for the engine's durable-fact boundary."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import ClassVar

import pytest

from beidou_control.plane import ControlAction
from beidou_core.engine import AutonomousEngine, MeanReversionEntry, TrendFollowingEntry
from beidou_core.guard import EnvironmentMode
from beidou_exchange.core.error_taxonomy import ErrorCategory, Result
from beidou_lifecycle.lifecycle import ModuleState
from beidou_safety.execution import OrderIntent, order_intent_binding_hash
from beidou_safety.execution.order_state import OrderEvent
from beidou_safety.protection.engine import ProtectionManager
from beidou_safety.risk.engine import RiskApprovalSignerImpl, RiskApprovalStateMachine
from beidou_shared.types import (
    AccountId,
    AccountRef,
    InstrumentId,
    OrderSide,
    OrderType,
    Price,
    Quantity,
    RiskApprovalId,
    VenueId,
)


class _Store:
    def __init__(self, orders=None, protections=None) -> None:
        self.orders = list(orders or [])
        self.protections = list(protections or [])
        self.saved_order_states: list[tuple[tuple, dict]] = []

    def restore_order_states(self):
        return list(self.orders)

    def restore_protections(self):
        return list(self.protections)

    def save_order_state(self, *args, **kwargs) -> None:
        self.saved_order_states.append((args, kwargs))


class _Protection:
    def __init__(self, positions=None) -> None:
        self.positions = dict(positions or {})

    def all_positions(self):
        return dict(self.positions)

    def is_healthy(self):
        return True


def _engine(*, orders=None, protections=None, outbox_stats=None) -> AutonomousEngine:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._store = _Store(orders, protections)
    engine._outbox = SimpleNamespace(stats=outbox_stats or {"state_counts": {}})
    engine._active_order_ids = {"active-1"}
    engine._owned_order_ids = {"active-1"}
    engine._protection_owner_id = "owner-1"
    engine._protection = _Protection()
    engine._last_account = {"positions": []}
    engine._position_projection = {}
    engine._position_generation = {}
    return engine


def test_durable_facts_fail_closed_without_backend() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._store = None
    engine._outbox = None
    assert engine._durable_fact_status() == (False, "DURABLE_FACT_STORE_UNAVAILABLE", {})


@pytest.mark.parametrize(
    ("account", "expected_ok", "expected_reason", "expected_withdraw"),
    [
        ({"canTrade": True, "canWithdraw": False}, True, "OK", False),
        ({"canTrade": True, "canWithdraw": True}, False, "WITHDRAWAL_PERMISSION_ENABLED", True),
        ({"canTrade": True}, False, "ACCOUNT_PERMISSION_UNKNOWN", True),
    ],
)
def test_venue_account_permissions_are_explicit_and_fail_closed(
    account: dict[str, object], expected_ok: bool, expected_reason: str, expected_withdraw: bool
) -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)

    result = engine._apply_venue_account_permissions(account)

    assert result == (expected_ok, expected_reason)
    assert engine._can_withdraw is expected_withdraw


@pytest.mark.parametrize(
    ("orders", "outbox_stats", "expected"),
    [
        ([{"order_id": "u-1", "status": "UNKNOWN"}], {"state_counts": {}}, "DURABLE_ORDER_UNKNOWN"),
        ([], {"state_counts": {"UNKNOWN": 1}}, "DURABLE_OUTBOX_UNKNOWN"),
        ([{"order_id": "not-tracked", "status": "NEW"}], {"state_counts": {}}, "DURABLE_ACTIVE_ORDER_UNTRACKED"),
    ],
)
def test_durable_facts_block_unknown_or_untracked_orders(orders, outbox_stats, expected: str) -> None:
    engine = _engine(orders=orders, outbox_stats=outbox_stats)
    ok, reason, evidence = engine._durable_fact_status()
    assert ok is False
    assert reason == expected
    assert evidence


def test_durable_unknown_order_is_never_reclassified_without_exchange_evidence() -> None:
    engine = _engine(
        orders=[{"order_id": "u-1", "symbol": "BTCUSDT", "status": "UNKNOWN"}],
        outbox_stats={"state_counts": {}},
    )

    ok, reason, evidence = engine._durable_fact_status()

    assert ok is False
    assert reason == "DURABLE_ORDER_UNKNOWN"
    assert evidence["order_ids"] == ["u-1"]
    assert engine._store.saved_order_states == []


@pytest.mark.asyncio
async def test_unqueryable_restored_order_remains_unknown() -> None:
    class RestoredTracker:
        status = "ACKED"

        def __init__(self) -> None:
            self.events: list[OrderEvent] = []

        def apply(self, event: OrderEvent) -> bool:
            self.events.append(event)
            self.status = event.value
            return True

    async def query_order(*_args, **_kwargs):
        return ({"code": -2013, "msg": "Order does not exist"}, False)

    engine = AutonomousEngine.__new__(AutonomousEngine)
    tracker = RestoredTracker()
    store = _Store()
    failures: list[str] = []
    engine._active_order_ids = {"123"}
    engine._order_symbols = {"123": "BTCUSDT"}
    engine._order_trackers = {"123": tracker}
    engine._store = store
    engine._api_async_safe = query_order
    engine._record_execution_fact_failure_env_guarded = failures.append

    await engine._monitor_orders("BTCUSDT")

    assert tracker.events[-1] is OrderEvent.UNKNOWN
    assert "123" not in engine._active_order_ids
    assert store.saved_order_states[-1][0][6] == "UNKNOWN"
    assert failures == ["ORDER_STATUS_UNKNOWN:123"]


@pytest.mark.asyncio
async def test_unknown_intent_lookup_failure_is_not_requeued() -> None:
    resolutions: list[tuple[str, bool]] = []

    async def failed_lookup(_symbol: str, _client_id: str) -> Result:
        return Result.failure(
            "network timeout",
            category=ErrorCategory.TIMEOUT,
            retryable=True,
            raw={"reason": "timeout"},
        )

    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._adapter = SimpleNamespace(query_order_by_client_id=failed_lookup)
    engine._outbox = SimpleNamespace(
        get_unknown_intents=lambda: [
            {
                "intent_id": "intent-unknown-1",
                "symbol": "BTCUSDT",
                "client_order_id": "beidou-intent-unknown-1",
            }
        ],
        resolve_unknown=lambda intent_id, *, exchange_order_found: resolutions.append(
            (intent_id, exchange_order_found)
        ),
    )

    resolved = await engine._resolve_unknown_outbox_intents()

    assert resolved == 0
    assert resolutions == []


@pytest.mark.asyncio
async def test_unknown_intent_identity_bound_venue_fact_is_acknowledged() -> None:
    resolutions: list[tuple[str, bool]] = []
    persisted: list[tuple[tuple, dict]] = []

    async def found_lookup(_symbol: str, _client_id: str) -> Result:
        return Result.success(
            {
                "orderId": 42,
                "clientOrderId": "beidou-intent-unknown-2",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "type": "MARKET",
                "origQty": "0.01",
                "executedQty": "0.01",
                "avgPrice": "95000",
                "status": "FILLED",
            }
        )

    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._adapter = SimpleNamespace(query_order_by_client_id=found_lookup)
    engine._store = SimpleNamespace(save_order_state=lambda *args, **kwargs: persisted.append((args, kwargs)))
    engine._outbox = SimpleNamespace(
        get_unknown_intents=lambda: [
            {
                "intent_id": "intent-unknown-2",
                "symbol": "BTCUSDT",
                "client_order_id": "beidou-intent-unknown-2",
            }
        ],
        resolve_unknown=lambda intent_id, *, exchange_order_found: resolutions.append(
            (intent_id, exchange_order_found)
        ),
    )

    resolved = await engine._resolve_unknown_outbox_intents()

    assert resolved == 1
    assert resolutions == [("intent-unknown-2", True)]
    assert persisted[0][1] == {
        "order_id": "42",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "order_type": "MARKET",
        "quantity": "0.01",
        "price": None,
        "status": "FILLED",
        "filled_qty": "0.01",
        "avg_price": "95000",
        "client_order_id": "beidou-intent-unknown-2",
    }


@pytest.mark.asyncio
async def test_unknown_intent_order_fact_persistence_failure_stays_unknown() -> None:
    resolutions: list[tuple[str, bool]] = []
    failures: list[str] = []

    async def found_lookup(_symbol: str, _client_id: str) -> Result:
        return Result.success(
            {
                "orderId": 44,
                "clientOrderId": "beidou-intent-unknown-4",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "type": "MARKET",
                "origQty": "0.01",
                "executedQty": "0",
                "status": "NEW",
            }
        )

    def fail_persist(*_args, **_kwargs) -> None:
        raise RuntimeError("injected persistence failure")

    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._adapter = SimpleNamespace(query_order_by_client_id=found_lookup)
    engine._store = SimpleNamespace(save_order_state=fail_persist)
    engine._record_execution_fact_failure_env_guarded = failures.append
    engine._outbox = SimpleNamespace(
        get_unknown_intents=lambda: [
            {
                "intent_id": "intent-unknown-4",
                "symbol": "BTCUSDT",
                "client_order_id": "beidou-intent-unknown-4",
            }
        ],
        resolve_unknown=lambda intent_id, *, exchange_order_found: resolutions.append(
            (intent_id, exchange_order_found)
        ),
    )

    resolved = await engine._resolve_unknown_outbox_intents()

    assert resolved == 0
    assert resolutions == []
    assert failures == ["UNKNOWN_ORDER_FACT_PERSISTENCE_FAILED:intent-unknown-4:RuntimeError"]


@pytest.mark.asyncio
async def test_unknown_intent_mismatched_lookup_identity_stays_unknown() -> None:
    resolutions: list[tuple[str, bool]] = []

    async def mismatched_lookup(_symbol: str, _client_id: str) -> Result:
        return Result.success(
            {
                "orderId": 43,
                "clientOrderId": "another-client-id",
                "symbol": "ETHUSDT",
                "status": "FILLED",
            }
        )

    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._adapter = SimpleNamespace(query_order_by_client_id=mismatched_lookup)
    engine._outbox = SimpleNamespace(
        get_unknown_intents=lambda: [
            {
                "intent_id": "intent-unknown-3",
                "symbol": "BTCUSDT",
                "client_order_id": "beidou-intent-unknown-3",
            }
        ],
        resolve_unknown=lambda intent_id, *, exchange_order_found: resolutions.append(
            (intent_id, exchange_order_found)
        ),
    )

    resolved = await engine._resolve_unknown_outbox_intents()

    assert resolved == 0
    assert resolutions == []


@pytest.mark.asyncio
async def test_algo_failure_preserves_sanitized_exchange_semantics() -> None:
    async def reject_algo(_params: dict) -> Result:
        return Result.failure(
            "Order would immediately trigger.",
            http_status=400,
            category=ErrorCategory.ORDER_REJECTED,
            retryable=False,
            raw={"code": -2021, "msg": "Order would immediately trigger."},
            source="binance_rest",
            correlation_id="corr-1",
        )

    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._adapter = SimpleNamespace(create_algo_order=reject_algo)

    response = await engine._create_algo_order({"symbol": "BTCUSDT"})

    assert response["code"] == -2021
    assert response["msg"] == "Order would immediately trigger."
    assert response["category"] == ErrorCategory.ORDER_REJECTED.value
    assert response["retryable"] is False
    assert response["http_status"] == 400


def test_protection_client_algo_id_is_stable_and_bound_to_exact_command() -> None:
    order = SimpleNamespace(
        protection_id="sl-position-1234567890",
        owner_id="owner-1",
        position_generation=7,
        instrument_id="BTCUSDT",
        side="SELL",
        order_type="STOP_MARKET",
        quantity=SimpleNamespace(amount="0.01"),
        trigger_price=SimpleNamespace(amount="95000"),
    )
    changed_trigger = SimpleNamespace(**{**vars(order), "trigger_price": SimpleNamespace(amount="94900")})

    first = AutonomousEngine._protection_client_algo_id(order)
    repeated = AutonomousEngine._protection_client_algo_id(order)
    changed = AutonomousEngine._protection_client_algo_id(changed_trigger)

    assert first == repeated
    assert first != changed
    assert len(first) <= 36
    assert re.fullmatch(r"[.A-Z:/a-z0-9_-]+", first)


def test_protection_algo_params_use_each_exit_slice_quantity() -> None:
    take_profit = SimpleNamespace(
        protection_id="tp-position-1-0",
        owner_id="owner-1",
        position_generation=7,
        instrument_id="BTCUSDT",
        side="SELL",
        order_type="TAKE_PROFIT_MARKET",
        quantity=SimpleNamespace(amount="0.003"),
        trigger_price=SimpleNamespace(amount="105000.25"),
    )

    params = AutonomousEngine._protection_algo_params(
        take_profit,
        symbol="BTCUSDT",
        side="SELL",
        precision={"quantity": 3, "price": 2},
    )

    assert params == {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "algoType": "CONDITIONAL",
        "type": "TAKE_PROFIT_MARKET",
        "quantity": "0.003",
        "triggerPrice": "105000.25",
        "reduceOnly": "true",
        "workingType": "CONTRACT_PRICE",
        "clientAlgoId": AutonomousEngine._protection_client_algo_id(take_profit),
    }


def test_protection_algo_params_reject_non_finite_quantity() -> None:
    protection = SimpleNamespace(
        protection_id="sl-position-1",
        owner_id="owner-1",
        position_generation=7,
        instrument_id="BTCUSDT",
        side="SELL",
        order_type="STOP_MARKET",
        quantity=SimpleNamespace(amount="NaN"),
        trigger_price=SimpleNamespace(amount="95000"),
    )

    with pytest.raises(ValueError, match="protection quantity"):
        AutonomousEngine._protection_algo_params(
            protection,
            symbol="BTCUSDT",
            side="SELL",
            precision={"quantity": 3, "price": 2},
        )


def test_protection_algo_params_reject_quantity_that_rounds_to_zero() -> None:
    protection = SimpleNamespace(
        protection_id="tp-position-1-0",
        owner_id="owner-1",
        position_generation=7,
        instrument_id="BTCUSDT",
        side="SELL",
        order_type="TAKE_PROFIT_MARKET",
        quantity=SimpleNamespace(amount="0.0004"),
        trigger_price=SimpleNamespace(amount="105000"),
    )

    with pytest.raises(ValueError, match="rounds to zero"):
        AutonomousEngine._protection_algo_params(
            protection,
            symbol="BTCUSDT",
            side="SELL",
            precision={"quantity": 3, "price": 2},
        )


def test_live_durable_facts_block_active_order_without_current_worker_ownership() -> None:
    engine = _engine(orders=[], outbox_stats={"state_counts": {}})
    engine._can_write = True
    engine._active_order_ids = {"venue-1"}
    engine._owned_order_ids = set()

    ok, reason, evidence = engine._durable_fact_status()

    assert ok is False
    assert reason == "DURABLE_ACTIVE_ORDER_OWNER_UNKNOWN"
    assert evidence["order_ids"] == ["venue-1"]


def test_owned_active_order_intersection_never_claims_unowned_venue_order() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._active_order_ids = {"owned-1", "manual-1"}
    engine._owned_order_ids = {"owned-1"}

    assert engine._owned_active_order_ids() == {"owned-1"}
    assert engine._unowned_active_order_ids() == {"manual-1"}


@pytest.mark.parametrize(
    ("mode", "can_simulate", "expected"),
    [
        (EnvironmentMode.TESTNET, False, set()),
        (EnvironmentMode.PAPER, True, {"meanrev_entry_v1"}),
    ],
)
def test_alpha_graph_never_executes_challenger_in_writable_testnet(
    mode: EnvironmentMode, can_simulate: bool, expected: set[str]
) -> None:
    """A challenger is diagnostic/paper evidence, never a Testnet write path."""

    record = SimpleNamespace(definition=SimpleNamespace(factor_id="meanrev_entry_v1"))
    registry = SimpleNamespace(get_active=lambda: [], get_challengers=lambda: [record])
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._env_mode = mode
    engine._can_simulate = can_simulate
    engine._factor_registry = registry
    engine._factor_component_registry = {"meanrev_entry_v1": (MeanReversionEntry, ())}
    engine._entry_ids = {"meanrev_entry_v1"}
    engine._filter_ids = set()
    engine._exit_ids = set()
    engine._factor_predictions = {}

    engine._rebuild_alpha_graph()

    assert set(engine._alpha_graph._components) == expected


def test_alpha_graph_rebuild_uses_factor_ids_for_mutable_active_records() -> None:
    """An approved ACTIVE record must rebuild the graph without hashing the record."""

    record = SimpleNamespace(definition=SimpleNamespace(factor_id="meanrev_entry_v1"))
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._env_mode = EnvironmentMode.TESTNET
    engine._can_simulate = False
    engine._factor_registry = SimpleNamespace(get_active=lambda: [record], get_challengers=lambda: [])
    engine._factor_component_registry = {"meanrev_entry_v1": (MeanReversionEntry, ())}
    engine._entry_ids = {"meanrev_entry_v1"}
    engine._filter_ids = set()
    engine._exit_ids = set()
    engine._factor_predictions = {}

    engine._rebuild_alpha_graph()

    assert set(engine._alpha_graph._components) == {"meanrev_entry_v1"}


def test_alpha_graph_rebuild_is_deterministic_for_registry_order() -> None:
    """Factor registration order must not change executable graph identity."""

    records = [
        SimpleNamespace(definition=SimpleNamespace(factor_id="trend_entry_v1")),
        SimpleNamespace(definition=SimpleNamespace(factor_id="meanrev_entry_v1")),
    ]
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._env_mode = EnvironmentMode.TESTNET
    engine._can_simulate = False
    engine._factor_registry = SimpleNamespace(get_active=lambda: records, get_challengers=lambda: [])
    engine._factor_component_registry = {
        "meanrev_entry_v1": (MeanReversionEntry, ()),
        "trend_entry_v1": (TrendFollowingEntry, ()),
    }
    engine._entry_ids = {"meanrev_entry_v1", "trend_entry_v1"}
    engine._filter_ids = set()
    engine._exit_ids = set()
    engine._factor_predictions = {}

    engine._rebuild_alpha_graph()

    assert list(engine._alpha_graph._components) == ["meanrev_entry_v1", "trend_entry_v1"]


def test_final_send_rejects_order_payload_mutation_after_approval() -> None:
    signer = RiskApprovalSignerImpl(signing_key="unit-intent-binding-key")
    approval_id = RiskApprovalId("approval-intent-binding")
    expires_at = time.time() + 60
    base = OrderIntent(
        intent_id="intent-binding-1",
        account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
        instrument_id=InstrumentId("BTCUSDT"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(amount="0.1"),
        client_order_id="cid-binding-1",
        idempotency_key="idem-binding-1",
        risk_approval_id=str(approval_id),
        risk_proposal_hash="proposal-1",
        risk_account_snapshot_hash="account-1",
        risk_snapshot_hash="risk-1",
        risk_policy_version="policy-1",
        risk_nonce="nonce-binding-1",
        risk_expires_at=expires_at,
    )
    intent_hash = order_intent_binding_hash(base)
    signature = signer.issue_for_approved_risk(
        approval_id,
        risk_approved=True,
        proposal_hash=base.risk_proposal_hash,
        intent_hash=intent_hash,
        account_snapshot_hash=base.risk_account_snapshot_hash,
        risk_snapshot_hash=base.risk_snapshot_hash,
        policy_version=base.risk_policy_version,
        nonce=base.risk_nonce,
        expires_at=expires_at,
    )
    intent = replace(base, risk_approval_signature=signature, risk_intent_hash=intent_hash)
    state_machine = RiskApprovalStateMachine()
    state_machine.approve(
        approval_id,
        nonce=base.risk_nonce,
        risk_snapshot_hash=base.risk_snapshot_hash,
        policy_version=base.risk_policy_version,
    )
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._can_write = True
    engine._state_backend_supported = True
    engine._risk_sm = state_machine
    engine._approval = signer

    # Preflight is repeatable and does not consume the one-shot approval.
    assert asyncio.run(engine._verify_intent_at_send(intent)) is True
    assert asyncio.run(engine._verify_intent_at_send(intent)) is True
    # The final send boundary consumes both signer nonce and approval state.
    assert asyncio.run(engine._verify_intent_at_send(intent, consume_nonce=True)) is True
    assert state_machine.is_consumed(approval_id)
    assert asyncio.run(engine._verify_intent_at_send(intent, consume_nonce=True)) is False
    tampered = replace(intent, quantity=Quantity(amount="0.2"))
    assert asyncio.run(engine._verify_intent_at_send(tampered)) is False


def test_final_send_blocks_risk_increase_without_signed_policy_but_allows_governed_close() -> None:
    signer = RiskApprovalSignerImpl(signing_key="unit-policy-gate-key")
    approval_id = RiskApprovalId("approval-policy-gate")
    expires_at = time.time() + 60
    base = OrderIntent(
        intent_id="intent-policy-gate",
        account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
        instrument_id=InstrumentId("BTCUSDT"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(amount="0.1"),
        client_order_id="cid-policy-gate",
        idempotency_key="idem-policy-gate",
        risk_approval_id=str(approval_id),
        risk_proposal_hash="proposal-policy-gate",
        risk_account_snapshot_hash="account-policy-gate",
        risk_snapshot_hash="risk-policy-gate",
        risk_policy_version="policy-1",
        risk_nonce="nonce-policy-gate",
        risk_expires_at=expires_at,
    )
    intent_hash = order_intent_binding_hash(base)
    signature = signer.issue_for_approved_risk(
        approval_id,
        risk_approved=True,
        proposal_hash=base.risk_proposal_hash,
        intent_hash=intent_hash,
        account_snapshot_hash=base.risk_account_snapshot_hash,
        risk_snapshot_hash=base.risk_snapshot_hash,
        policy_version=base.risk_policy_version,
        nonce=base.risk_nonce,
        expires_at=expires_at,
    )
    intent = replace(base, risk_approval_signature=signature, risk_intent_hash=intent_hash)
    state_machine = RiskApprovalStateMachine()
    state_machine.approve(approval_id)
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._can_write = True
    engine._state_backend_supported = True
    engine._policy_error = "SIGNED_POLICY_UNAVAILABLE"
    engine._risk_sm = state_machine
    engine._approval = signer

    assert asyncio.run(engine._verify_intent_at_send(intent)) is False

    close = replace(intent, risk_approval_id="RISK_EXEMPT_CLOSE", reduce_only=True)
    assert asyncio.run(engine._verify_intent_at_send(close)) is True


def test_execution_slices_cannot_change_signed_order_semantics() -> None:
    intent = OrderIntent(
        intent_id="intent-slices-1",
        account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
        instrument_id=InstrumentId("BTCUSDT"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(amount="1.0"),
        client_order_id="cid-slices-1",
    )
    valid = [("0.4", None, "MARKET", "GTC", "cid-slices-1-1"), ("0.6", None, "MARKET", "GTC", "cid-slices-1-2")]
    assert AutonomousEngine._validate_slices_against_intent(
        intent, valid, order_symbol="BTCUSDT", side="BUY", client_id="cid-slices-1"
    ) == (True, "OK")

    # MARKET → LIMIT 降级是允许的（更保守），但 LIMIT → MARKET 反向升级应被阻止
    limit_intent = OrderIntent(
        intent_id="intent-slices-2",
        account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
        instrument_id=InstrumentId("BTCUSDT"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(amount="1.0"),
        price=Price(amount="50000"),
        client_order_id="cid-slices-2",
    )
    bad_type = [
        ("0.5", "50000", "MARKET", "GTC", "cid-slices-2-1"),
        ("0.5", "50000", "MARKET", "GTC", "cid-slices-2-2"),
    ]
    ok, reason = AutonomousEngine._validate_slices_against_intent(
        limit_intent, bad_type, order_symbol="BTCUSDT", side="BUY", client_id="cid-slices-2"
    )
    assert ok is False
    assert reason == "ORDER_TYPE_MISMATCH"


def test_protection_coverage_requires_exact_side_generation_and_quantity() -> None:
    engine = _engine()
    engine._position_projection = {"BTCUSDT": {"position_generation": 2}}
    engine._position_generation = {"BTCUSDT": 2}
    venue_positions = [{"symbol": "BTCUSDT", "positionAmt": "2"}]
    valid = [
        {
            "symbol": "BTCUSDT",
            "side": "SELL",
            "position_generation": 2,
            "quantity": "2",
            "order_type": "STOP_MARKET",
            "owner_id": "owner-1",
            "exchange_order_id": "sl-1",
            "status": "ACTIVE",
        }
    ]
    assert engine._assess_protection_coverage(venue_positions, valid) == (True, {"unprotected_symbols": []})

    bad_side = [dict(valid[0], side="BUY")]
    ok, evidence = engine._assess_protection_coverage(venue_positions, bad_side)
    assert ok is False
    assert evidence["unprotected_symbols"][0]["reason"] == "STOP_LOSS_QUANTITY_UNCOVERED"

    bad_generation = [dict(valid[0], position_generation=1)]
    ok, evidence = engine._assess_protection_coverage(venue_positions, bad_generation)
    assert ok is False
    assert evidence["unprotected_symbols"]

    invalid_quantity = [dict(valid[0], quantity="not-a-number")]
    ok, evidence = engine._assess_protection_coverage(venue_positions, invalid_quantity)
    assert ok is False
    assert evidence["unprotected_symbols"][0]["reason"] == "STOP_LOSS_QUANTITY_UNCOVERED"


def test_pending_or_unacked_protection_never_counts_as_coverage() -> None:
    engine = _engine()
    engine._position_projection = {"BTCUSDT": {"position_generation": 1}}
    engine._position_generation = {"BTCUSDT": 1}
    position = [{"symbol": "BTCUSDT", "positionAmt": "0.01", "entryPrice": "50000"}]
    pending = [
        {
            "symbol": "BTCUSDT",
            "side": "SELL",
            "position_generation": 1,
            "quantity": "0.01",
            "order_type": "STOP_MARKET",
            "owner_id": "owner-1",
            "exchange_order_id": "",
            "status": "PENDING",
        }
    ]
    ok, evidence = engine._assess_protection_coverage(position, pending)
    assert ok is False
    assert evidence["unprotected_symbols"]


def test_small_position_is_not_exempt_from_protection() -> None:
    engine = _engine()
    engine._position_projection = {"ETHUSDT": {"position_generation": 1}}
    engine._position_generation = {"ETHUSDT": 1}
    position = [{"symbol": "ETHUSDT", "positionAmt": "0.01", "entryPrice": "2000"}]
    ok, evidence = engine._assess_protection_coverage(position, [])
    assert ok is False
    assert evidence["unprotected_symbols"][0]["reason"] == "STOP_LOSS_QUANTITY_UNCOVERED"


def test_protection_coverage_rejects_orphans_and_local_position_without_venue_fact() -> None:
    engine = _engine()
    local = SimpleNamespace(instrument_id="ETHUSDT")
    engine._protection = _Protection({"p-1": local})
    protections = [
        {
            "symbol": "SOLUSDT",
            "side": "SELL",
            "position_generation": 1,
            "quantity": "1",
            "order_type": "STOP_MARKET",
        }
    ]
    ok, evidence = engine._assess_protection_coverage([], protections, engine._protection.all_positions())
    assert ok is False
    reasons = {item["reason"] for item in evidence["unprotected_symbols"]}
    assert reasons == {"LOCAL_POSITION_WITHOUT_VENUE_FACT", "ORPHAN_PROTECTION_WITHOUT_VENUE_POSITION"}


def test_startup_protection_recovery_checks_venue_inventory_for_local_projection() -> None:
    """A local ACTIVE row cannot certify a venue protection that disappeared."""

    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._control = SimpleNamespace(get_status=lambda: ControlAction.RESUME)
    engine._last_account = {"positions": [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}]}
    engine._protection = _Protection({"pos-1": SimpleNamespace(instrument_id="BTCUSDT")})
    engine._active_algo_ids = {"pos-1": {"sl-1"}}
    engine._adapter = SimpleNamespace(reset_circuit_breaker=lambda: None)
    blocked: list[str] = []
    engine._block_unowned_protection_orders = lambda ids: blocked.extend(ids)

    async def missing_inventory() -> list[dict[str, object]]:
        return []

    engine._get_open_algo_inventory = missing_inventory
    asyncio.run(engine._ensure_exchange_position_protections())

    assert blocked == ["OWNED_PROTECTION_MISSING:sl-1"]


def test_startup_unprotected_position_never_uses_raw_algo_recovery_bypass() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._control = SimpleNamespace(get_status=lambda: ControlAction.RESUME)
    engine._last_account = {"positions": [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}]}
    engine._protection = ProtectionManager()
    engine._active_algo_ids = {}
    engine._protection_owner_id = "owner-1"
    engine._store = _Store()
    engine._adapter = SimpleNamespace(reset_circuit_breaker=lambda: None)
    blocked: list[str] = []
    engine._block_unowned_protection_orders = lambda ids: blocked.extend(ids)

    async def empty_inventory() -> list[dict[str, object]]:
        return []

    engine._get_open_algo_inventory = empty_inventory
    asyncio.run(engine._ensure_exchange_position_protections())

    assert blocked == ["UNPROTECTED_POSITION_RECOVERY_REQUIRED:BTCUSDT"]
    assert engine._protection.all_positions() == {}


def test_startup_hydrates_ack_backed_protection_without_duplicate_creation() -> None:
    """Durable ACTIVE rows rebuild local state instead of creating a second pair."""

    rows = [
        {
            "status": "ACTIVE",
            "owner_id": "owner-1",
            "protection_id": "sl-pos-1",
            "position_id": "pos-1",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "order_type": "STOP_MARKET",
            "quantity": "1",
            "trigger_price": "90",
            "stop_type": "ATR_BASED",
            "take_profit_type": None,
            "position_generation": 1,
            "session_id": "session-1",
            "exchange_order_id": "sl-1",
        },
        {
            "status": "ACTIVE",
            "owner_id": "owner-1",
            "protection_id": "tp-pos-1",
            "position_id": "pos-1",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "order_type": "TAKE_PROFIT_MARKET",
            "quantity": "1",
            "trigger_price": "120",
            "stop_type": None,
            "take_profit_type": "FIXED_RR",
            "position_generation": 1,
            "session_id": "session-1",
            "exchange_order_id": "tp-1",
        },
    ]
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._store = _Store(protections=rows)
    engine._protection = ProtectionManager()
    engine._protection_owner_id = "owner-1"
    engine._position_projection = {}
    engine._position_entry_times = {}
    blocked: list[str] = []
    engine._block_unowned_protection_orders = lambda ids: blocked.extend(ids)

    inventory = [
        {
            "algoId": "sl-1",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "orderType": "STOP_MARKET",
            "quantity": "1",
            "triggerPrice": "90",
            "reduceOnly": True,
        },
        {
            "algoId": "tp-1",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "orderType": "TAKE_PROFIT_MARKET",
            "quantity": "1",
            "triggerPrice": "120",
            "reduceOnly": True,
        },
    ]

    assert engine._restore_durable_protection_projection(
        {"positions": [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}]},
        inventory,
    )
    assert blocked == []
    restored = engine._protection.all_positions()
    assert set(restored) == {"pos-1"}
    assert restored["pos-1"].stop_loss.exchange_order_id == "sl-1"
    assert restored["pos-1"].take_profits[0].exchange_order_id == "tp-1"


def test_startup_durable_protection_hydration_blocks_without_venue_inventory() -> None:
    rows = [
        {
            "status": "ACTIVE",
            "owner_id": "owner-1",
            "protection_id": "sl-pos-1",
            "position_id": "pos-1",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "order_type": "STOP_MARKET",
            "quantity": "1",
            "trigger_price": "90",
            "stop_type": "ATR_BASED",
            "position_generation": 1,
            "session_id": "session-1",
            "exchange_order_id": "sl-1",
        }
    ]
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._store = _Store(protections=rows)
    engine._protection = ProtectionManager()
    engine._protection_owner_id = "owner-1"
    engine._position_projection = {}
    engine._position_entry_times = {}
    blocked: list[str] = []
    engine._block_unowned_protection_orders = lambda ids: blocked.extend(ids)

    assert not engine._restore_durable_protection_projection(
        {"positions": [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}]},
        None,
    )
    assert blocked == ["OPEN_ALGO_ORDERS_UNKNOWN"]


def test_protection_cleanup_requires_fresh_matched_reconciliation() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._control = SimpleNamespace(get_status=lambda: ControlAction.RESUME)
    engine._last_reconciliation_result = SimpleNamespace(matched=False, checked_at=datetime.now(timezone.utc))
    calls: list[str] = []

    async def unexpected_inventory_call():
        calls.append("inventory")
        return []

    engine._get_open_algo_inventory = unexpected_inventory_call
    asyncio.run(engine._cleanup_excess_orders())
    assert calls == []


def test_protection_inventory_rejects_semantic_mismatch() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._can_write = True
    engine._protection_owner_id = "owner-1"
    engine._store = _Store(
        protections=[
            {
                "status": "ACTIVE",
                "owner_id": "owner-1",
                "exchange_order_id": "sl-1",
                "symbol": "BTCUSDT",
                "side": "SELL",
                "order_type": "STOP_MARKET",
                "quantity": "1",
                "trigger_price": "90",
            }
        ]
    )

    issues = engine._protection_inventory_semantic_issues(
        [
            {
                "algoId": "sl-1",
                "symbol": "BTCUSDT",
                "side": "SELL",
                "orderType": "STOP_MARKET",
                "quantity": "2",
                "triggerPrice": "90",
                "reduceOnly": False,
            }
        ]
    )

    assert issues == ["PROTECTION_QUANTITY_MISMATCH:sl-1", "PROTECTION_REDUCE_ONLY_UNPROVEN:sl-1"]


def test_engine_liveness_and_trading_readiness_require_fresh_reconciliation() -> None:
    engine = _engine()
    engine._running = True
    engine._last_realtime = time.time()
    engine._control = SimpleNamespace(get_status=lambda: ControlAction.RESUME)
    engine._last_reconciliation_result = SimpleNamespace(matched=True)
    assert engine._check_liveness().value == "HEALTHY"
    engine._last_realtime = time.time() - 20
    assert engine._check_liveness().value == "UNHEALTHY"
    engine._last_realtime = time.time()
    engine._last_reconciliation_result = SimpleNamespace(matched=False)
    assert engine._check_liveness().value == "DEGRADED"

    engine._state_backend_supported = False
    assert engine._check_trading_ready() == (False, "STATE_BACKEND_UNSUPPORTED")


def test_engine_ready_gate_requires_all_runtime_facts() -> None:
    engine = _engine()
    engine._state_backend_supported = True
    engine._lifecycle = SimpleNamespace(state=ModuleState.ACTIVE)
    engine._feed = SimpleNamespace(is_healthy=lambda: True)
    engine._control = SimpleNamespace(get_status=lambda: ControlAction.RESUME)
    engine._protection_owner_unknown = False
    engine._last_reconciliation_result = SimpleNamespace(matched=True)
    engine._running = True
    engine._last_realtime = time.time()
    engine._can_write = True
    engine._user_stream_runtime = {
        "status": "HEALTHY",
        "last_event_mono": time.monotonic(),
        "listen_key_active": True,
    }
    engine._user_stream_projector = SimpleNamespace(sequencer=SimpleNamespace(status=SimpleNamespace(value="HEALTHY")))
    engine._event_stream_facts = SimpleNamespace(complete=True)
    assert engine._check_ready() is True
    assert engine._check_trading_ready() == (True, "READY")

    engine._can_write = False
    assert engine._check_trading_ready() == (False, "TRADING_WRITE_DISABLED")
    engine._control = SimpleNamespace(get_status=lambda: ControlAction.NO_NEW_RISK)
    assert engine._check_trading_ready() == (False, "CONTROL_NO_NEW_RISK")


def test_writable_readiness_requires_live_user_stream_event_fact() -> None:
    engine = _engine()
    engine._state_backend_supported = True
    engine._lifecycle = SimpleNamespace(state=ModuleState.ACTIVE)
    engine._feed = SimpleNamespace(is_healthy=lambda: True)
    engine._control = SimpleNamespace(get_status=lambda: ControlAction.RESUME)
    engine._protection_owner_unknown = False
    engine._last_reconciliation_result = SimpleNamespace(matched=True)
    engine._running = True
    engine._last_realtime = time.time()
    engine._can_write = True
    engine._user_stream_runtime = {"status": "NOT_STARTED", "listen_key_active": False}

    assert engine._check_ready() is False
    assert engine._check_trading_ready() == (False, "RUNTIME_NOT_READY")


def test_testnet_user_stream_gap_remains_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEIDOU_ENV", "testnet")
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._can_write = True
    engine._user_stream_runtime = {
        "status": "CONNECTED",
        "last_event_mono": time.monotonic(),
        "listen_key_active": True,
    }
    engine._user_stream_projector = SimpleNamespace(sequencer=SimpleNamespace(status=SimpleNamespace(value="GAP")))
    engine._event_stream_facts = SimpleNamespace(complete=False)

    ready, evidence = engine._user_stream_readiness()

    assert ready is False
    assert evidence["projector_status"] == "GAP"
    assert evidence["projection_complete"] is False


def test_user_stream_runtime_starts_and_stops_without_rest_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    import beidou_exchange.binance_usdm as binance_usdm

    class FakeWebSocket:
        instances: ClassVar[list[FakeWebSocket]] = []

        def __init__(self, base_url: str) -> None:
            self.base_url = base_url
            self.callback = None
            self.closed = False
            self._stopped = asyncio.Event()
            self.__class__.instances.append(self)

        def on_state_change(self, callback) -> None:
            self.state_callback = callback

        async def subscribe(self, _stream: str, callback) -> None:
            self.callback = callback

        async def run(self) -> None:
            await self._stopped.wait()

        async def close(self) -> None:
            self.closed = True
            self._stopped.set()

    class Adapter:
        async def create_user_listen_key(self):
            return Result.success({"listenKey": "secret-listen-key"})

        async def keepalive_user_listen_key(self, _listen_key: str):
            return Result.success({})

    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._can_write = True
    engine._env_mode = SimpleNamespace(value="testnet")
    engine._adapter = Adapter()
    engine._control = SimpleNamespace(
        get_status=lambda: ControlAction.NO_NEW_RISK,
        execute_action=lambda _action: None,
    )
    engine._alerts = SimpleNamespace(send_incident=lambda *_args, **_kwargs: None)
    engine._record_execution_fact_failure = lambda _reason: None
    engine.ingest_user_order_update = lambda _update: True
    engine._user_stream_runtime = {"status": "NOT_STARTED", "listen_key_active": False}

    monkeypatch.setattr(binance_usdm, "BinanceUsdmWebSocketClient", FakeWebSocket)

    async def scenario() -> None:
        assert await engine._start_user_stream() is True
        assert engine._user_stream_runtime["status"] == "CONNECTED"
        assert engine._user_stream_runtime["listen_key_active"] is True
        await FakeWebSocket.instances[0].callback(
            "listen-key-1",
            {
                "e": "ORDER_TRADE_UPDATE",
                "E": 1000,
                "T": 1000,
                "u": 1,
                "o": {
                    "i": "1",
                    "c": "cid-1",
                    "s": "BTCUSDT",
                    "S": "BUY",
                    "o": "MARKET",
                    "X": "NEW",
                    "x": "NEW",
                    "q": "0.1",
                    "z": "0",
                    "l": "0",
                    "L": "0",
                    "ap": "0",
                    "n": "0",
                    "N": "USDT",
                    "rp": "0",
                    "t": "-1",
                },
            },
        )
        assert engine._user_stream_runtime["status"] == "HEALTHY"
        assert engine._user_stream_runtime["last_event_mono"] is not None
        await engine._stop_user_stream()
        assert FakeWebSocket.instances[0].closed is True
        assert engine._user_stream_runtime["status"] == "STOPPED"

    asyncio.run(scenario())


def test_algo_update_informational_on_testnet(monkeypatch: pytest.MonkeyPatch) -> None:
    """demo 共享账户其他用户的算法单推送 ALGO_UPDATE —— testnet 按信息性
    事件处理保持流健康；live 保持 fault（自己的算法单状态必须复核）。

    镜像 15:52 现场：FILLED 达成后 ALGO_UPDATE 触发 fault → NO_NEW_RISK。
    """
    import beidou_exchange.binance_usdm as binance_usdm

    class FakeWebSocket:
        instances: ClassVar[list[FakeWebSocket]] = []

        def __init__(self, base_url: str) -> None:
            self.base_url = base_url
            self.callback = None
            self.closed = False
            self._stopped = asyncio.Event()
            self.__class__.instances.append(self)

        def on_state_change(self, callback) -> None:
            self.state_callback = callback

        async def subscribe(self, _stream: str, callback) -> None:
            self.callback = callback

        async def run(self) -> None:
            await self._stopped.wait()

        async def close(self) -> None:
            self.closed = True
            self._stopped.set()

    class Adapter:
        async def create_user_listen_key(self):
            return Result.success({"listenKey": "secret-listen-key"})

        async def keepalive_user_listen_key(self, _listen_key: str):
            return Result.success({})

    def _make_engine(env_mode: str) -> AutonomousEngine:
        engine = AutonomousEngine.__new__(AutonomousEngine)
        engine._can_write = True
        engine._env_mode = SimpleNamespace(value=env_mode)
        engine._adapter = Adapter()
        engine._control = SimpleNamespace(
            get_status=lambda: ControlAction.NO_NEW_RISK,
            execute_action=lambda _action: None,
        )
        engine._alerts = SimpleNamespace(send_incident=lambda *_args, **_kwargs: None)
        engine._record_execution_fact_failure = lambda _reason: None
        engine._record_execution_fact_failure_env_guarded = lambda _reason: None
        engine.ingest_user_order_update = lambda _update: True
        engine._user_stream_runtime = {"status": "NOT_STARTED", "listen_key_active": False}
        return engine

    monkeypatch.setattr(binance_usdm, "BinanceUsdmWebSocketClient", FakeWebSocket)

    async def scenario() -> None:
        # testnet：ALGO_UPDATE 不 fault，流保持健康
        testnet_engine = _make_engine("testnet")
        assert await testnet_engine._start_user_stream() is True
        await FakeWebSocket.instances[-1].callback("listen-key-1", {"e": "ALGO_UPDATE", "E": 1000})
        assert testnet_engine._user_stream_runtime["status"] in ("HEALTHY", "CONNECTED")
        assert "REVALIDATION" not in str(testnet_engine._user_stream_runtime.get("last_error", ""))
        await testnet_engine._stop_user_stream()

        # live：ALGO_UPDATE 仍 fault（算法单状态必须复核）
        live_engine = _make_engine("live")
        assert await live_engine._start_user_stream() is True
        await FakeWebSocket.instances[-1].callback("listen-key-1", {"e": "ALGO_UPDATE", "E": 1000})
        assert live_engine._user_stream_runtime["status"] == "DEGRADED"
        assert "ALGO_UPDATE_REVALIDATION_REQUIRED" in str(live_engine._user_stream_runtime.get("last_error", ""))

    asyncio.run(scenario())


def test_writable_stopped_engine_cannot_claim_health_or_readiness() -> None:
    engine = _engine()
    engine._state_backend_supported = True
    engine._lifecycle = SimpleNamespace(state=ModuleState.ACTIVE)
    engine._feed = SimpleNamespace(is_healthy=lambda: True)
    engine._control = SimpleNamespace(get_status=lambda: ControlAction.RESUME)
    engine._protection_owner_unknown = False
    engine._last_reconciliation_result = SimpleNamespace(matched=True)
    engine._can_write = True
    engine._running = False
    engine._last_realtime = time.time()

    assert engine._check_liveness().value == "UNHEALTHY"
    assert engine._check_ready() is False
    assert engine._check_trading_ready() == (False, "RUNTIME_NOT_READY")


def test_realtime_liveness_uses_monotonic_age_not_wall_clock() -> None:
    engine = _engine()
    engine._running = True
    engine._control = SimpleNamespace(get_status=lambda: ControlAction.RESUME)
    engine._last_reconciliation_result = SimpleNamespace(matched=True)
    engine._last_realtime = time.time()  # wall clock appears fresh
    engine._last_realtime_mono = time.monotonic() - 20.0

    assert engine._check_liveness().value == "UNHEALTHY"
