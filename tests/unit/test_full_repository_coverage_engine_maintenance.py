"""Behavior-backed coverage for engine protection and maintenance seams.

The tests use the real protection manager and adaptive protection calculator;
only exchange transport and durable writes are replaced with observable local
authorities.  No test writes to a venue or treats an unacknowledged order as
active.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from beidou_control.plane import ControlAction
from beidou_control.truth import TradingEligibility
from beidou_core.engine import AutonomousEngine
from beidou_safety.execution import OrderIntent
from beidou_safety.execution.command_aggregate import ChildCommandState, ParentExecutionState
from beidou_safety.protection.engine import ProtectionManager
from beidou_shared.types import (
    AccountId,
    AccountRef,
    InstrumentId,
    OrderSide,
    OrderType,
    Price,
    Quantity,
    TimeInForce,
    VenueId,
)


def _protection_features() -> dict[str, float]:
    return {
        "close": 100.0,
        "atr_pct": 1.0,
        "ann_volatility": 0.25,
        "rsi_14": 50.0,
        "trend_20_pct": 3.0,
        "spread_bps": 2.0,
    }


def _protection_engine(*, symbol_precision: dict | None, responses: list[dict] | None = None) -> AutonomousEngine:
    manager = ProtectionManager()
    manager.set_precision_from_rule(SimpleNamespace(price_precision=2, qty_precision=3))
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._position_projection = {}
    engine._position_generation = {}
    engine._position_entry_times = {}
    engine._symbol_precision = symbol_precision or {}
    engine._protection = manager
    engine._protection_owner_id = "owner"
    engine._session_id = "session"
    engine._feed = SimpleNamespace(
        async_get_kline_features=lambda *_args: asyncio.sleep(0, result=_protection_features())
    )
    engine._persisted_protections: list[tuple[object, str | None]] = []
    engine._persist_protection_order = lambda order, *, status=None: engine._persisted_protections.append(
        (order, status)
    )
    engine._removed: list[tuple] = []
    engine._canceled: list[tuple] = []
    engine._remove_protection_with_cleanup = lambda *args: engine._removed.append(args)
    engine._cancel_algo_orders = lambda *args: asyncio.sleep(0, result=engine._canceled.append(args))
    engine._blocked: list[str] = []
    engine._block_unowned_protection_orders = lambda ids: engine._blocked.extend(ids)
    engine._active_algo_ids = {}
    engine._create_algo_order = lambda _params: asyncio.sleep(0, result=(responses or [{}]).pop(0))
    return engine


@pytest.mark.asyncio
async def test_engine_entry_protection_builds_pending_projection_without_venue_write() -> None:
    engine = _protection_engine(symbol_precision={"price": 2, "quantity": 3})
    engine._position_projection = {"BTCUSDT": {"signed_quantity": "bad", "entry_price": "bad"}}

    await engine._ensure_entry_protection(
        "entry-1", "BTCUSDT", {"side": "BUY"}, qty=1.0, entry_price=100.0, submit=False
    )

    positions = engine._protection.all_positions()
    assert set(positions) == {"pos-entry-1"}
    position = positions["pos-entry-1"]
    assert position.quantity == 1.0
    assert position.side is OrderSide.BUY
    assert position.stop_loss is not None and position.take_profits
    assert len(engine._persisted_protections) == 2
    assert all(status == "PENDING" for _order, status in engine._persisted_protections)


@pytest.mark.asyncio
async def test_engine_entry_protection_covers_ack_duplicate_and_rejected_algo_paths() -> None:
    responses = [
        {"algoId": "sl-1", "algoStatus": "NEW"},
        {"code": -4116, "msg": "duplicate conditional"},
        {"code": -2010, "msg": "rejected"},
    ]
    engine = _protection_engine(symbol_precision=None, responses=responses)

    await engine._ensure_entry_protection(
        "entry-2", "BTCUSDT", {"side": "BUY"}, qty=1.0, entry_price=100.0, submit=True
    )

    position = engine._protection.all_positions()["pos-entry-2"]
    orders = [position.stop_loss, *position.take_profits]
    assert engine._active_algo_ids["pos-entry-2"] == {"sl-1"}
    assert orders[0].status.value == "ACTIVE"
    assert orders[1].status.value == "CREATED"
    assert orders[1].status.value == "CREATED"
    assert engine._blocked == [
        "DUPLICATE_CONDITIONAL_ORDER:BTCUSDT",
        "pos-entry-2:PROTECTION_ACK_INCOMPLETE",
        orders[1].protection_id,
    ]
    assert len(engine._persisted_protections) >= 4

    rejected = _protection_engine(
        symbol_precision={"price": 2, "quantity": 3}, responses=[{"code": -2010, "msg": "rejected"}]
    )
    await rejected._ensure_entry_protection(
        "entry-3", "BTCUSDT", {"side": "SELL"}, qty=1.0, entry_price=100.0, submit=True
    )
    assert rejected._blocked[0] == "pos-entry-3:PROTECTION_ACK_INCOMPLETE"


def test_engine_timestamp_and_small_authority_boundaries() -> None:
    assert AutonomousEngine._fact_timestamp(None) == 0.0
    naive = datetime.fromisoformat("2026-01-01T00:00:00")
    aware = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert AutonomousEngine._fact_timestamp(naive) == AutonomousEngine._fact_timestamp(aware)

    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._position_generation = {}
    assert engine._next_position_generation("BTCUSDT") == 1
    assert engine._next_position_generation("BTCUSDT") == 2

    engine._control = SimpleNamespace(get_status=lambda: ControlAction.RESUME)
    engine._safe_no_new_risk = lambda _reason: setattr(engine, "no_new_risk", True)
    engine._alerts = SimpleNamespace(send_incident=lambda *args, **kwargs: setattr(engine, "incident", (args, kwargs)))
    engine._protection_issues = set()
    engine._block_unowned_protection_orders(["p1"])
    assert engine._protection_owner_unknown is True
    assert engine._protection_issues == {"p1"}


class _PlanAggregate:
    def __init__(self, count: int) -> None:
        self.children = [SimpleNamespace(state=ChildCommandState.PLANNED) for _ in range(count)]

    @property
    def all_children_acknowledged(self) -> bool:
        return all(
            child.state
            in {
                ChildCommandState.ACKED,
                ChildCommandState.PARTIALLY_FILLED,
                ChildCommandState.FILLED,
                ChildCommandState.CANCELED,
                ChildCommandState.REJECTED,
            }
            for child in self.children
        )

    @property
    def state(self) -> ParentExecutionState:
        if any(child.state is ChildCommandState.UNKNOWN for child in self.children):
            return ParentExecutionState.UNKNOWN
        if self.all_children_acknowledged:
            return ParentExecutionState.ACKED
        return ParentExecutionState.IN_FLIGHT


class _PlanOutbox:
    def __init__(self, aggregate: _PlanAggregate, *, persist_error: Exception | None = None) -> None:
        self.aggregate = aggregate
        self.persist_error = persist_error
        self.actions: list[tuple] = []
        self.renewals: list[tuple] = []
        self.restore_calls = 0

    def persist_execution_plan(self, _intent_id: str, _commands: list[object]) -> _PlanAggregate:
        if self.persist_error:
            raise self.persist_error
        return self.aggregate

    def restore_execution_plan(self, _intent_id: str) -> _PlanAggregate:
        self.restore_calls += 1
        return self.aggregate

    def renew_lease(self, intent_id: str, *, lease_seconds: float) -> None:
        self.renewals.append((intent_id, lease_seconds))

    def ack(self, *args, **kwargs) -> None:
        self.actions.append(("ack", args, kwargs))

    def reject(self, *args, **kwargs) -> None:
        self.actions.append(("reject", args, kwargs))

    def mark_unknown(self, *args, **kwargs) -> None:
        self.actions.append(("unknown", args, kwargs))

    def dead_letter(self, *args, **kwargs) -> None:
        self.actions.append(("dead", args, kwargs))


def _live_intent(name: str = "live") -> OrderIntent:
    return OrderIntent(
        intent_id=f"intent-{name}",
        account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
        instrument_id=InstrumentId("BTCUSDT"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(amount="1"),
        price=Price(amount="100"),
        time_in_force=TimeInForce.GTC,
        client_order_id=f"cid-{name}",
        idempotency_key=f"idem-{name}",
    )


def _live_engine(
    monkeypatch: pytest.MonkeyPatch,
    *,
    outcomes: list[dict],
    slices: list[tuple] | None = None,
    control_answers: list[bool] | None = None,
    plan: _PlanAggregate | None = None,
    persist_error: Exception | None = None,
) -> tuple[AutonomousEngine, OrderIntent, _PlanOutbox, _PlanAggregate]:
    aggregate = plan or _PlanAggregate(len(slices or [("1", "", "MARKET", "GTC", "cid-live-0")]))
    outbox = _PlanOutbox(aggregate, persist_error=persist_error)
    intent = _live_intent()
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._can_write = True
    engine._state_backend_supported = True
    engine._env_mode = SimpleNamespace(value="testnet")
    engine._control_answers = list(control_answers or [])

    def should_accept(_intent: object) -> bool:
        return engine._control_answers.pop(0) if engine._control_answers else True

    engine._control = SimpleNamespace(
        should_accept=should_accept,
        get_status=lambda: ControlAction.RESUME,
        version=1,
    )
    engine._outbox = outbox
    engine._symbol_precision = {"BTCUSDT": {"rule_snapshot_hash": "rule-v1"}}
    engine._intent_retry_count = {}
    engine.evaluate_trading_eligibility = lambda: TradingEligibility.ELIGIBLE
    engine._record_execution_fact_failure_env_guarded = lambda *_args: None
    engine._verify_intent_at_send = lambda *_args, **_kwargs: asyncio.sleep(0, result=True)
    engine._plan_execution = lambda *_args: asyncio.sleep(
        0,
        result=(slices or [("1", "", "MARKET", "GTC", "cid-live-0")], "TWAP", SimpleNamespace(alpha_decay_seconds=4.0)),
    )
    remaining = list(outcomes)

    async def submit(*_args, **_kwargs) -> dict:
        return remaining.pop(0)

    engine._submit_order_slice = submit

    def transition(_intent_id: str, sequence: int, state: ChildCommandState, **_kwargs: object) -> _PlanAggregate:
        aggregate.children[sequence].state = state
        return aggregate

    engine._transition_execution_child_race_safe = transition
    engine._consume_cumulative_fill = lambda *_args, **_kwargs: (0.25, 100.0, "fill-live")
    engine._record_partial_fill_to_ledger = lambda *_args, **_kwargs: True
    engine._mark_fill_retryable = lambda *args: outbox.actions.append(("retry", args, {}))

    async def no_sleep(_seconds: float, result: object = None) -> object:
        return result

    monkeypatch.setattr("beidou_core.engine.asyncio.sleep", no_sleep)
    return engine, intent, outbox, aggregate


@pytest.mark.asyncio
async def test_engine_place_order_plan_persistence_control_and_child_replay_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed, intent, outbox, _aggregate = _live_engine(monkeypatch, outcomes=[], persist_error=RuntimeError("db"))
    await failed._place_order(intent)
    assert outbox.actions[0][0] == "unknown" and "PERSISTENCE_FAILED" in outbox.actions[0][1][1]

    aborted, intent, outbox, aggregate = _live_engine(
        monkeypatch,
        outcomes=[{"orderId": "1", "status": "NEW", "executedQty": "0"}],
        slices=[("0.5", "", "MARKET", "GTC", "cid-0"), ("0.5", "", "MARKET", "GTC", "cid-1")],
        control_answers=[True, False],
    )
    await aborted._place_order(intent)
    assert aggregate.children[1].state is ChildCommandState.REJECTED
    assert outbox.actions[-1][0] == "unknown"

    replay_plan = _PlanAggregate(1)
    replay_plan.children[0].state = ChildCommandState.UNKNOWN
    replay, intent, outbox, _aggregate = _live_engine(monkeypatch, outcomes=[], plan=replay_plan)
    await replay._place_order(intent)
    assert outbox.actions[-1][1][1] == "PLAN_RESEND_SKIPPED_UNKNOWN_CHILD"

    post_event, intent, outbox, aggregate = _live_engine(monkeypatch, outcomes=[])
    restore_states = [ChildCommandState.PLANNED, ChildCommandState.REJECTED]
    original_restore = outbox.restore_execution_plan

    def restore_after_event(_intent_id: str) -> _PlanAggregate:
        state = restore_states.pop(0) if restore_states else ChildCommandState.REJECTED
        aggregate.children[0].state = state
        return original_restore(_intent_id)

    outbox.restore_execution_plan = restore_after_event
    await post_event._place_order(intent)
    assert any(
        action[0] == "reject" and action[1][1] == "EXECUTION_CHILD_REJECTED_BY_EVENT_PATH" for action in outbox.actions
    )


@pytest.mark.asyncio
async def test_engine_place_order_live_child_outcome_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    cases = [
        ({"_submit_outcome": "REJECTED", "reason": "RATE_LIMIT"}, "unknown"),
        ({"_submit_outcome": "REJECTED", "reason": "HARD_REJECT"}, "reject"),
        ({"_submit_outcome": "UNKNOWN", "reason": "transport"}, "unknown"),
        ({"orderId": "2", "status": "NEW", "executedQty": "2"}, "unknown"),
        ({"orderId": "3", "status": "REJECTED", "executedQty": "0"}, "reject"),
        ({"orderId": "4", "status": "UNKNOWN", "executedQty": "0"}, "unknown"),
        ({"orderId": "5", "status": "CANCELED", "executedQty": "0.25"}, "ack"),
        ({"orderId": "6", "status": "NEW", "executedQty": "0"}, "ack"),
        ({"orderId": "7", "status": "PARTIALLY_FILLED", "executedQty": "0.25"}, "ack"),
        ({"orderId": "8", "status": "PENDING_CANCEL", "executedQty": "0.25"}, "ack"),
        ({"orderId": "9", "status": "FILLED", "executedQty": "1"}, "ack"),
    ]
    for index, (outcome, expected_action) in enumerate(cases):
        engine, intent, outbox, _aggregate = _live_engine(monkeypatch, outcomes=[outcome])
        intent = _live_intent(str(index))
        await engine._place_order(intent)
        assert outbox.actions, (index, outcome)
        assert outbox.actions[-1][0] == expected_action, (index, outcome, outbox.actions)


@pytest.mark.asyncio
async def test_engine_place_order_remaining_gates_and_race_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    # The eligibility gate is independent from the final approval gate.
    rejected, intent, outbox, _aggregate = _live_engine(monkeypatch, outcomes=[])
    rejected.evaluate_trading_eligibility = lambda: TradingEligibility.NO_NEW_RISK
    await rejected._place_order(intent)
    assert outbox.actions[-1][0:2] == ("reject", (intent.intent_id, "ELIGIBILITY_NO_NEW_RISK"))

    # The first retry creates the retry counter; the next governed attempt
    # crosses the dead-letter threshold without contacting the venue again.
    dead, intent, outbox, _aggregate = _live_engine(
        monkeypatch, outcomes=[{"_submit_outcome": "UNKNOWN", "reason": "transport"}]
    )
    del dead._intent_retry_count
    await dead._place_order(intent)
    dead._intent_retry_count[intent.intent_id] = 50
    await dead._place_order(intent)
    assert any(action[0] == "dead" for action in outbox.actions)

    planned_none, intent, outbox, _aggregate = _live_engine(monkeypatch, outcomes=[])
    planned_none._plan_execution = lambda *_args: asyncio.sleep(0, result=None)
    await planned_none._place_order(intent)
    assert outbox.actions == []

    # Multi-slice pacing renews the lease, and an unavailable restore read is
    # observable while the already durable aggregate remains authoritative.
    renewed, intent, outbox, aggregate = _live_engine(
        monkeypatch,
        outcomes=[
            {"orderId": "1", "status": "NEW", "executedQty": "0"},
            {"orderId": "2", "status": "NEW", "executedQty": "0"},
        ],
        slices=[("0.5", "", "MARKET", "GTC", "cid-0"), ("0.5", "", "MARKET", "GTC", "cid-1")],
    )
    outbox.renew_lease = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("lease"))
    outbox.restore_execution_plan = lambda _intent_id: (_ for _ in ()).throw(OSError("restore"))
    await renewed._place_order(intent)
    assert aggregate.all_children_acknowledged

    replay_ack, intent, outbox, aggregate = _live_engine(monkeypatch, outcomes=[], plan=_PlanAggregate(1))
    aggregate.children[0].state = ChildCommandState.ACKED
    await replay_ack._place_order(intent)
    assert outbox.actions[-1][0] == "ack"

    # An event path may win the race after SENDING.  UNKNOWN remains blocked;
    # an already acknowledged child is skipped without a duplicate write.
    event_unknown, intent, outbox, aggregate = _live_engine(monkeypatch, outcomes=[])
    restore_calls = 0

    def restore_unknown(_intent_id: str) -> _PlanAggregate:
        nonlocal restore_calls
        restore_calls += 1
        if restore_calls >= 2:
            aggregate.children[0].state = ChildCommandState.UNKNOWN
        return aggregate

    outbox.restore_execution_plan = restore_unknown
    await event_unknown._place_order(intent)
    assert outbox.actions[-1][1][1] == "EXECUTION_PLAN_INCOMPLETE_UNKNOWN"

    event_acked, intent, outbox, aggregate = _live_engine(monkeypatch, outcomes=[])
    restore_calls = 0

    def restore_acked(_intent_id: str) -> _PlanAggregate:
        nonlocal restore_calls
        restore_calls += 1
        if restore_calls >= 2:
            aggregate.children[0].state = ChildCommandState.ACKED
        return aggregate

    outbox.restore_execution_plan = restore_acked
    await event_acked._place_order(intent)
    assert outbox.actions[-1][0] == "ack"

    siblings, intent, outbox, aggregate = _live_engine(
        monkeypatch,
        outcomes=[{"_submit_outcome": "REJECTED", "reason": "HARD_REJECT"}],
        slices=[("0.5", "", "MARKET", "GTC", "cid-0"), ("0.5", "", "MARKET", "GTC", "cid-1")],
    )
    await siblings._place_order(intent)
    assert aggregate.children[1].state is ChildCommandState.REJECTED

    partial_error, intent, outbox, aggregate = _live_engine(
        monkeypatch, outcomes=[{"orderId": "partial", "status": "CANCELED", "executedQty": "0.25"}]
    )
    partial_error._record_partial_fill_to_ledger = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("ledger"))
    await partial_error._place_order(intent)
    assert any(action[0] == "retry" for action in outbox.actions)

    # A venue ACK can still leave the durable aggregate non-terminal when a
    # concurrent writer wins the child transition race.  The parent must keep
    # the fail-closed UNKNOWN/NOT_FULLY_ACKNOWLEDGED distinction observable.
    not_fully_acknowledged, intent, outbox, aggregate = _live_engine(
        monkeypatch, outcomes=[{"orderId": "unsettled", "status": "NEW", "executedQty": "0"}]
    )
    aggregate.children[0].state = ChildCommandState.PLANNED
    not_fully_acknowledged._transition_execution_child_race_safe = lambda *_args, **_kwargs: aggregate
    await not_fully_acknowledged._place_order(intent)
    assert outbox.actions[-1] == (
        "unknown",
        (intent.intent_id, "EXECUTION_PLAN_NOT_FULLY_ACKNOWLEDGED"),
        {},
    )


@pytest.mark.asyncio
async def test_engine_entry_protection_handles_none_orders_fallback_precision_and_dedup() -> None:
    class Order:
        def __init__(self, protection_id: str, reason: str, trigger: str, order_type: str) -> None:
            self.protection_id = protection_id
            self.reason = reason
            self.trigger_price = SimpleNamespace(amount=trigger)
            self.quantity = SimpleNamespace(amount="1")
            self.order_type = order_type
            self.stop_type = SimpleNamespace(value="ATR_BASED")
            self.status = None
            self.exchange_order_id = None

    class Position:
        def __init__(self, stop_trigger: str = "90", tp_trigger: str = "110") -> None:
            self.position_id = "pos-custom"
            self.instrument_id = InstrumentId("BTCUSDT")
            self.entry_price = 100.0
            self.quantity = 1.0
            self.side = OrderSide.BUY
            self.stop_loss = Order("stop-custom", "STOP_LOSS", stop_trigger, "STOP_MARKET")
            self._tp = Order("tp-custom", "TAKE_PROFIT", tp_trigger, "TAKE_PROFIT_MARKET")
            self._calls = 0

        @property
        def take_profits(self):
            self._calls += 1
            # The first durable list contains a malformed slot; later reads
            # expose only the valid TP so the summary remains well-defined.
            return [None, self._tp] if self._calls == 1 else [self._tp]

        def update_price_extremes(self, _price: float) -> None:
            return None

    positions: dict[str, Position] = {}
    engine = _protection_engine(symbol_precision={}, responses=[{"algoId": "tp-ack"}])
    engine._require_protection_config = lambda *_args: None
    engine._protection = SimpleNamespace(
        all_positions=lambda: positions,
        create_protection=lambda **_kwargs: positions.setdefault("pos-custom", Position()),
        set_precision_from_rule=lambda _rule: None,
    )
    await engine._ensure_entry_protection(
        "custom", "BTCUSDT", {"side": "BUY"}, qty=1.0, entry_price=100.0, submit=False
    )
    assert any(item[0] is None for item in engine._persisted_protections) is False

    # A repeated durable protection id is skipped at the exchange boundary.
    positions.clear()
    engine = _protection_engine(symbol_precision={}, responses=[{"algoId": "tp-ack"}])
    engine._require_protection_config = lambda *_args: None
    engine._protection_exchange_attempted = {"stop-custom"}
    engine._protection = SimpleNamespace(
        all_positions=lambda: positions,
        create_protection=lambda **_kwargs: positions.setdefault("pos-custom", Position()),
        set_precision_from_rule=lambda _rule: None,
    )
    await engine._ensure_entry_protection("custom", "BTCUSDT", {"side": "BUY"}, qty=1.0, entry_price=100.0, submit=True)
    assert engine._active_algo_ids["pos-custom"] == {"tp-ack"}

    high = _protection_engine(
        symbol_precision=None,
        responses=[{"algoId": "high-sl"}, {"algoId": "high-tp1"}, {"algoId": "high-tp2"}],
    )
    del high._active_algo_ids
    await high._ensure_entry_protection("high", "BTCUSDT", {"side": "BUY"}, qty=1.0, entry_price=10000.0, submit=True)
    assert high._active_algo_ids

    low = _protection_engine(
        symbol_precision=None,
        responses=[{"algoId": "low-sl"}, {"algoId": "low-tp1"}, {"algoId": "low-tp2"}],
    )
    low_positions: dict[str, Position] = {}
    low._require_protection_config = lambda *_args: None
    low._protection = SimpleNamespace(
        all_positions=lambda: low_positions,
        create_protection=lambda **_kwargs: low_positions.setdefault("pos-custom", Position("0.5", "0.8")),
        set_precision_from_rule=lambda _rule: None,
    )
    await low._ensure_entry_protection("low", "BTCUSDT", {"side": "BUY"}, qty=1.0, entry_price=0.5, submit=True)
    assert low._active_algo_ids
