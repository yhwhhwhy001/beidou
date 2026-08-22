"""Behavioral coverage for engine and domain-boundary failure paths.

These tests use small in-process authorities so that every branch remains
observable without contacting an exchange or changing a real account.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from beidou_control.plane import ControlAction
from beidou_core.engine import AutonomousEngine
from beidou_core.ports import (
    DOMAINS,
    DomainAuthorityRegistry,
    ExecutionPort,
    FactorEvidence,
    LedgerEntry,
    LedgerPort,
    MarketDataPort,
    MarketFact,
    MonitoringPort,
    OrderIntent,
    PositionFact,
    ProtectionPort,
    ResearchPort,
    RiskPort,
    StrategyPort,
    StrategyProposal,
)
from beidou_core.ports import (
    RiskDecision as PortRiskDecision,
)
from beidou_lifecycle.lifecycle import ModuleState
from beidou_shared.types import MonetaryValue, RiskDecision, StrategyId, VenueId
from beidou_strategy.risk.manager import RiskBudget, StrategyRiskLevel


def test_domain_ports_are_constructible_and_registry_enforces_one_authority() -> None:
    now = datetime.now(timezone.utc)
    market = MarketFact("BTCUSDT", 100.0, now, bid=99.9, ask=100.1, volume_24h=10.0)
    evidence = FactorEvidence(StrategyId("s"), "factor", 0.2, now, generation=2, is_valid=False)
    proposal = StrategyProposal(StrategyId("s"), "BTCUSDT", "LONG", 0.1, 0.8, owner="research")
    decision = PortRiskDecision("NORMAL", "ok", 3, "hash", now)
    intent = OrderIntent("i", "BTCUSDT", "BUY", 1.0, price=100.0, client_order_id="c")
    entry = LedgerEntry("tx", "cash", "BINANCE", "USDT", debit=1.0, credit=1.0)
    position = PositionFact("BTCUSDT", 1.0, 100.0, "LONG", VenueId("BINANCE"), now)
    assert market.source == "MARKET_DATA_AUTHORITY"
    assert evidence.generation == 2 and evidence.is_valid is False
    assert proposal.attribution == {} and proposal.owner == "research"
    assert decision.generation == 3 and intent.order_type == "LIMIT"
    assert isinstance(entry.timestamp, datetime) and position.venue == VenueId("BINANCE")

    # Protocol method bodies are executable contract stubs; invoking them
    # records that the public port surface is present without a fake authority.
    assert MarketDataPort.get_price(None, "BTCUSDT") is None
    assert MarketDataPort.get_order_book(None, "BTCUSDT") is None
    assert MarketDataPort.subscribe(None, ["BTCUSDT"]) is None
    assert ResearchPort.evaluate_factor(None, StrategyId("s"), "f") is None
    assert ResearchPort.get_factor_registry(None) is None
    assert ResearchPort.validate_factor(None, "f") is None
    assert StrategyPort.generate_proposal(None, StrategyId("s"), {}) is None
    assert StrategyPort.get_active_strategies(None) is None
    assert StrategyPort.validate_proposal(None, proposal) is None
    assert RiskPort.evaluate(None, {}) is None
    assert RiskPort.get_current_level(None) is None
    assert RiskPort.is_trading_allowed(None) is None
    assert ExecutionPort.submit_order(None, intent) is None
    assert ExecutionPort.cancel_order(None, "o", "BTCUSDT") is None
    assert ExecutionPort.get_order_status(None, "o", "BTCUSDT") is None
    assert LedgerPort.record_transaction(None, entry) is None
    assert LedgerPort.get_balance(None, "cash", "USDT") is None
    assert LedgerPort.reconcile(None) is None
    assert ProtectionPort.create_stop_loss(None, position, 90.0) is None
    assert ProtectionPort.create_take_profit(None, position, 110.0) is None
    assert ProtectionPort.cancel_protection(None, "p") is None
    assert ProtectionPort.get_active_protections(None) is None
    assert MonitoringPort.report_health(None) is None
    assert MonitoringPort.report_incident(None, "P1", "title", "detail") is None
    assert MonitoringPort.get_operational_facts(None) is None

    registry = DomainAuthorityRegistry()
    authority = object()
    registry.register_authority("Market", authority)
    with pytest.raises(ValueError, match="不能注册第二个"):
        registry.register_authority("Market", object())
    registry.register_read_model("Market", "dashboard")
    registry.register_read_model("Research", "report")
    assert registry.get_authority("Market") is authority
    assert registry.get_authority("Unknown") is None
    assert registry.registered_domains == ["Market"]
    DomainAuthorityRegistry._instance = None
    singleton = DomainAuthorityRegistry.get_instance()
    assert DomainAuthorityRegistry.get_instance() is singleton
    assert DOMAINS == ["Market", "Research", "Strategy", "Risk", "Execution", "Ledger", "Protection", "Monitoring"]


class _FeedProbe:
    def __init__(self, *, valid_ws: bool = True, fail: bool = False) -> None:
        self._ws_active = True
        self.valid_ws = valid_ws
        self.fail = fail
        self.saved = 0

    def is_ws_data_fresh(self, _symbol: str) -> bool:
        return True

    async def async_get_kline_features(self, _symbol: str) -> dict[str, float]:
        if self.fail:
            raise RuntimeError("feed unavailable")
        return {"price": 100.0, "bid": 99.0, "ask": 101.0, "spread_bps": 198.0, "volume_24h": 10.0}

    def get_last_ticker(self, _symbol: str) -> dict[str, str]:
        if not self.valid_ws:
            return {"lastPrice": "bad", "bidPrice": "", "askPrice": ""}
        return {"lastPrice": "100", "bid": "99", "ask": "101"}

    def get_last_orderbook(self, _symbol: str) -> dict[str, object]:
        return {"bids": [["99", "1"]], "asks": [["101", "1"]]}

    async def async_update_features(self, _symbol: str) -> dict[str, float]:
        if self.fail:
            raise RuntimeError("REST unavailable")
        return {"price": 100.0, "bid": 98.0, "ask": 102.0, "spread_bps": 400.0, "volume_24h": 9.0}

    def is_healthy(self) -> bool:
        return True


def _realtime_shell(feed: _FeedProbe, *, simulate: bool = False) -> AutonomousEngine:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._tick_count = 0
    engine._trading_pool = SimpleNamespace(active_instruments=lambda: ["BTCUSDT"])
    engine._feed = feed
    engine._can_write = False
    engine._can_simulate = simulate
    engine._outbox = SimpleNamespace(
        _db_path=None,
        _outbox=[],
        _processed=set(),
        _inbox={},
        unacked=lambda: ["intent"] if simulate else [],
        pending_count=lambda: 1 if simulate else 0,
    )
    engine._store = SimpleNamespace(save_market_snapshot=lambda *_args: setattr(feed, "saved", feed.saved + 1))
    engine._last_account = {}
    engine._error_count = 0
    engine._alerts = SimpleNamespace(send_incident=lambda *args, **kwargs: setattr(engine, "incident", (args, kwargs)))
    engine._protection = SimpleNamespace(
        all_positions=lambda: {
            "p": SimpleNamespace(
                instrument_id="BTCUSDT",
                entry_price=100.0,
                is_long=lambda: True,
                stop_loss=SimpleNamespace(is_active=lambda: True, trigger_price=SimpleNamespace(amount="90")),
                take_profits=[SimpleNamespace(is_active=lambda: True, trigger_price=SimpleNamespace(amount="110"))],
                unrealized_pnl_pct=lambda _price: 1.0,
            )
        }
    )
    engine._last_prices = {}
    engine._lease_owner = "probe"
    engine._intent_tasks = set()
    engine._realtime_loop = None
    engine._reconciliation_segment = lambda: asyncio.sleep(0)
    engine._place_order = lambda intent: asyncio.sleep(0, result=setattr(engine, "placed", intent))
    return engine


@pytest.mark.asyncio
async def test_realtime_tick_covers_ws_rest_intent_status_and_isolated_failures() -> None:
    feed = _FeedProbe(valid_ws=True)
    engine = _realtime_shell(feed, simulate=True)
    engine._tick_count = 59
    await engine._realtime_tick()
    assert feed.saved == 1
    assert engine._last_prices["BTCUSDT"] == 100.0
    assert engine.placed == "intent"
    assert engine._last_realtime_mono > 0

    # An invalid websocket quote falls back to the complete REST snapshot,
    # while an empty feature payload simply skips that symbol.
    engine = _realtime_shell(_FeedProbe(valid_ws=False), simulate=False)
    await engine._realtime_tick()
    assert engine._last_prices["BTCUSDT"] == 100.0
    engine._feed = SimpleNamespace(
        _ws_active=False,
        is_ws_data_fresh=lambda _s: False,
        async_update_features=lambda _s: asyncio.sleep(0, result={}),
    )
    await engine._realtime_tick()
    assert engine._last_realtime is not None

    # A per-symbol failure does not escape the tick; an outer failure is
    # incidented and the independent reconciliation segment still runs.
    failed = _realtime_shell(_FeedProbe(fail=True), simulate=False)
    await failed._realtime_tick()
    assert failed._market_data_failures["BTCUSDT"] == 1
    failed._trading_pool = SimpleNamespace(active_instruments=lambda: (_ for _ in ()).throw(RuntimeError("pool")))
    await failed._realtime_tick()
    assert "pool" in failed.incident[0][2]


@pytest.mark.asyncio
async def test_protection_order_cancel_and_fact_update_failure_matrix() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._active_algo_ids = {"p": {"1", "2", "3"}}
    engine._cancel_algo_order = lambda _s, algo: asyncio.sleep(
        0,
        result=(
            {"msg": "failed"}
            if algo == 1
            else ({"code": -1, "msg": "bad"} if algo == 2 else (_ for _ in ()).throw(RuntimeError("cancel")))
        ),
    )
    await engine._cancel_algo_orders("p", "BTCUSDT")
    assert engine._active_algo_ids == {}
    await engine._cancel_algo_orders("missing", "BTCUSDT")
    del engine._active_algo_ids
    await engine._cancel_algo_orders("missing", "BTCUSDT")

    engine._active_algo_ids = {}
    engine._position_generation = {}
    assert engine._next_position_generation("BTCUSDT") == 1
    assert engine._resolve_position_generation("BTCUSDT", SimpleNamespace(position_generation=0)) == 1
    assert engine._resolve_position_generation("ETHUSDT", SimpleNamespace(position_generation=3)) == 3

    engine._protection_owner_id = "owner"
    engine._protection_issues = set()
    engine._protection_owner_unknown = False
    engine._control = SimpleNamespace(get_status=lambda: ControlAction.RESUME)
    engine._safe_no_new_risk = lambda *_args: setattr(engine, "no_new_risk", True)
    engine._alerts = SimpleNamespace(send_incident=lambda *args, **kwargs: setattr(engine, "incident", (args, kwargs)))
    engine._block_unowned_protection_orders(["a", "b"])
    assert engine._protection_owner_unknown and engine._protection_issues == {"a", "b"}

    engine._store = SimpleNamespace(restore_protections=lambda: [])
    engine._protection = SimpleNamespace(all_positions=lambda: {})
    engine._last_account = {"positions": []}
    engine._update_protection_fact(hard_issues=[], venue_missing=[], unowned_ids=[], genuine_inventory=False)
    engine._update_protection_fact(hard_issues=["bad"], venue_missing=[], unowned_ids=[], genuine_inventory=True)
    assert engine._protection_owner_unknown is True
    engine._assess_protection_coverage = lambda *_args: (True, {"unprotected_symbols": []})
    engine._update_protection_fact(hard_issues=[], venue_missing=[], unowned_ids=[], genuine_inventory=True)
    assert engine._protection_owner_unknown is False

    engine._store = SimpleNamespace(restore_protections=lambda: (_ for _ in ()).throw(OSError("db")))
    engine._update_protection_fact(hard_issues=[], venue_missing=[], unowned_ids=[], genuine_inventory=True)
    assert engine._protection_owner_unknown is True


def test_protection_inventory_semantics_and_stale_row_cleanup() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._can_write = True
    engine._protection_owner_id = "owner"
    assert engine._protection_inventory_semantic_issues([]) == ["PROTECTION_STORE_UNAVAILABLE"]
    engine._can_write = False
    assert engine._protection_inventory_semantic_issues([]) == []

    class Store:
        def __init__(self) -> None:
            self.saved: list[dict] = []

        def restore_protections(self):
            return [
                {
                    "status": "ACTIVE",
                    "owner_id": "owner",
                    "exchange_order_id": "a",
                    "symbol": "BTCUSDT",
                    "side": "SELL",
                    "order_type": "STOP_MARKET",
                    "quantity": "1",
                    "trigger_price": "90",
                }
            ]

        def save_protection(self, **kwargs):
            self.saved.append(kwargs)

    store = Store()
    engine._store = store
    mismatch = engine._protection_inventory_semantic_issues(
        [
            {
                "algoId": "a",
                "symbol": "ETHUSDT",
                "side": "BUY",
                "orderType": "LIMIT",
                "quantity": "2",
                "triggerPrice": "0",
                "reduceOnly": False,
            }
        ]
    )
    assert {
        "PROTECTION_SYMBOL_MISMATCH:a",
        "PROTECTION_SIDE_MISMATCH:a",
        "PROTECTION_TYPE_MISMATCH:a",
        "PROTECTION_QUANTITY_MISMATCH:a",
        "PROTECTION_TRIGGER_MISMATCH:a",
        "PROTECTION_REDUCE_ONLY_UNPROVEN:a",
    }.issubset(mismatch)
    assert engine._protection_inventory_semantic_issues([]) == ["PROTECTION_VENUE_ROW_MISSING:a"]
    engine._store = SimpleNamespace(restore_protections=lambda: (_ for _ in ()).throw(RuntimeError("read")))
    assert engine._protection_inventory_semantic_issues([]) == ["PROTECTION_STORE_READ_UNKNOWN:RuntimeError"]

    engine._store = store
    engine._active_algo_ids = {"p": {"1"}}
    engine._protection_owner_id = "owner"
    engine._stale_protection_algos = []
    engine._cancel_stale_protection_rows(
        "BTCUSDT",
        [
            {"owner_id": "other", "exchange_order_id": "x", "protection_id": "other"},
            {
                "owner_id": "owner",
                "exchange_order_id": "1",
                "protection_id": "p",
                "position_id": "p",
                "symbol": "BTCUSDT",
                "side": "SELL",
                "quantity": "1",
                "trigger_price": "90",
                "position_generation": 1,
            },
        ],
        "stale",
    )
    assert engine._stale_protection_algos == [("BTCUSDT", "1")]
    engine._cancel_algo_order = lambda *_args: asyncio.sleep(0, result={"msg": "success"})
    assert asyncio.run(engine._cancel_stale_protection_algos()) == {"1"}
    engine._stale_protection_algos = [("BTCUSDT", "bad")]
    engine._cancel_algo_order = lambda *_args: (_ for _ in ()).throw(RuntimeError("venue"))
    assert asyncio.run(engine._cancel_stale_protection_algos()) == set()


class _ProtectionStore:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = list(rows)
        self.saved: list[dict] = []
        self.removed: list[str] = []

    def restore_protections(self) -> list[dict]:
        return list(self.rows)

    def remove_protection(self, position_id: str) -> None:
        self.removed.append(position_id)
        self.rows = [row for row in self.rows if str(row.get("position_id")) != str(position_id)]

    def save_protection(self, **kwargs) -> None:
        self.saved.append(kwargs)


def _restore_engine(store: _ProtectionStore) -> AutonomousEngine:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._store = store
    engine._protection_owner_id = "owner"
    engine._session_id = "session"
    engine._position_generation = {"BTCUSDT": 1}
    engine._position_projection = {}
    engine._position_entry_times = {}
    engine._active_algo_ids = {}
    engine._stale_protection_algos = []
    engine._pending_stop_intent = {}
    engine._protection_row_fresh = lambda _algo: False
    engine._blocked: list[str] = []
    engine._block_unowned_protection_orders = lambda ids: engine._blocked.extend(ids)
    engine._protection = SimpleNamespace(
        _positions={},
        all_positions=lambda: engine._protection._positions,
        restore_position_protection=lambda projection: engine._protection._positions.__setitem__(
            projection.position_id, projection
        ),
    )
    return engine


def _active_protection_rows(*, position_id: str = "pos", stop_side: str = "SELL") -> list[dict]:
    return [
        {
            "protection_id": "sl",
            "position_id": position_id,
            "symbol": "BTCUSDT",
            "side": stop_side,
            "trigger_price": "90",
            "quantity": "1",
            "order_type": "STOP_MARKET",
            "status": "ACTIVE",
            "stop_type": "ATR_BASED",
            "take_profit_type": "",
            "owner_id": "owner",
            "position_generation": 1,
            "session_id": "session",
            "exchange_order_id": "sl1",
        },
        {
            "protection_id": "tp",
            "position_id": position_id,
            "symbol": "BTCUSDT",
            "side": stop_side,
            "trigger_price": "110",
            "quantity": "1",
            "order_type": "TAKE_PROFIT_MARKET",
            "status": "ACTIVE",
            "stop_type": "",
            "take_profit_type": "FIXED_RR",
            "owner_id": "owner",
            "position_generation": 1,
            "session_id": "session",
            "exchange_order_id": "tp1",
        },
    ]


def _protection_inventory() -> list[dict]:
    return [
        {
            "algoId": "sl1",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "quantity": "1",
            "triggerPrice": "90",
            "reduceOnly": True,
        },
        {
            "algoId": "tp1",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "quantity": "1",
            "triggerPrice": "110",
            "reduceOnly": True,
        },
    ]


def test_restore_durable_protection_projection_valid_ack_pending_and_unknown_matrix() -> None:
    account = {"positions": [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}]}
    store = _ProtectionStore(_active_protection_rows())
    engine = _restore_engine(store)
    assert engine._restore_durable_protection_projection(account, _protection_inventory()) is True
    assert set(engine._active_algo_ids["pos"]) == {"sl1", "tp1"}
    assert engine._protection._positions["pos"].stop_loss is not None

    empty_store = _ProtectionStore([])
    assert _restore_engine(empty_store)._restore_durable_protection_projection({}, None) is True
    blocked = _restore_engine(_ProtectionStore(_active_protection_rows()))
    assert blocked._restore_durable_protection_projection(account, None) is False
    assert blocked._blocked == ["OPEN_ALGO_ORDERS_UNKNOWN"]
    blocked = _restore_engine(_ProtectionStore(_active_protection_rows()))
    assert blocked._restore_durable_protection_projection(account, []) is False
    assert blocked._blocked == ["OPEN_ALGO_ORDERS_EMPTY"]
    failing_store = SimpleNamespace(restore_protections=lambda: (_ for _ in ()).throw(OSError("db")))
    failing = _restore_engine(failing_store)  # type: ignore[arg-type]
    assert failing._restore_durable_protection_projection(account, _protection_inventory()) is False
    assert failing._blocked == ["PROTECTION_STORE_READ_UNKNOWN:OSError"]

    # A venue-missing row is cleaned only after the freshness grace period;
    # the second durable read then permits recovery to finish.
    missing_store = _ProtectionStore(_active_protection_rows())
    missing = _restore_engine(missing_store)
    missing._protection_row_fresh = lambda _algo: False
    assert (
        missing._restore_durable_protection_projection(
            account,
            [
                {
                    "algoId": "other",
                    "symbol": "ETHUSDT",
                    "side": "SELL",
                    "quantity": "1",
                    "triggerPrice": "90",
                    "reduceOnly": True,
                }
            ],
        )
        is True
    )
    assert missing_store.removed == ["pos", "pos"]
    missing_store.rows = []
    assert missing._restore_durable_protection_projection(account, _protection_inventory()) is True

    # Valid venue facts but malformed account rows are unknown, never treated
    # as flat.
    for bad_account, expected in [
        ({}, "ACCOUNT_POSITIONS_UNKNOWN"),
        ({"positions": ["bad"]}, "ACCOUNT_POSITION_ROW_UNKNOWN"),
        (
            {"positions": [{"symbol": "BTCUSDT", "positionAmt": "bad", "entryPrice": "100"}]},
            "ACCOUNT_POSITION_FACT_UNKNOWN:BTCUSDT",
        ),
        (
            {"positions": [{"symbol": "BTCUSDT", "positionAmt": "NaN", "entryPrice": "100"}]},
            "ACCOUNT_POSITION_FACT_UNKNOWN:BTCUSDT",
        ),
        (
            {
                "positions": [
                    {"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"},
                    {"symbol": "BTCUSDT", "positionAmt": "2", "entryPrice": "100"},
                ]
            },
            "ACCOUNT_POSITION_DUPLICATE:BTCUSDT",
        ),
    ]:
        probe = _restore_engine(_ProtectionStore(_active_protection_rows()))
        assert probe._restore_durable_protection_projection(bad_account, _protection_inventory()) is False
        assert expected in probe._blocked


def test_restore_durable_protection_projection_conflicts_pending_and_role_errors() -> None:
    account = {"positions": [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}]}
    inventory = _protection_inventory()

    conflict_rows = _active_protection_rows(position_id="old") + _active_protection_rows(position_id="new")
    conflict = _restore_engine(_ProtectionStore(conflict_rows))
    assert conflict._restore_durable_protection_projection(account, inventory) is True
    assert conflict._stale_protection_algos

    unowned_rows = _active_protection_rows(position_id="old") + [
        dict(row, position_id="new", owner_id="other") for row in _active_protection_rows(position_id="new")
    ]
    unowned = _restore_engine(_ProtectionStore(unowned_rows))
    assert unowned._restore_durable_protection_projection(account, inventory) is False
    assert any(item.startswith("PROTECTION_MULTIPLE_ACTIVE_GENERATIONS") for item in unowned._blocked)

    for rows, expected in [
        (
            [dict(_active_protection_rows()[0], position_id="", exchange_order_id="sl1")],
            "PROTECTION_POSITION_ID_UNKNOWN",
        ),
        ([dict(_active_protection_rows()[0], position_generation="bad")], "PROTECTION_GENERATION_UNKNOWN:pos"),
        ([dict(_active_protection_rows()[0], symbol="")], "PROTECTION_SYMBOL_UNKNOWN:pos"),
        ([dict(_active_protection_rows()[0], owner_id="other")], "PROTECTION_OWNER_OR_GENERATION_UNKNOWN:pos"),
        (
            [
                dict(_active_protection_rows()[0], session_id="s1"),
                dict(_active_protection_rows()[0], protection_id="sl2", exchange_order_id="sl2", session_id="s2"),
            ],
            "PROTECTION_SESSION_MISMATCH:pos",
        ),
        ([dict(_active_protection_rows()[0], exchange_order_id="sl1")], "PROTECTION_ENTRY_PRICE_UNKNOWN:BTCUSDT"),
        ([dict(_active_protection_rows()[0], side="BUY")], None),
    ]:
        probe = _restore_engine(_ProtectionStore(rows))
        probe._protection_inventory_semantic_issues = lambda _inventory: []
        probe._protection_row_fresh = lambda _algo: False
        bad_account = (
            account
            if expected != "PROTECTION_ENTRY_PRICE_UNKNOWN:BTCUSDT"
            else {"positions": [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "0"}]}
        )
        result = probe._restore_durable_protection_projection(bad_account, inventory)
        if expected is None:
            assert result is True
            assert probe._stale_protection_algos
        else:
            assert result is False and expected in probe._blocked

    pending_only = [
        dict(_active_protection_rows()[0], status="PENDING", exchange_order_id="", protection_id="pending-sl")
    ]
    pending = _restore_engine(_ProtectionStore(pending_only))
    assert pending._restore_durable_protection_projection(account, inventory) is True

    tp_only = [dict(_active_protection_rows()[1], protection_id="tp-only")]
    tp_probe = _restore_engine(_ProtectionStore(tp_only))
    assert tp_probe._restore_durable_protection_projection(account, inventory) is True
    assert tp_probe._protection._positions["pos"].stop_loss is None

    malformed = [dict(_active_protection_rows()[0], protection_id="", exchange_order_id="")]
    malformed_probe = _restore_engine(_ProtectionStore(malformed))
    assert malformed_probe._restore_durable_protection_projection(account, inventory) is False
    assert malformed_probe._blocked


def _orphan_algos() -> list[dict]:
    return [
        {
            "algoId": "sl-orphan",
            "clientAlgoId": "bdp-sl-orphan",
            "symbol": "BTCUSDT",
            "algoStatus": "NEW",
            "orderType": "STOP_MARKET",
            "side": "SELL",
            "quantity": "1",
            "triggerPrice": "90",
            "reduceOnly": True,
        },
        {
            "algoId": "tp-orphan",
            "clientAlgoId": "bdp-tp-orphan",
            "symbol": "BTCUSDT",
            "algoStatus": "WORKING",
            "orderType": "TAKE_PROFIT_MARKET",
            "side": "SELL",
            "quantity": "1",
            "triggerPrice": "110",
            "reduceOnly": "true",
        },
    ]


def test_adopt_orphaned_protection_algos_converges_and_stays_fail_closed() -> None:
    class Store(_ProtectionStore):
        pass

    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._last_algo_inventory_genuine = False
    assert engine._adopt_orphaned_protection_algos(_orphan_algos()) == (0, [])
    engine._last_algo_inventory_genuine = True
    engine._store = None
    assert engine._adopt_orphaned_protection_algos(_orphan_algos()) == (0, [])

    store = Store([])
    engine._store = store
    engine._last_account = {"positions": [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "0"}, "bad"]}
    engine._protection_owner_id = "owner"
    engine._position_projection = {"BTCUSDT": {"position_generation": "bad", "entry_price": "100"}}
    engine._position_generation = {"BTCUSDT": 0}
    engine._session_id = "session"
    engine._active_algo_ids = {}
    engine._protection = SimpleNamespace(
        all_positions=lambda: {},
        restored=[],
        restore_position_protection=lambda projection: engine._protection.restored.append(projection),
    )
    adopted, issues = engine._adopt_orphaned_protection_algos(_orphan_algos())
    assert adopted == 2 and issues == []
    assert engine._active_algo_ids["adopt-BTCUSDT"] == {"sl-orphan", "tp-orphan"}
    assert len(engine._protection.restored) == 1
    assert engine._position_generation["BTCUSDT"] == 1

    # External positions, already-owned symbols, malformed candidates, and a
    # store read failure all remain non-adopted and produce explicit evidence.
    external = AutonomousEngine.__new__(AutonomousEngine)
    external._last_algo_inventory_genuine = True
    external._last_account = {"positions": [{"symbol": "ETHUSDT", "positionAmt": "1", "entryPrice": "10"}]}
    external._store = Store([])
    external._protection_owner_id = "owner"
    external._position_projection = {}
    external._position_generation = {}
    external._protection = SimpleNamespace(all_positions=lambda: {})
    assert external._adopt_orphaned_protection_algos(_orphan_algos()) == (0, [])

    owned = AutonomousEngine.__new__(AutonomousEngine)
    owned._last_algo_inventory_genuine = True
    owned._last_account = {"positions": [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}]}
    owned._store = Store([{"status": "ACTIVE", "owner_id": "owner", "symbol": "BTCUSDT", "exchange_order_id": "old"}])
    owned._protection_owner_id = "owner"
    owned._position_projection = {}
    owned._position_generation = {}
    owned._protection = SimpleNamespace(all_positions=lambda: {})
    assert owned._adopt_orphaned_protection_algos(_orphan_algos()) == (0, [])

    ambiguous = AutonomousEngine.__new__(AutonomousEngine)
    ambiguous._last_algo_inventory_genuine = True
    ambiguous._last_account = {"positions": [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}]}
    ambiguous._store = Store([])
    ambiguous._protection_owner_id = "owner"
    ambiguous._position_projection = {"BTCUSDT": {}}
    ambiguous._position_generation = {"BTCUSDT": 1}
    ambiguous._protection = SimpleNamespace(all_positions=lambda: {})
    bad = [dict(item, reduceOnly=False) for item in _orphan_algos()]
    assert ambiguous._adopt_orphaned_protection_algos(bad)[0] == 0
    assert ambiguous._adopt_orphaned_protection_algos([*_orphan_algos(), dict(_orphan_algos()[0], algoId="sl2")])[1]

    read_error = AutonomousEngine.__new__(AutonomousEngine)
    read_error._last_algo_inventory_genuine = True
    read_error._last_account = {"positions": [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}]}
    read_error._store = SimpleNamespace(restore_protections=lambda: (_ for _ in ()).throw(OSError("db")))
    read_error._protection_owner_id = "owner"
    read_error._position_projection = {"BTCUSDT": {}}
    read_error._position_generation = {"BTCUSDT": 1}
    read_error._protection = SimpleNamespace(all_positions=lambda: {})
    assert read_error._adopt_orphaned_protection_algos(_orphan_algos()) == (
        0,
        ["PROTECTION_STORE_READ_UNKNOWN:OSError"],
    )

    persist_error = AutonomousEngine.__new__(AutonomousEngine)
    persist_error._last_algo_inventory_genuine = True
    persist_error._last_account = {"positions": [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}]}
    persist_error._store = SimpleNamespace(
        restore_protections=lambda: [],
        save_protection=lambda **_kwargs: (_ for _ in ()).throw(OSError("write")),
    )
    persist_error._protection_owner_id = "owner"
    persist_error._position_projection = {"BTCUSDT": {}}
    persist_error._position_generation = {"BTCUSDT": 1}
    persist_error._active_algo_ids = {}
    persist_error._protection = SimpleNamespace(
        all_positions=lambda: {},
        restore_position_protection=lambda _projection: (_ for _ in ()).throw(ValueError("projection")),
    )
    adopted, issues = persist_error._adopt_orphaned_protection_algos(_orphan_algos())
    assert adopted == 0
    assert any(item.startswith("PROTECTION_ADOPTION_PERSIST_FAILED") for item in issues)
    assert any(item.startswith("PROTECTION_ADOPTION_PROJECTION_FAILED") for item in issues)


@pytest.mark.asyncio
async def test_realtime_tick_extra_modes_and_reconciliation_recovery() -> None:
    engine = _realtime_shell(_FeedProbe(valid_ws=True), simulate=False)
    del engine._last_prices
    engine._can_write = True
    engine._monitor_orders = lambda _symbol: asyncio.sleep(0, result=setattr(engine, "monitored", True))
    engine._protection = SimpleNamespace(
        all_positions=lambda: {
            "short": SimpleNamespace(
                instrument_id="BTCUSDT",
                entry_price=100.0,
                is_long=lambda: False,
                stop_loss=SimpleNamespace(is_active=lambda: True, trigger_price=SimpleNamespace(amount="110")),
                take_profits=[SimpleNamespace(is_active=lambda: True, trigger_price=SimpleNamespace(amount="90"))],
                unrealized_pnl_pct=lambda _price: -1.0,
            )
        }
    )
    engine._tick_count = 59
    await engine._realtime_tick()
    assert engine.monitored is True and engine._last_prices["BTCUSDT"] == 100.0

    # Missing websocket auxiliary snapshots exercise the complete REST
    # refresh, while a durable outbox uses the non-blocking task handoff.
    no_ws = _realtime_shell(_FeedProbe(valid_ws=True), simulate=False)
    no_ws._feed = SimpleNamespace(
        _ws_active=True,
        is_ws_data_fresh=lambda _s: True,
        async_get_kline_features=lambda _s: asyncio.sleep(0, result={"price": 100.0, "bid": 99.0, "ask": 101.0}),
        get_last_ticker=lambda _s: {},
        get_last_orderbook=lambda _s: {},
        async_update_features=lambda _s: asyncio.sleep(0, result={"price": 100.0, "bid": 99.0, "ask": 101.0}),
    )
    no_ws._outbox = SimpleNamespace(
        _db_path="postgresql://authority",
        _outbox=[],
        _processed=set(),
        _inbox={},
        unacked=lambda: [],
        pending_count=lambda: 0,
        claim=lambda **_kwargs: None,
    )
    no_ws._reap_intent_tasks = lambda: None
    no_ws._tick_count = 4
    await no_ws._realtime_tick()
    assert no_ws._last_prices["BTCUSDT"] == 100.0

    claim_ids = iter(["intent", None])
    task_engine = _realtime_shell(_FeedProbe(), simulate=False)
    task_engine._can_simulate = True
    task_engine._outbox = SimpleNamespace(
        _db_path="authority",
        _outbox=[],
        _processed=set(),
        _inbox={},
        unacked=lambda: [],
        pending_count=lambda: 1,
        claim=lambda **_kwargs: next(claim_ids),
    )
    task_engine._execute_claimed_intent = lambda _intent: asyncio.sleep(
        0, result=setattr(task_engine, "executed", True)
    )
    task_engine._tick_count = 4
    await task_engine._realtime_tick()
    await asyncio.sleep(0)
    assert task_engine.executed is True

    fault_recovery = _realtime_shell(_FeedProbe(), simulate=False)
    fault_recovery._last_unknown_resolve = 0.0
    fault_recovery._last_recon = time.time()
    fault_recovery._outbox = SimpleNamespace(recover_inflight=lambda **_kwargs: (_ for _ in ()).throw(OSError("lease")))
    fault_recovery._resolve_unknown_outbox_intents = lambda: asyncio.sleep(0)
    fault_recovery._monitor_orphan_orders = lambda: asyncio.sleep(0)
    await AutonomousEngine._reconciliation_segment(fault_recovery)
    assert fault_recovery._last_recon > 0

    engine = _realtime_shell(_FeedProbe(), simulate=False)
    engine._last_unknown_resolve = time.time()
    engine._last_recon = 0.0
    engine._outbox = SimpleNamespace()
    engine._reconcile = lambda: asyncio.sleep(0, result=setattr(engine, "recon_called", True) or True)
    engine._durable_fact_status = lambda: (True, "OK", {})
    engine._maybe_auto_resolve_incidents = lambda: setattr(engine, "incidents_resolved", True)
    engine._control = SimpleNamespace(
        get_status=lambda: ControlAction.NO_NEW_RISK,
        execute_action=lambda action: setattr(engine, "resumed", action),
    )
    await AutonomousEngine._reconciliation_segment(engine)
    assert engine.__dict__.get("recon_called") is True
    assert engine.incidents_resolved and engine.resumed is ControlAction.RESUME

    broken = _realtime_shell(_FeedProbe(), simulate=False)
    broken._last_unknown_resolve = time.time()  # type: ignore[name-defined]
    broken._last_recon = time.time()  # type: ignore[name-defined]
    broken._resolve_unknown_outbox_intents = lambda: (_ for _ in ()).throw(RuntimeError("resolve"))
    broken._reconcile = lambda: (_ for _ in ()).throw(RuntimeError("recon"))
    await AutonomousEngine._reconciliation_segment(broken)
    assert broken._last_recon > 0


def test_engine_protection_config_fact_failure_and_binding_helpers() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._control = SimpleNamespace(
        get_status=lambda: ControlAction.RESUME, execute_action=lambda action: setattr(engine, "action", action)
    )
    engine._alerts = SimpleNamespace(send_incident=lambda *args, **kwargs: setattr(engine, "incident", (args, kwargs)))
    with pytest.raises(RuntimeError, match="PROTECTION_CONFIG_UNKNOWN:BTCUSDT"):
        engine._require_protection_config("BTCUSDT", SimpleNamespace(stop_pct=0, metadata={"reason": "no ATR"}))
    assert engine._protection_config_unknown and engine.action is ControlAction.NO_NEW_RISK
    engine._require_protection_config("BTCUSDT", SimpleNamespace(stop_pct=1.0, metadata={}))

    engine._control = None
    engine._safe_no_new_risk("no-control")
    engine._control = SimpleNamespace(
        get_status=lambda: ControlAction.RESUME,
        execute_action=lambda _action: (_ for _ in ()).throw(RuntimeError("control")),
    )
    engine._safe_no_new_risk("control-error")
    engine._env_mode = SimpleNamespace(value="testnet")
    engine._alerts = SimpleNamespace(send_incident=lambda *args, **kwargs: setattr(engine, "incident", (args, kwargs)))
    engine._safe_no_new_risk = lambda _reason="": None
    engine._record_execution_fact_failure_env_guarded("testnet-failure")
    assert "testnet-failure" in engine.incident[0][2]

    class Ledger:
        def __init__(self) -> None:
            self.frozen = False

        def freeze(self):
            self.frozen = True

    engine._ledger = Ledger()
    engine._control = SimpleNamespace(get_status=lambda: ControlAction.RESUME, execute_action=lambda _action: None)
    engine._env_mode = SimpleNamespace(value="live")
    engine._record_execution_fact_failure_env_guarded("live-failure")
    assert engine._ledger.frozen is True

    class Tracker:
        def __init__(self):
            self.events = []

        def apply(self, event):
            self.events.append(event)

    engine._order_trackers = {"o": Tracker()}
    engine._active_order_ids = {"o"}
    engine._store = SimpleNamespace(save_order_state=lambda *_args, **_kwargs: None)
    engine._record_execution_fact_failure_env_guarded = lambda reason: setattr(engine, "failure", reason)
    engine._mark_order_unknown("o", "BTCUSDT", "ambiguous")
    assert "o" not in engine._active_order_ids and engine.failure.startswith("ambiguous")
    engine._store = None
    engine._mark_order_unknown("x", "BTCUSDT", "missing")
    assert "UNKNOWN_PERSISTENCE_FAILED" in engine.failure

    p = SimpleNamespace(
        protection_id="p",
        owner_id="owner",
        position_generation=1,
        instrument_id="BTCUSDT",
        side=SimpleNamespace(value="SELL"),
        order_type="STOP_MARKET",
        quantity=SimpleNamespace(amount="1"),
        trigger_price=SimpleNamespace(amount="90"),
    )
    key = engine._protection_client_algo_id(p)
    assert key.startswith("bdp-")
    params = engine._protection_algo_params(p, symbol="BTCUSDT", side="SELL", precision={"quantity": 3, "price": 2})
    assert params["clientAlgoId"] == key and params["quantity"] == "1.000"
    for quantity, trigger, expected in [
        ("bad", "90", "invalid"),
        ("0", "90", "positive"),
        ("1", "bad", "invalid"),
        ("1", "0", "positive"),
    ]:
        bad = SimpleNamespace(
            order_type="STOP_MARKET",
            quantity=SimpleNamespace(amount=quantity),
            trigger_price=SimpleNamespace(amount=trigger),
        )
        with pytest.raises(ValueError, match=expected):
            engine._protection_algo_params(bad, symbol="BTCUSDT", side="SELL", precision={"quantity": 0, "price": 0})

    class LedgerProbe:
        def validate(self, _tx):
            pass

        def post(self, _tx):
            self.posted = True

    ledger = LedgerProbe()
    engine._ledger = ledger
    engine._store = SimpleNamespace(save_ledger_transaction=lambda _tx: None)
    tx = SimpleNamespace(transaction_id="tx")
    engine._post_ledger_transaction(tx)
    assert ledger.posted is True
    engine._store = SimpleNamespace(save_ledger_transaction=lambda _tx: (_ for _ in ()).throw(OSError("db")))
    engine._record_execution_fact_failure_env_guarded = lambda reason: setattr(engine, "failure", reason)
    with pytest.raises(OSError):
        engine._post_ledger_transaction(tx)
    assert "persistence/post failed" in engine.failure


def test_execution_race_safe_wrapper_and_small_send_gates() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    aggregate = object()
    engine._outbox = SimpleNamespace(
        transition_execution_child=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("TERMINAL_CHILD_STATE:ACKED")
        ),
        restore_execution_plan=lambda _intent: aggregate,
    )
    assert engine._transition_execution_child_race_safe("i", 0, "ACKED", event_id="e") is aggregate
    engine._outbox = SimpleNamespace(
        transition_execution_child=lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("other")),
    )
    with pytest.raises(ValueError, match="other"):
        engine._transition_execution_child_race_safe("i", 0, "ACKED", event_id="e")


def _venue_order(
    *,
    status: str = "NEW",
    client: str = "client",
    symbol: str = "BTCUSDT",
    executed: str = "0",
    order_id: str = "venue-1",
) -> dict:
    return {
        "orderId": order_id,
        "clientOrderId": client,
        "symbol": symbol,
        "status": status,
        "executedQty": executed,
        "side": "BUY",
        "type": "MARKET",
        "origQty": "1",
        "price": "0",
        "avgPrice": "100",
        "reduceOnly": False,
        "stopPrice": "0",
    }


@pytest.mark.asyncio
async def test_unknown_resolution_and_adjudication_full_matrix() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._api_async = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("network"))
    assert await engine._adjudicate_unknown_slice("i", "BTCUSDT", "c", None) == "INCONCLUSIVE"
    engine._api_async = lambda *_args, **_kwargs: asyncio.sleep(0, result=["not-dict"])
    assert await engine._adjudicate_unknown_slice("i", "BTCUSDT", "c", None) == "INCONCLUSIVE"

    transitions: list[tuple] = []
    engine._outbox = SimpleNamespace(
        transition_execution_child=lambda *args, **kwargs: transitions.append((args, kwargs)),
    )
    engine._api_async = lambda *_args, **_kwargs: asyncio.sleep(0, result={"code": -2013, "msg": "Unknown order"})
    assert await engine._adjudicate_unknown_slice("i", "BTCUSDT", "c", 0) == "ABSENT"
    assert transitions
    engine._api_async = lambda *_args, **_kwargs: asyncio.sleep(0, result={"code": -1000, "msg": "server"})
    assert await engine._adjudicate_unknown_slice("i", "BTCUSDT", "c", None) == "INCONCLUSIVE"
    for raw in [
        {"status": "NEW"},
        _venue_order(client="different"),
        _venue_order(status="NOT_A_STATUS"),
        _venue_order(executed="bad"),
        _venue_order(executed="NaN"),
    ]:
        engine._api_async = lambda *_args, raw=raw, **_kwargs: asyncio.sleep(0, result=raw)
        assert await engine._adjudicate_unknown_slice("i", "BTCUSDT", "c", None) == "INCONCLUSIVE"

    store_calls: list[dict] = []
    engine._store = SimpleNamespace(save_order_state=lambda **kwargs: store_calls.append(kwargs))
    engine._order_trackers = {}
    engine._order_symbols = {}
    engine._active_order_ids = set()
    engine._book_venue_terminal_fill = lambda *_args, **_kwargs: asyncio.sleep(0, result=True)
    engine._api_async = lambda *_args, **_kwargs: asyncio.sleep(
        0, result=_venue_order(status="FILLED", client="c", executed="1")
    )
    assert await engine._adjudicate_unknown_slice("i", "BTCUSDT", "c", 0) == "FOUND"
    assert store_calls[-1]["status"] == "FILLED"
    engine._api_async = lambda *_args, **_kwargs: asyncio.sleep(
        0, result=_venue_order(status="CANCELED", client="c2", executed="0.5")
    )
    assert await engine._adjudicate_unknown_slice("i", "BTCUSDT", "c2", None) == "FOUND"
    engine._api_async = lambda *_args, **_kwargs: asyncio.sleep(0, result=_venue_order(status="NEW", client="c3"))
    assert await engine._adjudicate_unknown_slice("i", "BTCUSDT", "c3", None) == "FOUND"
    assert "venue-1" in engine._active_order_ids
    engine._api_async = lambda *_args, **_kwargs: asyncio.sleep(
        0,
        result=_venue_order(status="PARTIALLY_FILLED", client="c-partial", executed="0.5", order_id="venue-partial"),
    )
    assert await engine._adjudicate_unknown_slice("i", "BTCUSDT", "c-partial", None) == "FOUND"

    engine._store = SimpleNamespace()
    engine._api_async = lambda *_args, **_kwargs: asyncio.sleep(
        0, result=_venue_order(status="FILLED", client="c4", executed="1")
    )
    assert await engine._adjudicate_unknown_slice("i", "BTCUSDT", "c4", None) == "INCONCLUSIVE"

    class Child:
        def __init__(self, client_order_id: str):
            self.client_order_id = client_order_id

    class RiskSM:
        def __init__(self):
            self.rearmed = []

        def rearm_for_retry(self, approval):
            self.rearmed.append(approval)

    class Approval:
        def __init__(self):
            self.rearmed = []

        def rearm_nonce(self, nonce):
            self.rearmed.append(nonce)

    actions: list[tuple] = []
    unknown = [
        None,
        {"intent_id": "bad", "client_order_id": "", "symbol": "BTCUSDT"},
        {
            "intent_id": "absent",
            "client_order_id": "parent-a",
            "symbol": "BTCUSDT",
            "risk_approval_id": "a",
            "risk_nonce": "n",
        },
        {"intent_id": "found", "client_order_id": "parent-f", "symbol": "BTCUSDT"},
        {"intent_id": "inconclusive", "client_order_id": "parent-i", "symbol": "BTCUSDT"},
    ]
    outbox = SimpleNamespace(
        get_unknown_intents=lambda: unknown,
        restore_execution_plan=lambda intent_id: (
            SimpleNamespace(children=[Child("slice-1"), Child("slice-2")]) if intent_id == "found" else None
        ),
        resolve_unknown=lambda *args, **kwargs: actions.append((args, kwargs)),
    )
    resolver = AutonomousEngine.__new__(AutonomousEngine)
    resolver._outbox = outbox
    resolver._risk_sm = RiskSM()
    resolver._approval = Approval()

    async def adjudicate(intent_id, _symbol, client_id, sequence):
        if intent_id == "absent":
            return "ABSENT"
        if intent_id == "found":
            return "FOUND" if client_id == "slice-1" else "ABSENT"
        return "INCONCLUSIVE"

    resolver._adjudicate_unknown_slice = adjudicate
    assert await resolver._resolve_unknown_outbox_intents() == 2
    assert [item[0][0] for item in actions] == ["absent", "found"]
    assert [str(item) for item in resolver._risk_sm.rearmed] == ["a"] and resolver._approval.rearmed == ["n"]

    class RaisingRiskSM:
        def rearm_for_retry(self, _approval) -> None:
            raise RuntimeError("risk store")

    retry_actions: list[tuple] = []
    retry = AutonomousEngine.__new__(AutonomousEngine)
    retry._outbox = SimpleNamespace(
        get_unknown_intents=lambda: [
            {
                "intent_id": "rearm-error",
                "client_order_id": "parent",
                "symbol": "BTCUSDT",
                "risk_approval_id": "approval",
                "risk_nonce": "nonce",
            }
        ],
        resolve_unknown=lambda *args, **kwargs: retry_actions.append((args, kwargs)),
    )
    retry._risk_sm = RaisingRiskSM()
    retry._approval = Approval()
    retry._adjudicate_unknown_slice = lambda *_args, **_kwargs: asyncio.sleep(0, result="ABSENT")
    assert await retry._resolve_unknown_outbox_intents() == 1
    assert retry_actions and retry._approval.rearmed == ["nonce"]

    resolver._outbox = SimpleNamespace(get_unknown_intents=lambda: (_ for _ in ()).throw(OSError("db")))
    assert await resolver._resolve_unknown_outbox_intents() == 0


def test_engine_coverage_gates_and_invalid_protection_quantities() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._env_mode = SimpleNamespace(value="live")
    engine._position_generation = {"BTCUSDT": 1}
    engine._position_projection = {"BTCUSDT": {"position_generation": 1}}
    engine._protection_owner_id = "svc"
    engine._symbol_precision = {}
    covered, evidence = engine._assess_protection_coverage(
        [{"symbol": "BTCUSDT", "positionAmt": "1"}],
        [
            {
                "symbol": "BTCUSDT",
                "status": "ACTIVE",
                "exchange_order_id": "sl-1",
                "owner_id": "svc",
                "side": "SELL",
                "position_generation": 1,
                "quantity": "0",
                "stop_type": "ATR_BASED",
            },
            {
                "symbol": "BTCUSDT",
                "status": "ACTIVE",
                "exchange_order_id": "sl-2",
                "owner_id": "svc",
                "side": "SELL",
                "position_generation": 1,
                "quantity": "not-a-number",
                "order_type": "STOP_MARKET",
            },
        ],
        {"p": SimpleNamespace(instrument_id="BTCUSDT")},
    )
    assert covered is False
    assert evidence["unprotected_symbols"][0]["reason"] == "STOP_LOSS_QUANTITY_UNCOVERED"

    engine._state_backend_supported = True
    engine._can_write = False
    engine._policy_error = None
    engine._running = False
    engine._durable_fact_status = lambda: (True, "OK", {})
    engine._lifecycle = SimpleNamespace(state=ModuleState.ACTIVE)
    engine._feed = SimpleNamespace(is_healthy=lambda: True)
    engine._control = SimpleNamespace(get_status=lambda: ControlAction.RESUME)
    engine._protection_owner_unknown = False
    engine._last_reconciliation_result = SimpleNamespace(matched=True)
    engine._user_stream_readiness = lambda: (True, {})
    engine._realtime_age_seconds = lambda: 0.0
    assert engine._check_ready() is True
    engine._durable_fact_status = lambda: (False, "UNKNOWN", {})
    assert engine._check_ready() is False
    engine._durable_fact_status = lambda: (True, "OK", {})
    engine._lifecycle.state = ModuleState.DEGRADED
    assert engine._check_ready() is False
    engine._lifecycle.state = ModuleState.ACTIVE
    engine._feed.is_healthy = lambda: False
    assert engine._check_ready() is False
    engine._feed.is_healthy = lambda: True
    engine._control.get_status = lambda: ControlAction.NO_NEW_RISK
    assert engine._check_ready() is False
    engine._control.get_status = lambda: ControlAction.RESUME
    engine._protection_owner_unknown = True
    assert engine._check_ready() is False
    engine._protection_owner_unknown = False
    engine._last_reconciliation_result = None
    assert engine._check_ready() is False


def test_engine_unowned_protection_initializes_issue_set_and_fails_closed() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._protection_owner_unknown = False
    engine._control = SimpleNamespace(
        get_status=lambda: ControlAction.RESUME,
    )
    engine._safe_no_new_risk = lambda source: setattr(engine, "safe_source", source)
    engine._alerts = SimpleNamespace(send_incident=lambda *args, **kwargs: setattr(engine, "incident", args))
    engine._block_unowned_protection_orders(["algo-1", "algo-2"])
    assert engine._protection_owner_unknown is True
    assert engine._protection_issues == {"algo-1", "algo-2"}
    assert engine.safe_source == "auto"
    assert engine.incident


@pytest.mark.asyncio
async def test_realtime_tick_covers_empty_durable_claim_and_reconciliation_exception() -> None:
    feed = _FeedProbe(valid_ws=True)
    engine = _realtime_shell(feed, simulate=False)
    engine._tick_count = 1
    engine._can_simulate = True
    engine._outbox._db_path = "durable"
    engine._outbox.claim = lambda **_kwargs: None

    async def broken_reconciliation() -> None:
        raise RuntimeError("reconciliation unavailable")

    engine._reconciliation_segment = broken_reconciliation
    await engine._realtime_tick()
    assert engine._last_realtime_mono > 0
    assert engine._error_count == 0


@pytest.mark.asyncio
async def test_nearline_tick_covers_risk_gates_and_a_successful_signed_intent(monkeypatch) -> None:
    import beidou_core.engine as engine_module

    early = AutonomousEngine.__new__(AutonomousEngine)
    early._strategy_risk = SimpleNamespace(
        get_state=lambda _sid: SimpleNamespace(
            risk_level=StrategyRiskLevel.LOCKED,
            active_circuit_breakers=[SimpleNamespace(value="DAILY_LOSS_LIMIT")],
        ),
        is_trading_allowed=lambda _sid: False,
    )
    early._autopilot_strategy_id = StrategyId("autopilot")
    early._lifecycle = SimpleNamespace(get_degradation_level=lambda: SimpleNamespace(value="LOCKED"))
    early._last_stale_resolve_mono = time.monotonic()
    early._last_order_state_resolve_mono = time.monotonic()
    early._last_account = {}
    await AutonomousEngine._nearline_tick(early)
    assert early._last_risk_fact_at > 0

    e = AutonomousEngine.__new__(AutonomousEngine)
    now = datetime.now(timezone.utc)
    features = {
        "symbol": "BTCUSDT",
        "close": 100.0,
        "bar_is_closed": True,
        "bar_open_time": now - timedelta(minutes=2),
        "bar_close_time": now - timedelta(minutes=1),
        "bar_available_at": now - timedelta(minutes=1),
        "n_candles": 20,
        "ann_volatility": 0.1,
        "spread_bps": 2.0,
        "atr_pct": 2.0,
    }
    e._strategy_risk = SimpleNamespace(
        get_state=lambda _sid: SimpleNamespace(
            risk_level=StrategyRiskLevel.NORMAL,
            active_circuit_breakers=[],
            current_drawdown_pct=0.0,
            daily_pnl=0.0,
        ),
        is_trading_allowed=lambda _sid: True,
        get_budget=lambda _sid: RiskBudget(
            StrategyId("autopilot"),
            max_position_notional=100_000.0,
            risk_per_trade_pct=1.0,
        ),
    )
    e._autopilot_strategy_id = StrategyId("autopilot")
    e._lifecycle = SimpleNamespace(get_degradation_level=lambda: SimpleNamespace(value="ACTIVE"))
    e._control = SimpleNamespace(
        get_status=lambda: ControlAction.RESUME,
        should_accept=lambda _intent: True,
    )
    e._env_mode = SimpleNamespace(value="paper")
    e._can_write = False
    e._can_trade = True
    e._risk_can_withdraw = False
    e._running = True
    e._tick_count = 10
    e._error_count = 0
    e._last_realtime = time.time()
    e._last_realtime_mono = time.monotonic()
    e._check_liveness = lambda: engine_module.HealthState.HEALTHY
    e._last_market_state = SimpleNamespace(state_hash="market-hash")
    e._symbols = ["BTCUSDT"]
    e._policy_id_active = None
    e._policy_version = None
    e._last_reconciliation_result = SimpleNamespace(
        checked_at=now,
        matched=True,
    )
    e._last_account = {"totalWalletBalance": "10000", "positions": [{"symbol": "BTCUSDT", "positionAmt": "0"}]}
    e._last_prices = {"BTCUSDT": 100.0}
    e._last_stale_resolve_mono = time.monotonic()
    e._last_order_state_resolve_mono = time.monotonic()
    e._fresh_matched_reconciliation = lambda **_kwargs: True
    e._recently_matched_reconciliation = lambda **_kwargs: True
    e._protection = SimpleNamespace(all_positions=lambda: {}, position_count=lambda: 0)
    e._trading_pool = SimpleNamespace(
        active_instruments=lambda: ["BTCUSDT"],
        is_tradable=lambda _symbol: True,
    )
    e._feed = SimpleNamespace(
        async_get_kline_features=lambda _symbol, timeframe, _limit: asyncio.sleep(
            0, result=features if timeframe == "1m" else {}
        )
    )
    e._advance_factor_bar = lambda *_args: True
    e._store_factor_predictions = lambda *_args: None
    e._estimate_market_state = lambda *_args, **_kwargs: {"direction": "UP", "stress": "NORMAL", "quality": "RELIABLE"}
    proposal = SimpleNamespace(side=engine_module.OrderSide.BUY, strength=0.8, confidence=0.9)
    e._strategy_kernel = SimpleNamespace(
        evaluate=lambda _context: asyncio.sleep(0, result={"kernel": "typed_graph", "proposal": proposal})
    )
    e._position_entry_times = {}
    e._symbol_precision = {"BTCUSDT": {"min_quantity": 0.001, "quantity": 3, "min_notional": 5.0}}
    e._policy_params = {}
    e._policy_error = None
    e._settings = SimpleNamespace(
        production=SimpleNamespace(
            max_leverage=3.0,
            max_concentration_pct=100.0,
            max_drawdown_pct=20.0,
            max_daily_loss_pct=5.0,
            max_consecutive_losses=5,
            min_sharpe_rolling=-10.0,
            max_total_leverage=3.0,
        )
    )
    e._loss_count = 0
    e._drift_detector = SimpleNamespace(is_calibrated=lambda: False, _baseline={})
    e._active_order_ids = set()
    e._outbox = SimpleNamespace(
        _outbox=[],
        _processed=set(),
        _inbox={},
        inflight_signed_quantity=lambda _symbol: Decimal("0"),
        duplicate_order_count_24h=lambda: 0,
        commit=lambda intent: setattr(e, "committed_intent", intent),
    )
    e._optimizer = SimpleNamespace(
        resolve_conflicts=lambda targets: (targets, 0),
        allocate_capital=lambda _ids, _total: {StrategyId("autopilot"): MonetaryValue(amount="1000", currency="USDT")},
    )
    e._cost_model = SimpleNamespace(
        set_fee_tier=lambda *_args: None,
        estimate_order=lambda *_args, **_kwargs: SimpleNamespace(total_fee_bps=1.0),
    )
    e._fuser = engine_module.SignalFuser()
    e._venue_exact_quantity = lambda _symbol, amount, **_kwargs: amount
    e._approval = SimpleNamespace(
        issue_for_approved_risk=lambda *_args, **_kwargs: "signature",
        verify=lambda *_args, **_kwargs: asyncio.sleep(0, result=True),
    )
    e._risk_sm = SimpleNamespace(approve_if_verified=lambda *_args, **_kwargs: RiskDecision.APPROVED)
    e._pre_risk = SimpleNamespace(
        check=lambda _context: asyncio.sleep(0, result=[SimpleNamespace(decision=RiskDecision.APPROVED, reason="ok")])
    )

    async def _full_evaluate(*_args, **_kwargs):
        return [SimpleNamespace(decision=RiskDecision.APPROVED, rule_level=SimpleNamespace(value="R0"), reason="ok")]

    e._risk_engine = SimpleNamespace(full_evaluate=_full_evaluate)
    e._post_risk = SimpleNamespace(record_violation=lambda *_args: None)
    e._apply_fee_tier = lambda *_args: None
    e._capital_budget_amount = lambda _balance: MonetaryValue(amount="1000", currency="USDT")
    e._check_post_risk_safety = lambda *_args: None
    e._retry_missing_protections = lambda *_args: asyncio.sleep(0)
    e._cleanup_excess_orders = lambda: asyncio.sleep(0)
    e._cancel_algo_orders = lambda *_args: asyncio.sleep(0)
    e._remove_protection_with_cleanup = lambda *_args: None
    e._account = None
    e._protection_owner_unknown = False

    monkeypatch.setattr(
        engine_module.RiskRuleRegistry,
        "evaluate_all",
        classmethod(lambda _cls, _ctx: {f"R{i}": engine_module.RuleDecision.PASS for i in range(11)}),
    )
    monkeypatch.setattr(engine_module.RiskRuleRegistry, "is_approved", classmethod(lambda _cls, _results: True))
    await AutonomousEngine._nearline_tick(e)
    assert e.committed_intent.instrument_id == "BTCUSDT"
    assert e.committed_intent.side is engine_module.OrderSide.BUY
