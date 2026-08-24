"""End-to-end-in-process coverage for engine startup and execution gates.

The doubles below model exchange responses and durable acknowledgements.  They
never call a venue and assert the engine's fail-closed decisions instead of
merely importing code.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

import beidou_core.engine as engine_module
from beidou_core.engine import AutonomousEngine
from beidou_exchange.core.protocol import OrderResponse
from beidou_safety.execution import OrderIntent
from beidou_shared.types import (
    AccountId,
    AccountRef,
    InstrumentId,
    OrderSide,
    OrderStatus,
    OrderType,
    Price,
    Quantity,
    VenueId,
)


def _intent(
    intent_id: str = "runtime-intent",
    *,
    reduce_only: bool = False,
    net_alpha_bps: float = 1.0,
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
        instrument_id=InstrumentId("BTCUSDT"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(amount="1"),
        client_order_id=f"cid-{intent_id}",
        idempotency_key=f"idem-{intent_id}",
        reduce_only=reduce_only,
        net_alpha_bps=net_alpha_bps,
    )


class _RejectingOutbox:
    def __init__(self) -> None:
        self.rejected: list[tuple[str, str]] = []
        self.unknown: list[tuple[str, str]] = []

    def reject(self, intent_id: str, reason: str, **_kwargs: object) -> None:
        self.rejected.append((str(intent_id), str(reason)))

    def mark_unknown(self, intent_id: str, reason: str) -> None:
        self.unknown.append((str(intent_id), str(reason)))


class _Book:
    def __init__(self, book: dict | None = None, features: dict | None = None) -> None:
        self.book = book
        self.features = features or {}

    async def async_fetch_orderbook(self, _symbol: str, _limit: int) -> dict | None:
        if isinstance(self.book, BaseException):
            raise self.book
        return self.book

    async def async_update_features(self, _symbol: str) -> dict:
        if isinstance(self.features, BaseException):
            raise self.features
        return self.features


class _PlanAlgorithm:
    def __init__(self, plan: object) -> None:
        self.plan_value = plan

    def plan(self, _ctx: object, _order_id: object) -> object:
        return self.plan_value


def _plan_slice(
    sequence: int,
    *,
    valid: bool = True,
    quantity: str = "1",
    price: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        sequence_number=sequence,
        slice_id=f"slice-{sequence}",
        invariants_check_passed=valid,
        quantity=SimpleNamespace(amount=quantity),
        price=SimpleNamespace(amount=price) if price is not None else None,
        order_type=OrderType.MARKET,
        time_in_force=SimpleNamespace(value="GTC"),
    )


def _plan(*slices: object, canceled: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        slices=list(slices),
        is_canceled=canceled,
        cancel_reason="cancelled-by-test" if canceled else None,
        algorithm=SimpleNamespace(value="TWAP"),
        total_quantity=lambda: 1.0,
        total_estimated_cost_bps=2.0,
        estimated_completion_seconds=1.0,
    )


def _planning_engine(feed: object, outbox: _RejectingOutbox) -> AutonomousEngine:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._feed = feed
    engine._outbox = outbox
    engine._exec_quality_history = {}
    engine._symbol_precision = {"BTCUSDT": {"min_quantity": 0.1, "min_notional": 0.0}}
    engine._apply_fee_tier = lambda _venue: None
    engine._cost_model = SimpleNamespace(estimate_order=lambda *_args, **_kwargs: SimpleNamespace(total_fee_bps=2.0))
    engine._exec_selector = SimpleNamespace(select=lambda _ctx: None)
    return engine


@pytest.mark.asyncio
async def test_engine_plan_market_and_cost_fail_closed_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    # A complete order book reaches the normal context boundary, while zero
    # depth is rejected as an unverifiable market fact.
    outbox = _RejectingOutbox()
    engine = _planning_engine(_Book({"bids": [["99", "1"]], "asks": [["101", "1"]]}), outbox)
    engine._exec_selector = SimpleNamespace(select=lambda _ctx: None)
    assert await engine._plan_execution(_intent("no-algo"), "BTCUSDT", "cid-no-algo") is None
    assert outbox.rejected[-1][1] == "EXECUTION_ALGORITHM_UNAVAILABLE"

    outbox = _RejectingOutbox()
    engine = _planning_engine(_Book({"bids": [["99", "0"]], "asks": [["101", "0"]]}), outbox)
    assert await engine._plan_execution(_intent("no-depth"), "BTCUSDT", "cid-no-depth") is None
    assert outbox.rejected[-1][1] == "MARKET_DEPTH_UNKNOWN"

    # Feature fallback is permitted for a reduce-only path, but a cost model
    # failure still remains observable while the emergency path continues to
    # the algorithm gate.
    outbox = _RejectingOutbox()
    engine = _planning_engine(_Book({}, {"bid": 99, "ask": 101, "spread_bps": 200}), outbox)
    engine._cost_model = SimpleNamespace(
        estimate_order=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cost"))
    )
    monkeypatch.setattr(engine_module.ExecutionAlgorithmSelector, "ALL_ALGORITHMS", [])
    assert await engine._plan_execution(_intent("reduce-no-algo", reduce_only=True), "BTCUSDT", "cid-reduce") is None
    assert outbox.rejected[-1][1] == "EXECUTION_ALGORITHM_UNAVAILABLE"

    # Non-reduce intents cannot proceed when neither the order book nor the
    # feature fallback proves executable prices.
    outbox = _RejectingOutbox()
    engine = _planning_engine(_Book({}, {}), outbox)
    assert await engine._plan_execution(_intent("no-market"), "BTCUSDT", "cid-no-market") is None
    assert outbox.rejected[-1][1] == "MARKET_DATA_UNKNOWN"

    # A cost-model exception is a hard rejection for risk-increasing orders.
    outbox = _RejectingOutbox()
    engine = _planning_engine(_Book({"bids": [["99", "1"]], "asks": [["101", "1"]]}), outbox)
    engine._cost_model = SimpleNamespace(
        estimate_order=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cost"))
    )
    assert await engine._plan_execution(_intent("cost-unknown"), "BTCUSDT", "cid-cost") is None
    assert outbox.rejected[-1][1] == "COST_MODEL_UNKNOWN"


@pytest.mark.asyncio
async def test_engine_plan_algorithm_and_slice_invariant_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    good_book = _Book({"bids": [["99", "2"]], "asks": [["101", "2"]]})

    async def run_plan(
        intent_id: str, plan: object, *, validate: tuple[bool, str] = (True, "OK"), quantized: str | None = "1"
    ):
        outbox = _RejectingOutbox()
        engine = _planning_engine(good_book, outbox)
        engine._exec_selector = SimpleNamespace(select=lambda _ctx: _PlanAlgorithm(plan))
        engine._quantize_slice_quantity = lambda _symbol, _qty: quantized
        monkeypatch.setattr(engine_module.SliceInvariantChecker, "validate_plan", staticmethod(lambda *_args: validate))
        return engine, outbox, await engine._plan_execution(_intent(intent_id), "BTCUSDT", f"cid-{intent_id}")

    engine, outbox, result = await run_plan("cancel", _plan(canceled=True))
    assert result is None and outbox.rejected[-1][1].startswith("EXECUTION_PLAN_CANCELED")

    engine, outbox, result = await run_plan("empty", _plan())
    assert result is None and outbox.rejected[-1][1] == "EXECUTION_PLAN_EMPTY"

    engine, outbox, result = await run_plan("invariant", _plan(_plan_slice(0)), validate=(False, "hard-limit"))
    assert result is None and outbox.rejected[-1][1].startswith("EXECUTION_PLAN_INVARIANT_FAILED")

    engine, outbox, result = await run_plan("invalid-slice", _plan(_plan_slice(0, valid=False)), quantized="1")
    assert result is None and outbox.rejected[-1][1] == "EXECUTION_PLAN_NO_VALID_SLICES"

    engine, outbox, result = await run_plan("zero-quantity", _plan(_plan_slice(0)), quantized=None)
    assert result is None and outbox.rejected[-1][1] == "EXECUTION_PLAN_NO_VALID_SLICES"

    outbox = _RejectingOutbox()
    engine = _planning_engine(good_book, outbox)
    engine._exec_selector = SimpleNamespace(select=lambda _ctx: _PlanAlgorithm(_plan(_plan_slice(0))))
    engine._quantize_slice_quantity = lambda _symbol, _qty: "1"
    monkeypatch.setattr(engine_module.SliceInvariantChecker, "validate_plan", staticmethod(lambda *_args: (True, "OK")))
    monkeypatch.setattr(
        AutonomousEngine, "_validate_slices_against_intent", staticmethod(lambda *_args, **_kwargs: (False, "binding"))
    )
    assert await engine._plan_execution(_intent("binding"), "BTCUSDT", "cid-binding") is None
    assert outbox.rejected[-1][1] == "EXECUTION_SLICE_BINDING_FAILED:binding"

    # A valid plan returns the immutable execution context and the exact
    # venue-quantized child contract.
    outbox = _RejectingOutbox()
    engine = _planning_engine(good_book, outbox)
    engine._exec_selector = SimpleNamespace(select=lambda _ctx: _PlanAlgorithm(_plan(_plan_slice(0))))
    engine._quantize_slice_quantity = lambda _symbol, _qty: "1"
    monkeypatch.setattr(engine_module.SliceInvariantChecker, "validate_plan", staticmethod(lambda *_args: (True, "OK")))
    monkeypatch.setattr(
        AutonomousEngine, "_validate_slices_against_intent", staticmethod(lambda *_args, **_kwargs: (True, "OK"))
    )
    result = await engine._plan_execution(_intent("valid-plan"), "BTCUSDT", "cid-valid")
    assert result is not None and result[0][0][0] == "1"


def _runtime_engine(monkeypatch: pytest.MonkeyPatch, *, account_ok: bool = True) -> AutonomousEngine:
    engine = AutonomousEngine(["BTCUSDT"], mode="paper")
    engine._health.start = lambda: None
    engine._health.stop = lambda: None
    engine._feed.start_ws = lambda *_args, **_kwargs: asyncio.sleep(0, result=True)
    engine._feed.is_healthy = lambda: True
    engine._start_user_stream = lambda: asyncio.sleep(0, result=False)
    engine._get_open_algo_inventory = lambda: asyncio.sleep(0, result=[])
    engine._ensure_exchange_position_protections = lambda: asyncio.sleep(0)
    engine._reconcile = lambda: asyncio.sleep(0, result=True)
    engine._durable_fact_status = lambda: (True, "OK", {})
    engine._sync_adapter_rule_snapshots = lambda _info: True
    engine._adapter.reset_circuit_breaker = lambda: None
    engine._adapter.is_circuit_breaker_open = lambda: False
    engine._api_async_safe = lambda endpoint, **_kwargs: asyncio.sleep(
        0,
        result=(
            ({"serverTime": 1}, True)
            if endpoint == engine_module.Endpoint.SERVER_TIME
            else (
                ({"totalWalletBalance": "1000", "positions": [], "canTrade": True}, True)
                if account_ok
                else (None, False)
            )
        ),
    )
    engine._api_async = lambda endpoint, **_kwargs: asyncio.sleep(
        0,
        result=[]
        if endpoint in {engine_module.Endpoint.OPEN_ORDERS, engine_module.Endpoint.OPEN_ALGO_ORDERS}
        else {"symbols": []},
    )

    async def stop_after_first_tick() -> None:
        engine._running = False

    engine._realtime_tick = stop_after_first_tick
    engine._shutdown = lambda: asyncio.sleep(0, result=setattr(engine, "shutdown_seen", True))
    return engine


@pytest.mark.asyncio
async def test_engine_run_startup_success_and_fatal_connectivity_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    # Other constructor tests intentionally bind the process-local store to a
    # temporary database.  Start this lifecycle matrix from a clean singleton
    # binding so the constructor's own paper database path is exercised.
    monkeypatch.setattr(engine_module.PersistentStore, "_instance", None)
    engine = _runtime_engine(monkeypatch)
    await engine.run()
    assert engine.shutdown_seen

    # Retry loops are tested with a zero-duration sleep so the failure path is
    # deterministic and never contacts the exchange.
    async def no_sleep(_seconds: float, result: object = None) -> object:
        return result

    monkeypatch.setattr(engine_module.asyncio, "sleep", no_sleep)
    failed_server = _runtime_engine(monkeypatch)
    failed_server._api_async_safe = lambda *_args, **_kwargs: asyncio.sleep(0, result=(None, False))
    await failed_server.run()
    assert failed_server._lifecycle.state.value == "FAILED"

    failed_account = _runtime_engine(monkeypatch, account_ok=False)
    failed_account._api_async_safe = lambda endpoint, **_kwargs: asyncio.sleep(
        0, result=({"serverTime": 1}, True) if endpoint == engine_module.Endpoint.SERVER_TIME else (None, False)
    )
    await failed_account.run()
    assert failed_account._lifecycle.state.value == "FAILED"


@pytest.mark.asyncio
async def test_engine_run_restores_exchange_orders_rules_and_reaches_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the non-empty startup recovery path and all loop ownership hooks."""

    # The constructor is intentionally real: startup uses the same store,
    # lifecycle, adapter and health objects as the application.  Only venue
    # reads and the final tick are deterministic test authorities.
    monkeypatch.setattr(engine_module.PersistentStore, "_instance", None)
    engine = _runtime_engine(monkeypatch)
    order = {
        "orderId": "7001",
        "symbol": "BTCUSDT",
        "status": "PARTIALLY_FILLED",
        "side": "BUY",
        "type": "LIMIT",
        "origQty": "1",
        "executedQty": "0.4",
        "price": "100",
        "clientOrderId": "beidou-btcusdt-close-7001",
        "reduceOnly": True,
        "stopPrice": "",
    }
    exchange_info = {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                ],
            },
            {
                "symbol": "ETHUSDT",
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                    {"filterType": "MIN_NOTIONAL", "notional": "not-a-number"},
                ],
            },
            {
                "symbol": "SOLUSDT",
                "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"}],
            },
        ]
    }

    async def api(endpoint: object, **_kwargs: object) -> object:
        if endpoint == engine_module.Endpoint.OPEN_ORDERS:
            return [order]
        return []

    async def safe_api(endpoint: object, **_kwargs: object) -> tuple[object, bool]:
        if endpoint == engine_module.Endpoint.SERVER_TIME:
            return {"serverTime": 1}, True
        if endpoint == engine_module.Endpoint.ACCOUNT:
            return {"totalWalletBalance": "1000", "positions": [], "canTrade": True}, True
        if endpoint == engine_module.Endpoint.EXCHANGE_INFO:
            return exchange_info, True
        return {}, True

    engine._api_async = api
    engine._api_async_safe = safe_api
    engine._get_open_algo_inventory = lambda: asyncio.sleep(0, result=[])
    engine._adapter.is_circuit_breaker_open = lambda: True
    engine._durable_fact_status = lambda: (True, "OK", {})
    engine._fresh_matched_reconciliation = lambda **_kwargs: True
    engine._realtime_age_seconds = lambda: 10.0

    async def stop_after_tick() -> None:
        engine._running = False

    engine._realtime_tick = stop_after_tick
    await engine.run()

    assert "7001" in engine._active_order_ids
    assert "7001" in engine._close_order_ids
    assert engine._symbol_precision["BTCUSDT"]["min_quantity"] == pytest.approx(0.001)
    assert "ETHUSDT" not in engine._symbol_precision
    assert engine._lifecycle.state.value == "ACTIVE"


@pytest.mark.asyncio
async def test_engine_run_writable_permissions_stream_and_exchange_info_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise startup gates that are only reachable in writable mode."""

    monkeypatch.setattr(engine_module.PersistentStore, "_instance", None)

    async def no_sleep(_seconds: float, result: object = None) -> object:
        return result

    monkeypatch.setattr(engine_module.asyncio, "sleep", no_sleep)

    # Testnet keeps retrying the server-time authority before it proceeds to
    # the account gate.  The account remains unavailable, so the run is still
    # fail-closed and never starts its clocks.
    long_retry = _runtime_engine(monkeypatch)
    long_retry._env_mode = SimpleNamespace(value="testnet")
    server_attempts = 0

    async def long_retry_api(endpoint: object, **_kwargs: object) -> tuple[object, bool]:
        nonlocal server_attempts
        if endpoint == engine_module.Endpoint.SERVER_TIME:
            server_attempts += 1
            return ({"serverTime": 1}, True) if server_attempts >= 6 else (None, False)
        return None, False

    long_retry._api_async_safe = long_retry_api
    await long_retry.run()
    assert server_attempts == 6
    assert long_retry._lifecycle.state.value == "FAILED"

    # A writable account with trading disabled is a terminal startup gate.
    permission_blocked = _runtime_engine(monkeypatch)
    permission_blocked._can_write = True
    permission_blocked._safe_no_new_risk = lambda *_args: None
    permission_blocked._record_execution_fact_failure_env_guarded = lambda *_args, **_kwargs: None
    permission_blocked._alerts = SimpleNamespace(send_incident=lambda *_args, **_kwargs: None)

    async def blocked_account_api(endpoint: object, **_kwargs: object) -> tuple[object, bool]:
        if endpoint == engine_module.Endpoint.SERVER_TIME:
            return {"serverTime": 1}, True
        return {
            "totalWalletBalance": "1000",
            "positions": [],
            "canTrade": False,
            "canWithdraw": False,
        }, True

    permission_blocked._api_async_safe = blocked_account_api
    await permission_blocked.run()
    assert permission_blocked._lifecycle.state.value == "FAILED"

    # Writable mode records both REST fallback and user-stream timeout as
    # blocked authority facts, then continues through read-only recovery.
    stream_timeout = _runtime_engine(monkeypatch)
    stream_timeout._can_write = True
    stream_timeout._safe_no_new_risk = lambda *_args: None
    stream_timeout._record_execution_fact_failure_env_guarded = lambda *_args, **_kwargs: None
    stream_timeout._store.restore_account_opening_projection = lambda *_args: object()
    stream_timeout._feed.start_ws = lambda *_args, **_kwargs: no_sleep(0, result=False)

    async def timed_out_user_stream() -> bool:
        raise asyncio.TimeoutError

    stream_timeout._start_user_stream = timed_out_user_stream

    async def writable_api(endpoint: object, **_kwargs: object) -> tuple[object, bool]:
        if endpoint == engine_module.Endpoint.SERVER_TIME:
            return {"serverTime": 1}, True
        if endpoint == engine_module.Endpoint.ACCOUNT:
            return {
                "totalWalletBalance": "1000",
                "positions": [],
                "canTrade": True,
                "canWithdraw": False,
            }, True
        if endpoint == engine_module.Endpoint.EXCHANGE_INFO:
            return {"symbols": []}, True
        return {}, True

    stream_timeout._api_async_safe = writable_api
    stream_timeout._resolve_unknown_outbox_intents = lambda: no_sleep(0, result=1)
    await stream_timeout.run()
    assert stream_timeout.shutdown_seen

    # A discovered order with no current-worker ownership remains untouched;
    # persistence failure is observable but does not broaden cancellation.
    owner_unknown = _runtime_engine(monkeypatch)
    owner_unknown._can_write = True
    owner_unknown._safe_no_new_risk = lambda *_args: None
    owner_unknown._record_execution_fact_failure_env_guarded = lambda *_args, **_kwargs: None
    owner_unknown._store.restore_account_opening_projection = lambda *_args: object()
    owner_unknown._store.save_order_state = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("store"))
    owner_unknown._alerts = SimpleNamespace(send_incident=lambda *_args, **_kwargs: None)
    owner_unknown._owned_order_ids = set()
    discovered = {
        "orderId": "7010",
        "symbol": "BTCUSDT",
        "status": "NEW",
        "side": "BUY",
        "type": "LIMIT",
        "origQty": "1",
        "executedQty": "0",
        "price": "100",
        "clientOrderId": "beidou-entry-7010",
    }

    async def owner_api(endpoint: object, **_kwargs: object) -> object:
        return [discovered] if endpoint == engine_module.Endpoint.OPEN_ORDERS else []

    owner_unknown._api_async = owner_api
    owner_unknown._api_async_safe = writable_api
    owner_unknown._resolve_unknown_outbox_intents = lambda: no_sleep(0, result=1)
    await owner_unknown.run()
    assert owner_unknown.shutdown_seen

    # An unavailable exchangeInfo response initializes an empty precision
    # cache, including the cold attribute path used by a fresh process.
    info_missing = _runtime_engine(monkeypatch)
    info_missing._api_async_safe = lambda endpoint, **_kwargs: no_sleep(
        0,
        result=(
            ({"serverTime": 1}, True)
            if endpoint == engine_module.Endpoint.SERVER_TIME
            else (
                (None, False)
                if endpoint == engine_module.Endpoint.EXCHANGE_INFO
                else ({"totalWalletBalance": "1000", "positions": [], "canTrade": True}, True)
            )
        ),
    )
    await info_missing.run()
    assert info_missing.shutdown_seen and info_missing._symbol_precision == {}

    # A raised exchangeInfo request is normalized to the same empty sync input
    # instead of leaking an unbound local into the startup path.
    info_error = _runtime_engine(monkeypatch)

    async def error_info_api(endpoint: object, **_kwargs: object) -> tuple[object, bool]:
        if endpoint == engine_module.Endpoint.SERVER_TIME:
            return {"serverTime": 1}, True
        if endpoint == engine_module.Endpoint.EXCHANGE_INFO:
            raise RuntimeError("exchange-info")
        return {"totalWalletBalance": "1000", "positions": [], "canTrade": True}, True

    info_error._api_async_safe = error_info_api
    await info_error.run()
    assert info_error.shutdown_seen


@pytest.mark.asyncio
async def test_engine_run_account_recovery_and_clock_loop_error_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine_module.PersistentStore, "_instance", None)

    # Initial account access succeeds, but the independent position-recovery
    # query fails twice.  The engine remains alive in DEGRADED state.
    recovery = _runtime_engine(monkeypatch)
    account_calls = 0

    async def recovery_api(endpoint: object, **_kwargs: object) -> tuple[object, bool]:
        nonlocal account_calls
        if endpoint == engine_module.Endpoint.SERVER_TIME:
            return {"serverTime": 1}, True
        if endpoint == engine_module.Endpoint.ACCOUNT:
            account_calls += 1
            if account_calls == 1:
                return {"totalWalletBalance": "1000", "positions": [], "canTrade": True}, True
            recovery._health_started = False
            return None, False
        if endpoint == engine_module.Endpoint.EXCHANGE_INFO:
            return {"symbols": []}, True
        return {}, True

    recovery._api_async_safe = recovery_api
    recovery._adapter.is_circuit_breaker_open = lambda: True
    recovery._store.restore_account_opening_projection = lambda *_args: object()
    await recovery.run()
    assert recovery._lifecycle.state.value == "DEGRADED"
    assert account_calls == 3

    base_sleep = asyncio.sleep

    async def run_clock_case(kind: str) -> AutonomousEngine:
        engine = _runtime_engine(monkeypatch)
        engine._store.restore_account_opening_projection = lambda *_args: object()
        engine._realtime_age_seconds = lambda: 10.0
        stop_seconds = {"realtime": 1.0, "nearline": 10.0, "offline": 60.0}[kind]

        async def stop_after_clock_sleep(seconds: float, result: object = None) -> object:
            # Startup uses only zero-duration helper sleeps in this fixture;
            # the first positive interval belongs to one of the clock loops.
            if seconds == stop_seconds:
                engine._running = False
            await base_sleep(0)
            return result

        monkeypatch.setattr(engine_module.asyncio, "sleep", stop_after_clock_sleep)
        if kind == "realtime":
            engine._realtime_age_seconds = lambda: (_ for _ in ()).throw(RuntimeError("realtime-clock"))
        elif kind == "nearline":
            engine._last_nearline = 0.0
            engine._last_offline = time.time()

            async def keep_realtime_running() -> None:
                return None

            engine._realtime_tick = keep_realtime_running

            async def fail_nearline() -> None:
                raise RuntimeError("nearline-clock")

            engine._nearline_tick = fail_nearline
        else:
            engine._last_nearline = time.time()
            engine._last_offline = 0.0

            async def keep_realtime_running() -> None:
                return None

            engine._realtime_tick = keep_realtime_running

            async def fail_offline() -> None:
                raise RuntimeError("offline-clock")

            engine._offline_tick = fail_offline
        await engine.run()
        return engine

    realtime_error = await run_clock_case("realtime")
    nearline_error = await run_clock_case("nearline")
    offline_error = await run_clock_case("offline")
    assert realtime_error._error_count >= 1
    assert nearline_error._error_count >= 1
    assert offline_error._error_count >= 1


@pytest.mark.asyncio
async def test_engine_startup_protection_recovery_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise venue-position protection reconstruction without venue writes."""

    monkeypatch.setattr(engine_module.PersistentStore, "_instance", None)
    cfg = SimpleNamespace(
        stop_loss_config={"type": "FIXED_PERCENT", "stop_pct": 1.0},
        take_profit_config={"type": "FIXED_RR", "rr_ratio": 2.0},
        stop_pct=1.0,
        metadata={},
    )

    async def no_sleep(_seconds: float, result: object = None) -> object:
        return result

    monkeypatch.setattr(engine_module.asyncio, "sleep", no_sleep)

    async def run_recovery(
        positions: list[dict],
        *,
        inventory: list[dict] | None = None,
        stale: set[str] | None = None,
        owner_unknown: bool = False,
        submit_mode: str = "success",
        feature_error: bool = False,
        delete_algo_ids: bool = False,
        update_error: bool = False,
        empty_update: bool = False,
        preprotected: bool = False,
        durable_projection_ok: bool = True,
        blocked_config: bool = False,
        with_precision: bool = False,
        protection_error: bool = False,
        none_order: bool = False,
    ) -> tuple[AutonomousEngine, int]:
        engine = _runtime_engine(monkeypatch)
        engine._symbols = [str(p["symbol"]) for p in positions]
        engine._symbol_precision = {}
        if with_precision:
            engine._symbol_precision = {str(positions[0]["symbol"]): {"price": 2, "quantity": 2}}
        engine._protection.set_precision_from_rule(SimpleNamespace(price_precision=3, qty_precision=3))
        engine._store.restore_ledger_entries = lambda: []
        engine._store.get_active_orders = lambda: []
        engine._store.restore_protections = lambda: []
        engine._feed.async_get_kline_features = lambda _symbol: asyncio.sleep(0, result={})
        engine._feed.async_update_features = lambda _symbol: asyncio.sleep(
            0,
            result={} if empty_update else {"price": 100},
        )
        if update_error:

            async def fail_update(_symbol: str) -> dict:
                raise RuntimeError("market-data")

            engine._feed.async_update_features = fail_update
        if feature_error:

            async def fail_features(_symbol: str) -> dict:
                raise RuntimeError("kline")

            engine._feed.async_get_kline_features = fail_features
        engine._restore_durable_protection_projection = lambda *_args: durable_projection_ok
        engine._cancel_stale_protection_algos = lambda: asyncio.sleep(0, result=stale or set())
        engine._get_open_algo_inventory = lambda: asyncio.sleep(0, result=inventory or [])
        engine._persist_protection_order = lambda *_args, **_kwargs: None
        if preprotected:
            engine._protection._protections["pos-recovered-BTCUSDT"] = SimpleNamespace(
                instrument_id="BTCUSDT",
                quantity=1.0,
            )
        if owner_unknown:

            def mark_owner_unknown(*_args: object) -> bool:
                engine._protection_owner_unknown = True
                return True

            engine._restore_durable_protection_projection = mark_owner_unknown

        submissions = 0

        async def create_algo(_params: dict) -> dict:
            nonlocal submissions
            submissions += 1
            if submit_mode == "exception":
                raise RuntimeError("algo")
            if submit_mode == "mixed" and submissions == 2:
                return {"msg": "rejected"}
            return {"algoId": f"algo-{submissions}"}

        engine._create_algo_order = create_algo
        if delete_algo_ids:
            delattr(engine, "_active_algo_ids")
        if protection_error:
            engine._protection.create_protection = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("protection")
            )
        if none_order:
            create_protection = engine._protection.create_protection

            def create_with_none(*args: object, **kwargs: object):
                protection = create_protection(*args, **kwargs)
                protection.take_profits.insert(0, None)
                return protection

            engine._protection.create_protection = create_with_none
        if blocked_config:
            monkeypatch.setattr(
                engine_module.AdaptiveProtectionCalculator,
                "calculate",
                lambda *_args: SimpleNamespace(
                    stop_loss_config={},
                    take_profit_config={},
                    stop_pct=0.0,
                    metadata={"blocked": True, "reason": "FEATURES_UNKNOWN"},
                ),
            )
        else:
            monkeypatch.setattr(engine_module.AdaptiveProtectionCalculator, "calculate", lambda *_args: cfg)

        async def safe_api(endpoint: object, **_kwargs: object) -> tuple[object, bool]:
            if endpoint == engine_module.Endpoint.SERVER_TIME:
                return {"serverTime": 1}, True
            if endpoint == engine_module.Endpoint.ACCOUNT:
                return {
                    "totalWalletBalance": "1000",
                    "positions": positions,
                    "canTrade": True,
                    "canWithdraw": False,
                }, True
            if endpoint == engine_module.Endpoint.EXCHANGE_INFO:
                return {"symbols": []}, True
            return {}, True

        engine._api_async_safe = safe_api
        engine._realtime_tick = lambda: asyncio.sleep(0, result=setattr(engine, "_running", False))
        engine._shutdown = lambda: asyncio.sleep(0, result=setattr(engine, "shutdown_seen", True))
        await engine.run()
        return engine, submissions

    recovered, submissions = await run_recovery(
        [
            {"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"},
            {"symbol": "ETHUSDT", "positionAmt": "-2", "entryPrice": "6000"},
            {"symbol": "SOLUSDT", "positionAmt": "3", "entryPrice": "0.5"},
            {"symbol": "XRPUSDT", "positionAmt": "0", "entryPrice": "100"},
        ],
        inventory=[{"algoId": "stale", "symbol": "BTCUSDT"}],
        stale={"stale"},
        submit_mode="mixed",
        delete_algo_ids=True,
    )
    assert recovered.shutdown_seen and submissions == 6
    assert sum(len(ids) for ids in recovered._active_algo_ids.values()) == 5

    # Existing venue coverage >=2 is a deduplication gate, and an invalid
    # market-data snapshot is skipped without constructing a new protection.
    skipped, skipped_submissions = await run_recovery(
        [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}],
        inventory=[
            {"algoId": "a1", "symbol": "BTCUSDT"},
            {"algoId": "a2", "symbol": "BTCUSDT"},
        ],
    )
    assert skipped.shutdown_seen and skipped_submissions == 0

    zero_entry, zero_submissions = await run_recovery(
        [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "0"}],
        update_error=True,
    )
    assert zero_entry.shutdown_seen and zero_submissions == 0

    empty_entry, empty_submissions = await run_recovery(
        [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "0"}],
        empty_update=True,
    )
    assert empty_entry.shutdown_seen and empty_submissions == 0

    kline_error, kline_submissions = await run_recovery(
        [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}],
        feature_error=True,
    )
    assert kline_error.shutdown_seen and kline_submissions == 0

    already_protected, protected_submissions = await run_recovery(
        [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}],
        preprotected=True,
    )
    assert already_protected.shutdown_seen and protected_submissions == 0

    projection_unknown, projection_submissions = await run_recovery(
        [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}],
        durable_projection_ok=False,
    )
    assert projection_unknown.shutdown_seen and projection_submissions == 0

    precise, precise_submissions = await run_recovery(
        [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}],
        with_precision=True,
    )
    assert precise.shutdown_seen and precise_submissions == 2

    fallback_config, fallback_submissions = await run_recovery(
        [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}],
        blocked_config=True,
    )
    assert fallback_config.shutdown_seen and fallback_submissions == 2

    owner_blocked, owner_submissions = await run_recovery(
        [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}],
        owner_unknown=True,
    )
    assert owner_blocked.shutdown_seen and owner_submissions == 0

    submission_error, error_count = await run_recovery(
        [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}],
        submit_mode="exception",
    )
    assert submission_error.shutdown_seen and error_count == 2

    protection_failure, protection_failure_submissions = await run_recovery(
        [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}],
        protection_error=True,
    )
    assert protection_failure.shutdown_seen and protection_failure_submissions == 0

    none_order_engine, none_order_submissions = await run_recovery(
        [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}],
        none_order=True,
    )
    assert none_order_engine.shutdown_seen and none_order_submissions == 2


@pytest.mark.asyncio
async def test_engine_run_baseline_reconciliation_durable_and_task_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine_module.PersistentStore, "_instance", None)

    async def no_sleep(_seconds: float, result: object = None) -> object:
        return result

    monkeypatch.setattr(engine_module.asyncio, "sleep", no_sleep)

    def writable_safe_api(positions: list[dict]):
        async def safe_api(endpoint: object, **_kwargs: object) -> tuple[object, bool]:
            if endpoint == engine_module.Endpoint.SERVER_TIME:
                return {"serverTime": 1}, True
            if endpoint == engine_module.Endpoint.ACCOUNT:
                return {
                    "totalWalletBalance": "1000",
                    "positions": positions,
                    "canTrade": True,
                    "canWithdraw": False,
                }, True
            if endpoint == engine_module.Endpoint.EXCHANGE_INFO:
                return {"symbols": []}, True
            return {}, True

        return safe_api

    baseline = _runtime_engine(monkeypatch)
    baseline._can_write = True
    baseline._safe_no_new_risk = lambda *_args: None
    baseline._record_execution_fact_failure_env_guarded = lambda *_args, **_kwargs: None
    baseline._alerts = SimpleNamespace(send_incident=lambda *_args, **_kwargs: None)
    baseline_positions = [{"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100"}]
    baseline._api_async_safe = writable_safe_api(baseline_positions)
    baseline._store.restore_account_opening_projection = lambda *_args: None
    baseline._store.save_account_opening_projection = lambda **kwargs: setattr(baseline, "opening", kwargs)
    baseline._restore_durable_protection_projection = lambda *_args: False
    baseline._resolve_unknown_outbox_intents = lambda: no_sleep(0, result=0)
    await baseline.run()
    assert baseline.shutdown_seen and baseline.opening["source"] == "AUTO_OPENING_BASELINE"

    baseline_error = _runtime_engine(monkeypatch)
    baseline_error._can_write = True
    baseline_error._safe_no_new_risk = lambda *_args: None
    baseline_error._record_execution_fact_failure_env_guarded = lambda *_args, **_kwargs: None
    baseline_error._alerts = SimpleNamespace(send_incident=lambda *_args, **_kwargs: None)
    baseline_error._api_async_safe = writable_safe_api([])
    baseline_error._store.restore_account_opening_projection = lambda *_args: (_ for _ in ()).throw(OSError("baseline"))
    baseline_error._resolve_unknown_outbox_intents = lambda: no_sleep(0, result=0)
    await baseline_error.run()
    assert baseline_error.shutdown_seen

    open_order_error = _runtime_engine(monkeypatch)

    async def open_order_failure(*_args, **_kwargs):
        raise RuntimeError("open-orders")

    open_order_error._api_async = open_order_failure
    await open_order_error.run()
    assert open_order_error.shutdown_seen

    precision_parse_error = _runtime_engine(monkeypatch)

    async def malformed_info(endpoint: object, **_kwargs: object) -> tuple[object, bool]:
        if endpoint == engine_module.Endpoint.SERVER_TIME:
            return {"serverTime": 1}, True
        if endpoint == engine_module.Endpoint.ACCOUNT:
            return {"totalWalletBalance": "1000", "positions": [], "canTrade": True}, True
        if endpoint == engine_module.Endpoint.EXCHANGE_INFO:
            return {
                "symbols": [
                    {
                        "symbol": "BROKEN",
                        "filters": [
                            {"filterType": "LOT_SIZE", "stepSize": "bad", "minQty": "bad"},
                            {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                            {"filterType": "MIN_NOTIONAL", "notional": "1"},
                        ],
                    }
                ]
            }, True
        return {}, True

    precision_parse_error._api_async_safe = malformed_info
    await precision_parse_error.run()
    assert precision_parse_error.shutdown_seen

    recon_timeout = _runtime_engine(monkeypatch)

    async def timeout_reconcile() -> bool:
        raise asyncio.TimeoutError

    recon_timeout._reconcile = timeout_reconcile
    await recon_timeout.run()
    assert recon_timeout.shutdown_seen and recon_timeout._lifecycle.state.value == "DEGRADED"

    protection_timeout = _runtime_engine(monkeypatch)

    async def timeout_protection() -> None:
        raise asyncio.TimeoutError

    protection_timeout._ensure_exchange_position_protections = timeout_protection
    await protection_timeout.run()
    assert protection_timeout.shutdown_seen

    durable_blocked = _runtime_engine(monkeypatch)
    durable_blocked._durable_fact_status = lambda: (False, "UNKNOWN", {"source": "test"})
    durable_blocked._record_execution_fact_failure_env_guarded = lambda *_args, **_kwargs: None
    await durable_blocked.run()
    assert durable_blocked.shutdown_seen and durable_blocked._lifecycle.state.value == "DEGRADED"

    adapter_missing = _runtime_engine(monkeypatch)

    async def detach_adapter() -> bool:
        adapter_missing._adapter = None
        return True

    adapter_missing._reconcile = detach_adapter
    adapter_missing._feed._ws_client = object()
    adapter_missing._feed.is_healthy = lambda: True
    await adapter_missing.run()
    assert adapter_missing.shutdown_seen

    async def raise_realtime_sleep(seconds: float, result: object = None) -> object:
        if seconds == 1:
            raise RuntimeError("loop-task")
        return result

    monkeypatch.setattr(engine_module.asyncio, "sleep", raise_realtime_sleep)
    task_error = _runtime_engine(monkeypatch)
    task_error._realtime_age_seconds = lambda: 0.0
    await task_error.run()
    assert task_error.shutdown_seen and task_error._error_count >= 1

    async def cancel_wait(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(engine_module.asyncio, "wait", cancel_wait)
    cancelled = _runtime_engine(monkeypatch)
    await cancelled.run()
    assert cancelled.shutdown_seen


@pytest.mark.asyncio
async def test_engine_shutdown_cancels_owned_orders_and_preserves_unowned_facts() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._running = True
    engine._can_write = True
    engine._active_order_ids = {"1", "2"}
    engine._owned_order_ids = {"1"}
    engine._order_symbols = {"1": "BTCUSDT", "2": "ETHUSDT"}
    engine._intent_tasks = {asyncio.create_task(asyncio.sleep(60))}
    engine._api_calls: list[tuple] = []
    engine._api_async = lambda *args, **kwargs: asyncio.sleep(0, result=engine._api_calls.append((args, kwargs)) or {})
    engine._safe_no_new_risk = lambda *_args: None
    engine._record_execution_fact_failure_env_guarded = lambda *_args, **_kwargs: None
    engine._feed = SimpleNamespace(stop_ws=lambda: asyncio.sleep(0))
    engine._stop_user_stream = lambda: asyncio.sleep(0)
    engine._mapek = SimpleNamespace(save_checkpoint=lambda *_args, **_kwargs: setattr(engine, "checkpoint", True))
    engine._store = SimpleNamespace(close=lambda: setattr(engine, "store_closed", True))
    engine._health = SimpleNamespace(stop=lambda: setattr(engine, "health_stopped", True))
    engine._alerts = SimpleNamespace(send_incident=lambda *args, **kwargs: setattr(engine, "incident", (args, kwargs)))
    engine._tick_count = 4
    engine._order_count = 2
    engine._win_count = 1
    engine._loss_count = 1

    await engine._shutdown()

    assert engine._running is False
    assert engine._api_calls and engine._api_calls[0][1]["params"]["symbol"] == "BTCUSDT"
    assert engine._intent_tasks == set()
    assert engine.checkpoint is True
    assert engine.store_closed is True
    assert engine.health_stopped is True
    assert engine.incident[0][1] == "Shutdown left unowned active orders untouched"

    failure = AutonomousEngine.__new__(AutonomousEngine)
    failure._running = True
    failure._can_write = True
    failure._active_order_ids = {"blank", "bad"}
    failure._owned_order_ids = {"blank", "bad"}
    failure._order_symbols = {"blank": "", "bad": "BTCUSDT"}
    failure._intent_tasks = set()
    failure._safe_no_new_risk = lambda *_args: None

    async def cancel_failure(*_args, **_kwargs):
        raise RuntimeError("cancel")

    failure._api_async = cancel_failure

    async def ws_failure():
        raise RuntimeError("ws")

    async def user_stream_failure():
        raise RuntimeError("user-stream")

    failure._feed = SimpleNamespace(stop_ws=ws_failure)
    failure._stop_user_stream = user_stream_failure
    failure._mapek = SimpleNamespace(save_checkpoint=lambda *_args, **_kwargs: setattr(failure, "checkpoint", True))
    failure._store = SimpleNamespace(close=lambda: setattr(failure, "store_closed", True))
    failure._health = SimpleNamespace(stop=lambda: setattr(failure, "health_stopped", True))
    failure._tick_count = 0
    failure._order_count = 0
    failure._win_count = 0
    failure._loss_count = 0
    failure._record_execution_fact_failure_env_guarded = lambda *_args, **_kwargs: None
    failure._alerts = SimpleNamespace(send_incident=lambda *_args, **_kwargs: None)

    await failure._shutdown()
    assert failure.checkpoint and failure.store_closed and failure.health_stopped


class _SubmitSnapshot:
    def __init__(
        self,
        *,
        known: bool = True,
        stale: bool = False,
        quantity: str | None = None,
        price: str | None = None,
        min_qty: str = "0.1",
        min_notional: str = "1",
        digest: str = "rule-v1",
    ) -> None:
        self.is_known = known
        self.is_stale = stale
        self.qty_precision = 1
        self.price_precision = 1
        self.step_size = "0.1"
        self.tick_size = "0.1"
        self.min_qty = min_qty
        self.min_notional = min_notional
        self.rule_version = "v1"
        self._quantity = quantity
        self._price = price
        self._digest = digest

    def compute_hash(self) -> str:
        return self._digest

    def quantize_quantity(self, value: str) -> str:
        if self._quantity == "RAISE":
            raise ValueError("quantity")
        return self._quantity if self._quantity is not None else value

    def quantize_price(self, value: str, *, side: str) -> str:
        if self._price == "RAISE":
            raise ValueError("price")
        return self._price if self._price is not None else value


def _submit_engine(
    snapshot: object | None, raw: dict | None = None, status: OrderStatus = OrderStatus.NEW
) -> AutonomousEngine:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._symbol_precision = {}
    engine._rule_snapshot_hashes = {}
    engine._rule_change_detected = set()
    engine._close_order_ids = set()
    engine._order_trackers = {}
    engine._order_symbols = {}
    engine._active_order_ids = set()
    engine._owned_order_ids = set()
    engine._order_count = 0
    engine._last_order_placed_at = 0.0
    engine._store_rows: list[tuple[tuple, dict]] = []
    engine._store = SimpleNamespace(save_order_state=lambda *args, **kwargs: engine._store_rows.append((args, kwargs)))
    engine._process_fill_calls: list[tuple[str, str]] = []

    async def process_fill(order_id: str, symbol: str, _result: dict) -> None:
        engine._process_fill_calls.append((order_id, symbol))

    engine._process_fill = process_fill
    engine._verify_intent_at_send = lambda _intent, **_kwargs: asyncio.sleep(0, result=True)

    class Adapter:
        def get_rule_snapshot(self, _symbol: str) -> object:
            if snapshot is None:
                raise AttributeError("no snapshot")
            return snapshot

        async def create_order(self, request: object) -> OrderResponse:
            return OrderResponse(
                venue_instrument=request.venue_instrument,
                account_ref=request.account_ref,
                order_id=str((raw or {}).get("orderId", "")),
                client_order_id=request.client_order_id,
                status=status,
                side=request.side,
                order_type=request.order_type,
                original_quantity=request.quantity,
                executed_quantity=Quantity(amount=str((raw or {}).get("executedQty", "0"))),
                average_price=Price(amount=str((raw or {}).get("avgPrice"))) if (raw or {}).get("avgPrice") else None,
                commission=None,
                correlation_id=request.correlation_id,
                raw_response=raw or {},
            )

        async def query_order_by_client_id(self, _symbol: str, _client_id: str) -> object:
            return SimpleNamespace(is_success=lambda: True, data=raw)

    engine._adapter = Adapter()
    if snapshot is None:
        delattr(type(engine._adapter), "get_rule_snapshot")
    engine._api_async = lambda *_args, **_kwargs: asyncio.sleep(0, result={"symbols": []})
    return engine


def _submit_params(client_id: str = "cid-runtime", quantity: str = "1", price: str | None = None) -> dict:
    params = {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": quantity, "newClientOrderId": client_id}
    if price is not None:
        params.update({"type": "LIMIT", "price": price, "timeInForce": "GTC"})
    return params


@pytest.mark.asyncio
async def test_engine_submit_slice_rule_precision_and_adapter_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    intent = _intent("submit")
    result = await _submit_engine(_SubmitSnapshot(known=False))._submit_order_slice(
        intent, _submit_params(), "BTCUSDT", "BUY", "MARKET", False
    )
    assert result == {"_submit_outcome": "REJECTED", "reason": "VENUE_RULE_SNAPSHOT_UNKNOWN_OR_STALE"}

    changed = _submit_engine(_SubmitSnapshot(digest="new"))
    changed._rule_snapshot_hashes["BTCUSDT"] = "old"
    result = await changed._submit_order_slice(intent, _submit_params(), "BTCUSDT", "BUY", "MARKET", False)
    assert result["reason"] == "VENUE_RULE_SNAPSHOT_CHANGED"

    missing = _submit_engine(None)
    missing._api_async = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("exchange-info"))
    result = await missing._submit_order_slice(intent, _submit_params(), "BTCUSDT", "BUY", "MARKET", False)
    assert result["reason"] == "EXCHANGE_INFO_UNKNOWN"

    unknown_precision = _submit_engine(None)
    unknown_precision._api_async = lambda *_args, **_kwargs: asyncio.sleep(0, result={"symbols": []})
    result = await unknown_precision._submit_order_slice(intent, _submit_params(), "BTCUSDT", "BUY", "MARKET", False)
    assert result["reason"] == "EXCHANGE_PRECISION_UNKNOWN"

    from_exchange_cache = _submit_engine(None)
    from_exchange_cache._symbol_precision["BTCUSDT"] = {
        "quantity": 1,
        "price": 1,
        "step_size": "0.1",
        "tick_size": "0.1",
        "min_quantity": 0.1,
        "min_notional": 1,
    }
    result = await from_exchange_cache._submit_order_slice(intent, _submit_params(), "BTCUSDT", "BUY", "MARKET", False)
    assert result["reason"] == "ORDER_ACK_UNKNOWN"

    unknown_increment = _submit_engine(None)
    unknown_increment._symbol_precision["BTCUSDT"] = {
        "quantity": -1,
        "price": -1,
        "step_size": "",
        "tick_size": "",
        "min_quantity": 0,
        "min_notional": 0,
    }
    result = await unknown_increment._submit_order_slice(intent, _submit_params(), "BTCUSDT", "BUY", "MARKET", False)
    assert result["reason"] == "VENUE_INCREMENT_UNKNOWN"

    for snapshot, expected in (
        (_SubmitSnapshot(quantity="RAISE"), "QUANTITY_QUANTIZATION_REJECTED"),
        (_SubmitSnapshot(quantity="0.9"), "PLANNED_QUANTITY_NOT_VENUE_EXACT"),
    ):
        result = await _submit_engine(snapshot)._submit_order_slice(
            intent, _submit_params(), "BTCUSDT", "BUY", "MARKET", False
        )
        assert result["reason"] == expected
    result = await _submit_engine(_SubmitSnapshot(quantity="2"))._submit_order_slice(
        intent, _submit_params(quantity="2"), "BTCUSDT", "BUY", "MARKET", False
    )
    assert result["reason"] == "FINAL_QUANTITY_OUTSIDE_APPROVAL_OR_MIN_QTY"

    limit_intent = replace(_intent("limit"), order_type=OrderType.LIMIT, price=Price(amount="100"))
    for snapshot, expected in (
        (_SubmitSnapshot(price="RAISE"), "PRICE_QUANTIZATION_REJECTED"),
        (_SubmitSnapshot(price="100.1"), "PLANNED_PRICE_NOT_VENUE_EXACT"),
    ):
        result = await _submit_engine(snapshot)._submit_order_slice(
            limit_intent, _submit_params(price="100"), "BTCUSDT", "BUY", "LIMIT", False
        )
        assert result["reason"] == expected
    result = await _submit_engine(_SubmitSnapshot(price="99"))._submit_order_slice(
        limit_intent, _submit_params(price="99"), "BTCUSDT", "BUY", "LIMIT", False
    )
    assert result["reason"] == "FINAL_LIMIT_PRICE_DIFFERS_FROM_APPROVAL"
    result = await _submit_engine(_SubmitSnapshot(min_notional="200"))._submit_order_slice(
        limit_intent, _submit_params(price="100"), "BTCUSDT", "BUY", "LIMIT", False
    )
    assert result["reason"] == "FINAL_ORDER_BELOW_MIN_NOTIONAL"

    verify_failed = _submit_engine(_SubmitSnapshot())
    verify_failed._verify_intent_at_send = lambda _intent, **_kwargs: asyncio.sleep(0, result=False)
    result = await verify_failed._submit_order_slice(intent, _submit_params(), "BTCUSDT", "BUY", "MARKET", True)
    assert result["reason"] == "FINAL_APPROVAL_CONSUMPTION_FAILED"

    for status, raw, expected in (
        (OrderStatus.REJECTED, {}, "ADAPTER_REJECTED_WITHOUT_ACK"),
        (OrderStatus.UNKNOWN, {}, "ADAPTER_ACK_UNKNOWN"),
    ):
        result = await _submit_engine(_SubmitSnapshot(), raw=raw, status=status)._submit_order_slice(
            intent, _submit_params(), "BTCUSDT", "BUY", "MARKET", False
        )
        assert result["reason"] == expected

    type_error = _submit_engine(_SubmitSnapshot())
    type_error._adapter.create_order = lambda _request: (_ for _ in ()).throw(TypeError("bad request"))
    result = await type_error._submit_order_slice(intent, _submit_params(), "BTCUSDT", "BUY", "MARKET", False)
    assert result["_submit_outcome"] == "REJECTED"
    general_error = _submit_engine(_SubmitSnapshot())
    general_error._adapter.create_order = lambda _request: (_ for _ in ()).throw(RuntimeError("transport"))
    result = await general_error._submit_order_slice(intent, _submit_params(), "BTCUSDT", "BUY", "MARKET", False)
    assert result["_submit_outcome"] == "UNKNOWN"

    # Legacy adapters without a rule-snapshot method may still load a complete
    # exchangeInfo response once; malformed symbols are ignored, while the
    # requested symbol receives the same authoritative snapshot fields.
    fallback = _submit_engine(
        None,
        raw={
            "orderId": "fallback-1",
            "symbol": "BTCUSDT",
            "status": "NEW",
            "executedQty": "0",
            "origQty": "1",
            "clientOrderId": "cid-runtime",
            "type": "MARKET",
            "side": "BUY",
        },
    )
    fallback._api_async = lambda *_args, **_kwargs: asyncio.sleep(
        0,
        result={
            "symbols": [
                {"symbol": "BROKEN", "filters": []},
                {
                    "symbol": "BTCUSDT",
                    "filters": [
                        {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                },
            ]
        },
    )
    result = await fallback._submit_order_slice(intent, _submit_params(), "BTCUSDT", "BUY", "MARKET", False)
    assert result["orderId"] == "fallback-1"

    missing_precision = _submit_engine(None)
    missing_precision._adapter = SimpleNamespace()
    missing_precision._symbol_precision = {"BTCUSDT": None}
    result = await missing_precision._submit_order_slice(intent, _submit_params(), "BTCUSDT", "BUY", "MARKET", False)
    assert result["reason"] == "EXCHANGE_PRECISION_UNKNOWN"


@pytest.mark.asyncio
async def test_engine_submit_slice_ack_fill_duplicate_and_terminal_errors() -> None:
    raw = {
        "orderId": "42",
        "symbol": "BTCUSDT",
        "status": "NEW",
        "executedQty": "0",
        "origQty": "1",
        "clientOrderId": "cid-runtime",
        "type": "MARKET",
        "side": "BUY",
    }
    engine = _submit_engine(_SubmitSnapshot(), raw=raw)
    result = await engine._submit_order_slice(_intent("ack"), _submit_params(), "BTCUSDT", "BUY", "MARKET", False)
    assert result["orderId"] == "42" and "42" in engine._active_order_ids

    filled = {**raw, "orderId": "43", "status": "FILLED", "executedQty": "1", "avgPrice": "100"}
    engine = _submit_engine(_SubmitSnapshot(), raw=filled)
    result = await engine._submit_order_slice(_intent("filled"), _submit_params(), "BTCUSDT", "BUY", "MARKET", False)
    assert result["status"] == "FILLED" and engine._process_fill_calls == [("43", "BTCUSDT")]

    async def duplicate_response(request: object) -> OrderResponse:
        return OrderResponse(
            venue_instrument=request.venue_instrument,
            account_ref=request.account_ref,
            order_id="",
            client_order_id=request.client_order_id,
            status=OrderStatus.REJECTED,
            side=request.side,
            order_type=request.order_type,
            original_quantity=request.quantity,
            executed_quantity=Quantity(amount="0"),
            average_price=None,
            commission=None,
            correlation_id=request.correlation_id,
            raw_response={"code": -4141, "msg": "duplicate"},
        )

    duplicate_filled = {
        **raw,
        "orderId": "44-filled",
        "clientOrderId": "cid-dup-filled",
        "status": "FILLED",
        "executedQty": "1",
        "avgPrice": "100",
    }
    engine = _submit_engine(_SubmitSnapshot(), raw=duplicate_filled, status=OrderStatus.REJECTED)
    engine._adapter.create_order = duplicate_response
    result = await engine._submit_order_slice(
        _intent("dup-filled"), _submit_params("cid-dup-filled"), "BTCUSDT", "BUY", "MARKET", False
    )
    assert result["status"] == "FILLED" and engine._process_fill_calls == [("44-filled", "BTCUSDT")]

    duplicate_persist_failure = {
        **raw,
        "orderId": "44-persist",
        "clientOrderId": "cid-dup-persist",
        "status": "NEW",
    }
    engine = _submit_engine(_SubmitSnapshot(), raw=duplicate_persist_failure, status=OrderStatus.REJECTED)
    engine._adapter.create_order = duplicate_response
    engine._store.save_order_state = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("store"))
    result = await engine._submit_order_slice(
        _intent("dup-persist"), _submit_params("cid-dup-persist"), "BTCUSDT", "BUY", "MARKET", False
    )
    assert result["orderId"] == "44-persist"

    duplicate = {**raw, "orderId": "44", "clientOrderId": "cid-dup", "status": "NEW"}
    engine = _submit_engine(_SubmitSnapshot(), raw=duplicate, status=OrderStatus.REJECTED)
    engine._adapter.create_order = duplicate_response
    result = await engine._submit_order_slice(
        _intent("dup"), _submit_params("cid-dup"), "BTCUSDT", "BUY", "MARKET", False
    )
    assert result["orderId"] == "44"

    mismatch = {**duplicate, "orderId": "45", "symbol": "ETHUSDT"}
    engine = _submit_engine(_SubmitSnapshot(), raw=mismatch, status=OrderStatus.REJECTED)
    engine._adapter.create_order = duplicate_response
    result = await engine._submit_order_slice(
        _intent("dup-mismatch"), _submit_params("cid-dup"), "BTCUSDT", "BUY", "MARKET", False
    )
    assert result["_submit_outcome"] == "UNKNOWN"

    failed_query = _submit_engine(
        _SubmitSnapshot(), raw={"code": -4141, "msg": "duplicate"}, status=OrderStatus.REJECTED
    )
    failed_query._adapter.create_order = duplicate_response
    failed_query._adapter.query_order_by_client_id = lambda *_args: (_ for _ in ()).throw(RuntimeError("query"))
    result = await failed_query._submit_order_slice(
        _intent("dup-fail"), _submit_params(), "BTCUSDT", "BUY", "MARKET", False
    )
    assert result["reason"] == "DUPLICATE_QUERY_UNKNOWN"


class _RuntimeTracker:
    def __init__(self, status: str = "NEW") -> None:
        self.status = SimpleNamespace(value=status)
        self.events: list[object] = []

    def apply(self, event: object) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_engine_monitor_orders_status_matrix_and_orphan_sweep() -> None:
    store_rows: list[tuple[tuple, dict]] = []
    unknown: list[tuple[str, str, str]] = []
    fills: list[tuple[str, str]] = []
    partials: list[tuple[str, float, float, str]] = []
    fill_rows = {
        "pending-fill": {"processing_state": "PENDING"},
        "committed-fill": {"processing_state": "COMMITTED"},
    }
    results = {
        "101": (None, False),
        "102": ({"status": "NEW", "msg": "Order does not exist"}, True),
        "103": ({"status": "NEW", "msg": "Unknown order"}, True),
        "104": ({"status": "NEW"}, True),
        "105": ({"status": "FILLED", "executedQty": "1", "avgPrice": "100"}, True),
        "106": (
            {"status": "CANCELED", "executedQty": "1", "avgPrice": "100", "side": "BUY", "type": "MARKET"},
            True,
        ),
        "107": (
            {"status": "EXPIRED", "executedQty": "1", "avgPrice": "100", "side": "SELL", "type": "LIMIT"},
            True,
        ),
        "108": (
            {"status": "CANCELED", "executedQty": "0", "side": "BUY", "type": "MARKET"},
            True,
        ),
        "109": (
            {"status": "PARTIALLY_FILLED", "executedQty": "0.4", "avgPrice": "101", "side": "BUY", "type": "LIMIT"},
            True,
        ),
        "111": (
            {"status": "PARTIALLY_FILLED", "executedQty": "0.2", "avgPrice": "102", "side": "BUY", "type": "LIMIT"},
            True,
        ),
    }
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._active_order_ids = set(results) | {"100", "110"}
    engine._order_symbols = dict.fromkeys(engine._active_order_ids, "BTCUSDT")
    engine._order_symbols["100"] = "ETHUSDT"
    engine._order_trackers = {
        "102": _RuntimeTracker("NEW"),
        "103": _RuntimeTracker("FILLED"),
        "105": _RuntimeTracker(),
        "106": _RuntimeTracker(),
        "107": _RuntimeTracker(),
        "108": _RuntimeTracker(),
        "109": _RuntimeTracker(),
        "111": _RuntimeTracker(),
        "110": _RuntimeTracker(),
    }
    engine._store = SimpleNamespace(
        get_fill_event=lambda fill_id: fill_rows.get(fill_id),
        save_order_state=lambda *args, **kwargs: store_rows.append((args, kwargs)),
    )
    engine._api_async_safe = lambda _endpoint, **kwargs: _monitor_response(results, kwargs, engine)
    engine._mark_order_unknown = lambda oid, sym, reason: unknown.append((oid, sym, reason))

    async def process_fill(order_id: str, symbol: str, _result: dict) -> None:
        fills.append((order_id, symbol))

    engine._process_fill = process_fill
    engine._consume_cumulative_fill = lambda order_id, *_args, **_kwargs: {
        "106": (1.0, 100.0, "pending-fill"),
        "107": (1.0, 100.0, "committed-fill"),
        "109": (0.4, 101.0, "partial-fill"),
        "111": (0.2, 102.0, "retry-fill"),
    }.get(order_id, (0.0, 0.0, ""))
    retryable: list[tuple] = []

    def record_partial(order_id, _symbol, _result, qty, price, _executed, fill_id, **_kwargs):
        if order_id == "111":
            raise OSError("ledger")
        partials.append((order_id, qty, price, fill_id))
        return True

    engine._record_partial_fill_to_ledger = record_partial
    engine._mark_fill_retryable = lambda *args: retryable.append(args)

    async def monitor_response(_endpoint: object, **kwargs: object) -> tuple[object, bool]:
        order_id = str(kwargs["params"]["orderId"])
        if order_id == "110":
            raise RuntimeError("transport")
        return results[order_id]

    # Bind the async response helper after the local coroutine exists.  The
    # method is intentionally exercised with a real await boundary.
    engine._api_async_safe = monitor_response
    await engine._monitor_orders("BTCUSDT")

    assert fills == [("105", "BTCUSDT")]
    assert partials == [("109", 0.4, 101.0, "partial-fill")]
    assert "100" in engine._active_order_ids  # cross-symbol order was skipped
    assert "103" not in engine._active_order_ids  # expected terminal disappearance
    assert "107" not in engine._active_order_ids and "108" not in engine._active_order_ids
    assert any(oid == "101" and "STATUS_UNKNOWN" in reason for oid, _sym, reason in unknown)
    assert any(oid == "106" and "RECONCILIATION_REQUIRED" in reason for oid, _sym, reason in unknown)
    assert any(oid == "110" and "RuntimeError" in reason for oid, _sym, reason in unknown)
    assert retryable == [("111", "retry-fill")]
    assert len(store_rows) == 2

    # The orphan sweep is write-gated, pool-aware, bounded, and keeps going if
    # one orphan symbol's monitor fails.
    orphan = AutonomousEngine.__new__(AutonomousEngine)
    orphan._can_write = False
    orphan._active_order_ids = {"1"}
    orphan._monitor_called = False
    await orphan._monitor_orphan_orders()
    assert orphan._monitor_called is False

    orphan._can_write = True
    orphan._active_order_ids = {"1", "2"}
    orphan._order_symbols = {"1": "ETHUSDT", "2": "XRPUSDT"}
    orphan._trading_pool = SimpleNamespace(active_instruments=lambda: ["BTCUSDT"])
    orphan.monitored: list[str] = []

    async def monitor_orphan(symbol: str) -> None:
        orphan.monitored.append(symbol)
        if symbol == "ETHUSDT":
            raise RuntimeError("one orphan failed")

    orphan._monitor_orders = monitor_orphan
    await orphan._monitor_orphan_orders(batch_limit=2)
    assert orphan.monitored == ["ETHUSDT", "XRPUSDT"]


async def _monitor_response(results: dict, kwargs: dict, _engine: AutonomousEngine) -> tuple[object, bool]:
    """Unused placeholder replaced by the local coroutine in the test."""
    order_id = str(kwargs["params"]["orderId"])
    return results[order_id]


def _fill_engine() -> AutonomousEngine:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._filled_quantities_by_order = {}
    engine._pending_fill_previous_qty = {}
    engine._pending_fill_retry = set()
    engine._position_projection = {}
    engine._position_generation = {}
    engine._ledger_calls: list[object] = []
    engine._projection_calls: list[tuple] = []
    engine._store = SimpleNamespace(
        get_fill_event=lambda _event_id: None,
        save_fill_event=lambda *_args: True,
        mark_fill_event_committed=lambda event_id: engine._ledger_calls.append(("committed", event_id)),
        save_position_projection=lambda *args: engine._projection_calls.append(args),
    )
    engine._post_ledger_transaction = lambda tx: engine._ledger_calls.append(tx)
    engine._mark_fill_committed = lambda order_id, fill_id, cumulative: AutonomousEngine._mark_fill_committed(
        engine, order_id, fill_id, cumulative
    )
    return engine


def test_engine_fill_journal_and_projection_boundary_matrix() -> None:
    engine = _fill_engine()
    assert engine._consume_cumulative_fill(
        "o", "BTCUSDT", {"executedQty": "0", "avgPrice": "100"}, status="FILLED"
    ) == (
        0.0,
        100.0,
        "",
    )
    engine._filled_quantities_by_order["o"] = 1.0
    assert engine._consume_cumulative_fill(
        "o", "BTCUSDT", {"executedQty": "1", "avgPrice": "100", "tradeId": "t"}, status="FILLED"
    ) == (0.0, 100.0, "trade:o:t")

    engine._store = SimpleNamespace(
        get_fill_event=lambda event_id: {"processing_state": "COMMITTED"} if event_id == "trade:o2:t" else None,
        save_fill_event=lambda *_args: False,
        mark_fill_event_committed=lambda event_id: engine._ledger_calls.append(("committed", event_id)),
        save_position_projection=lambda *args: engine._projection_calls.append(args),
    )
    assert engine._consume_cumulative_fill(
        "o2", "BTCUSDT", {"executedQty": "1", "avgPrice": "100", "tradeId": "t"}, status="FILLED"
    ) == (0.0, 100.0, "trade:o2:t")

    reread_calls = 0

    def reread(event_id: str) -> dict | None:
        nonlocal reread_calls
        reread_calls += 1
        if reread_calls == 1:
            return None
        return {"processing_state": "COMMITTED"}

    reread_store = _fill_engine()
    reread_store._store = SimpleNamespace(
        get_fill_event=reread,
        save_fill_event=lambda *_args: False,
        mark_fill_event_committed=lambda _event_id: None,
        save_position_projection=lambda *_args: None,
    )
    assert reread_store._consume_cumulative_fill(
        "o-reread", "BTCUSDT", {"executedQty": "1", "avgPrice": "100", "tradeId": "t"}, status="FILLED"
    ) == (0.0, 100.0, "trade:o-reread:t")
    assert reread_calls == 2

    pending_store = _fill_engine()
    pending_store._store = SimpleNamespace(
        get_fill_event=lambda event_id: (
            {"processing_state": "PENDING", "delta_qty": "0", "price": "0"} if event_id == "trade:pending:t" else None
        ),
        save_fill_event=lambda *_args: False,
        mark_fill_event_committed=lambda _event_id: None,
        save_position_projection=lambda *_args: None,
    )
    assert pending_store._consume_cumulative_fill(
        "pending", "BTCUSDT", {"executedQty": "1", "avgPrice": "100", "tradeId": "t"}, status="FILLED"
    ) == (0.0, 100.0, "trade:pending:t")
    pending_store._pending_fill_retry.add("trade:pending:t")
    assert pending_store._consume_cumulative_fill(
        "pending", "BTCUSDT", {"executedQty": "1", "avgPrice": "100", "tradeId": "t"}, status="FILLED"
    ) == (0.0, 0.0, "trade:pending:t")

    inserted = _fill_engine()
    assert inserted._consume_cumulative_fill(
        "fresh", "BTCUSDT", {"executedQty": "1", "avgPrice": "100", "tradeId": "t"}, status="FILLED"
    ) == (1.0, 100.0, "trade:fresh:t")

    engine._filled_quantities_by_order = {"o3": 0.0}
    engine._pending_fill_previous_qty = {"fill:o3": 0.0}
    engine._pending_fill_retry = {"fill:o3"}
    engine._mark_fill_committed("o3", "fill:o3", 1.0)
    assert engine._filled_quantities_by_order["o3"] == 1.0
    assert "fill:o3" not in engine._pending_fill_retry
    engine._mark_fill_retryable("o3", "")
    engine._mark_fill_retryable("o3", "fill:o4")
    assert "fill:o4" in engine._pending_fill_retry

    engine._update_position_projection("BTCUSDT", "BUY", 0.0, 100.0, "zero")
    engine._position_projection["BTCUSDT"] = {"signed_quantity": "bad", "entry_price": "bad", "position_generation": 3}
    engine._position_generation["BTCUSDT"] = 4
    engine._update_position_projection("BTCUSDT", "SELL", 1.0, 100.0, "recovery")
    assert engine._position_projection["BTCUSDT"]["signed_quantity"] == "-1.0"
    assert engine._position_projection["BTCUSDT"]["position_generation"] == 4


def _lite_update(
    order_id: str = "lite-order", trade_id: str = "trade-1", qty: str = "0.1", price: str = "100"
) -> SimpleNamespace:
    return SimpleNamespace(
        order_id=order_id,
        symbol="BTCUSDT",
        side=SimpleNamespace(value="BUY"),
        trade_id=trade_id,
        last_quantity=SimpleNamespace(amount=qty),
        last_price=SimpleNamespace(amount=price),
    )


def test_engine_trade_lite_invalid_and_idempotent_guards() -> None:
    engine = _fill_engine()
    engine._order_has_committed_cumulative_fill = lambda _order_id: False
    engine._record_trade_lite_fill(_lite_update(qty="nan"))
    engine._record_trade_lite_fill(_lite_update(price="0"))
    engine._store = SimpleNamespace(
        get_fill_event=lambda _event_id: {"processing_state": "COMMITTED"},
        save_fill_event=lambda *_args: True,
    )
    engine._record_trade_lite_fill(_lite_update())
    assert engine._ledger_calls == []

    successful = _fill_engine()
    successful._order_has_committed_cumulative_fill = lambda _order_id: False
    successful._record_trade_lite_fill(_lite_update(order_id="lite-success"))
    assert any(call[0] == "committed" for call in successful._ledger_calls if isinstance(call, tuple))

    mismatch = _fill_engine()
    mismatch._order_has_committed_cumulative_fill = lambda _order_id: False
    mismatch._filled_quantities_by_order["lite-mismatch"] = 1.0
    mismatch._lite_applied_qty = {"lite-mismatch": 0.0}
    mismatch._record_trade_lite_fill(_lite_update(order_id="lite-mismatch"))
    assert mismatch._filled_quantities_by_order["lite-mismatch"] == 1.0

    pending = _fill_engine()
    pending._order_has_committed_cumulative_fill = lambda _order_id: False
    pending._store.save_fill_event = lambda *_args: False
    pending._store.get_fill_event = lambda _event_id: {"processing_state": "PENDING"}
    pending._record_trade_lite_fill(_lite_update(order_id="lite-pending"))
    assert "lite-pending" not in getattr(pending, "_lite_applied_qty", {})

    failed = _fill_engine()
    failed._order_has_committed_cumulative_fill = lambda _order_id: False
    failed._post_ledger_transaction = lambda _tx: (_ for _ in ()).throw(OSError("ledger"))
    try:
        failed._record_trade_lite_fill(_lite_update(order_id="lite-failed"))
    except OSError:
        pass
    else:
        raise AssertionError("TRADE_LITE ledger failure must remain observable")
    assert failed._lite_applied_qty["lite-failed"] == 0.0


def test_engine_fill_accounting_and_cumulative_authority_helpers() -> None:
    partial = _fill_engine()
    partial._store.save_order_state = lambda *args, **kwargs: partial._ledger_calls.append(("order", args, kwargs))
    assert (
        partial._record_partial_fill_to_ledger(
            "partial",
            "BTCUSDT",
            {"side": "BUY", "type": "MARKET", "origQty": "1"},
            0.25,
            100.0,
            0.25,
            "partial-fill",
            status="PARTIALLY_FILLED",
        )
        is True
    )
    assert any(isinstance(call, tuple) and call[0] == "order" for call in partial._ledger_calls)

    helper = AutonomousEngine.__new__(AutonomousEngine)
    helper._store = SimpleNamespace(
        restore_fill_events=lambda: [
            {"order_id": "known", "processing_state": "COMMITTED", "fill_event_id": "fill:known"},
            {"order_id": "known", "processing_state": "COMMITTED", "fill_event_id": "lite:known"},
        ]
    )
    assert helper._order_has_committed_cumulative_fill("known") is True
    assert helper._order_has_committed_cumulative_fill("known") is True
    helper._store = SimpleNamespace(restore_fill_events=lambda: (_ for _ in ()).throw(OSError("read")))
    helper._cum_fill_cache = {}
    assert helper._order_has_committed_cumulative_fill("unknown") is True
    helper._store = SimpleNamespace()
    helper._cum_fill_cache = {}
    assert helper._order_has_committed_cumulative_fill("missing") is True

    projection = _fill_engine()
    projection._position_projection["BTCUSDT"] = {
        "signed_quantity": "1",
        "entry_price": "100",
        "position_generation": 1,
    }
    projection._update_position_projection("BTCUSDT", "SELL", 2.0, 90.0, "reverse")
    assert projection._position_projection["BTCUSDT"]["signed_quantity"] == "-1.0"
    projection._update_position_projection("BTCUSDT", "BUY", 1.0, 90.0, "flat")
    assert projection._position_projection["BTCUSDT"]["signed_quantity"] == "0.0"

    commit = _fill_engine()
    commit._store.save_ledger_entry = lambda *args: commit._ledger_calls.append(("entry", args))
    commit._store.save_order_state = lambda *args, **kwargs: commit._ledger_calls.append(("order", args, kwargs))
    commit._record_execution_fact_failure_env_guarded = lambda _reason: None
    commit._commit_fill_facts(
        "commit-order",
        "BTCUSDT",
        "BUY",
        1.0,
        100.0,
        "fill:commit",
        {"side": "BUY", "type": "MARKET", "origQty": "1", "executedQty": "1"},
    )
    assert any(call[0] == "entry" for call in commit._ledger_calls if isinstance(call, tuple))

    secondary_failed = _fill_engine()
    secondary_failed._store.save_ledger_entry = lambda *_args: (_ for _ in ()).throw(OSError("index"))
    secondary_failed._store.save_order_state = lambda *_args, **_kwargs: None
    secondary_failed._record_execution_fact_failure_env_guarded = lambda reason: setattr(
        secondary_failed, "failure", reason
    )
    try:
        secondary_failed._commit_fill_facts(
            "commit-order",
            "BTCUSDT",
            "BUY",
            1.0,
            100.0,
            "fill:secondary",
            {"side": "BUY", "type": "MARKET", "origQty": "1", "executedQty": "1"},
        )
    except OSError:
        pass
    else:
        raise AssertionError("secondary ledger index failure must remain observable")
    assert "secondary index persistence failed" in secondary_failed.failure


def _process_engine(*, close: bool = False, residual: float = 0.0) -> AutonomousEngine:
    engine = _fill_engine()
    order_id = "close-order" if close else "entry-order"
    engine._order_trackers = {order_id: _RuntimeTracker()}
    engine._active_order_ids = {order_id}
    engine._order_symbols = {order_id: "BTCUSDT"}
    engine._close_order_ids = {order_id} if close else set()
    engine._store = SimpleNamespace(
        get_fill_event=lambda _event_id: {"processing_state": "COMMITTED"},
        save_order_state=lambda *_args, **_kwargs: None,
    )
    engine._consume_cumulative_fill = lambda *_args, **_kwargs: (1.0, 110.0, "fill-event")
    engine._commit_fill_facts = lambda *_args, **_kwargs: None
    engine._project_order_terminal = lambda *_args: None
    engine._mark_order_unknown = lambda *_args: None
    engine._last_account = {"totalWalletBalance": "1000"}
    engine._autopilot_strategy_id = "strategy"
    engine._peak_equity = 900.0
    engine._win_count = 0
    engine._loss_count = 0
    engine._trade_pnls = []
    engine._strategy_risk = SimpleNamespace(record_trade=lambda *_args: None, update_equity=lambda *_args: None)
    engine._ensure_calls: list[tuple] = []
    engine._ensure_entry_protection = lambda *args, **kwargs: asyncio.sleep(
        0, result=engine._ensure_calls.append((args, kwargs))
    )
    engine._position_projection = {"BTCUSDT": {"signed_quantity": str(residual)}}
    pp = SimpleNamespace(
        instrument_id=InstrumentId("BTCUSDT"),
        entry_price=100.0,
        quantity=2.0,
        is_long=lambda: True,
    )
    engine._protection = SimpleNamespace(all_positions=lambda: {"pid": pp})
    engine._removed: list[tuple] = []
    engine._canceled: list[tuple] = []
    engine._remove_protection_with_cleanup = lambda *args: engine._removed.append(args)
    engine._cancel_algo_orders = lambda *args: asyncio.sleep(0, result=engine._canceled.append(args))
    return engine


@pytest.mark.asyncio
async def test_engine_process_fill_incomplete_commit_unknown_and_close_paths() -> None:
    missing_tracker = _process_engine()
    missing_tracker._order_trackers = {}
    await missing_tracker._process_fill("entry-order", "BTCUSDT", {"executedQty": "1", "avgPrice": "100"})

    incomplete = _process_engine()
    reasons: list[tuple] = []
    incomplete._mark_order_unknown = lambda *args: reasons.append(args)
    await incomplete._process_fill("entry-order", "BTCUSDT", {"executedQty": "1", "avgPrice": "0"})
    assert reasons[-1][-1] == "FILLED_EXECUTION_FACTS_INCOMPLETE"

    derived = _process_engine()
    await derived._process_fill(
        "entry-order", "BTCUSDT", {"executedQty": "2", "avgPrice": "0", "cumQuote": "220", "side": "BUY"}
    )
    assert derived._ensure_calls
    assert "entry-order" not in derived._active_order_ids

    committed = _process_engine()
    committed._consume_cumulative_fill = lambda *_args, **_kwargs: (0.0, 110.0, "fill-event")
    await committed._process_fill("entry-order", "BTCUSDT", {"executedQty": "1", "avgPrice": "110", "side": "BUY"})
    assert committed._ensure_calls and "entry-order" not in committed._active_order_ids

    failed = _process_engine()
    failed._commit_fill_facts = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("ledger"))
    with pytest.raises(RuntimeError, match="ledger"):
        await failed._process_fill("entry-order", "BTCUSDT", {"executedQty": "1", "avgPrice": "110", "side": "BUY"})

    closed = _process_engine(close=True, residual=0.0)
    await closed._process_fill("close-order", "BTCUSDT", {"executedQty": "1", "avgPrice": "110", "side": "SELL"})
    assert closed._removed == [("pid", "BTCUSDT")] and closed._canceled == [("pid", "BTCUSDT")]

    partial_close = _process_engine(close=True, residual=1.0)
    await partial_close._process_fill("close-order", "BTCUSDT", {"executedQty": "1", "avgPrice": "90", "side": "SELL"})
    assert partial_close._removed == []

    losing = _process_engine(close=True, residual=1.0)
    losing._trade_pnls = [0.0] * 10001
    losing._consume_cumulative_fill = lambda *_args, **_kwargs: (1.0, 90.0, "fill-event")
    await losing._process_fill("close-order", "BTCUSDT", {"executedQty": "1", "avgPrice": "90", "side": "SELL"})
    assert losing._loss_count == 1 and len(losing._trade_pnls) == 5000

    malformed_projection = _process_engine(close=True, residual=1.0)
    malformed_projection._position_projection = {"BTCUSDT": {"signed_quantity": "bad"}}
    await malformed_projection._process_fill(
        "close-order", "BTCUSDT", {"executedQty": "1", "avgPrice": "90", "side": "SELL"}
    )
    assert malformed_projection._removed == [("pid", "BTCUSDT")]


@pytest.mark.asyncio
async def test_engine_process_fill_recovery_exception_and_cumulative_race_paths() -> None:
    class BadNumber:
        def __float__(self) -> float:
            raise TypeError("malformed cumQuote")

    incomplete = _process_engine()
    reasons: list[tuple] = []
    incomplete._mark_order_unknown = lambda *args: reasons.append(args)
    await incomplete._process_fill(
        "entry-order", "BTCUSDT", {"executedQty": "1", "avgPrice": "0", "cumQuote": BadNumber()}
    )
    assert reasons[-1][-1] == "FILLED_EXECUTION_FACTS_INCOMPLETE"

    class BadFloat:
        def __float__(self) -> float:
            raise TypeError("bad float")

    malformed_fields = _process_engine()
    reasons = []
    malformed_fields._mark_order_unknown = lambda *args: reasons.append(args)
    await malformed_fields._process_fill("entry-order", "BTCUSDT", {"executedQty": BadFloat(), "avgPrice": BadFloat()})
    assert reasons[-1][-1] == "FILLED_EXECUTION_FACTS_INCOMPLETE"

    unknown = _process_engine()
    unknown._consume_cumulative_fill = lambda *_args, **_kwargs: (0.0, 100.0, "not-committed")
    unknown._store = SimpleNamespace(get_fill_event=lambda _event_id: None)
    reasons = []
    unknown._mark_order_unknown = lambda *args: reasons.append(args)
    await unknown._process_fill("entry-order", "BTCUSDT", {"executedQty": "1", "avgPrice": "100"})
    assert "FILL_FACT_COMMIT_UNKNOWN" in reasons[-1][-1]

    lite_committed = _process_engine()
    lite_committed._consume_cumulative_fill = lambda *_args, **_kwargs: (
        0.0,
        100.0,
        "order:entry-order:cum:1:avg:100",
    )
    lite_committed._store = SimpleNamespace(
        get_fill_event=lambda _event_id: None,
        restore_fill_events=lambda: [
            {
                "fill_event_id": "lite:entry-order:trade-1",
                "order_id": "entry-order",
                "delta_qty": "1",
                "processing_state": "COMMITTED",
            }
        ],
    )
    reasons = []
    lite_committed._mark_order_unknown = lambda *args: reasons.append(args)
    await lite_committed._process_fill(
        "entry-order",
        "BTCUSDT",
        {"executedQty": "1", "avgPrice": "100", "side": "BUY"},
    )
    assert reasons == []
    assert lite_committed._ensure_calls and "entry-order" not in lite_committed._active_order_ids

    insufficient = _process_engine()
    insufficient._consume_cumulative_fill = lambda *_args, **_kwargs: (
        0.0,
        100.0,
        "order:entry-order:cum:1:avg:100",
    )
    insufficient._store = SimpleNamespace(
        get_fill_event=lambda _event_id: None,
        restore_fill_events=lambda: [
            {
                "fill_event_id": "lite:entry-order:trade-1",
                "order_id": "entry-order",
                "delta_qty": "0.5",
                "processing_state": "COMMITTED",
            }
        ],
    )
    reasons = []
    insufficient._mark_order_unknown = lambda *args: reasons.append(args)
    await insufficient._process_fill(
        "entry-order",
        "BTCUSDT",
        {"executedQty": "1", "avgPrice": "100", "side": "BUY"},
    )
    assert "FILL_FACT_COMMIT_UNKNOWN" in reasons[-1][-1]

    unreadable = _process_engine()
    unreadable._store = SimpleNamespace(
        restore_fill_events=lambda: (_ for _ in ()).throw(OSError("database unavailable"))
    )
    assert unreadable._committed_fill_qty_covers("entry-order", 1.0) is False

    deferred = _process_engine()
    deferred._consume_cumulative_fill = lambda *_args, **_kwargs: (0.0, 100.0, "committed")
    deferred._ensure_entry_protection = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("protection"))
    await deferred._process_fill("entry-order", "BTCUSDT", {"executedQty": "1", "avgPrice": "100"})
    assert "entry-order" not in deferred._active_order_ids

    retryable = _process_engine()
    retryable._store = SimpleNamespace(
        get_fill_event=lambda _event_id: {"processing_state": "PENDING"},
    )
    retryable._commit_fill_facts = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("commit"))
    retryable._mark_fill_retryable = lambda *args: setattr(retryable, "retryable", args)
    with pytest.raises(RuntimeError, match="commit"):
        await retryable._process_fill("entry-order", "BTCUSDT", {"executedQty": "1", "avgPrice": "100"})
    assert retryable.retryable[0] == "entry-order"

    class BadExecuted:
        def __le__(self, other: object) -> bool:
            return False

        def __float__(self) -> float:
            raise ValueError("bad executed quantity")

    close_bad = _process_engine(close=True, residual=0.0)
    close_bad._consume_cumulative_fill = lambda *_args, **_kwargs: (BadExecuted(), 110.0, "fill-event")
    await close_bad._process_fill("close-order", "BTCUSDT", {"executedQty": "1", "avgPrice": "110", "side": "SELL"})
    assert close_bad._removed == []

    race = _fill_engine()
    race._filled_quantities_by_order = {}
    race._store = SimpleNamespace(
        get_fill_event=lambda event_id: {"processing_state": "COMMITTED"} if event_id == "trade:race:t" else None,
        save_fill_event=lambda *_args: False,
    )
    assert race._consume_cumulative_fill(
        "race", "BTCUSDT", {"executedQty": "1", "avgPrice": "100", "tradeId": "t"}, status="FILLED"
    ) == (0.0, 100.0, "trade:race:t")

    rejected = _submit_engine(_SubmitSnapshot(), raw={"code": -2010, "msg": "rejected"}, status=OrderStatus.REJECTED)
    result = await rejected._submit_order_slice(_intent("reject"), _submit_params(), "BTCUSDT", "BUY", "MARKET", False)
    assert result["_submit_outcome"] == "REJECTED"
    unknown = _submit_engine(_SubmitSnapshot(), raw={"code": -1, "msg": "unknown"}, status=OrderStatus.UNKNOWN)
    result = await unknown._submit_order_slice(_intent("unknown"), _submit_params(), "BTCUSDT", "BUY", "MARKET", False)
    assert result["_submit_outcome"] == "UNKNOWN"
