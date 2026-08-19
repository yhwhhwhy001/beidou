from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from beidou_control.truth import TradingEligibility
from beidou_core.engine import AutonomousEngine
from beidou_exchange.binance_usdm.adapter import BinanceUsdmAdapter
from beidou_exchange.core.protocol import OrderResponse
from beidou_exchange.core.rule_snapshot import InstrumentRuleSnapshot
from beidou_safety.execution import OrderIntent
from beidou_safety.execution.command_aggregate import (
    ChildCommandState,
    ExecutionChildCommand,
    ParentExecutionAggregate,
    ParentExecutionState,
    TargetDeltaPlan,
)
from beidou_safety.execution.intent import IntentOutbox
from beidou_shared.types import (
    AccountId,
    AccountRef,
    InstrumentId,
    OrderSide,
    OrderStatus,
    OrderType,
    Quantity,
    VenueId,
)


def _child(sequence: int, *, side: str = "BUY", quantity: str = "1") -> ExecutionChildCommand:
    return ExecutionChildCommand.create(
        parent_intent_id="intent-1",
        sequence=sequence,
        symbol="BTCUSDT",
        side=side,
        quantity=quantity,
        order_type="LIMIT",
        time_in_force="GTC",
        client_order_id=f"beidou-intent-1-{sequence}",
        limit_price="50000",
        rule_snapshot_hash="rule-1",
    )


def test_parent_is_not_acked_until_every_child_has_venue_ack() -> None:
    aggregate = ParentExecutionAggregate.create("intent-1", [_child(0), _child(1)])

    first = aggregate.transition_child(0, ChildCommandState.SENDING, event_id="send-0")
    first = first.transition_child(0, ChildCommandState.ACKED, event_id="ack-0", exchange_order_id="venue-0")
    assert first.state is ParentExecutionState.IN_FLIGHT

    complete = first.transition_child(1, ChildCommandState.SENDING, event_id="send-1")
    complete = complete.transition_child(1, ChildCommandState.ACKED, event_id="ack-1", exchange_order_id="venue-1")
    assert complete.state is ParentExecutionState.ACKED


def test_unknown_child_keeps_parent_unknown_and_requires_query_identity() -> None:
    aggregate = ParentExecutionAggregate.create("intent-1", [_child(0), _child(1)])
    aggregate = aggregate.transition_child(0, ChildCommandState.SENDING, event_id="send-0")
    aggregate = aggregate.transition_child(0, ChildCommandState.ACKED, event_id="ack-0", exchange_order_id="venue-0")
    aggregate = aggregate.transition_child(1, ChildCommandState.SENDING, event_id="send-1")
    aggregate = aggregate.transition_child(1, ChildCommandState.UNKNOWN, event_id="unknown-1")

    assert aggregate.state is ParentExecutionState.UNKNOWN
    assert aggregate.children[1].recovery_action == "QUERY_BY_CLIENT_ID:beidou-intent-1-1"


def test_duplicate_partial_fill_event_is_idempotent_and_terminal_cannot_regress() -> None:
    aggregate = ParentExecutionAggregate.create("intent-1", [_child(0, quantity="2")])
    aggregate = aggregate.transition_child(0, ChildCommandState.SENDING, event_id="send")
    aggregate = aggregate.transition_child(0, ChildCommandState.ACKED, event_id="ack", exchange_order_id="venue")
    aggregate = aggregate.transition_child(
        0,
        ChildCommandState.PARTIALLY_FILLED,
        event_id="fill-1",
        cumulative_filled_quantity="1.25",
    )
    duplicate = aggregate.transition_child(
        0,
        ChildCommandState.PARTIALLY_FILLED,
        event_id="fill-1",
        cumulative_filled_quantity="1.25",
    )
    assert duplicate == aggregate
    assert duplicate.children[0].filled_quantity == Decimal("1.25")

    filled = duplicate.transition_child(
        0,
        ChildCommandState.FILLED,
        event_id="fill-2",
        cumulative_filled_quantity="2",
    )
    with pytest.raises(ValueError, match="TERMINAL_CHILD_STATE"):
        filled.transition_child(0, ChildCommandState.ACKED, event_id="late-ack")


@pytest.mark.parametrize(
    ("target", "current", "inflight", "expected_side", "expected_qty"),
    [
        ("5", "0", "0", "BUY", "5"),
        ("5", "2", "0", "BUY", "3"),
        ("5", "2", "1", "BUY", "2"),
        ("-4", "-1", "0", "SELL", "3"),
        ("-4", "2", "0", "SELL", "6"),
        ("0", "2", "0", "SELL", "2"),
        ("2", "2", "0", None, "0"),
    ],
)
def test_target_delta_uses_current_and_inflight_signed_exposure(
    target: str,
    current: str,
    inflight: str,
    expected_side: str | None,
    expected_qty: str,
) -> None:
    plan = TargetDeltaPlan.compute(
        symbol="BTCUSDT",
        target_quantity=target,
        current_quantity=current,
        inflight_quantity=inflight,
    )

    assert plan.side == expected_side
    assert plan.order_quantity == Decimal(expected_qty)
    assert plan.delta_quantity == Decimal(target) - Decimal(current) - Decimal(inflight)


def test_child_economic_hash_is_deterministic_and_detects_mutation() -> None:
    first = _child(0)
    same = _child(0)
    changed = ExecutionChildCommand.create(
        parent_intent_id="intent-1",
        sequence=0,
        symbol="BTCUSDT",
        side="BUY",
        quantity="1.1",
        order_type="LIMIT",
        time_in_force="GTC",
        client_order_id="beidou-intent-1-0",
        limit_price="50000",
        rule_snapshot_hash="rule-1",
    )

    assert first.command_hash == same.command_hash
    assert first.command_hash != changed.command_hash


def test_sqlite_plan_survives_restart_and_sending_child_becomes_unknown(tmp_path) -> None:
    db_path = str(tmp_path / "execution-plan.db")
    intent = OrderIntent(
        intent_id="intent-1",
        account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
        instrument_id=InstrumentId("BTCUSDT"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(amount="2"),
        client_order_id="beidou-intent-1",
        idempotency_key="idem-intent-1",
        reduce_only=True,
    )
    first = IntentOutbox(db_path)
    first.commit(intent)
    assert first.claim("engine-test") is not None
    first.persist_execution_plan(intent.intent_id, [_child(0), _child(1)])
    first.transition_execution_child(
        intent.intent_id,
        0,
        ChildCommandState.SENDING,
        event_id="send-0",
    )
    first.transition_execution_child(
        intent.intent_id,
        0,
        ChildCommandState.ACKED,
        event_id="ack-0",
        exchange_order_id="venue-0",
    )
    first.transition_execution_child(
        intent.intent_id,
        1,
        ChildCommandState.SENDING,
        event_id="send-1",
    )

    restarted_outbox = IntentOutbox(db_path)
    restored = restarted_outbox.restore_execution_plan(intent.intent_id)
    assert restored is not None
    assert restored.children[0].state is ChildCommandState.ACKED
    assert restored.children[1].state is ChildCommandState.UNKNOWN
    assert restored.state is ParentExecutionState.UNKNOWN
    # Both the venue-ACKed order and the ambiguous send remain outstanding
    # until authoritative fills/cancel facts reduce their remainder.
    assert restored.signed_remaining_quantity == Decimal("2")
    assert restarted_outbox.inflight_signed_quantity("BTCUSDT") == Decimal("2")


def test_persisted_plan_is_idempotent_but_rejects_economic_mutation(tmp_path) -> None:
    db_path = str(tmp_path / "execution-plan-conflict.db")
    outbox = IntentOutbox(db_path)
    intent = OrderIntent(
        intent_id="intent-1",
        account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
        instrument_id=InstrumentId("BTCUSDT"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(amount="2"),
        idempotency_key="idem-intent-1",
        reduce_only=True,
    )
    outbox.commit(intent)
    with pytest.raises(ValueError, match="EXECUTION_PLAN_PARENT_NOT_CLAIMED"):
        outbox.persist_execution_plan(intent.intent_id, [_child(0), _child(1)])
    assert outbox.claim("engine-test") is not None
    outbox.persist_execution_plan(intent.intent_id, [_child(0), _child(1)])
    outbox.persist_execution_plan(intent.intent_id, [_child(0), _child(1)])

    with pytest.raises(ValueError, match="EXECUTION_PLAN_CONFLICT"):
        outbox.persist_execution_plan(intent.intent_id, [_child(0, quantity="1.1"), _child(1)])


def test_user_stream_partial_and_fill_update_same_durable_child_idempotently(tmp_path) -> None:
    db_path = str(tmp_path / "execution-user-events.db")
    outbox = IntentOutbox(db_path)
    intent = OrderIntent(
        intent_id="intent-1",
        account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
        instrument_id=InstrumentId("BTCUSDT"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(amount="1"),
        idempotency_key="idem-intent-1",
        reduce_only=True,
    )
    outbox.commit(intent)
    assert outbox.claim("engine-test") is not None
    outbox.persist_execution_plan(intent.intent_id, [_child(0)])
    outbox.transition_execution_child(intent.intent_id, 0, ChildCommandState.SENDING, event_id="send-0")

    def parsed_update(
        *,
        status: str,
        cumulative: str,
        event_id: int,
        client_order_id: str = "beidou-intent-1-0",
    ):
        result = BinanceUsdmAdapter.parse_user_order_update(
            {
                "e": "ORDER_TRADE_UPDATE",
                "E": 1_000 + event_id,
                "T": 999 + event_id,
                "u": event_id,
                "o": {
                    "i": 987,
                    "I": event_id,
                    "c": client_order_id,
                    "s": "BTCUSDT",
                    "S": "BUY",
                    "o": "LIMIT",
                    "X": status,
                    "x": "TRADE",
                    "q": "1",
                    "z": cumulative,
                    "l": cumulative,
                    "L": "50000",
                    "ap": "50000",
                    "t": event_id,
                    "n": "0",
                    "N": "USDT",
                    "rp": "0",
                },
            }
        )
        assert result.is_success() and result.data is not None
        return result.data

    partial_update = parsed_update(status="PARTIALLY_FILLED", cumulative="0.4", event_id=1)
    partial = outbox.project_user_order_update(partial_update)
    assert partial.children[0].state is ChildCommandState.PARTIALLY_FILLED
    assert partial.children[0].filled_quantity == Decimal("0.4")
    assert outbox.project_user_order_update(partial_update) == partial

    pending_cancel = outbox.project_user_order_update(
        parsed_update(status="PENDING_CANCEL", cumulative="0.4", event_id=2)
    )
    assert pending_cancel.children[0].state is ChildCommandState.PARTIALLY_FILLED
    assert pending_cancel.children[0].filled_quantity == Decimal("0.4")

    filled = outbox.project_user_order_update(parsed_update(status="FILLED", cumulative="1", event_id=3))
    assert filled.state is ParentExecutionState.FILLED
    assert filled.children[0].filled_quantity == Decimal("1")

    external = parsed_update(
        status="FILLED",
        cumulative="1",
        event_id=4,
        client_order_id="external-protection-order",
    )
    assert outbox.project_user_order_update(external) is None
    assert outbox.restore_execution_plan(intent.intent_id) == filled


def test_user_stream_terminal_partial_fill_projects_unknown(tmp_path) -> None:
    outbox = IntentOutbox(str(tmp_path / "execution-terminal-partial.db"))
    intent = OrderIntent(
        intent_id="intent-1",
        account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
        instrument_id=InstrumentId("BTCUSDT"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(amount="1"),
        idempotency_key="idem-intent-terminal-partial",
        reduce_only=True,
    )
    outbox.commit(intent)
    assert outbox.claim("engine-test") is not None
    outbox.persist_execution_plan(intent.intent_id, [_child(0)])
    outbox.transition_execution_child(intent.intent_id, 0, ChildCommandState.SENDING, event_id="send-terminal")
    update = SimpleNamespace(
        client_order_id="beidou-intent-1-0",
        order_status=SimpleNamespace(value="CANCELED"),
        cumulative_quantity=Quantity(amount="0.4"),
        event=SimpleNamespace(event_id="terminal-partial-1"),
        order_id="987",
    )

    projected = outbox.project_user_order_update(update)

    assert projected.children[0].state is ChildCommandState.UNKNOWN
    assert projected.state is ParentExecutionState.UNKNOWN


def test_trade_lite_envelope_parses_as_order_update() -> None:
    """TRADE_LITE(轻量用户流)字段在顶层且无订单状态,必须解析出成交事实。

    真实 demo-fstream 抓包结构(2026-08-20 实测):
    {"e":"TRADE_LITE","E":...,"T":...,"s":"BTCUSDT","q":"0.0009",
     "p":"68130.90","m":false,"c":"beidou-...","S":"SELL","L":"68130.90",
     "l":"0.0008","t":528914974,"i":28547636202}
    —— 无 ``o`` 包裹、无 X/x/z/ap/n,状态只能显式 UNKNOWN。
    """

    raw = {
        "e": "TRADE_LITE",
        "E": 1787162897848,
        "T": 1787162897819,
        "s": "BTCUSDT",
        "q": "0.0009",
        "p": "68130.90",
        "m": False,
        "c": "beidou-btcusdt-entry-1787162894",
        "S": "SELL",
        "L": "68130.90",
        "l": "0.0008",
        "t": 528914974,
        "i": 28547636202,
    }
    result = BinanceUsdmAdapter.parse_user_order_update(raw)
    assert result.is_success() and result.data is not None
    assert result.data.order_id == "28547636202"
    assert result.data.client_order_id == "beidou-btcusdt-entry-1787162894"
    assert result.data.symbol == InstrumentId("BTCUSDT")
    assert result.data.side.value == "SELL"
    assert result.data.order_status.value == "UNKNOWN"
    assert result.data.execution_type == "TRADE"
    assert result.data.last_quantity.amount == "0.0008"
    assert result.data.last_price.amount == "68130.90"
    assert result.data.trade_id == "528914974"
    assert result.data.order_type.value == "UNKNOWN"


def test_engine_persists_complete_multi_slice_plan_before_first_write(monkeypatch) -> None:
    outbox = IntentOutbox()
    intent = OrderIntent(
        intent_id="intent-engine-plan",
        account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
        instrument_id=InstrumentId("BTCUSDT"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(amount="2"),
        client_order_id="beidou-intent-engine-plan",
        idempotency_key="idem-intent-engine-plan",
        reduce_only=True,
    )
    outbox.commit(intent)
    assert outbox.claim("engine-test") is intent

    engine = object.__new__(AutonomousEngine)
    engine._can_write = True
    engine._state_backend_supported = True
    engine._outbox = outbox
    engine._symbol_precision = {"BTCUSDT": {"rule_snapshot_hash": "rule-engine"}}
    engine._intent_retry_count = {}
    engine._control = SimpleNamespace(
        should_accept=lambda _intent: True,
        get_status=lambda: SimpleNamespace(value="NORMAL"),
        version=1,
    )
    engine.evaluate_trading_eligibility = lambda: TradingEligibility.ELIGIBLE
    engine._record_execution_fact_failure = lambda _reason: None

    async def verify(_intent) -> bool:
        return True

    async def plan(_intent, _symbol, _client_id):
        return (
            [
                ("1", "50000", "LIMIT", "GTC", "beidou-intent-engine-plan-0"),
                ("1", "50000", "LIMIT", "GTC", "beidou-intent-engine-plan-1"),
            ],
            "TWAP",
            SimpleNamespace(alpha_decay_seconds=0.0),
        )

    writes: list[tuple[str, tuple[ChildCommandState, ...]]] = []

    async def submit(_intent, *, params, **_kwargs):
        aggregate = outbox.restore_execution_plan(intent.intent_id)
        assert aggregate is not None
        assert len(aggregate.children) == 2
        writes.append(
            (
                str(params["newClientOrderId"]),
                tuple(child.state for child in aggregate.children),
            )
        )
        return {
            "orderId": f"venue-{len(writes)}",
            "clientOrderId": params["newClientOrderId"],
            "status": "NEW",
        }

    async def no_wait(_seconds: float) -> None:
        return None

    engine._verify_intent_at_send = verify
    engine._plan_execution = plan
    engine._submit_order_slice = submit
    monkeypatch.setattr("beidou_core.engine.asyncio.sleep", no_wait)

    asyncio.run(engine._place_order(intent))

    assert writes == [
        (
            "beidou-intent-engine-plan-0",
            (ChildCommandState.SENDING, ChildCommandState.PLANNED),
        ),
        (
            "beidou-intent-engine-plan-1",
            (ChildCommandState.ACKED, ChildCommandState.SENDING),
        ),
    ]
    aggregate = outbox.restore_execution_plan(intent.intent_id)
    assert aggregate is not None
    assert aggregate.state is ParentExecutionState.ACKED
    assert outbox.stats["state_counts"] == {"ACKED": 1}


@pytest.mark.parametrize(
    ("venue_status", "executed_qty", "child_state", "parent_state"),
    [
        ("REJECTED", "0", ChildCommandState.REJECTED, "FAILED"),
        ("CANCELED", "0", ChildCommandState.CANCELED, "ACKED"),
        # M11-R2: 真实入账 transition CANCELED（UNKNOWN containment 为 M11 闭合前过渡）
        ("CANCELED", "0.25", ChildCommandState.CANCELED, "ACKED"),
        ("MYSTERY", "0", ChildCommandState.UNKNOWN, "UNKNOWN"),
        ("FILLED", "NaN", ChildCommandState.UNKNOWN, "UNKNOWN"),
    ],
)
def test_engine_projects_terminal_and_invalid_venue_statuses_conservatively(
    venue_status: str,
    executed_qty: str,
    child_state: ChildCommandState,
    parent_state: str,
) -> None:
    outbox = IntentOutbox()
    intent = OrderIntent(
        intent_id=f"intent-{venue_status.lower()}",
        account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
        instrument_id=InstrumentId("BTCUSDT"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(amount="1"),
        price=None,
        client_order_id=f"beidou-{venue_status.lower()}",
        idempotency_key=f"idem-{venue_status.lower()}",
        reduce_only=True,
    )
    outbox.commit(intent)
    assert outbox.claim("engine-test") is intent

    engine = object.__new__(AutonomousEngine)
    engine._can_write = True
    engine._state_backend_supported = True
    engine._outbox = outbox
    engine._symbol_precision = {"BTCUSDT": {"rule_snapshot_hash": "rule-engine"}}
    engine._intent_retry_count = {}
    engine._control = SimpleNamespace(
        should_accept=lambda _intent: True,
        get_status=lambda: SimpleNamespace(value="NORMAL"),
        version=1,
    )
    engine._record_execution_fact_failure = lambda _reason: None

    async def verify(_intent) -> bool:
        return True

    async def plan(_intent, _symbol, client_id):
        return (
            [("1", None, "MARKET", "GTC", client_id)],
            "DIRECT",
            SimpleNamespace(alpha_decay_seconds=0.0),
        )

    async def submit(_intent, *, params, **_kwargs):
        return {
            "orderId": "venue-terminal-1",
            "clientOrderId": params["newClientOrderId"],
            "status": venue_status,
            "executedQty": executed_qty,
        }

    engine._verify_intent_at_send = verify
    engine._plan_execution = plan
    engine._submit_order_slice = submit

    asyncio.run(engine._place_order(intent))

    aggregate = outbox.restore_execution_plan(intent.intent_id)
    assert aggregate is not None
    assert aggregate.children[0].state is child_state
    assert outbox.stats["state_counts"] == {parent_state: 1}


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"side": "HOLD"}, "side must be BUY or SELL"),
        ({"sequence": -1}, "sequence must be nonnegative"),
        ({"parent_intent_id": ""}, "identity are required"),
        ({"quantity": "invalid"}, "quantity must be a decimal"),
        ({"quantity": "0"}, "quantity must be finite and positive"),
        ({"limit_price": "NaN"}, "limit_price must be finite and positive"),
    ],
)
def test_child_creation_rejects_invalid_economic_fields(changes, message: str) -> None:
    fields = {
        "parent_intent_id": "intent-1",
        "sequence": 0,
        "symbol": "BTCUSDT",
        "side": "BUY",
        "quantity": "1",
        "order_type": "LIMIT",
        "time_in_force": "GTC",
        "client_order_id": "cid-1",
        "limit_price": "50000",
    }
    fields.update(changes)

    with pytest.raises(ValueError, match=message):
        ExecutionChildCommand.create(**fields)


def test_payload_integrity_and_filled_quantity_validation() -> None:
    payload = _child(0).to_payload()
    with pytest.raises(ValueError, match="EXECUTION_COMMAND_HASH_MISMATCH"):
        ExecutionChildCommand.from_payload({**payload, "quantity": "2"})
    with pytest.raises(ValueError, match="filled_quantity must be a decimal"):
        ExecutionChildCommand.from_payload({**payload, "filled_quantity": "invalid"})
    with pytest.raises(ValueError, match="filled_quantity must be finite and nonnegative"):
        ExecutionChildCommand.from_payload({**payload, "filled_quantity": "-1"})
    with pytest.raises(ValueError, match="FILLED_QUANTITY_EXCEEDS_COMMAND"):
        ExecutionChildCommand.from_payload({**payload, "filled_quantity": "2"})


def test_child_transition_rejects_ambiguous_or_regressive_facts() -> None:
    child = _child(0, quantity="2")
    assert child.recovery_action == "NONE"
    with pytest.raises(ValueError, match="event_id is required"):
        child.transition(ChildCommandState.SENDING, event_id="")
    with pytest.raises(ValueError, match="INVALID_CHILD_TRANSITION"):
        child.transition(ChildCommandState.ACKED, event_id="ack", exchange_order_id="venue")

    sending = child.transition(ChildCommandState.SENDING, event_id="send")
    with pytest.raises(ValueError, match="ACKNOWLEDGED_CHILD_REQUIRES_EXCHANGE_ORDER_ID"):
        sending.transition(ChildCommandState.ACKED, event_id="ack")
    with pytest.raises(ValueError, match="FILLED_STATE_REQUIRES_FULL_QUANTITY"):
        sending.transition(
            ChildCommandState.FILLED,
            event_id="fill-short",
            exchange_order_id="venue",
            cumulative_filled_quantity="1",
        )
    with pytest.raises(ValueError, match="PARTIAL_STATE_REQUIRES_PARTIAL_QUANTITY"):
        sending.transition(
            ChildCommandState.PARTIALLY_FILLED,
            event_id="partial-zero",
            exchange_order_id="venue",
            cumulative_filled_quantity="0",
        )
    with pytest.raises(ValueError, match="FILLED_QUANTITY_EXCEEDS_COMMAND"):
        sending.transition(
            ChildCommandState.PARTIALLY_FILLED,
            event_id="partial-over",
            exchange_order_id="venue",
            cumulative_filled_quantity="3",
        )

    partial = sending.transition(
        ChildCommandState.PARTIALLY_FILLED,
        event_id="partial",
        exchange_order_id="venue",
        cumulative_filled_quantity="1",
    )
    with pytest.raises(ValueError, match="FILLED_QUANTITY_REGRESSION"):
        partial.transition(
            ChildCommandState.PARTIALLY_FILLED,
            event_id="partial-regression",
            cumulative_filled_quantity="0.5",
        )


def test_reduce_only_child_converges_on_venue_capped_filled_fact() -> None:
    """BD-FIX: 交易所把 reduce-only 平仓单按剩余持仓截断后回报 FILLED。

    本地计划量(1.100)大于真实剩余持仓(0.109)时,venue origQty/executedQty
    都是 0.109。按"满量成交"守卫拒绝会把子命令永久钉在 UNKNOWN,在途量
    泄漏并把新订单挤到最小下单量门槛之外。截断 FILLED 是终态 venue 事实
    (余量对 reduce-only 恒 -2022,禁止重发),必须收敛且不再计入在途。
    """
    child = ExecutionChildCommand.create(
        parent_intent_id="intent-1",
        sequence=0,
        symbol="LTCUSDT",
        side="BUY",
        quantity="1.100",
        order_type="MARKET",
        time_in_force="IOC",
        client_order_id="beidou-emg-1",
        reduce_only=True,
        rule_snapshot_hash="rule-1",
    )
    aggregate = ParentExecutionAggregate.create("intent-1", [child])
    aggregate = aggregate.transition_child(0, ChildCommandState.SENDING, event_id="send")
    aggregate = aggregate.transition_child(
        0, ChildCommandState.UNKNOWN, event_id="unknown", exchange_order_id="venue-1"
    )
    assert aggregate.children[0].signed_remaining_quantity == Decimal("1.100")

    resolved = aggregate.transition_child(
        0,
        ChildCommandState.FILLED,
        event_id="stale-resolve:venue-capped",
        exchange_order_id="venue-1",
        cumulative_filled_quantity="0.109",
    )
    assert resolved.children[0].state is ChildCommandState.FILLED
    assert resolved.children[0].filled_quantity == Decimal("0.109")
    # 终态后余量不再计入在途 —— 幽灵在途治理的核心断言。
    assert resolved.children[0].signed_remaining_quantity == Decimal("0")


def test_non_reduce_only_child_still_rejects_short_filled_fact() -> None:
    """非 reduce-only 子命令必须保持满量成交守卫:部分成交的 FILLED
    声明是歧义事实,禁止收敛(否则在途量被错误清零,剩余敞口裸奔)。"""
    aggregate = ParentExecutionAggregate.create("intent-1", [_child(0, quantity="2")])
    aggregate = aggregate.transition_child(0, ChildCommandState.SENDING, event_id="send")
    with pytest.raises(ValueError, match="FILLED_STATE_REQUIRES_FULL_QUANTITY"):
        aggregate.transition_child(
            0,
            ChildCommandState.FILLED,
            event_id="fill-short",
            exchange_order_id="venue",
            cumulative_filled_quantity="1",
        )


def test_outbox_stale_child_recovery_accepts_capped_reduce_only_fill(tmp_path) -> None:
    """SQLite outbox 层验证:recover 路径(recover_stale_child/监控投影共用
    的 transition_execution_child)对 venue 截断成交收敛后,该标的在途量归零。"""
    outbox = IntentOutbox(str(tmp_path / "capped-fill.db"))
    intent = OrderIntent(
        intent_id="intent-1",
        account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
        instrument_id=InstrumentId("LTCUSDT"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(amount="1.1"),
        client_order_id="beidou-intent-1",
        idempotency_key="idem-intent-1",
        reduce_only=True,
    )
    outbox.commit(intent)
    assert outbox.claim("engine-test") is not None
    outbox.persist_execution_plan(
        intent.intent_id,
        [
            ExecutionChildCommand.create(
                parent_intent_id=intent.intent_id,
                sequence=0,
                symbol="LTCUSDT",
                side="BUY",
                quantity="1.100",
                order_type="MARKET",
                time_in_force="IOC",
                client_order_id="beidou-emg-1",
                reduce_only=True,
                rule_snapshot_hash="rule-1",
            )
        ],
    )
    outbox.transition_execution_child(intent.intent_id, 0, ChildCommandState.SENDING, event_id="send")
    outbox.transition_execution_child(
        intent.intent_id, 0, ChildCommandState.UNKNOWN, event_id="unknown", exchange_order_id="venue-1"
    )
    assert outbox.inflight_signed_quantity("LTCUSDT") == Decimal("1.100")

    outbox.transition_execution_child(
        intent.intent_id,
        0,
        ChildCommandState.FILLED,
        event_id="stale-resolve:venue-capped",
        exchange_order_id="venue-1",
        cumulative_filled_quantity="0.109",
    )
    restored = outbox.restore_execution_plan(intent.intent_id)
    assert restored is not None
    assert restored.children[0].state is ChildCommandState.FILLED
    assert outbox.inflight_signed_quantity("LTCUSDT") == Decimal("0")


def test_engine_submit_slice_adopts_venue_capped_reduce_only_fill() -> None:
    """BD-FIX: 适配器对截断成交报 ACK_QUANTITY_MISMATCH→UNKNOWN 时,
    执行器直接采纳 venue 终态(FILLED),而不是把子命令钉 30 分钟 UNKNOWN。"""
    engine = object.__new__(AutonomousEngine)
    engine._symbol_precision = {}
    engine._rule_snapshot_hashes = {}
    engine._rule_change_detected = set()
    engine._close_order_ids = set()
    engine._order_trackers = {}
    engine._order_symbols = {}
    engine._order_count = 0
    engine._process_fill_calls: list[tuple[str, str]] = []

    snap = InstrumentRuleSnapshot(
        symbol="LTCUSDT",
        tick_size="0.01",
        step_size="0.001",
        min_qty="0.001",
        min_notional="5",
        price_precision=2,
        qty_precision=3,
        observed_at="2099-01-01T00:00:00+00:00",
    )

    class FakeAdapter:
        def get_rule_snapshot(self, _symbol: str) -> InstrumentRuleSnapshot:
            return snap

        async def create_order(self, _request) -> OrderResponse:
            return OrderResponse(
                venue_instrument=_request.venue_instrument,
                account_ref=_request.account_ref,
                order_id="",
                client_order_id=_request.client_order_id,
                status=OrderStatus.UNKNOWN,
                side=_request.side,
                order_type=_request.order_type,
                original_quantity=_request.quantity,
                executed_quantity=Quantity(amount="0"),
                average_price=None,
                commission=None,
                correlation_id=_request.correlation_id,
                raw_response={
                    "reason": "ACK_QUANTITY_MISMATCH",
                    "venue_response": {
                        "orderId": 1571545784,
                        "symbol": "LTCUSDT",
                        "status": "FILLED",
                        "clientOrderId": "beidou-emg-1",
                        "side": "BUY",
                        "type": "MARKET",
                        "origQty": "0.109",
                        "executedQty": "0.109",
                        "avgPrice": "46.5",
                        "reduceOnly": True,
                    },
                },
            )

    engine._adapter = FakeAdapter()

    async def verify(_intent, *, consume_nonce: bool = False) -> bool:
        assert consume_nonce is True
        return True

    async def process_fill(order_id: str, symbol: str, _result: dict) -> None:
        engine._process_fill_calls.append((order_id, symbol))

    engine._verify_intent_at_send = verify
    engine._process_fill = process_fill

    intent = OrderIntent(
        intent_id="emergency-LTCUSDT-1",
        account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("default")),
        instrument_id=InstrumentId("LTCUSDT"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(amount="1.1"),
        client_order_id="beidou-emg-1",
        idempotency_key="idem-emg-1",
        reduce_only=True,
        close_position=True,
    )
    result = asyncio.run(
        engine._submit_order_slice(
            intent,
            params={
                "symbol": "LTCUSDT",
                "side": "BUY",
                "type": "MARKET",
                "quantity": "1.100",
                "newClientOrderId": "beidou-emg-1",
            },
            order_symbol="LTCUSDT",
            side="BUY",
            order_type="MARKET",
            consume_approval=True,
        )
    )
    assert result is not None
    assert result.get("orderId") == 1571545784
    assert result.get("status") == "FILLED"
    assert result.get("origQty") == "0.109"
    # 采纳后按即时成交处理(状态机/账本投影),而不是返回 UNKNOWN 等待幽灵扫描。
    assert engine._process_fill_calls == [("1571545784", "LTCUSDT")]
    assert "1571545784" in engine._close_order_ids


def test_engine_position_projection_uses_decimal_arithmetic() -> None:
    """BD-FIX: 持仓投影全程 Decimal —— 浮点累加曾把 BNBUSDT 投影写成
    0.01000000000000001,保护数量照抄后 durable SL 行带噪声、覆盖判定
    永不收敛。0.08-0.05 必须精确等于 0.03 而非 0.030000000000000006。"""
    engine = object.__new__(AutonomousEngine)
    engine._position_projection = {}
    engine._position_generation = {}
    saved: list[dict] = []

    class FakeStore:
        def save_position_projection(self, symbol, signed_quantity, entry_price, generation, source_event_id) -> None:
            saved.append(
                {
                    "symbol": symbol,
                    "signed_quantity": signed_quantity,
                    "entry_price": entry_price,
                    "generation": generation,
                    "source_event_id": source_event_id,
                }
            )

    engine._store = FakeStore()

    engine._update_position_projection("BNBUSDT", "BUY", 0.08, 600.0, "fill-1")
    assert engine._position_projection["BNBUSDT"]["signed_quantity"] == "0.08"
    assert engine._position_projection["BNBUSDT"]["entry_price"] == "600.0"
    # 反转:残余持仓按最新成交价,数量必须精确 0.03。
    engine._update_position_projection("BNBUSDT", "SELL", 0.05, 610.0, "fill-2")
    assert engine._position_projection["BNBUSDT"]["signed_quantity"] == "0.03"
    assert engine._position_projection["BNBUSDT"]["entry_price"] == "610.0"
    # 同向加仓:混合均价精确 615。
    engine._update_position_projection("BNBUSDT", "BUY", 0.03, 620.0, "fill-3")
    assert engine._position_projection["BNBUSDT"]["signed_quantity"] == "0.06"
    assert engine._position_projection["BNBUSDT"]["entry_price"] == "615.0"
    # 全平:归零后不留噪声尾数。
    engine._update_position_projection("BNBUSDT", "SELL", 0.06, 615.0, "fill-4")
    assert engine._position_projection["BNBUSDT"]["signed_quantity"] == "0.00"
    assert len(saved) == 4
    assert saved[-1]["signed_quantity"] == "0.00"


def test_engine_submit_slice_keeps_non_reduce_only_mismatch_unknown() -> None:
    """非 reduce-only 的 ACK 数量不一致仍是歧义事实:必须保持 UNKNOWN,
    不得采纳 venue 响应(重复下单/错误成交都不可被静默接受)。"""
    engine = object.__new__(AutonomousEngine)
    engine._symbol_precision = {}
    engine._rule_snapshot_hashes = {}
    engine._rule_change_detected = set()

    snap = InstrumentRuleSnapshot(
        symbol="BTCUSDT",
        tick_size="0.01",
        step_size="0.001",
        min_qty="0.001",
        min_notional="5",
        price_precision=2,
        qty_precision=3,
        observed_at="2099-01-01T00:00:00+00:00",
    )

    class FakeAdapter:
        def get_rule_snapshot(self, _symbol: str) -> InstrumentRuleSnapshot:
            return snap

        async def create_order(self, _request) -> OrderResponse:
            return OrderResponse(
                venue_instrument=_request.venue_instrument,
                account_ref=_request.account_ref,
                order_id="",
                client_order_id=_request.client_order_id,
                status=OrderStatus.UNKNOWN,
                side=_request.side,
                order_type=_request.order_type,
                original_quantity=_request.quantity,
                executed_quantity=Quantity(amount="0"),
                average_price=None,
                commission=None,
                correlation_id=_request.correlation_id,
                raw_response={
                    "reason": "ACK_QUANTITY_MISMATCH",
                    "venue_response": {
                        "orderId": 1,
                        "symbol": "BTCUSDT",
                        "status": "FILLED",
                        "clientOrderId": "beidou-1",
                        "side": "BUY",
                        "type": "MARKET",
                        "origQty": "0.5",
                        "executedQty": "0.5",
                    },
                },
            )

    engine._adapter = FakeAdapter()

    async def verify(_intent, *, consume_nonce: bool = False) -> bool:
        return True

    engine._verify_intent_at_send = verify

    intent = OrderIntent(
        intent_id="intent-BTCUSDT-1",
        account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("default")),
        instrument_id=InstrumentId("BTCUSDT"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(amount="1"),
        client_order_id="beidou-1",
        idempotency_key="idem-1",
    )
    result = asyncio.run(
        engine._submit_order_slice(
            intent,
            params={
                "symbol": "BTCUSDT",
                "side": "BUY",
                "type": "MARKET",
                "quantity": "1.000",
                "newClientOrderId": "beidou-1",
            },
            order_symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            consume_approval=True,
        )
    )
    assert result is not None
    assert result.get("_submit_outcome") == "UNKNOWN"
    assert "orderId" not in result


def test_parent_invariants_and_terminal_states() -> None:
    with pytest.raises(ValueError, match="parent intent and at least one child"):
        ParentExecutionAggregate.create("intent-1", [])
    with pytest.raises(ValueError, match="CHILD_PARENT_MISMATCH"):
        ParentExecutionAggregate.create(
            "intent-1",
            [replace(_child(0), parent_intent_id="other")],
        )
    with pytest.raises(ValueError, match="CHILD_SEQUENCE_MUST_BE_CONTIGUOUS"):
        ParentExecutionAggregate.create("intent-1", [replace(_child(0), sequence=1)])
    with pytest.raises(ValueError, match="DUPLICATE_CHILD_CLIENT_ORDER_ID"):
        ParentExecutionAggregate.create(
            "intent-1",
            [_child(0), replace(_child(1), client_order_id=_child(0).client_order_id)],
        )
    with pytest.raises(ValueError, match="EXECUTION_COMMAND_HASH_MISMATCH"):
        ParentExecutionAggregate.create("intent-1", [replace(_child(0), quantity=Decimal("2"))])

    aggregate = ParentExecutionAggregate.create("intent-1", [_child(0)])
    with pytest.raises(ValueError, match="UNKNOWN_CHILD_SEQUENCE"):
        aggregate.transition_child(1, ChildCommandState.SENDING, event_id="send")
    failed = aggregate.transition_child(0, ChildCommandState.REJECTED, event_id="reject")
    assert failed.state is ParentExecutionState.FAILED
    assert failed.children[0].signed_remaining_quantity == Decimal("0")

    canceled = aggregate.transition_child(0, ChildCommandState.SENDING, event_id="send")
    canceled = canceled.transition_child(
        0,
        ChildCommandState.CANCELED,
        event_id="cancel",
        exchange_order_id="venue",
    )
    assert canceled.state is ParentExecutionState.CANCELED


@pytest.mark.parametrize("value", ["invalid", "NaN", "Infinity"])
def test_target_delta_rejects_invalid_or_nonfinite_values(value: str) -> None:
    with pytest.raises(ValueError, match="target quantities"):
        TargetDeltaPlan.compute(
            symbol="BTCUSDT",
            target_quantity=value,
            current_quantity="0",
        )
