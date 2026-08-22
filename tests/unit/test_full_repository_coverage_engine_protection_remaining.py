"""Exhaustive protection-recovery boundary tests for the engine."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from beidou_control.plane import ControlAction
from beidou_core.engine import AutonomousEngine
from beidou_safety.protection.engine import ProtectionStatus
from beidou_shared.types import OrderSide


class _Store:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.saved: list[dict] = []
        self.removed: list[str] = []
        self.fail_save = False
        self.restore_calls = 0
        self.fail_restore_after: int | None = None

    def restore_protections(self):
        self.restore_calls += 1
        if self.fail_restore_after is not None and self.restore_calls > self.fail_restore_after:
            raise RuntimeError("restore failed")
        return list(self.rows)

    def save_protection(self, **kwargs):
        if self.fail_save:
            raise RuntimeError("save failed")
        self.saved.append(kwargs)

    def remove_protection(self, position_id: str):
        self.removed.append(position_id)
        self.rows = [row for row in self.rows if str(row.get("position_id")) != position_id]


def _row(
    *,
    protection_id: str = "sl",
    position_id: str = "pos",
    symbol: str = "BTCUSDT",
    side: str = "SELL",
    order_type: str = "STOP_MARKET",
    trigger: str = "90",
    quantity: str = "1",
    status: str = "ACTIVE",
    stop_type: str | None = "ATR_BASED",
    take_profit_type: str | None = None,
    exchange_order_id: str = "sl",
    owner_id: str = "svc",
    generation: int = 1,
    session_id: str = "session",
) -> dict:
    return {
        "protection_id": protection_id,
        "position_id": position_id,
        "symbol": symbol,
        "side": side,
        "trigger_price": trigger,
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


def _engine(rows: list[dict]) -> AutonomousEngine:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._store = _Store(rows)
    engine._protection_owner_id = "svc"
    engine._session_id = "session"
    engine._active_algo_ids = {}
    engine._position_generation = {"BTCUSDT": 1}
    engine._position_projection = {}
    engine._position_entry_times = {}
    engine._pending_stop_intent = {}
    engine._protection_issues = set()
    engine._protection_owner_unknown = False
    engine._protection = SimpleNamespace(
        all_positions=lambda: {},
        restore_position_protection=lambda _projection: None,
    )
    engine._block_calls: list[list[str]] = []
    engine._cancel_calls: list[tuple[str, str]] = []
    engine._block_unowned_protection_orders = lambda ids: engine._block_calls.append(list(ids))
    engine._cancel_stale_protection_rows = lambda symbol, _rows, reason: engine._cancel_calls.append((symbol, reason))
    engine._protection_inventory_semantic_issues = lambda _inventory: []
    engine._protection_row_fresh = lambda _algo_id: False
    return engine


def _account(*positions: dict) -> dict:
    return {"positions": list(positions)}


def _startup_engine(
    account: dict,
    inventories: list[object],
    *,
    local_positions: dict | None = None,
    active: dict[str, set[str]] | None = None,
    genuine: bool = False,
    control: ControlAction = ControlAction.RESUME,
    store_rows: list[dict] | None = None,
) -> AutonomousEngine:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._control = SimpleNamespace(get_status=lambda: control)
    engine._last_account = account
    engine._protection_owner_id = "svc"
    engine._active_algo_ids = active or {}
    engine._last_algo_inventory_genuine = genuine
    positions = local_positions or {}
    engine._protection = SimpleNamespace(all_positions=lambda: positions)
    engine._adapter = SimpleNamespace(
        reset_circuit_breaker=lambda: None,
        cancel_algo_order=lambda *_args: asyncio.sleep(0),
    )
    responses = list(inventories)

    async def inventory() -> object:
        return responses.pop(0) if responses else inventories[-1]

    engine._get_open_algo_inventory = inventory
    engine._block_calls: list[list[str]] = []
    engine._block_unowned_protection_orders = lambda ids: engine._block_calls.append(list(ids))
    engine._protection_inventory_semantic_issues = lambda _inventory: []
    engine._adopt_orphaned_protection_algos = lambda _inventory: (0, [])
    engine._update_protection_fact = lambda **_kwargs: None
    engine._store = SimpleNamespace(restore_protections=lambda: list(store_rows or []))
    return engine


@pytest.mark.asyncio
async def test_startup_exchange_protection_inventory_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("beidou_core.engine.asyncio.sleep", no_sleep)

    # Control state is reported before the read-only startup return; an empty
    # account and an empty position list are both legitimate no-op states.
    locked = _startup_engine({}, [], control=ControlAction.LOCK)
    await locked._ensure_exchange_position_protections()
    no_positions = _startup_engine(_account(), [])
    await no_positions._ensure_exchange_position_protections()

    unknown = _startup_engine(_account({"symbol": "BTCUSDT", "positionAmt": "1"}), [None])
    await unknown._ensure_exchange_position_protections()
    assert unknown._block_calls == [["OPEN_ALGO_ORDERS_UNKNOWN"]]

    foreign_cleanup = _startup_engine(
        _account({"symbol": "BTCUSDT", "positionAmt": "1"}),
        [
            [
                {"algoId": "123", "clientAlgoId": "beidou-stale", "symbol": "BTCUSDT"},
                {"algoId": "456", "clientAlgoId": "other-service", "symbol": "ETHUSDT"},
            ],
            [],
        ],
    )
    await foreign_cleanup._ensure_exchange_position_protections()

    # Cleanup can fail to refresh, or can refresh to another owned namespace
    # row that remains ambiguous; both states stay blocked.
    refresh_unknown = _startup_engine(
        _account({"symbol": "BTCUSDT", "positionAmt": "1"}),
        [[{"algoId": "stale-1", "clientAlgoId": "beidou-stale", "symbol": "BTCUSDT"}], None],
    )
    await refresh_unknown._ensure_exchange_position_protections()
    assert refresh_unknown._block_calls == [["OPEN_ALGO_ORDERS_UNKNOWN"]]

    remains_unknown = _startup_engine(
        _account({"symbol": "BTCUSDT", "positionAmt": "1"}),
        [
            [{"algoId": "stale-2", "clientAlgoId": "beidou-stale", "symbol": "BTCUSDT"}],
            [{"algoId": "stale-2", "clientAlgoId": "beidou-stale", "symbol": "BTCUSDT"}],
        ],
    )
    await remains_unknown._ensure_exchange_position_protections()
    assert remains_unknown._block_calls == [["stale-2"]]

    # A locally owned ID absent from the first and second venue snapshots is
    # offered one adoption pass, then converted to an explicit missing-ID
    # block if ownership still cannot be proven.
    adoption = _startup_engine(
        _account({"symbol": "BTCUSDT", "positionAmt": "1"}),
        [[{"algoId": "foreign", "symbol": "BTCUSDT"}], [{"algoId": "foreign", "symbol": "BTCUSDT"}]],
        active={"pos": {"owned-1"}},
    )
    adoption._adopt_orphaned_protection_algos = lambda _inventory: (1, ["ADOPTION_AMBIGUOUS"])
    await adoption._ensure_exchange_position_protections()
    assert adoption._block_calls[-1] == ["OWNED_PROTECTION_MISSING:owned-1"]

    semantic = _startup_engine(
        _account({"symbol": "BTCUSDT", "positionAmt": "1"}),
        [[{"algoId": "owned-1", "symbol": "BTCUSDT"}]],
        active={"pos": {"owned-1"}},
    )
    semantic._protection_inventory_semantic_issues = lambda _inventory: ["PROTECTION_SIDE_MISMATCH:owned-1"]
    await semantic._ensure_exchange_position_protections()
    assert semantic._block_calls == [["PROTECTION_SIDE_MISMATCH:owned-1"]]

    genuine = _startup_engine(
        _account({"symbol": "BTCUSDT", "positionAmt": "1"}),
        [[{"algoId": "adopted-1", "symbol": "BTCUSDT"}]],
        genuine=True,
        store_rows=[{"status": "PENDING", "symbol": "BTCUSDT"}],
    )
    genuine._adopt_orphaned_protection_algos = lambda _inventory: (
        genuine._active_algo_ids.update({"pos": {"adopted-1"}}) or (1, ["ADOPTION_NOTE"])
    )
    await genuine._ensure_exchange_position_protections()
    assert genuine._block_calls == [["ADOPTION_NOTE"]]

    unprotected = _startup_engine(
        _account({"symbol": "BTCUSDT", "positionAmt": "1"}),
        [[{"algoId": "owned-2", "symbol": "ETHUSDT"}]],
        active={"pos": {"owned-2"}},
        control=ControlAction.LOCK,
    )
    await unprotected._ensure_exchange_position_protections()
    assert unprotected._block_calls == [["UNPROTECTED_POSITION_RECOVERY_REQUIRED:BTCUSDT"]]

    handled = _startup_engine(
        _account({"symbol": "BTCUSDT", "positionAmt": "1"}),
        [[{"algoId": "owned-3", "symbol": "BTCUSDT"}]],
        local_positions={"pos": SimpleNamespace(instrument_id="BTCUSDT")},
        active={"pos": {"owned-3"}},
    )
    await handled._ensure_exchange_position_protections()
    assert handled._block_calls == []

    outer_error = _startup_engine({"positions": [None]}, [])
    await outer_error._ensure_exchange_position_protections()


def _cleanup_engine(
    inventory: object,
    *,
    active: dict[str, set[str]] | None = None,
    durable_rows: list[dict] | None = None,
    positions: list[dict] | None = None,
    local_positions: dict | None = None,
) -> AutonomousEngine:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._control = SimpleNamespace(get_status=lambda: ControlAction.RESUME)
    engine._fresh_matched_reconciliation = lambda: True
    engine._last_algo_inventory_genuine = False
    engine._active_algo_ids = active or {}
    engine._last_account = {"positions": positions or []}
    engine._protection_owner_id = "svc"
    engine._protection = SimpleNamespace(all_positions=lambda: local_positions or {})
    store = _Store(durable_rows or [])
    engine._store = store
    engine._get_open_algo_inventory = lambda: asyncio.sleep(0, result=inventory)
    engine._protection_inventory_semantic_issues = lambda _inventory: []
    engine._adopt_orphaned_protection_algos = lambda _inventory: (0, [])
    engine._block_calls = []
    engine._block_unowned_protection_orders = lambda ids: engine._block_calls.append(list(ids))
    engine._fact_updates: list[dict] = []
    engine._update_protection_fact = lambda **kwargs: engine._fact_updates.append(kwargs)
    engine._ghost_absence_count = {}
    engine._cancel_calls: list[tuple[str, int]] = []

    async def cancel(symbol: str, algo_id: int) -> dict:
        engine._cancel_calls.append((symbol, algo_id))
        return {}

    engine._cancel_algo_order = cancel
    return engine


def _retry_shell(inventory: object, *, genuine: bool = False) -> AutonomousEngine:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._can_write = True
    engine._control = SimpleNamespace(get_status=lambda: ControlAction.RESUME)
    engine._flush_pending_protection_persist = lambda: None
    engine._last_retry_diag = 10**12
    engine._last_algo_inventory_genuine = genuine
    engine._protection_owner_id = "svc"
    engine._active_algo_ids = {}
    engine._position_projection = {}
    engine._position_generation = {}
    engine._symbol_precision = {}
    engine._protection_retries = {}
    engine._pending_protection_retry = set()
    engine._venue_missing_streaks = {}
    engine._last_account = {"positions": []}
    engine._protection = SimpleNamespace(all_positions=lambda: {})
    engine._store = SimpleNamespace(restore_protections=lambda: [])
    engine._get_open_algo_inventory = lambda: asyncio.sleep(0, result=inventory)
    engine._protection_inventory_semantic_issues = lambda _inventory: []
    engine._protection_row_fresh = lambda _algo_id: False
    engine._venue_position_gone = lambda _symbol: False
    engine._dedup_ghost_protection_positions = lambda: 0
    engine._update_protection_fact = lambda **_kwargs: None
    engine._block_calls: list[list[str]] = []
    engine._block_unowned_protection_orders = lambda ids: engine._block_calls.append(list(ids))
    return engine


@pytest.mark.asyncio
async def test_retry_missing_protections_gate_inventory_and_stale_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    locked = _retry_shell([])
    locked._control = SimpleNamespace(get_status=lambda: ControlAction.LOCK)
    await locked._retry_missing_protections(set())

    unknown = _retry_shell(None)
    await unknown._retry_missing_protections(set())
    assert unknown._block_calls == [["OPEN_ALGO_ORDERS_UNKNOWN"]]

    semantic = _retry_shell([{"algoId": "1", "symbol": "BTCUSDT"}])
    semantic._protection_inventory_semantic_issues = lambda _inventory: ["PROTECTION_TRIGGER_MISMATCH:1"]
    await semantic._retry_missing_protections(set())
    assert semantic._block_calls == [["PROTECTION_TRIGGER_MISMATCH:1"]]

    adopted = _retry_shell([{"algoId": "1", "clientAlgoId": "beidou-1", "symbol": "BTCUSDT"}], genuine=True)
    adopted._adopt_orphaned_protection_algos = lambda _inventory: (1, ["ADOPTION_WARNING"])
    await adopted._retry_missing_protections(set())
    assert adopted._block_calls == [["1"]]

    durable_read_error = _retry_shell([], genuine=True)
    durable_read_error._store = SimpleNamespace(restore_protections=lambda: (_ for _ in ()).throw(OSError("read")))
    await durable_read_error._retry_missing_protections(set())

    class MissingStore(_Store):
        def remove_protection(self, position_id: str) -> None:
            super().remove_protection(position_id)

    pp = SimpleNamespace(
        instrument_id="BTCUSDT",
        quantity=1.0,
        entry_price=100.0,
        side=SimpleNamespace(value="BUY"),
        stop_loss=SimpleNamespace(exchange_order_id="old", status=SimpleNamespace(value="ACTIVE")),
        take_profits=[None],
    )
    stale = _retry_shell([], genuine=True)
    stale._last_account = {"positions": []}
    stale._store = MissingStore([_row(position_id="pos", exchange_order_id="old")])
    stale._active_algo_ids = {"pos": {"old"}}
    stale._protection = SimpleNamespace(
        all_positions=lambda: {"pos": pp},
        get_protection=lambda _pos_id: pp,
    )
    stale._protection_inventory_semantic_issues = lambda _inventory: ["PROTECTION_VENUE_ROW_MISSING:old"]
    stale._venue_missing_streaks = {"other": 1}
    for _ in range(3):
        await stale._retry_missing_protections(set())
    assert stale._venue_missing_streaks == {}
    assert stale._store.removed == ["pos"]
    assert pp.stop_loss.exchange_order_id is None

    remove_error = _retry_shell([], genuine=True)
    remove_error._store = MissingStore([_row(position_id="pos", exchange_order_id="old")])
    remove_error._store.remove_protection = lambda _pos_id: (_ for _ in ()).throw(OSError("remove"))
    remove_error._protection_inventory_semantic_issues = lambda _inventory: ["PROTECTION_VENUE_ROW_MISSING:old"]
    remove_error._venue_missing_streaks = {"old": 2}
    await remove_error._retry_missing_protections(set())

    restore_error, restore_pp, _restore_tp = _active_retry_engine(entry_price=100.0, tp_status="CANCELED")
    restore_pp.stop_loss.quantity = SimpleNamespace(amount="bad")
    restore_error._store = SimpleNamespace(
        restore_protections=lambda: (_ for _ in ()).throw(OSError("restore")),
    )
    await restore_error._retry_missing_protections({"BTCUSDT"})


@pytest.mark.asyncio
async def test_retry_projection_rebuild_handles_divergence_and_malformed_facts() -> None:
    malformed = _retry_shell(
        [{"algoId": "foreign", "clientAlgoId": "other", "symbol": "BTCUSDT"}],
        genuine=True,
    )
    malformed._last_account = {
        "positions": [
            {"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"},
            {"symbol": "ETHUSDT", "positionAmt": "bad", "entryPrice": "100"},
            {"symbol": "LTCUSDT", "positionAmt": "1", "entryPrice": "100"},
            None,
        ]
    }
    malformed._position_projection = {"BTCUSDT": {"signed_quantity": "bad", "entry_price": "bad"}}
    malformed._protection_owner_id = "svc"
    malformed._store = SimpleNamespace(
        restore_protections=lambda: [
            {"symbol": "LTCUSDT", "status": "ACTIVE", "owner_id": "svc", "exchange_order_id": "ltc-1"}
        ]
    )
    await malformed._retry_missing_protections({"BTCUSDT", "ETHUSDT", "LTCUSDT"})

    pp = SimpleNamespace(
        instrument_id="BTCUSDT",
        quantity=1.0,
        side=OrderSide.SELL,
        entry_price=100.0,
        stop_loss=SimpleNamespace(status=SimpleNamespace(value="ACTIVE")),
        take_profits=[],
    )
    diverged = _retry_shell(
        [{"algoId": "foreign", "clientAlgoId": "other", "symbol": "BTCUSDT"}],
        genuine=True,
    )
    diverged._last_account = {"positions": [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "bad"}]}
    diverged._position_projection = {"BTCUSDT": {"signed_quantity": "-1", "entry_price": "100"}}
    diverged._position_generation = {"BTCUSDT": 2}
    saved_projection: list[tuple] = []
    diverged._store = SimpleNamespace(
        restore_protections=lambda: [],
        save_position_projection=lambda *args: saved_projection.append(args),
    )
    calls: list[tuple] = []
    diverged._protection = SimpleNamespace(
        all_positions=lambda: {"pos": pp},
    )
    diverged._ensure_entry_protection = lambda *args, **kwargs: asyncio.sleep(0, result=calls.append((args, kwargs)))
    await diverged._retry_missing_protections({"BTCUSDT"})
    assert saved_projection and calls

    save_error = _retry_shell(
        [{"algoId": "foreign", "clientAlgoId": "other", "symbol": "BTCUSDT"}],
        genuine=True,
    )
    save_error._last_account = {"positions": [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}]}
    save_error._position_projection = {"BTCUSDT": {"signed_quantity": "-1", "entry_price": "100"}}
    save_error._store = SimpleNamespace(
        restore_protections=lambda: [],
        save_position_projection=lambda *_args: (_ for _ in ()).throw(OSError("persist")),
    )
    save_error._protection = SimpleNamespace(all_positions=lambda: {"pos": pp})
    save_error._ensure_entry_protection = lambda *_args, **_kwargs: asyncio.sleep(0)
    await save_error._retry_missing_protections({"BTCUSDT"})


def _active_retry_engine(
    *,
    entry_price: float = 100.0,
    stop_status: str = "ACTIVE",
    stop_id: str | None = None,
    tp_status: str = "ACTIVE",
    tp_id: str | None = None,
    inventory: list[dict] | None = None,
    genuine: bool = True,
    durable_rows: list[dict] | None = None,
) -> tuple[AutonomousEngine, SimpleNamespace, SimpleNamespace]:
    engine = _retry_shell(inventory or [], genuine=genuine)
    stop = SimpleNamespace(
        protection_id="sl-pos",
        position_id="pos",
        quantity=SimpleNamespace(amount="1"),
        trigger_price=SimpleNamespace(amount="90"),
        order_type="STOP_MARKET",
        status=SimpleNamespace(value=stop_status),
        stop_type=None,
        exchange_order_id=stop_id,
        side=OrderSide.SELL,
        owner_id="svc",
        position_generation=1,
    )
    tp = SimpleNamespace(
        protection_id="tp-pos",
        position_id="pos",
        quantity=SimpleNamespace(amount="1"),
        trigger_price=SimpleNamespace(amount="110"),
        order_type="TAKE_PROFIT_MARKET",
        status=SimpleNamespace(value=tp_status),
        stop_type=None,
        exchange_order_id=tp_id,
        side=OrderSide.SELL,
        owner_id="svc",
        position_generation=1,
    )
    pp = SimpleNamespace(
        instrument_id="BTCUSDT",
        quantity=1.0,
        side=OrderSide.BUY,
        entry_price=entry_price,
        position_generation=1,
        stop_loss=stop,
        take_profits=[tp],
    )
    engine._last_account = {"positions": [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": str(entry_price)}]}
    engine._protection = SimpleNamespace(all_positions=lambda: {"pos": pp})
    engine._store = SimpleNamespace(restore_protections=lambda: list(durable_rows or []))
    engine._active_algo_ids = {"pos": set(filter(None, (stop_id, tp_id)))}
    engine._symbol_precision = {}
    engine._protection_retries = {}
    engine._pending_stop_intent = {}
    engine._pending_protection_persist = {}
    engine._protection_algo_params = lambda *_args, **_kwargs: {}
    engine._persist_protection_order = lambda *_args, **_kwargs: None
    return engine, pp, tp


@pytest.mark.asyncio
async def test_retry_sl_precision_retry_limits_and_tp_coverage() -> None:
    for index, entry in enumerate((10000.0, 100.0, 0.5)):
        engine, _pp, _tp = _active_retry_engine(entry_price=entry, tp_status="CANCELED")
        engine._create_algo_order = lambda *_args, index=index, **_kwargs: asyncio.sleep(
            0, result={"algoId": f"sl-{index}"}
        )
        await engine._retry_missing_protections({"BTCUSDT"})

    reset, _pp, _tp = _active_retry_engine(entry_price=100.0, tp_status="CANCELED")
    reset._protection_retries = {"pos": 10, "pos_last": 0}
    reset._create_algo_order = lambda *_args, **_kwargs: asyncio.sleep(0, result={"algoId": "sl-reset"})
    await reset._retry_missing_protections({"BTCUSDT"})

    limited, _pp, _tp = _active_retry_engine(entry_price=100.0, tp_status="CANCELED")
    limited._protection_retries = {"pos": 10, "pos_last": 10**20}
    limited._create_algo_order = lambda *_args, **_kwargs: asyncio.sleep(0, result={"algoId": "sl-limited"})
    await limited._retry_missing_protections({"BTCUSDT"})

    tp_skip, _pp, _tp = _active_retry_engine(
        entry_price=100.0,
        stop_id="known-sl",
        tp_id="known-tp",
        inventory=[{"algoId": "known-tp", "symbol": "BTCUSDT", "clientAlgoId": "bdp-tp"}],
    )
    tp_skip._create_algo_order = lambda *_args, **_kwargs: asyncio.sleep(0, result={"algoId": "new-sl"})
    await tp_skip._retry_missing_protections({"BTCUSDT"})

    durable_tp, pp, tp = _active_retry_engine(
        entry_price=100.0,
        stop_id="durable-sl",
        tp_id=None,
        inventory=[],
        durable_rows=[
            {"protection_id": "sl-pos", "status": "ACTIVE", "owner_id": "svc", "exchange_order_id": "durable-sl"},
            {"protection_id": "tp-pos", "status": "ACTIVE", "owner_id": "svc", "exchange_order_id": "durable-tp"},
        ],
    )
    pp.stop_loss.exchange_order_id = None
    tp.exchange_order_id = None
    durable_tp._create_algo_order = lambda *_args, **_kwargs: asyncio.sleep(0, result={"algoId": "should-not-place"})
    await durable_tp._retry_missing_protections({"BTCUSDT"})

    bad_quantity, pp, _tp = _active_retry_engine(entry_price=100.0, tp_status="CANCELED")
    pp.stop_loss.quantity = SimpleNamespace(amount="bad")
    bad_quantity._create_algo_order = lambda *_args, **_kwargs: asyncio.sleep(0, result={"algoId": "bad-qty"})
    await bad_quantity._retry_missing_protections({"BTCUSDT"})

    durable_bad_quantity, pp, _tp = _active_retry_engine(entry_price=100.0, tp_status="CANCELED")
    pp.stop_loss = None
    durable_bad_quantity._store = SimpleNamespace(
        restore_protections=lambda: [
            {
                "symbol": "BTCUSDT",
                "status": "ACTIVE",
                "owner_id": "svc",
                "order_type": "STOP_MARKET",
                "quantity": "bad",
                "exchange_order_id": "old",
            }
        ]
    )
    durable_bad_quantity._cancel_stale_protection_rows = lambda *_args: None
    durable_bad_quantity._cancel_stale_protection_algos = lambda: asyncio.sleep(0, result=set())
    durable_bad_quantity._feed = SimpleNamespace(async_get_kline_features=lambda _symbol: asyncio.sleep(0, result={}))
    durable_bad_quantity._position_projection = {}
    await durable_bad_quantity._retry_missing_protections({"BTCUSDT"})

    durable_read_failure, pp, _tp = _active_retry_engine(entry_price=100.0, tp_status="CANCELED")
    pp.stop_loss = None
    restore_calls = 0

    def restore_then_fail() -> list[dict]:
        nonlocal restore_calls
        restore_calls += 1
        if restore_calls == 1:
            return [
                {
                    "symbol": "BTCUSDT",
                    "status": "ACTIVE",
                    "owner_id": "svc",
                    "order_type": "STOP_MARKET",
                    "quantity": "bad",
                    "exchange_order_id": "old",
                }
            ]
        raise OSError("restore")

    durable_read_failure._store = SimpleNamespace(restore_protections=restore_then_fail)
    durable_read_failure._cancel_stale_protection_rows = lambda *_args: None
    durable_read_failure._cancel_stale_protection_algos = lambda: asyncio.sleep(0, result=set())
    durable_read_failure._feed = SimpleNamespace(async_get_kline_features=lambda _symbol: asyncio.sleep(0, result={}))
    await durable_read_failure._retry_missing_protections({"BTCUSDT"})

    s41, pp, tp = _active_retry_engine(
        entry_price=100.0,
        stop_id="sl-existing",
        tp_id="tp-existing",
        inventory=[
            {"algoId": "sl-existing", "symbol": "BTCUSDT", "clientAlgoId": "bdp-sl"},
            {"algoId": "tp-existing", "symbol": "BTCUSDT", "clientAlgoId": "bdp-tp"},
        ],
    )
    pp.stop_loss.is_active = lambda: True
    tp2 = SimpleNamespace(
        protection_id="tp-2",
        quantity=SimpleNamespace(amount="1"),
        trigger_price=SimpleNamespace(amount="120"),
        order_type="TAKE_PROFIT_MARKET",
        status=SimpleNamespace(value="ACTIVE"),
        exchange_order_id=None,
    )
    tp3 = SimpleNamespace(
        protection_id="tp-3",
        quantity=SimpleNamespace(amount="1"),
        trigger_price=SimpleNamespace(amount="130"),
        order_type="TAKE_PROFIT_MARKET",
        status=SimpleNamespace(value="ACTIVE"),
        exchange_order_id=None,
    )
    pp.take_profits = [tp, tp2, tp3]
    s41._create_algo_order = lambda *_args, **_kwargs: asyncio.sleep(0, result={"algoId": "unused"})
    await s41._retry_missing_protections({"BTCUSDT"})

    zero_entry, pp, _tp = _active_retry_engine(entry_price=0.0, tp_status="CANCELED")
    pp.stop_loss = None
    pp.take_profits = []
    zero_entry._feed = SimpleNamespace(async_get_kline_features=lambda _symbol: asyncio.sleep(0, result={}))
    await zero_entry._retry_missing_protections({"BTCUSDT"})

    zero_sweep = _retry_shell([], genuine=True)
    zero_sweep._last_account = {"positions": []}
    zero_sweep._position_projection = {"ZEROUSDT": {"signed_quantity": "0", "entry_price": "0"}}
    await zero_sweep._retry_missing_protections({"ZEROUSDT"})


@pytest.mark.asyncio
async def test_retry_s33_creation_blocked_invalid_and_persistence_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    from beidou_safety.protection.engine import StopLossCalculator
    from beidou_strategy.protection.adaptive import AdaptiveProtectionCalculator

    blocked_cfg = SimpleNamespace(
        stop_pct=0.0,
        metadata={"blocked": True, "reason": "NO_MARKET_DATA"},
        stop_loss_config={},
        take_profit_config={},
    )
    monkeypatch.setattr(AdaptiveProtectionCalculator, "calculate", staticmethod(lambda *_args, **_kwargs: blocked_cfg))
    blocked, pp, _tp = _active_retry_engine(entry_price=200.0, tp_status="CANCELED")
    pp.stop_loss = None
    pp.take_profits = []
    blocked._feed = SimpleNamespace(async_get_kline_features=lambda _symbol: asyncio.sleep(0, result={}))
    blocked._maybe_emergency_close_unprotectable = lambda *_args: asyncio.sleep(0, result=False)
    await blocked._retry_missing_protections({"BTCUSDT"})

    valid_cfg = SimpleNamespace(
        stop_pct=1.0,
        metadata={},
        stop_loss_config={"type": "FIXED_PERCENT", "stop_pct": 1.0},
        take_profit_config={},
    )
    monkeypatch.setattr(AdaptiveProtectionCalculator, "calculate", staticmethod(lambda *_args, **_kwargs: valid_cfg))
    monkeypatch.setattr(StopLossCalculator, "calculate", staticmethod(lambda *_args, **_kwargs: 0.0))
    invalid, pp, _tp = _active_retry_engine(entry_price=200.0, tp_status="CANCELED")
    pp.stop_loss = None
    pp.take_profits = []
    invalid._feed = SimpleNamespace(async_get_kline_features=lambda _symbol: asyncio.sleep(0, result={}))
    await invalid._retry_missing_protections({"BTCUSDT"})

    def s33_engine(entry_price: float) -> tuple[AutonomousEngine, SimpleNamespace]:
        engine, position, _tp = _active_retry_engine(entry_price=entry_price, tp_status="CANCELED")
        position.stop_loss = None
        position.take_profits = []
        engine._feed = SimpleNamespace(async_get_kline_features=lambda _symbol: asyncio.sleep(0, result={}))
        lookups = 0

        def all_positions() -> dict[str, object]:
            nonlocal lookups
            lookups += 1
            return {"pos": position} if lookups <= 2 else {}

        def create_protection(**_kwargs: object) -> None:
            position.stop_loss = SimpleNamespace(
                protection_id="sl-s33",
                position_id="pos",
                quantity=SimpleNamespace(amount="1"),
                trigger_price=SimpleNamespace(amount="90"),
                order_type="STOP_MARKET",
                status=ProtectionStatus.CREATED,
                stop_type=SimpleNamespace(value="ATR_BASED"),
                exchange_order_id=None,
                side=OrderSide.SELL,
            )

        engine._protection = SimpleNamespace(all_positions=all_positions, create_protection=create_protection)
        engine._create_algo_order = lambda *_args, **_kwargs: asyncio.sleep(0, result={"algoId": "s33-ok"})
        return engine, position

    for entry_price in (10000.0, 1000.0, 100.0, 0.5):
        precision_engine, _position = s33_engine(entry_price)
        await precision_engine._retry_missing_protections({"BTCUSDT"})

    immediate_create, _position = s33_engine(100.0)
    immediate_create._create_algo_order = lambda *_args, **_kwargs: asyncio.sleep(
        0, result={"msg": "would immediately trigger"}
    )
    immediate_create._maybe_emergency_close_unprotectable = lambda *_args: asyncio.sleep(0, result=False)
    await immediate_create._retry_missing_protections({"BTCUSDT"})

    create_exception, _position = s33_engine(100.0)
    create_exception._create_algo_order = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("venue"))
    await create_exception._retry_missing_protections({"BTCUSDT"})

    outer_creation_error, _position = s33_engine(100.0)
    outer_creation_error._protection.create_protection = lambda **_kwargs: (_ for _ in ()).throw(OSError("create"))
    await outer_creation_error._retry_missing_protections({"BTCUSDT"})

    # The projection exists in the position list, but the second ownership
    # lookup reports it absent; S33 therefore uses the governed create path.
    monkeypatch.setattr(StopLossCalculator, "calculate", staticmethod(lambda *_args, **_kwargs: 90.0))
    created, pp, _tp = _active_retry_engine(entry_price=100.0, tp_status="CANCELED")
    pp.stop_loss = None
    pp.take_profits = []
    created._feed = SimpleNamespace(async_get_kline_features=lambda _symbol: asyncio.sleep(0, result={}))
    created._pending_protection_persist = {}
    created._pending_stop_intent = {"pos": "intent"}
    lookup_calls = 0

    def changing_positions() -> dict[str, object]:
        nonlocal lookup_calls
        lookup_calls += 1
        return {"pos": pp} if lookup_calls <= 2 else {}

    created._protection = SimpleNamespace(
        all_positions=changing_positions,
        create_protection=lambda **_kwargs: setattr(
            pp,
            "stop_loss",
            SimpleNamespace(
                protection_id="sl-created",
                position_id="pos",
                quantity=SimpleNamespace(amount="1"),
                trigger_price=SimpleNamespace(amount="90"),
                order_type="STOP_MARKET",
                status=ProtectionStatus.CREATED,
                stop_type=None,
                exchange_order_id=None,
                side=OrderSide.SELL,
            ),
        ),
    )
    created._create_algo_order = lambda *_args, **_kwargs: asyncio.sleep(0, result={"algoId": "created-1"})
    created._persist_protection_order = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("persist"))
    saved_pending: list[dict] = []
    created._store = SimpleNamespace(
        restore_protections=lambda: [
            {
                "position_id": "pos",
                "protection_id": "old-pending",
                "symbol": "BTCUSDT",
                "side": "SELL",
                "trigger_price": "80",
                "quantity": "1",
                "order_type": "STOP_MARKET",
                "status": "PENDING",
                "stop_type": "ATR_BASED",
                "take_profit_type": None,
                "owner_id": "svc",
                "position_generation": 1,
                "session_id": "s",
                "exchange_order_id": None,
            }
        ],
        save_protection=lambda **kwargs: saved_pending.append(kwargs),
    )
    await created._retry_missing_protections({"BTCUSDT"})
    assert created._pending_protection_persist["pos"] and saved_pending

    created_none, pp, _tp = _active_retry_engine(entry_price=100.0, tp_status="CANCELED")
    pp.stop_loss = None
    pp.take_profits = [None]
    created_none._feed = SimpleNamespace(async_get_kline_features=lambda _symbol: asyncio.sleep(0, result={}))
    calls = 0

    def create_none_positions() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"pos": pp} if calls <= 2 else {}

    created_none._protection = SimpleNamespace(
        all_positions=create_none_positions,
        create_protection=lambda **_kwargs: None,
    )
    await created_none._retry_missing_protections({"BTCUSDT"})

    pending_discard_error, pp, _tp = _active_retry_engine(entry_price=100.0, tp_status="CANCELED")
    pp.stop_loss = None
    pp.take_profits = []
    pending_discard_error._feed = SimpleNamespace(async_get_kline_features=lambda _symbol: asyncio.sleep(0, result={}))
    call_count = 0

    def pending_positions() -> dict[str, object]:
        nonlocal call_count
        call_count += 1
        return {"pos": pp} if call_count <= 2 else {}

    pending_discard_error._protection = SimpleNamespace(
        all_positions=pending_positions,
        create_protection=lambda **_kwargs: setattr(
            pp,
            "stop_loss",
            SimpleNamespace(
                protection_id="sl-created",
                position_id="pos",
                quantity=SimpleNamespace(amount="1"),
                trigger_price=SimpleNamespace(amount="90"),
                order_type="STOP_MARKET",
                status=ProtectionStatus.CREATED,
                stop_type=None,
                exchange_order_id=None,
                side=OrderSide.SELL,
            ),
        ),
    )
    pending_discard_error._create_algo_order = lambda *_args, **_kwargs: asyncio.sleep(
        0, result={"algoId": "created-2"}
    )
    pending_discard_error._persist_protection_order = lambda *_args, **_kwargs: None
    pending_discard_error._store = SimpleNamespace(
        restore_protections=lambda: [
            {
                "position_id": "pos",
                "protection_id": "pending",
                "symbol": "BTCUSDT",
                "side": "SELL",
                "trigger_price": "80",
                "quantity": "1",
                "order_type": "STOP_MARKET",
                "status": "PENDING",
                "stop_type": "ATR_BASED",
                "take_profit_type": None,
                "owner_id": "svc",
                "position_generation": 1,
                "session_id": "s",
                "exchange_order_id": None,
            }
        ],
        save_protection=lambda **_kwargs: (_ for _ in ()).throw(OSError("discard")),
    )
    await pending_discard_error._retry_missing_protections({"BTCUSDT"})

    immediate, _pp, _tp = _active_retry_engine(entry_price=100.0, tp_status="CANCELED")
    immediate._create_algo_order = lambda *_args, **_kwargs: asyncio.sleep(
        0, result={"msg": "would immediately trigger"}
    )
    emergency_calls: list[tuple] = []
    immediate._maybe_emergency_close_unprotectable = lambda *args: asyncio.sleep(
        0, result=emergency_calls.append(args) or False
    )
    await immediate._retry_missing_protections({"BTCUSDT"})
    assert emergency_calls

    partial, _pp, _tp = _active_retry_engine(entry_price=100.0)
    responses = iter([{"algoId": "sl-partial"}, {"msg": "tp unavailable"}])
    partial._create_algo_order = lambda *_args, **_kwargs: asyncio.sleep(0, result=next(responses))
    await partial._retry_missing_protections({"BTCUSDT"})

    failed, _pp, _tp = _active_retry_engine(entry_price=100.0)
    failed._create_algo_order = lambda *_args, **_kwargs: asyncio.sleep(0, result={"msg": "unavailable"})
    await failed._retry_missing_protections({"BTCUSDT"})


@pytest.mark.asyncio
async def test_cleanup_excess_orders_uses_verified_inventory_and_debounces_orphans() -> None:
    assert await _cleanup_engine(None)._cleanup_excess_orders() is None

    blocked = _cleanup_engine(
        [{"algoId": "unknown", "clientAlgoId": "beidou-unknown", "symbol": "BTCUSDT"}],
    )
    await blocked._cleanup_excess_orders()
    assert blocked._block_calls == [["unknown"]]

    empty = _cleanup_engine([])
    await empty._cleanup_excess_orders()
    assert empty._fact_updates == []

    adoption = _cleanup_engine(
        [{"algoId": "1", "clientAlgoId": "bdp-1", "symbol": "BTCUSDT"}],
        active={"pos": {"1"}},
        durable_rows=[_row(exchange_order_id="1")],
        positions=[{"symbol": "BTCUSDT", "positionAmt": "1"}],
    )
    adoption._last_algo_inventory_genuine = True
    adoption._adopt_orphaned_protection_algos = lambda _inventory: (1, ["ADOPTION_WARNING"])
    await adoption._cleanup_excess_orders()
    assert adoption._block_calls == [["ADOPTION_WARNING"]]

    missing = _cleanup_engine(
        [{"algoId": "1", "clientAlgoId": "bdp-1", "symbol": "BTCUSDT"}],
        active={"pos": {"1"}},
        durable_rows=[_row(exchange_order_id="1")],
        positions=[{"symbol": "BTCUSDT", "positionAmt": "1"}],
    )
    missing._protection_inventory_semantic_issues = lambda _inventory: ["PROTECTION_VENUE_ROW_MISSING:1"]
    await missing._cleanup_excess_orders()
    assert missing._block_calls == []

    hard = _cleanup_engine(
        [{"algoId": "1", "clientAlgoId": "bdp-1", "symbol": "BTCUSDT"}],
        active={"pos": {"1"}},
        durable_rows=[_row(exchange_order_id="1")],
        positions=[{"symbol": "BTCUSDT", "positionAmt": "1"}],
    )
    hard._protection_inventory_semantic_issues = lambda _inventory: ["PROTECTION_QTY_MISMATCH:1"]
    await hard._cleanup_excess_orders()
    assert hard._block_calls == [["PROTECTION_QTY_MISMATCH:1"]]

    mismatch = _cleanup_engine(
        [{"algoId": "1", "clientAlgoId": "bdp-1", "symbol": "BTCUSDT"}],
        active={"pos": {"1"}},
        durable_rows=[_row(exchange_order_id="other")],
        positions=[{"symbol": "BTCUSDT", "positionAmt": "1"}],
    )
    await mismatch._cleanup_excess_orders()
    assert mismatch._block_calls == [["PROTECTION_OWNER_MAPPING_INCOMPLETE"]]

    age_unknown = _cleanup_engine(
        [
            {"algoId": "1", "clientAlgoId": "bdp-1", "symbol": "BTCUSDT", "createTime": "1"},
            {"algoId": "2", "clientAlgoId": "bdp-2", "symbol": "BTCUSDT"},
            {"algoId": "3", "clientAlgoId": "bdp-3", "symbol": "BTCUSDT", "createTime": "3"},
        ],
        active={"pos": {"1", "2"}},
        durable_rows=[
            _row(exchange_order_id="1"),
            _row(
                protection_id="tp",
                exchange_order_id="2",
                order_type="TAKE_PROFIT_MARKET",
                stop_type=None,
                take_profit_type="FIXED_RR",
            ),
        ],
        positions=[{"symbol": "BTCUSDT", "positionAmt": "1"}],
    )
    await age_unknown._cleanup_excess_orders()
    assert age_unknown._block_calls == [["EXCESS_ORDER_AGE_UNKNOWN:BTCUSDT"]]

    bad_age = _cleanup_engine(
        [
            {"algoId": "1", "clientAlgoId": "bdp-1", "symbol": "BTCUSDT", "createTime": "bad"},
            {"algoId": "2", "clientAlgoId": "bdp-2", "symbol": "BTCUSDT", "createTime": "2"},
            {"algoId": "3", "clientAlgoId": "bdp-3", "symbol": "BTCUSDT", "createTime": "3"},
        ],
        active={"pos": {"1", "2"}},
        durable_rows=[
            _row(exchange_order_id="1"),
            _row(
                protection_id="tp",
                exchange_order_id="2",
                order_type="TAKE_PROFIT_MARKET",
                stop_type=None,
                take_profit_type="FIXED_RR",
            ),
        ],
        positions=[{"symbol": "BTCUSDT", "positionAmt": "1"}],
    )
    await bad_age._cleanup_excess_orders()
    assert bad_age._block_calls == [["EXCESS_ORDER_AGE_UNKNOWN:BTCUSDT"]]

    excess = _cleanup_engine(
        [
            {
                "algoId": "1",
                "clientAlgoId": "bdp-1",
                "symbol": "BTCUSDT",
                "createTime": "1",
                "orderType": "STOP_MARKET",
            },
            {
                "algoId": "2",
                "clientAlgoId": "bdp-2",
                "symbol": "BTCUSDT",
                "createTime": "2",
                "orderType": "TAKE_PROFIT_MARKET",
            },
            {
                "algoId": "3",
                "clientAlgoId": "bdp-3",
                "symbol": "BTCUSDT",
                "createTime": "3",
                "orderType": "STOP_MARKET",
            },
            {
                "algoId": "9",
                "clientAlgoId": "bdp-9",
                "symbol": "ETHUSDT",
                "createTime": "1",
                "orderType": "STOP_MARKET",
            },
        ],
        active={"pos": {"1", "2"}},
        durable_rows=[
            _row(exchange_order_id="1"),
            _row(
                protection_id="tp",
                exchange_order_id="2",
                order_type="TAKE_PROFIT_MARKET",
                stop_type=None,
                take_profit_type="FIXED_RR",
            ),
        ],
        positions=[{"symbol": "BTCUSDT", "positionAmt": "1"}, {"symbol": "ETHUSDT", "positionAmt": "1"}],
    )
    await excess._cleanup_excess_orders()
    assert excess._cancel_calls == [("BTCUSDT", 1)]
    assert excess._fact_updates

    cancel_error = _cleanup_engine(
        [
            {
                "algoId": "11",
                "clientAlgoId": "bdp-11",
                "symbol": "BTCUSDT",
                "createTime": "1",
                "orderType": "STOP_MARKET",
            },
            {
                "algoId": "12",
                "clientAlgoId": "bdp-12",
                "symbol": "BTCUSDT",
                "createTime": "2",
                "orderType": "TAKE_PROFIT_MARKET",
            },
            {
                "algoId": "13",
                "clientAlgoId": "bdp-13",
                "symbol": "BTCUSDT",
                "createTime": "3",
                "orderType": "STOP_MARKET",
            },
        ],
        active={"pos": {"11", "12"}},
        durable_rows=[
            _row(exchange_order_id="11"),
            _row(
                protection_id="tp",
                exchange_order_id="12",
                order_type="TAKE_PROFIT_MARKET",
                stop_type=None,
                take_profit_type="FIXED_RR",
            ),
        ],
        positions=[{"symbol": "BTCUSDT", "positionAmt": "1"}],
    )
    cancel_error._cancel_algo_order = lambda *_args: (_ for _ in ()).throw(OSError("cancel"))
    await cancel_error._cleanup_excess_orders()

    orphan = _cleanup_engine(
        [
            {"algoId": "101", "clientAlgoId": "bdp-orphan", "symbol": "ETHUSDT"},
            {"algoId": "102", "clientAlgoId": "bdp-orphan-fail", "symbol": "ETHUSDT"},
            {"algoId": "103", "clientAlgoId": "bdp-local", "symbol": "LTCUSDT"},
            {"algoId": "104", "clientAlgoId": "other-service", "symbol": "XRPUSDT"},
        ]
    )
    orphan._protection = SimpleNamespace(all_positions=lambda: {"p": SimpleNamespace(instrument_id="LTCUSDT")})
    calls = 0

    async def orphan_cancel(symbol: str, algo_id: int) -> dict:
        nonlocal calls
        calls += 1
        if algo_id == 102:
            raise OSError("cancel")
        return {}

    orphan._cancel_algo_order = orphan_cancel
    for _ in range(3):
        await orphan._cleanup_excess_orders()
    assert calls == 2 and orphan._ghost_absence_count == {}

    exception = _cleanup_engine([])
    exception._get_open_algo_inventory = lambda: (_ for _ in ()).throw(RuntimeError("inventory"))
    await exception._cleanup_excess_orders()


def test_adoption_handles_empty_venue_positions_and_invalid_projection_entry() -> None:
    engine = _engine([])
    engine._last_algo_inventory_genuine = True
    engine._last_account = {"positions": []}
    assert engine._adopt_orphaned_protection_algos([{"symbol": "BTCUSDT"}]) == (0, [])

    engine = _engine([])
    engine._last_algo_inventory_genuine = True
    engine._last_account = {"positions": [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "0"}]}
    engine._position_projection = {"BTCUSDT": {"position_generation": "bad", "entry_price": "bad"}}
    algos = [
        {
            "symbol": "BTCUSDT",
            "clientAlgoId": "bdp-sl",
            "algoStatus": "NEW",
            "orderType": "STOP_MARKET",
            "side": "SELL",
            "reduceOnly": True,
            "quantity": "1",
            "triggerPrice": "90",
            "algoId": "sl",
        },
        {
            "symbol": "BTCUSDT",
            "clientAlgoId": "bdp-tp",
            "algoStatus": "NEW",
            "orderType": "TAKE_PROFIT_MARKET",
            "side": "SELL",
            "reduceOnly": True,
            "quantity": "1",
            "triggerPrice": "120",
            "algoId": "tp",
        },
    ]
    adopted, issues = engine._adopt_orphaned_protection_algos(algos)
    assert adopted == 2
    assert engine._store.saved
    assert (
        any("PROTECTION_ADOPTION_PROJECTION_FAILED" in issue for issue in issues) or engine._protection.all_positions()
    )


def test_restore_protection_handles_unknown_inventory_cleanup_and_account_edges() -> None:
    for inventory in (None, []):
        engine = _engine([_row()])
        assert engine._restore_durable_protection_projection(_account(), inventory) is False
        assert engine._block_calls

    engine = _engine([_row()])
    engine._protection_inventory_semantic_issues = lambda _inventory: ["PROTECTION_VENUE_ROW_MISSING:sl"]
    assert engine._restore_durable_protection_projection(_account(), [{"algoId": "other"}]) is True
    assert engine._store.removed == ["pos"]

    failing_reload = _engine([_row()])
    failing_reload._protection_inventory_semantic_issues = lambda _inventory: ["PROTECTION_VENUE_ROW_MISSING:sl"]
    failing_reload._store.fail_restore_after = 1
    assert failing_reload._restore_durable_protection_projection(_account(), [{"algoId": "other"}]) is False
    assert failing_reload._block_calls[-1] == ["PROTECTION_RELOAD_UNKNOWN"]

    blank_symbol = _engine([_row()])
    assert (
        blank_symbol._restore_durable_protection_projection(
            _account({"symbol": "", "positionAmt": "1", "entryPrice": "100"}), [{"algoId": "sl"}]
        )
        is True
    )
    assert blank_symbol._cancel_calls == [("BTCUSDT", "POSITION_FLAT_ON_VENUE")]


def test_restore_protection_handles_generation_conflicts_and_entry_facts() -> None:
    conflict = _engine([_row(protection_id="a", position_id="pos-a"), _row(protection_id="b", position_id="pos-b")])
    assert (
        conflict._restore_durable_protection_projection(
            _account({"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}), [{"algoId": "sl"}]
        )
        is True
    )
    assert len(conflict._cancel_calls) == 1

    bad_entry = _engine([_row()])
    bad_entry._position_projection = {"BTCUSDT": {"entry_price": "bad"}}
    assert (
        bad_entry._restore_durable_protection_projection(
            _account({"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "0"}), [{"algoId": "sl"}]
        )
        is False
    )
    assert bad_entry._block_calls


def test_restore_protection_handles_row_parse_ack_adoption_and_save_failure() -> None:
    malformed = _engine([_row()])
    malformed._store.rows[0].pop("trigger_price")
    assert (
        malformed._restore_durable_protection_projection(
            _account({"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}), [{"algoId": "sl"}]
        )
        is False
    )

    pending = _engine([_row(status="PENDING")])
    assert (
        pending._restore_durable_protection_projection(
            _account({"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}), [{"algoId": "sl"}]
        )
        is True
    )
    assert pending._store.saved and pending._store.rows[0]["status"] == "ACTIVE"

    failed_save = _engine([_row(status="PENDING")])
    failed_save._store.fail_save = True
    assert (
        failed_save._restore_durable_protection_projection(
            _account({"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}), [{"algoId": "sl"}]
        )
        is True
    )


def test_restore_protection_handles_unknown_type_role_existing_projection_and_restore_failure() -> None:
    bad_type = _engine([_row(stop_type="NOT_A_STOP")])
    assert (
        bad_type._restore_durable_protection_projection(
            _account({"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}), [{"algoId": "sl"}]
        )
        is False
    )

    bad_role = _engine([_row(stop_type=None, order_type="STOP_MARKET")])
    assert (
        bad_role._restore_durable_protection_projection(
            _account({"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}), [{"algoId": "sl"}]
        )
        is False
    )

    existing = _engine([_row()])
    existing._protection = SimpleNamespace(
        all_positions=lambda: {"pos": object()},
        restore_position_protection=lambda _projection: None,
    )
    assert (
        existing._restore_durable_protection_projection(
            _account({"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}), [{"algoId": "sl"}]
        )
        is True
    )

    restore_failed = _engine([_row()])
    restore_failed._protection = SimpleNamespace(
        all_positions=lambda: {},
        restore_position_protection=lambda _projection: (_ for _ in ()).throw(ValueError("projection")),
    )
    assert (
        restore_failed._restore_durable_protection_projection(
            _account({"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}), [{"algoId": "sl"}]
        )
        is False
    )


def test_restore_protection_rehydrates_pending_stop_intent_with_tp_ack() -> None:
    pending_stop = _row(status="PENDING", exchange_order_id="pending-sl")
    take_profit = _row(
        protection_id="tp",
        order_type="TAKE_PROFIT_MARKET",
        trigger="120",
        stop_type=None,
        take_profit_type="FIXED_RR",
        exchange_order_id="tp",
    )
    engine = _engine([pending_stop, take_profit])
    del engine._pending_stop_intent
    assert (
        engine._restore_durable_protection_projection(
            _account({"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}), [{"algoId": "tp"}]
        )
        is True
    )
    assert engine._pending_stop_intent["pos"] is True


def test_restore_protection_rebuilds_grouping_after_owned_conflict() -> None:
    # Keep a second symbol in the durable rows so the conflict cleanup branch
    # must rebuild grouped rows instead of returning with an empty projection.
    btc_old = _row(protection_id="btc-old", position_id="pos-btc-old", symbol="BTCUSDT")
    btc_new = _row(protection_id="btc-new", position_id="pos-btc-new", symbol="BTCUSDT")
    eth = _row(
        protection_id="eth-sl",
        position_id="pos-eth",
        symbol="ETHUSDT",
        exchange_order_id="eth-sl",
    )
    engine = _engine([btc_old, btc_new, eth])
    restored: dict[str, object] = {}
    engine._protection.all_positions = lambda: restored
    engine._protection.restore_position_protection = lambda projection: restored.__setitem__(
        projection.position_id, projection
    )
    engine._cancel_calls.clear()
    assert (
        engine._restore_durable_protection_projection(
            {
                "positions": [
                    {"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"},
                    {"symbol": "ETHUSDT", "positionAmt": "1", "entryPrice": "100"},
                ]
            },
            [
                {
                    "algoId": "sl",
                    "symbol": "BTCUSDT",
                    "side": "SELL",
                    "quantity": "1",
                    "triggerPrice": "90",
                    "reduceOnly": True,
                },
                {
                    "algoId": "eth-sl",
                    "symbol": "ETHUSDT",
                    "side": "SELL",
                    "quantity": "1",
                    "triggerPrice": "90",
                    "reduceOnly": True,
                },
            ],
        )
        is True
    )
    assert ("BTCUSDT", "MULTIPLE_ACTIVE_GENERATIONS") in engine._cancel_calls
    assert "pos-eth" in engine._protection.all_positions()
