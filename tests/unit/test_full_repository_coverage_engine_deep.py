"""Behavioral branch coverage for engine recovery and protection boundaries."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from beidou_core.engine import AutonomousEngine


def _engine() -> AutonomousEngine:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._protection_owner_id = "svc"
    engine._session_id = "session"
    engine._position_generation = {}
    engine._position_projection = {}
    engine._position_entry_times = {}
    engine._active_algo_ids = {}
    engine._pending_stop_intent = {}
    engine._protection_issues = set()
    engine._protection_owner_unknown = False
    engine._last_protection_fact_at = 0.0
    engine._can_write = False
    engine._control = SimpleNamespace(
        get_status=lambda: SimpleNamespace(value="NO_NEW_RISK"), execute_action=lambda *_args: None
    )
    engine._alerts = SimpleNamespace(send_incident=lambda *_args, **_kwargs: None)
    return engine


class _Store:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.saved: list[dict] = []
        self.removed: list[str] = []
        self.fail_save = False
        self.fail_remove = False

    def restore_protections(self) -> list[dict]:
        return self.rows

    def save_protection(self, **kwargs) -> None:
        if self.fail_save:
            raise RuntimeError("save failed")
        self.saved.append(kwargs)

    def remove_protection(self, position_id: str) -> None:
        if self.fail_remove:
            raise RuntimeError("remove failed")
        self.removed.append(position_id)
        self.rows = [row for row in self.rows if str(row.get("position_id")) != position_id]


def _protection_row(
    *,
    protection_id: str,
    position_id: str = "pos-BTCUSDT",
    exchange_order_id: str,
    order_type: str,
    trigger_price: str,
    stop_type: str | None,
    take_profit_type: str | None,
    status: str = "ACTIVE",
    side: str = "SELL",
    quantity: str = "1",
    owner_id: str = "svc",
    generation: int = 1,
    session_id: str = "session",
) -> dict:
    return {
        "protection_id": protection_id,
        "position_id": position_id,
        "symbol": "BTCUSDT",
        "side": side,
        "trigger_price": trigger_price,
        "order_price": None,
        "quantity": quantity,
        "order_type": order_type,
        "status": status,
        "stop_type": stop_type,
        "take_profit_type": take_profit_type,
        "owner_id": owner_id,
        "position_generation": generation,
        "session_id": session_id,
        "exchange_order_id": exchange_order_id,
    }


def test_engine_adopts_valid_orphaned_algos_and_keeps_ambiguous_facts_closed() -> None:
    engine = _engine()
    engine._last_algo_inventory_genuine = True
    engine._last_account = {
        "positions": [
            {"symbol": "BAD", "positionAmt": "not-a-number", "entryPrice": "100"},
            {"symbol": "ZERO", "positionAmt": "0", "entryPrice": "100"},
            {"symbol": "BTCUSDT", "positionAmt": "2", "entryPrice": "100"},
        ]
    }
    store = _Store([])
    engine._store = store
    engine._position_projection = {"BTCUSDT": {"position_generation": "bad", "entry_price": "100"}}
    engine._next_position_generation = lambda _symbol: 7
    restored: list[object] = []
    engine._protection = SimpleNamespace(
        all_positions=lambda: {},
        restore_position_protection=lambda projection: restored.append(projection),
    )
    algos = [
        {
            "symbol": "BTCUSDT",
            "clientAlgoId": "bdp-invalid",
            "algoStatus": "NEW",
            "orderType": "STOP_MARKET",
            "side": "SELL",
            "reduceOnly": True,
            "quantity": "bad",
            "triggerPrice": "90",
        },
        {
            "symbol": "BTCUSDT",
            "clientAlgoId": "bdp-sl",
            "algoStatus": "NEW",
            "orderType": "STOP_MARKET",
            "side": "SELL",
            "reduceOnly": True,
            "quantity": "2",
            "triggerPrice": "90",
            "algoId": "101",
        },
        {
            "symbol": "BTCUSDT",
            "clientAlgoId": "bdp-tp",
            "algoStatus": "WORKING",
            "orderType": "TAKE_PROFIT_MARKET",
            "side": "SELL",
            "reduceOnly": "yes",
            "quantity": "2",
            "triggerPrice": "120",
            "algoId": "102",
        },
        {
            "symbol": "BTCUSDT",
            "clientAlgoId": "foreign",
            "algoStatus": "NEW",
            "orderType": "STOP_MARKET",
            "side": "SELL",
            "reduceOnly": True,
            "quantity": "2",
            "triggerPrice": "90",
        },
    ]

    adopted, issues = engine._adopt_orphaned_protection_algos(algos)
    assert adopted == 2
    assert issues == []
    assert {item["exchange_order_id"] for item in store.saved} == {"101", "102"}
    assert restored and restored[0].instrument_id == "BTCUSDT"
    assert engine._active_algo_ids["adopt-BTCUSDT"] == {"101", "102"}

    # A malformed trigger is rejected by the semantic predicate, and two
    # otherwise valid stops remain ambiguous rather than being adopted.
    ambiguous = [
        {
            "symbol": "BTCUSDT",
            "clientAlgoId": "bdp-a",
            "algoStatus": "NEW",
            "orderType": "STOP_MARKET",
            "side": "SELL",
            "reduceOnly": True,
            "quantity": "2",
            "triggerPrice": "90",
            "algoId": "201",
        },
        {
            "symbol": "BTCUSDT",
            "clientAlgoId": "bdp-b",
            "algoStatus": "NEW",
            "orderType": "STOP_MARKET",
            "side": "SELL",
            "reduceOnly": True,
            "quantity": "2",
            "triggerPrice": "91",
            "algoId": "202",
        },
        {
            "symbol": "BTCUSDT",
            "clientAlgoId": "bdp-tp",
            "algoStatus": "NEW",
            "orderType": "TAKE_PROFIT_MARKET",
            "side": "SELL",
            "reduceOnly": True,
            "quantity": "2",
            "triggerPrice": "120",
            "algoId": "203",
        },
    ]
    engine._store = _Store([])
    adopted, issues = engine._adopt_orphaned_protection_algos(ambiguous)
    assert adopted == 0 and issues == ["PROTECTION_ADOPTION_AMBIGUOUS:BTCUSDT"]


def test_engine_protection_semantics_stale_rows_and_algo_cancellation() -> None:
    engine = _engine()
    expected = _protection_row(
        protection_id="p1",
        exchange_order_id="1",
        order_type="STOP_MARKET",
        trigger_price="90",
        stop_type="ATR_BASED",
        take_profit_type=None,
    )
    engine._store = _Store([expected])
    issues = engine._protection_inventory_semantic_issues(
        [
            {
                "algoId": "1",
                "symbol": "BAD",
                "side": "BUY",
                "orderType": "TAKE_PROFIT_MARKET",
                "quantity": "bad",
                "triggerPrice": "bad",
                "reduceOnly": False,
            }
        ]
    )
    assert any(item.startswith("PROTECTION_SYMBOL_MISMATCH") for item in issues)
    assert "PROTECTION_QUANTITY_MISMATCH:1" in issues
    assert "PROTECTION_TRIGGER_MISMATCH:1" in issues
    assert "PROTECTION_REDUCE_ONLY_UNPROVEN:1" in issues

    engine._active_algo_ids = {"pos-BTCUSDT": {"1", "2"}}
    engine._store.fail_save = True
    engine._cancel_stale_protection_rows(
        "BTCUSDT",
        [expected, {**expected, "protection_id": "p2", "exchange_order_id": "2", "owner_id": "other"}],
        "test",
    )
    assert engine._stale_protection_algos == [("BTCUSDT", "1")]
    assert engine._active_algo_ids["pos-BTCUSDT"] == {"2"}

    calls: list[tuple[str, int]] = []

    async def cancel(symbol: str, algo_id: int):
        calls.append((symbol, algo_id))
        if algo_id == 1:
            return {"code": 200, "msg": "success"}
        if algo_id == 2:
            return {"code": 500, "msg": "rejected"}
        raise RuntimeError("transport")

    engine._stale_protection_algos = [("BTCUSDT", ""), ("BTCUSDT", "1"), ("BTCUSDT", "2"), ("BTCUSDT", "3")]
    engine._cancel_algo_order = cancel
    canceled = asyncio.run(engine._cancel_stale_protection_algos())
    assert canceled == {"1"}
    assert calls == [("BTCUSDT", 1), ("BTCUSDT", 2), ("BTCUSDT", 3)]


def test_engine_restores_ack_backed_projection_and_handles_pending_rows() -> None:
    engine = _engine()
    stop = _protection_row(
        protection_id="stop",
        exchange_order_id="11",
        order_type="STOP_MARKET",
        trigger_price="90",
        stop_type="ATR_BASED",
        take_profit_type=None,
    )
    take = _protection_row(
        protection_id="take",
        exchange_order_id="12",
        order_type="TAKE_PROFIT_MARKET",
        trigger_price="120",
        stop_type=None,
        take_profit_type="FIXED_RR",
    )
    store = _Store([stop, take])
    engine._store = store
    restored: list[object] = []
    engine._protection = SimpleNamespace(
        all_positions=lambda: {},
        restore_position_protection=lambda projection: restored.append(projection),
    )
    engine._active_algo_ids = None
    inventory = [
        {
            "algoId": "11",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "orderType": "STOP_MARKET",
            "quantity": "1",
            "triggerPrice": "90",
            "reduceOnly": True,
        },
        {
            "algoId": "12",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "orderType": "TAKE_PROFIT_MARKET",
            "quantity": "1",
            "triggerPrice": "120",
            "reduceOnly": True,
        },
    ]
    assert (
        engine._restore_durable_protection_projection(
            {"positions": [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}]}, inventory
        )
        is True
    )
    assert restored and restored[0].stop_loss.exchange_order_id == "11"
    assert engine._active_algo_ids["pos-BTCUSDT"] == {"11", "12"}

    pending = _protection_row(
        protection_id="pending",
        exchange_order_id="13",
        order_type="STOP_MARKET",
        trigger_price="90",
        stop_type="ATR_BASED",
        take_profit_type=None,
        status="PENDING",
    )
    pending["exchange_order_id"] = ""
    engine._store = _Store([pending])
    engine._protection = SimpleNamespace(
        all_positions=lambda: {}, restore_position_protection=lambda projection: restored.append(projection)
    )
    assert (
        engine._restore_durable_protection_projection(
            {"positions": [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}]}, []
        )
        is False
    )


def test_engine_restore_fail_closed_matrix_and_recovery_cleanup() -> None:
    engine = _engine()
    engine._block_unowned_protection_orders = lambda issues: setattr(engine, "blocked", list(issues))
    engine._store = _Store(
        [
            _protection_row(
                protection_id="p",
                exchange_order_id="1",
                order_type="STOP_MARKET",
                trigger_price="90",
                stop_type="ATR_BASED",
                take_profit_type=None,
            )
        ]
    )
    assert engine._restore_durable_protection_projection({}, None) is False
    assert engine.blocked == ["OPEN_ALGO_ORDERS_UNKNOWN"]
    assert engine._restore_durable_protection_projection({}, []) is False
    assert engine.blocked == ["OPEN_ALGO_ORDERS_EMPTY"]

    # Missing venue rows are cleaned, while a failed cleanup is retained as a
    # blocked recovery fact and does not silently become a valid projection.
    row = _protection_row(
        protection_id="p",
        exchange_order_id="missing",
        order_type="STOP_MARKET",
        trigger_price="90",
        stop_type="ATR_BASED",
        take_profit_type=None,
    )
    store = _Store([row])
    engine._store = store
    engine._protection_row_fresh = lambda _algo: False
    engine._protection_inventory_semantic_issues = lambda _inventory: ["PROTECTION_VENUE_ROW_MISSING:missing"]
    engine._protection = SimpleNamespace(all_positions=lambda: {}, restore_position_protection=lambda _projection: None)
    assert engine._restore_durable_protection_projection({"positions": []}, [{"algoId": "other"}]) is True
    assert store.removed == ["pos-BTCUSDT"]

    store = _Store([row])
    store.fail_remove = True
    engine._store = store
    engine._protection_inventory_semantic_issues = lambda _inventory: ["PROTECTION_VENUE_ROW_MISSING:missing"]
    engine._protection_row_fresh = lambda _algo: False
    assert engine._restore_durable_protection_projection({"positions": []}, [{"algoId": "other"}]) is False


class _FillStore:
    def __init__(self) -> None:
        self.events: dict[str, dict] = {}
        self.orders: list[tuple] = []
        self.projections: list[tuple] = []
        self.ledger_entries: list[tuple] = []
        self.fail_secondary = False

    def get_fill_event(self, event_id: str):
        return self.events.get(event_id)

    def save_fill_event(self, event_id, order_id, symbol, side, cumulative, delta, price, status):
        if event_id in self.events:
            return False
        self.events[event_id] = {
            "fill_event_id": event_id,
            "order_id": order_id,
            "delta_qty": delta,
            "price": price,
            "processing_state": "PENDING",
        }
        return True

    def mark_fill_event_committed(self, event_id: str) -> None:
        self.events.setdefault(event_id, {})["processing_state"] = "COMMITTED"

    def save_order_state(self, *args, **kwargs) -> None:
        self.orders.append((args, kwargs))

    def save_position_projection(self, *args) -> None:
        self.projections.append(args)

    def save_ledger_entry(self, *args) -> None:
        if self.fail_secondary:
            raise RuntimeError("secondary index")
        self.ledger_entries.append(args)

    def restore_fill_events(self):
        return list(self.events.values())


def test_engine_fill_fact_helpers_cover_lite_cumulative_and_projection_paths() -> None:
    engine = _engine()
    store = _FillStore()
    engine._store = store
    engine._filled_quantities_by_order = {}
    engine._position_projection = {}
    engine._position_generation = {}
    posted: list[object] = []
    projected: list[tuple] = []
    committed: list[tuple] = []
    engine._post_ledger_transaction = lambda transaction: posted.append(transaction)
    engine._update_position_projection = lambda *args: projected.append(args)
    engine._mark_fill_committed = lambda *args: committed.append(args)

    assert (
        engine._record_partial_fill_to_ledger(
            "o1",
            "BTCUSDT",
            {"side": "BUY", "type": "LIMIT", "origQty": "2", "price": "100"},
            0.5,
            100.0,
            0.5,
            "trade:o1:1",
            status="PARTIALLY_FILLED",
        )
        is True
    )
    assert posted and projected and committed and store.orders

    assert engine._order_has_committed_cumulative_fill("o1") is False
    store.events["trade:o1:1"] = {"order_id": "o1", "fill_event_id": "trade:o1:1", "processing_state": "COMMITTED"}
    engine._cum_fill_cache = {}
    store.events["cum"] = {"order_id": "o1", "fill_event_id": "cum", "processing_state": "COMMITTED"}
    assert engine._order_has_committed_cumulative_fill("o1") is True
    assert engine._order_has_committed_cumulative_fill("o1") is True  # cache path
    engine._store = SimpleNamespace(restore_fill_events=lambda: (_ for _ in ()).throw(RuntimeError("read")))
    engine._cum_fill_cache = {}
    assert engine._order_has_committed_cumulative_fill("o2") is True
    engine._store = SimpleNamespace()
    engine._cum_fill_cache = {}
    assert engine._order_has_committed_cumulative_fill("o3") is True

    # Restore the durable double for TRADE_LITE and exercise identity,
    # duplicate, cumulative-owner, and rollback paths.
    engine._store = store
    engine._filled_quantities_by_order = {}
    engine._order_has_committed_cumulative_fill = lambda _oid: False
    update = SimpleNamespace(
        order_id="o2",
        symbol="BTCUSDT",
        side=SimpleNamespace(value="BUY"),
        trade_id="t1",
        last_quantity=SimpleNamespace(amount="0.2"),
        last_price=SimpleNamespace(amount="100"),
    )
    engine._record_trade_lite_fill(update)
    assert engine._filled_quantities_by_order["o2"] == pytest.approx(0.2)
    engine._record_trade_lite_fill(update)  # durable trade-id idempotency
    engine._record_trade_lite_fill(
        SimpleNamespace(order_id="", symbol="BTCUSDT", side=SimpleNamespace(value="BUY"), trade_id="x")
    )
    engine._record_trade_lite_fill(
        SimpleNamespace(
            order_id="o3",
            symbol="BTCUSDT",
            side=SimpleNamespace(value="BUY"),
            trade_id="x",
            last_quantity=SimpleNamespace(amount="bad"),
            last_price=SimpleNamespace(amount="100"),
        )
    )
    engine._filled_quantities_by_order["o4"] = 1.0
    engine._lite_applied_qty = {"o4": 0.0}
    engine._record_trade_lite_fill(
        SimpleNamespace(
            order_id="o4",
            symbol="BTCUSDT",
            side=SimpleNamespace(value="BUY"),
            trade_id="x",
            last_quantity=SimpleNamespace(amount="0.1"),
            last_price=SimpleNamespace(amount="100"),
        )
    )
    engine._order_has_committed_cumulative_fill = lambda _oid: True
    engine._record_trade_lite_fill(
        SimpleNamespace(
            order_id="o5",
            symbol="BTCUSDT",
            side=SimpleNamespace(value="BUY"),
            trade_id="x",
            last_quantity=SimpleNamespace(amount="0.1"),
            last_price=SimpleNamespace(amount="100"),
        )
    )
    engine._order_has_committed_cumulative_fill = lambda _oid: False
    engine._post_ledger_transaction = lambda _tx: (_ for _ in ()).throw(RuntimeError("ledger"))
    with pytest.raises(RuntimeError):
        engine._record_trade_lite_fill(
            SimpleNamespace(
                order_id="o6",
                symbol="BTCUSDT",
                side=SimpleNamespace(value="BUY"),
                trade_id="x",
                last_quantity=SimpleNamespace(amount="0.1"),
                last_price=SimpleNamespace(amount="100"),
            )
        )

    engine._store = store
    engine._filled_quantities_by_order = {}
    engine._pending_fill_retry = set()
    assert (
        engine._consume_cumulative_fill("o7", "BTCUSDT", {"executedQty": "0", "avgPrice": "100"}, status="FILLED")[0]
        == 0
    )
    delta, price, event_id = engine._consume_cumulative_fill(
        "o7", "BTCUSDT", {"executedQty": "1", "avgPrice": "100", "side": "BUY", "tradeId": "7"}, status="FILLED"
    )
    assert delta == 1 and price == 100 and event_id.startswith("trade:")
    assert (
        engine._consume_cumulative_fill(
            "o7", "BTCUSDT", {"executedQty": "1", "avgPrice": "100", "side": "BUY", "tradeId": "7"}, status="FILLED"
        )[0]
        == 0
    )
    pending_id = "trade:o8:8"
    store.events[pending_id] = {
        "order_id": "o8",
        "fill_event_id": pending_id,
        "processing_state": "PENDING",
        "delta_qty": "0",
        "price": "0",
    }
    assert (
        engine._consume_cumulative_fill(
            "o8", "BTCUSDT", {"executedQty": "1", "avgPrice": "100", "tradeId": "8"}, status="FILLED"
        )[0]
        == 0
    )
    engine._pending_fill_retry.add(pending_id)
    assert (
        engine._consume_cumulative_fill(
            "o8", "BTCUSDT", {"executedQty": "1", "avgPrice": "100", "tradeId": "8"}, status="FILLED"
        )[0]
        == 0
    )
    engine._store = SimpleNamespace(get_fill_event=lambda _id: None, save_fill_event=lambda *_args: False)
    engine._filled_quantities_by_order = {}
    assert (
        engine._consume_cumulative_fill(
            "o9", "BTCUSDT", {"executedQty": "1", "avgPrice": "100", "tradeId": "9"}, status="FILLED"
        )[0]
        == 0
    )

    engine._store = store
    engine._filled_quantities_by_order = {"p": 1.0}
    engine._position_projection = {
        "BTCUSDT": {"signed_quantity": "bad", "entry_price": "bad", "position_generation": 2}
    }
    engine._position_generation = {"BTCUSDT": 3}
    engine._update_position_projection("BTCUSDT", "BUY", 1.0, 100.0, "source")
    engine._update_position_projection("BTCUSDT", "SELL", 3.0, 90.0, "reverse")
    engine._update_position_projection("BTCUSDT", "BUY", 0.0, 100.0, "ignored")
    assert engine._position_generation["BTCUSDT"] >= 3


def test_engine_commit_fill_facts_and_terminal_processor_failure_paths() -> None:
    engine = _engine()
    store = _FillStore()
    engine._store = store
    engine._position_projection = {}
    engine._position_generation = {}
    engine._post_ledger_transaction = lambda _tx: None
    engine._update_position_projection = lambda *_args: None
    engine._mark_fill_committed = lambda *_args: None
    engine._commit_fill_facts(
        "o1",
        "BTCUSDT",
        "SELL",
        1.0,
        100.0,
        "trade:o1:1",
        {"executedQty": "1", "side": "SELL", "type": "MARKET", "origQty": "1"},
    )
    assert store.ledger_entries and store.orders
    store.fail_secondary = True
    engine._record_execution_fact_failure_env_guarded = lambda *_args, **_kwargs: None
    with pytest.raises(RuntimeError):
        engine._commit_fill_facts("o2", "BTCUSDT", "BUY", 1.0, 100.0, "trade:o2:1", {"executedQty": "1"})

    class Tracker:
        def __init__(self):
            self.events = []

        def apply(self, event):
            self.events.append(event)

    engine._store = store
    engine._order_trackers = {"o3": Tracker()}
    engine._order_symbols = {"o3": "BTCUSDT"}
    engine._active_order_ids = {"o3"}
    engine._close_order_ids = set()
    engine._mark_order_unknown = lambda *args: setattr(engine, "unknown", args)
    asyncio.run(engine._process_fill("missing", "BTCUSDT", {"executedQty": "1", "avgPrice": "100"}))
    asyncio.run(engine._process_fill("o3", "BTCUSDT", {"executedQty": "bad", "avgPrice": "100"}))
    assert engine.unknown[2] == "FILLED_EXECUTION_FACTS_INCOMPLETE"

    engine._consume_cumulative_fill = lambda *_args, **_kwargs: (1.0, 100.0, "fill-o3")
    engine._commit_fill_facts = lambda *_args: None
    engine._project_order_terminal = lambda *_args: None
    engine._ensure_entry_protection = lambda *_args, **_kwargs: asyncio.sleep(0)
    engine._last_account = {"totalWalletBalance": "1000"}
    engine._peak_equity = 0.0
    engine._strategy_risk = SimpleNamespace(update_equity=lambda *_args: None)
    engine._autopilot_strategy_id = "auto"
    engine._process_fill = AutonomousEngine._process_fill.__get__(engine)
    asyncio.run(
        engine._process_fill(
            "o3", "BTCUSDT", {"executedQty": "1", "avgPrice": "100", "side": "BUY", "type": "MARKET", "origQty": "1"}
        )
    )
    assert "o3" not in engine._order_trackers
