"""Semantic V4 coverage for bounded exchange execution and pool recovery."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from beidou_data.trading_pool_lifecycle import PoolStatus, TradingPool, discover_startup_candidates
from beidou_exchange.binance_usdm.adapter import BinanceUsdmAdapter
from beidou_exchange.binance_usdm.endpoints import Endpoint
from beidou_exchange.binance_usdm.rest_client import BinanceRESTClient
from beidou_exchange.binance_usdm.write_guard import classify_terminal_write
from beidou_exchange.core.error_taxonomy import ErrorCategory, Result
from beidou_exchange.core.protocol import OrderRequest
from beidou_exchange.core.write_authority import (
    TerminalWriteContext,
    TerminalWriteDecision,
    TerminalWriteKind,
    TerminalWriteRequest,
    evaluate_terminal_write,
    terminal_write_request_hash,
)
from beidou_exchange.testnet_guard import (
    TestnetCap as _TestnetCap,
)
from beidou_exchange.testnet_guard import (
    TestnetEnvironmentGuard,
    TestnetGuardError,
    _decimal,
    _positive_decimal,
)
from beidou_shared.types import (
    AccountId,
    AccountRef,
    HealthStatus,
    InstrumentId,
    OrderSide,
    OrderStatus,
    OrderType,
    Quantity,
    VenueId,
    VenueInstrument,
)


class _MutableTransport:
    def __init__(self, response: Any = None) -> None:
        self.response = {} if response is None else response
        self.calls: list[dict[str, Any]] = []
        self.listen_calls: list[tuple[str, Any]] = []
        self._rest_url = "https://demo-fapi.binance.com"

    async def request(
        self,
        method: str,
        path: str,
        signed: bool = False,
        params: dict[str, Any] | None = None,
        write_context: TerminalWriteContext | None = None,
    ) -> Result[Any]:
        self.calls.append(
            {
                "method": method,
                "path": path,
                "signed": signed,
                "params": dict(params or {}),
                "write_context": write_context,
            }
        )
        return self.response if isinstance(self.response, Result) else Result.success(self.response)

    async def create_listen_key(self, write_context: TerminalWriteContext | None = None) -> Result[dict[str, Any]]:
        self.listen_calls.append(("create", write_context))
        return Result.success({"listenKey": "bound-key"})

    async def keepalive_listen_key(
        self,
        listen_key: str,
        write_context: TerminalWriteContext | None = None,
    ) -> Result[dict[str, Any]]:
        self.listen_calls.append((listen_key, write_context))
        return Result.success({"listenKey": listen_key})


class _DecisionGuard:
    rest_base_url = "https://demo-fapi.binance.com"

    def __init__(self, *, context_allowed: bool = True, write_allowed: bool = True) -> None:
        self.context_allowed = context_allowed
        self.write_allowed = write_allowed

    def validate_context(self, _context: TerminalWriteContext, _request: TerminalWriteRequest) -> TerminalWriteDecision:
        return TerminalWriteDecision(self.context_allowed, "CONTEXT_OK" if self.context_allowed else "CONTEXT_DENY")

    def authorize(self, _request: TerminalWriteRequest) -> TerminalWriteDecision:
        return TerminalWriteDecision(self.write_allowed, "WRITE_OK" if self.write_allowed else "WRITE_DENY")


def _context(**changes: Any) -> TerminalWriteContext:
    base = TerminalWriteContext(
        task_id="testnet-verification",
        entrypoint="apps.testnet_verify",
        owner_id="test-owner",
        generation="trace-v4",
        approval_id="approval-v4",
        expires_at=time.time() + 3_600,
        nonce="nonce-v4",
        intent_id="intent-v4",
        position_id="position-v4",
        quantity="0.01",
        dedicated_account=True,
        account_id="dedicated-testnet-account",
        venue_id="BINANCE",
        environment="TESTNET",
        rest_base_url="https://demo-fapi.binance.com",
        notional="20",
        leverage="2",
        side="BUY",
        order_type="MARKET",
        pool_id="pool-v4",
        pool_version="4",
        pool_hash="a" * 64,
        pool_symbols=("BTCUSDT",),
        symbol="BTCUSDT",
        client_order_id="intent-v4",
        expected_method="POST",
        expected_path=Endpoint.ORDER,
        account_exposure="0",
        projected_account_exposure="20",
        order_id="17",
    )
    return replace(base, **changes)


def _account() -> AccountRef:
    return AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("dedicated-testnet-account"))


def _instrument() -> VenueInstrument:
    return VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))


def _order(*, reduce_only: bool = False) -> OrderRequest:
    return OrderRequest(
        venue_instrument=_instrument(),
        account_ref=_account(),
        side=OrderSide.SELL if reduce_only else OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(amount="0.01"),
        client_order_id="intent-v4",
        reduce_only=reduce_only,
    )


def _guard() -> TestnetEnvironmentGuard:
    return TestnetEnvironmentGuard(
        "https://demo-fapi.binance.com",
        max_notional="25",
        max_leverage="3",
        max_account_exposure="25",
        account_id="dedicated-testnet-account",
        owner_id="test-owner",
        writes_enabled=True,
    )


def _increase_context(guard: TestnetEnvironmentGuard, **changes: Any) -> TerminalWriteContext:
    values: dict[str, Any] = {
        "intent_id": "intent-v4",
        "trace_id": "trace-v4",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "order_type": "MARKET",
        "quantity": "0.01",
        "notional": "20",
        "leverage": "2",
        "client_order_id": "intent-v4",
        "account_exposure": "0",
        "projected_account_exposure": "20",
        "pool_id": "pool-v4",
        "pool_version": "4",
        "pool_hash": "a" * 64,
        "pool_symbols": ("BTCUSDT",),
    }
    values.update(changes)
    return guard.build_write_context(**values)


def _increase_request(guard: TestnetEnvironmentGuard, context: TerminalWriteContext) -> TerminalWriteRequest:
    request = classify_terminal_write(
        "POST",
        Endpoint.ORDER,
        {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "quantity": "0.01",
            "newClientOrderId": "intent-v4",
        },
        account_id=guard.account_id,
        context=context,
        signed=True,
        rest_base_url=guard.rest_base_url,
    )
    assert request is not None
    return request


def test_terminal_write_hash_binds_enum_and_leverage_scope_fails_closed() -> None:
    request = TerminalWriteRequest(
        kind=TerminalWriteKind.INCREASE,
        method="POST",
        path=Endpoint.ORDER,
        account_id="dedicated-testnet-account",
        symbol="BTCUSDT",
        quantity="0.01",
        task_id="task",
        entrypoint="entry",
        owner_id="owner",
        generation="generation",
        approval_id="approval",
        expires_at=time.time() + 60,
        nonce="nonce",
        intent_id="intent",
        dedicated_account=True,
        pool_symbols=("BTCUSDT",),
    )
    digest = terminal_write_request_hash(request)
    assert len(digest) == 64
    assert digest == terminal_write_request_hash(request)
    assert digest != terminal_write_request_hash(replace(request, quantity="0.02"))

    leverage = replace(request, path=Endpoint.LEVERAGE, leverage="", quantity="")
    decision = evaluate_terminal_write(_DecisionGuard(), leverage)
    assert decision == TerminalWriteDecision(False, "WRITE_OBJECT_SCOPE_INCOMPLETE")


def test_guard_scalar_url_cap_and_constructor_boundaries() -> None:
    with pytest.raises(TestnetGuardError, match="finite decimal"):
        _decimal(object(), field_name="amount")
    with pytest.raises(TestnetGuardError, match="finite and >="):
        _decimal("NaN", field_name="amount")
    with pytest.raises(TestnetGuardError, match="must be > 0"):
        _positive_decimal("0", field_name="amount")

    for url, message in (
        ("https://demo-fapi.binance.com:invalid", "invalid Testnet URL"),
        ("https:///missing-host", "must contain a hostname"),
    ):
        with pytest.raises(TestnetGuardError, match=message):
            TestnetEnvironmentGuard.canonical_base_url(url)

    valid = Decimal("1")
    for cap in (
        (Decimal("0"), valid, valid, "max_notional"),
        (valid, Decimal("NaN"), valid, "max_leverage"),
        (valid, valid, Decimal("0"), "max_account_exposure"),
    ):
        with pytest.raises(TestnetGuardError, match=cap[3]):
            _TestnetCap(cap[0], cap[1], cap[2])

    kwargs = {"max_notional": 25, "max_leverage": 3}
    with pytest.raises(TestnetGuardError, match="account_id"):
        TestnetEnvironmentGuard("https://demo-fapi.binance.com", account_id="DEFAULT", **kwargs)
    with pytest.raises(TestnetGuardError, match="owner_id"):
        TestnetEnvironmentGuard("https://demo-fapi.binance.com", account_id="acct", owner_id="", **kwargs)
    with pytest.raises(TestnetGuardError, match="boolean"):
        TestnetEnvironmentGuard(
            "https://demo-fapi.binance.com",
            account_id="acct",
            writes_enabled=1,
            **kwargs,  # type: ignore[arg-type]
        )
    with pytest.raises(TestnetGuardError, match="<= 86400"):
        TestnetEnvironmentGuard(
            "https://demo-fapi.binance.com", account_id="acct", context_ttl_seconds=86_401, **kwargs
        )


def test_guard_context_building_rejects_ambiguity_and_binds_cancel_and_close() -> None:
    guard = _guard()
    guard.engage_kill_switch()
    assert guard.kill_switch_active
    guard.clear_kill_switch()
    assert not guard.kill_switch_active

    with pytest.raises(TestnetGuardError, match="intent_id and trace_id"):
        guard.build_write_context(intent_id="", trace_id="trace", symbol="BTCUSDT")
    with pytest.raises(TestnetGuardError, match="symbol is required"):
        guard.build_write_context(intent_id="intent", trace_id="trace", order_type="MARKET")
    with pytest.raises(TestnetGuardError, match="clock is not finite"):
        guard.build_write_context(
            intent_id="intent", trace_id="trace", symbol="BTCUSDT", order_type="MARKET", now=float("nan")
        )

    cancel = guard.build_write_context(
        intent_id="intent-cancel",
        trace_id="trace-cancel",
        symbol="BTCUSDT",
        quantity="0.01",
        position_id="position-v4",
        method="DELETE",
        path=Endpoint.ORDER,
        order_id="17",
    )
    cancel_request = classify_terminal_write(
        "DELETE",
        Endpoint.ORDER,
        {"symbol": "BTCUSDT", "orderId": 17},
        account_id=guard.account_id,
        context=cancel,
        signed=True,
        rest_base_url=guard.rest_base_url,
    )
    assert cancel_request is not None
    assert guard.validate_context(cancel, cancel_request).allowed

    close = guard.build_write_context(
        intent_id="intent-close",
        trace_id="trace-close",
        symbol="BTCUSDT",
        side="SELL",
        order_type="MARKET",
        quantity="0.01",
        position_id="position-v4",
        reduce_only=True,
        close_position=True,
    )
    assert close.close_position is True
    assert close.final_request_hash
    with pytest.raises(TestnetGuardError, match="does not bind"):
        guard.build_write_context(
            intent_id="intent", trace_id="trace", symbol="BTCUSDT", order_type="MARKET", final_request_hash="bad"
        )


def test_guard_context_identity_pool_and_cap_decisions_are_fail_closed() -> None:
    guard = _guard()
    context = _increase_context(guard)
    request = _increase_request(guard, context)
    assert guard.validate_context(context, request).allowed

    context_cases = (
        (replace(context, task_id="other"), "TESTNET_TASK_MISMATCH"),
        (replace(context, entrypoint="other"), "TESTNET_ENTRYPOINT_MISMATCH"),
        (replace(context, owner_id="other"), "TESTNET_OWNER_MISMATCH"),
        (replace(context, environment="MAINNET"), "TESTNET_ENVIRONMENT_REQUIRED"),
        (replace(context, rest_base_url="https://fapi.binance.com"), "TESTNET_URL_MISMATCH"),
        (replace(context, account_id="other"), "TESTNET_ACCOUNT_MISMATCH"),
    )
    for changed, reason in context_cases:
        assert guard.validate_context(changed).reason_code == reason

    no_pool = _increase_context(guard, pool_id="", pool_version="", pool_hash="", pool_symbols=())
    assert guard.validate_context(no_pool, _increase_request(guard, no_pool)).reason_code == (
        "TESTNET_CONTEXT_POOL_SCOPE_INCOMPLETE"
    )
    inactive = _increase_context(guard, pool_symbols=("ETHUSDT",))
    assert guard.validate_context(inactive, _increase_request(guard, inactive)).reason_code == (
        "TESTNET_CONTEXT_SYMBOL_NOT_ACTIVE"
    )

    direct_cases = (
        (replace(request, kind=TerminalWriteKind.UNKNOWN), "UNCLASSIFIED_TERMINAL_WRITE"),
        (replace(request, environment="MAINNET"), "TESTNET_ENVIRONMENT_REQUIRED"),
        (replace(request, rest_base_url="https://fapi.binance.com"), "TESTNET_URL_MISMATCH"),
        (replace(request, venue_id="OTHER"), "TESTNET_VENUE_MISMATCH"),
        (replace(request, account_id="other"), "TESTNET_ACCOUNT_MISMATCH"),
        (replace(request, signed=False), "BINANCE_HMAC_REQUIRED"),
        (replace(request, leverage="bad"), "LEVERAGE_MUST_BE_A_FINITE_DECIMAL"),
        (replace(request, account_exposure="bad"), "ACCOUNT_EXPOSURE_MUST_BE_A_FINITE_DECIMAL"),
        (
            replace(request, projected_account_exposure="21"),
            "TESTNET_PROJECTED_ACCOUNT_EXPOSURE_MISMATCH",
        ),
    )
    for changed, reason in direct_cases:
        assert guard.authorize(changed).reason_code == reason


def test_guard_durable_kill_switch_io_failure_is_unknown(tmp_path: Path) -> None:
    guard = TestnetEnvironmentGuard(
        "https://demo-fapi.binance.com",
        max_notional=25,
        max_leverage=3,
        account_id="dedicated-testnet-account",
        owner_id="test-owner",
        writes_enabled=True,
        kill_switch_path=tmp_path / "KILL_SWITCH",
    )
    context = _increase_context(guard)
    request = _increase_request(guard, context)

    class _UnreadableSwitch:
        def stat(self) -> None:
            raise OSError("state unavailable")

    guard._kill_switch_path = _UnreadableSwitch()  # type: ignore[assignment]
    assert guard.authorize(request).reason_code == "TESTNET_KILL_SWITCH_STATE_UNKNOWN"


@pytest.mark.asyncio
async def test_rest_wrappers_forward_exact_write_context() -> None:
    client = BinanceRESTClient("https://demo-fapi.binance.com")
    calls: list[tuple[str, str, bool, dict[str, Any], TerminalWriteContext | None]] = []

    async def record(
        method: str,
        path: str,
        signed: bool = False,
        params: dict[str, Any] | None = None,
        *,
        write_context: TerminalWriteContext | None = None,
    ) -> Result[Any]:
        calls.append((method, path, signed, dict(params or {}), write_context))
        return Result.success({})

    client._request = record  # type: ignore[method-assign]
    context = _context()
    await client.create_order("BTCUSDT", "BUY", "MARKET", "0.01", write_context=context)
    await client.cancel_order("BTCUSDT", 17, write_context=context)
    await client.create_algo_order({"symbol": "BTCUSDT"}, write_context=context)
    await client.cancel_algo_order("BTCUSDT", 19, write_context=context)
    await client.create_listen_key(write_context=context)
    await client.keepalive_listen_key("listen-v4", write_context=context)
    await client.request("POST", "/custom", True, {"x": 1}, write_context=context)

    assert [call[1] for call in calls] == [
        Endpoint.ORDER,
        Endpoint.ORDER,
        Endpoint.ALGO_ORDER,
        Endpoint.ALGO_ORDER,
        Endpoint.LISTEN_KEY,
        Endpoint.LISTEN_KEY,
        "/custom",
    ]
    assert all(call[4] is context for call in calls)
    assert calls[1][3] == {"symbol": "BTCUSDT", "orderId": 17}
    assert calls[5][3] == {"listenKey": "listen-v4"}


@pytest.mark.asyncio
async def test_rest_guard_denials_never_reach_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    transport_calls = 0

    def unexpected_transport(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal transport_calls
        transport_calls += 1
        raise AssertionError("denied write reached transport")

    monkeypatch.setattr("beidou_exchange.binance_usdm.rest_client._sync_urlopen", unexpected_transport)
    params = {
        "symbol": "BTCUSDT",
        "side": "BUY",
        "type": "MARKET",
        "quantity": "0.01",
        "newClientOrderId": "intent-v4",
    }
    cases = (
        (None, "WRITE_AUTHORITY_MISSING"),
        (_DecisionGuard(context_allowed=False), "CONTEXT_DENY"),
        (_DecisionGuard(write_allowed=False), "WRITE_DENY"),
    )
    for authority, reason in cases:
        client = BinanceRESTClient(
            "https://demo-fapi.binance.com",
            account_id="dedicated-testnet-account",
            testnet_guard=authority,
        )
        result = await client._request("POST", Endpoint.ORDER, True, params, write_context=_context())
        assert result.error is not None
        assert result.error.category is ErrorCategory.PERMISSION_DENIED
        assert result.error.raw["reason"] == reason
    assert transport_calls == 0


@pytest.mark.asyncio
async def test_adapter_guard_denials_and_context_forwarding_are_terminal() -> None:
    params = {
        "symbol": "BTCUSDT",
        "side": "BUY",
        "type": "MARKET",
        "quantity": "0.01",
        "newClientOrderId": "intent-v4",
    }
    for authority, reason in (
        (None, "WRITE_AUTHORITY_MISSING"),
        (_DecisionGuard(context_allowed=False), "CONTEXT_DENY"),
        (_DecisionGuard(write_allowed=False), "WRITE_DENY"),
    ):
        transport = _MutableTransport()
        adapter = BinanceUsdmAdapter(
            account_id=AccountId("dedicated-testnet-account"),
            rest_client=transport,
            testnet_guard=authority,
        )
        adapter.health_monitor.update_venue_health(HealthStatus.HEALTHY)
        result = await adapter.request(
            "POST",
            Endpoint.ORDER,
            signed=True,
            params=params,
            write_account_id="dedicated-testnet-account",
            write_context=_context(),
        )
        assert result.error is not None and result.error.raw["reason"] == reason
        assert transport.calls == []

    authority = _DecisionGuard()
    transport = _MutableTransport(
        {
            "orderId": 17,
            "clientOrderId": "intent-v4",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "origQty": "0.01",
            "executedQty": "0",
            "status": "NEW",
            "reduceOnly": False,
        }
    )
    adapter = BinanceUsdmAdapter(
        account_id=AccountId("dedicated-testnet-account"), rest_client=transport, testnet_guard=authority
    )
    adapter.health_monitor.update_venue_health(HealthStatus.HEALTHY)
    context = _context()
    created = await adapter.create_order(_order(), write_context=context)
    assert created.status is OrderStatus.NEW
    assert transport.calls[-1]["write_context"] is context

    transport.response = {
        "orderId": 17,
        "symbol": "BTCUSDT",
        "side": "BUY",
        "type": "MARKET",
        "origQty": "0.01",
        "executedQty": "0",
        "status": "CANCELED",
    }
    cancel_context = _context(
        side="",
        order_type="",
        client_order_id="",
        expected_method="DELETE",
        order_id="17",
    )
    canceled = await adapter.cancel_order("17", _instrument(), write_context=cancel_context)
    assert canceled.status is OrderStatus.CANCELED
    assert transport.calls[-1]["write_context"] is cancel_context


@pytest.mark.asyncio
async def test_adapter_listen_reference_and_market_reads_preserve_unknown_semantics() -> None:
    transport = _MutableTransport()
    adapter = BinanceUsdmAdapter(rest_client=transport)
    context = _context()
    assert (await adapter.create_user_listen_key(context)).data == {"listenKey": "bound-key"}
    assert (await adapter.keepalive_user_listen_key("bound-key", context)).is_success()
    assert transport.listen_calls == [("create", context), ("bound-key", context)]

    transport.response = Result.failure("offline", category=ErrorCategory.NETWORK)
    assert not (await adapter.fetch_exchange_info()).is_success()
    transport.response = {"symbols": "bad"}
    assert not (await adapter.fetch_exchange_info()).is_success()
    transport.response = {"symbols": [None]}
    assert not (await adapter.fetch_exchange_info()).is_success()
    transport.response = {
        "symbols": [{"symbol": "BTCUSDT", "filters": []}],
        "rateLimits": [{"rateLimitType": "REQUEST_WEIGHT", "limit": "10"}, {"limit": "bad"}],
    }
    assert (await adapter.fetch_exchange_info()).is_success()

    transport.response = {"serverTime": 1}
    assert (await adapter.get_server_time()).data == {"serverTime": 1}
    transport.response = {"dualSidePosition": False}
    assert (await adapter.get_position_mode()).data == {"dualSidePosition": False}
    transport.response = Result.failure("account")
    assert not (await adapter.get_account_snapshot()).is_success()
    transport.response = {"canTrade": True}
    assert (await adapter.get_account_snapshot()).data == {"canTrade": True}
    transport.response = {"lastPrice": "100"}
    assert (await adapter.get_ticker("btcusdt")).is_success()
    assert not (await adapter.get_depth("BTCUSDT", 0)).is_success()
    assert not (await adapter.get_depth("BTCUSDT", 1001)).is_success()
    transport.response = {"bids": []}
    assert (await adapter.get_depth("btcusdt", 20)).is_success()
    assert transport.calls[-1]["params"]["symbol"] == "BTCUSDT"


@pytest.mark.asyncio
async def test_adapter_kline_funding_leverage_and_position_readback_boundaries() -> None:
    transport = _MutableTransport()
    adapter = BinanceUsdmAdapter(rest_client=transport)
    assert not (await adapter.get_closed_klines("BTCUSDT", limit=0)).is_success()
    transport.response = Result.failure("klines")
    assert not (await adapter.get_closed_klines("BTCUSDT")).is_success()
    transport.response = [[1, "1"]]
    assert not (await adapter.get_closed_klines("BTCUSDT")).is_success()
    transport.response = [[1, "1", "1", "1", "1", "1", "bad"]]
    assert not (await adapter.get_closed_klines("BTCUSDT")).is_success()
    future = int(datetime.now(timezone.utc).timestamp() * 1000) + 60_000
    transport.response = [[1, "1", "1", "1", "1", "1", future]]
    assert not (await adapter.get_closed_klines("BTCUSDT")).is_success()

    transport.response = Result.failure("funding")
    assert not (await adapter.get_funding_rate("BTCUSDT")).is_success()
    transport.response = [{"fundingRate": "0.001"}]
    assert (await adapter.get_funding_rate("BTCUSDT")).data == [{"fundingRate": "0.001"}]
    transport.response = {"openInterest": "1"}
    assert (await adapter.get_open_interest("btcusdt")).is_success()

    guard = _DecisionGuard()
    adapter = BinanceUsdmAdapter(
        account_id=AccountId("dedicated-testnet-account"), rest_client=transport, testnet_guard=guard
    )
    adapter.health_monitor.update_venue_health(HealthStatus.HEALTHY)
    for leverage in ("bad", "1.5"):
        assert not (
            await adapter.set_leverage("BTCUSDT", leverage, account_ref=_account(), write_context=_context())
        ).is_success()
    transport.response = Result.failure("leverage")
    assert not (
        await adapter.set_leverage("BTCUSDT", 2, account_ref=_account(), write_context=_context(order_type="LEVERAGE"))
    ).is_success()
    transport.response = {"symbol": "BTCUSDT"}
    assert not (
        await adapter.set_leverage("BTCUSDT", 2, account_ref=_account(), write_context=_context(order_type="LEVERAGE"))
    ).is_success()
    transport.response = {"symbol": "ETHUSDT", "leverage": 2}
    assert not (
        await adapter.set_leverage("BTCUSDT", 2, account_ref=_account(), write_context=_context(order_type="LEVERAGE"))
    ).is_success()

    transport.response = Result.failure("readback")
    assert not (await adapter.read_leverage("BTCUSDT")).is_success()
    transport.response = []
    assert not (await adapter.read_leverage("BTCUSDT")).is_success()
    transport.response = [{"symbol": "BTCUSDT", "leverage": "bad"}]
    assert not (await adapter.read_leverage("BTCUSDT")).is_success()
    transport.response = [
        {"symbol": "BTCUSDT", "leverage": "2"},
        {"symbol": "BTCUSDT", "leverage": "3"},
    ]
    assert not (await adapter.read_leverage("BTCUSDT")).is_success()

    transport.response = Result.failure("positions")
    assert not (await adapter.get_position_risk()).is_success()
    transport.response = [{"symbol": "BTCUSDT", "positionAmt": "0"}]
    assert (await adapter.get_position_risk("btcusdt")).is_success()


@pytest.mark.asyncio
async def test_adapter_execution_attribution_rejects_malformed_rows() -> None:
    transport = _MutableTransport()
    adapter = BinanceUsdmAdapter(rest_client=transport)
    assert not (await adapter.get_account_trades("BTCUSDT", "not-an-id")).is_success()
    transport.response = Result.failure("trades")
    assert not (await adapter.get_account_trades("BTCUSDT", "17")).is_success()
    transport.response = [{"id": 1}, None]
    assert not (await adapter.get_account_trades("BTCUSDT", "17")).is_success()
    transport.response = [{"id": 1}]
    assert (await adapter.get_account_trades("btcusdt", "17")).data == [{"id": 1}]

    transport.response = Result.failure("income")
    assert not (await adapter.get_income_history("BTCUSDT", income_type="FUNDING_FEE", start_time=1)).is_success()
    transport.response = [{"income": "1"}, None]
    assert not (await adapter.get_income_history("BTCUSDT", income_type="FUNDING_FEE", start_time=1)).is_success()
    transport.response = [{"income": "1"}]
    assert (await adapter.get_income_history("btcusdt", income_type="FUNDING_FEE", start_time=1)).data == [
        {"income": "1"}
    ]


def test_trading_pool_discovery_recovery_and_score_boundaries() -> None:
    assert discover_startup_candidates(None, max_instruments=1) == []
    assert discover_startup_candidates({}, max_instruments=1) == []
    exchange_info = {
        "symbols": [
            None,
            {"symbol": ""},
            {"symbol": "ALL", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
            {
                "symbol": "BTCUSDT",
                "status": "TRADING",
                "contractType": "PERPETUAL",
                "quoteAsset": "USDT",
            },
            {
                "symbol": "btcusdt",
                "status": "TRADING",
                "contractType": "PERPETUAL",
                "quoteAsset": "USDT",
            },
        ]
    }
    assert discover_startup_candidates(exchange_info, max_instruments=5) == ["BTCUSDT"]

    restored = TradingPool(
        initial_state=[
            {
                "instrument_id": "BTCUSDT",
                "status": PoolStatus.QUARANTINED.value,
                "pool_version": 4,
                "score_detail": {},
            }
        ]
    )
    snapshot = restored.snapshot()
    assert snapshot.quarantined_symbols == ("BTCUSDT",)
    assert snapshot.membership_diff["quarantined_added"] == []
    assert snapshot.score_by_symbol == {}

    for bad in (
        {"spread_bps": float("nan"), "depth_score": 1, "volume_score": 1, "stability_score": 1, "capacity_score": 1},
        {"spread_bps": "bad", "depth_score": 1, "volume_score": 1, "stability_score": 1, "capacity_score": 1},
        {"spread_bps": 1, "depth_score": 1.1, "volume_score": 1, "stability_score": 1, "capacity_score": 1},
    ):
        with pytest.raises(ValueError, match="market quality"):
            restored.update_market_score("BTCUSDT", **bad)  # type: ignore[arg-type]
