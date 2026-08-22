"""Additional behavior coverage for Binance adapter error and identity gates."""

from __future__ import annotations

import asyncio
from io import BytesIO
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from beidou_exchange.binance_usdm import rest_client as rest_module
from beidou_exchange.binance_usdm.adapter import (
    BinanceHealthMonitor,
    BinanceUsdmAdapter,
    _format_decimal,
    _safe_enum,
    _sanitized_adapter_error,
    _strict_bool,
)
from beidou_exchange.binance_usdm.endpoints import Endpoint
from beidou_exchange.binance_usdm.rest_client import BinanceRESTClient
from beidou_exchange.core.error_taxonomy import ErrorCategory, Result
from beidou_exchange.core.protocol import OrderRequest
from beidou_shared.types import (
    AccountId,
    AccountRef,
    HealthStatus,
    InstrumentId,
    OrderSide,
    OrderStatus,
    OrderType,
    Quantity,
    ResultStatus,
    VenueId,
    VenueInstrument,
)


class QueueTransport:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, bool, dict[str, Any]]] = []

    async def request(
        self, method: str, path: str, signed: bool = False, params: dict[str, Any] | None = None
    ) -> Result[Any]:
        self.calls.append((method, path, signed, dict(params or {})))
        response = self.responses.pop(0) if self.responses else Result.success({})
        if isinstance(response, BaseException):
            raise response
        return response if isinstance(response, Result) else Result.success(response)

    async def create_listen_key(self) -> Result[dict[str, Any]]:
        return self.responses.pop(0) if self.responses else Result.success({"listenKey": "lk"})

    async def keepalive_listen_key(self, listen_key: str) -> Result[dict[str, Any]]:
        del listen_key
        return self.responses.pop(0) if self.responses else Result.success({"listenKey": "lk"})

    def reset_circuit_breaker(self) -> None:
        return None

    def is_circuit_breaker_open(self) -> bool:
        return False


class DirectAdapter(BinanceUsdmAdapter):
    async def request(
        self,
        method: str,
        path: str,
        signed: bool = False,
        params: dict[str, Any] | None = None,
        write_account_id: str | None = None,
    ) -> Result[Any]:
        del write_account_id
        if self._rest_client is None:
            return Result.failure("transport missing", category=ErrorCategory.UNKNOWN)
        return await self._rest_client.request(method, path, signed=signed, params=params)


def _account() -> AccountRef:
    return AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("unit"))


def _instrument() -> VenueInstrument:
    return VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))


def _order_request(
    *, order_type: OrderType = OrderType.MARKET, client_id: str = "cid", reduce_only: bool = False
) -> OrderRequest:
    return OrderRequest(
        venue_instrument=_instrument(),
        account_ref=_account(),
        side=OrderSide.BUY,
        order_type=order_type,
        quantity=Quantity(amount="1.25"),
        client_order_id=client_id,
        reduce_only=reduce_only,
    )


def _order_ack(*, order_type: str = "MARKET", status: str = "NEW", quantity: str = "1.25") -> dict[str, Any]:
    return {
        "orderId": 17,
        "clientOrderId": "cid",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "type": order_type,
        "origQty": quantity,
        "status": status,
        "executedQty": "0",
        "reduceOnly": False,
    }


def _algo_snapshot(**overrides: Any) -> dict[str, Any]:
    value = {
        "algoId": "a-1",
        "symbol": "BTCUSDT",
        "side": "SELL",
        "orderType": "STOP_MARKET",
        "triggerPrice": "90",
        "algoStatus": "NEW",
        "quantity": "1",
        "clientAlgoId": "algo-cid",
        "reduceOnly": True,
        "closePosition": False,
        "positionSide": "BOTH",
        "updateTime": 10,
    }
    value.update(overrides)
    return value


def test_adapter_helpers_and_transport_health_boundaries() -> None:
    assert _format_decimal("1.2300") == "1.23"
    assert _format_decimal("bad") == "bad"
    assert _safe_enum(OrderType, "NOT_A_TYPE") is OrderType.UNKNOWN
    for value, expected in ((True, True), (0, False), ("yes", True), ("no", False)):
        assert _strict_bool(value, field_name="flag") is expected
    with pytest.raises(ValueError):
        _strict_bool("maybe", field_name="flag")

    error = Result.failure(
        "fallback",
        http_status=418,
        category=ErrorCategory.RATE_LIMIT,
        retryable=True,
        raw={"code": -1003, "msg": "detail"},
    ).error
    assert error is not None
    sanitized = _sanitized_adapter_error(error, fallback="fallback")
    assert sanitized["code"] == -1003 and sanitized["http_status"] == 418
    assert _sanitized_adapter_error(object(), fallback="fallback")["msg"] == "fallback"

    monitor = BinanceHealthMonitor(VenueId("BINANCE"))
    assert monitor.get_instrument_health(InstrumentId("BTCUSDT")) is HealthStatus.UNKNOWN
    monitor.record_transport_failure()
    monitor.record_transport_failure()
    monitor.record_transport_failure()
    assert monitor.venue_health is HealthStatus.UNAVAILABLE
    monitor.record_transport_success()
    assert monitor.venue_health is HealthStatus.HEALTHY
    monitor.record_error("/x", "NETWORK")
    monitor.set_instrument_health(InstrumentId("BTCUSDT"), HealthStatus.UNKNOWN)
    assert monitor.instruments_requiring_action()[InstrumentId("BTCUSDT")].value == "EXIT_ONLY"


@pytest.mark.asyncio
async def test_adapter_transport_balance_position_and_listen_key_failures() -> None:
    missing = BinanceUsdmAdapter()
    assert (await missing.request("GET", Endpoint.ACCOUNT)).is_success() is False
    assert await missing.get_exchange_info()  # typed read-only surface
    assert await missing.check_health(VenueId("BINANCE")) is HealthStatus.UNKNOWN
    assert (await missing.create_user_listen_key()).is_success() is False
    assert (await missing.keepalive_user_listen_key("lk")).is_success() is False
    missing.reset_circuit_breaker()
    assert missing.is_circuit_breaker_open() is False

    bad_balance = DirectAdapter(rest_client=QueueTransport(Result.failure("balance", category=ErrorCategory.NETWORK)))
    bad_balance.health_monitor.update_venue_health(HealthStatus.HEALTHY)
    assert await bad_balance.get_balances(_account()) == (ResultStatus.UNKNOWN, {})
    exception_balance = DirectAdapter(rest_client=QueueTransport(RuntimeError("balance")))
    exception_balance.health_monitor.update_venue_health(HealthStatus.HEALTHY)
    assert await exception_balance.get_balances(_account()) == (ResultStatus.UNKNOWN, {})
    malformed_balance = DirectAdapter(rest_client=QueueTransport([{"asset": "USDT", "balance": "bad"}]))
    malformed_balance.health_monitor.update_venue_health(HealthStatus.HEALTHY)
    assert await malformed_balance.get_balances(_account()) == (ResultStatus.UNKNOWN, {})

    bad_positions = DirectAdapter(rest_client=QueueTransport(Result.failure("positions")))
    bad_positions.health_monitor.update_venue_health(HealthStatus.HEALTHY)
    assert await bad_positions.get_positions(_account()) == (ResultStatus.UNKNOWN, {})
    exception_positions = DirectAdapter(rest_client=QueueTransport(RuntimeError("positions")))
    exception_positions.health_monitor.update_venue_health(HealthStatus.HEALTHY)
    assert await exception_positions.get_positions(_account()) == (ResultStatus.UNKNOWN, {})
    malformed_positions = DirectAdapter(rest_client=QueueTransport({"positions": [{"symbol": "BTCUSDT"}]}))
    malformed_positions.health_monitor.update_venue_health(HealthStatus.HEALTHY)
    assert await malformed_positions.get_positions(_account()) == (ResultStatus.UNKNOWN, {})

    listen = DirectAdapter(rest_client=QueueTransport(Result.success({"listenKey": "lk"}), Result.failure("bad")))
    assert (await listen.create_user_listen_key()).is_success() is True
    assert (await listen.keepalive_user_listen_key("lk")).is_success() is False


@pytest.mark.asyncio
async def test_adapter_order_cancel_status_and_recovery_edges() -> None:
    unsafe = BinanceUsdmAdapter()
    rejected = await unsafe.create_order(_order_request())
    assert rejected.status is OrderStatus.REJECTED

    non_market = DirectAdapter(rest_client=QueueTransport(_order_ack(order_type="LIMIT")))
    non_market.health_monitor.update_venue_health(HealthStatus.HEALTHY)
    response = await non_market.create_order(_order_request(order_type=OrderType.LIMIT))
    assert response.status is OrderStatus.NEW
    assert non_market._rest_client.calls[-1][3]["timeInForce"] == "GTC"  # type: ignore[union-attr]

    failed = DirectAdapter(rest_client=QueueTransport(RuntimeError("post")))
    failed.health_monitor.update_venue_health(HealthStatus.HEALTHY)
    assert (await failed.create_order(_order_request())).status is OrderStatus.UNKNOWN

    venue = _instrument()
    cancel_failed = DirectAdapter(rest_client=QueueTransport(Result.failure("cancel")))
    failed_cancel = await cancel_failed.cancel_order("17", venue)
    assert failed_cancel.status is OrderStatus.UNKNOWN
    cancel_bad = DirectAdapter(rest_client=QueueTransport({"orderId": "17"}))
    bad_cancel = await cancel_bad.cancel_order("17", venue)
    assert bad_cancel.status is OrderStatus.UNKNOWN
    cancel_exception = DirectAdapter(rest_client=QueueTransport(TypeError("cancel")))
    assert (await cancel_exception.cancel_order("17", venue)).status is OrderStatus.UNKNOWN

    valid_cancel = {
        "orderId": "17",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "type": "MARKET",
        "origQty": "1",
        "executedQty": "0",
        "status": "CANCELED",
    }
    acknowledged = DirectAdapter(rest_client=QueueTransport(valid_cancel))
    assert (await acknowledged.cancel_order("17", venue)).status is OrderStatus.CANCELED

    assert (
        await DirectAdapter(rest_client=QueueTransport(Result.failure("status"))).get_order_status("17", venue)
        is OrderStatus.UNKNOWN
    )
    assert (
        await DirectAdapter(
            rest_client=QueueTransport({"orderId": "18", "symbol": "BTCUSDT", "status": "NEW"})
        ).get_order_status("17", venue)
        is OrderStatus.UNKNOWN
    )
    assert (
        await DirectAdapter(
            rest_client=QueueTransport({"orderId": "17", "symbol": "ETHUSDT", "status": "NEW"})
        ).get_order_status("17", venue)
        is OrderStatus.UNKNOWN
    )
    assert (
        await DirectAdapter(
            rest_client=QueueTransport({"orderId": "17", "symbol": "BTCUSDT", "status": "INVALID"})
        ).get_order_status("17", venue)
        is OrderStatus.UNKNOWN
    )
    assert (
        await DirectAdapter(rest_client=QueueTransport(TypeError("status"))).get_order_status("17", venue)
        is OrderStatus.UNKNOWN
    )

    lookup = DirectAdapter(rest_client=QueueTransport(RuntimeError("lookup")))
    assert (await lookup.query_order_by_client_id("BTCUSDT", "cid")).is_success() is False
    incomplete = DirectAdapter(rest_client=QueueTransport({"symbol": "BTCUSDT", "clientOrderId": "cid"}))
    assert (await incomplete.query_order_by_client_id("BTCUSDT", "cid")).is_success() is False
    mismatched = DirectAdapter(
        rest_client=QueueTransport(
            {
                "symbol": "ETHUSDT",
                "clientOrderId": "cid",
                "orderId": 1,
                "status": "NEW",
                "side": "BUY",
                "type": "MARKET",
                "origQty": "1",
                "executedQty": "0",
            }
        )
    )
    assert (await mismatched.query_order_by_client_id("BTCUSDT", "cid")).is_success() is False
    invalid_semantics = DirectAdapter(
        rest_client=QueueTransport(
            {
                "symbol": "BTCUSDT",
                "clientOrderId": "cid",
                "orderId": 1,
                "status": "BAD",
                "side": "BUY",
                "type": "MARKET",
                "origQty": "1",
                "executedQty": "0",
            }
        )
    )
    assert (await invalid_semantics.query_order_by_client_id("BTCUSDT", "cid")).is_success() is False


def test_adapter_ack_and_algo_parser_edges() -> None:
    request = _order_request(reduce_only=True)
    for raw, reason in (
        (None, "ACK_NOT_OBJECT"),
        ({}, "ACK_IDENTITY_MISSING"),
        ({**_order_ack(), "executedQty": None}, "ACK_EXECUTED_QTY_MISSING"),
        ({**_order_ack(), "symbol": "ETHUSDT"}, "ACK_SYMBOL_MISMATCH"),
        ({**_order_ack(), "side": "SELL"}, "ACK_SIDE_MISMATCH"),
        ({**_order_ack(), "type": "LIMIT"}, "ACK_ORDER_TYPE_MISMATCH"),
        ({**_order_ack(), "status": "BAD"}, "ACK_STATUS_UNKNOWN"),
        ({**_order_ack(), "clientOrderId": "wrong"}, "ACK_CLIENT_ORDER_ID_MISMATCH"),
        ({**_order_ack(), "origQty": "bad"}, "ACK_QUANTITY_INVALID"),
        ({**_order_ack(), "origQty": "2", "executedQty": "0"}, "ACK_QUANTITY_MISMATCH"),
        ({**_order_ack(), "reduceOnly": False}, "ACK_REDUCE_ONLY_UNPROVEN"),
    ):
        assert BinanceUsdmAdapter._validate_order_ack(request, raw) == (False, reason)
    truncated = {**_order_ack(status="FILLED", quantity="0.5"), "reduceOnly": True, "executedQty": "0.5"}
    assert BinanceUsdmAdapter._validate_order_ack(request, truncated) == (True, "OK")

    account_ref = _account()
    assert BinanceUsdmAdapter.parse_algo_order_snapshot(None, account_ref).is_success() is False
    assert BinanceUsdmAdapter.parse_algo_order_snapshot({"algoId": "a"}, account_ref).is_success() is False
    assert (
        BinanceUsdmAdapter.parse_algo_order_snapshot(_algo_snapshot(triggerPrice="bad"), account_ref).is_success()
        is False
    )
    assert (
        BinanceUsdmAdapter.parse_algo_order_snapshot(_algo_snapshot(reduceOnly="maybe"), account_ref).is_success()
        is False
    )
    parsed = BinanceUsdmAdapter.parse_algo_order_snapshot(_algo_snapshot(), account_ref)
    assert parsed.is_success() and parsed.data is not None


@pytest.mark.asyncio
async def test_adapter_algo_orders_user_stream_and_account_parser_edges() -> None:
    missing = BinanceUsdmAdapter()
    assert (await missing.get_open_algo_orders()).is_success() is False
    assert (await missing.create_algo_order({})).is_success() is False
    assert (await missing.cancel_algo_order("BTCUSDT", 1)).is_success() is False

    error_response = DirectAdapter(rest_client=QueueTransport(Result.failure("algo", category=ErrorCategory.NETWORK)))
    assert (await error_response.get_open_algo_orders()).is_success() is False
    non_list = DirectAdapter(rest_client=QueueTransport({"not": "list"}))
    assert (await non_list.get_open_algo_orders()).is_success() is False
    incomplete_row = DirectAdapter(rest_client=QueueTransport([_algo_snapshot(), {"algoId": "bad"}]))
    assert (await incomplete_row.get_open_algo_orders()).is_success() is False

    no_error = DirectAdapter(rest_client=QueueTransport(Result(is_ok=False, data={"x": 1}, error=None)))
    assert (await no_error.create_algo_order({})).is_success() is False
    invalid_ack = DirectAdapter(rest_client=QueueTransport({"bad": "ack"}))
    assert (await invalid_ack.create_algo_order({})).is_success() is False
    params = {"symbol": "BTCUSDT", "side": "SELL", "type": "STOP_MARKET", "triggerPrice": "90", "quantity": "bad"}
    quantity_bad = DirectAdapter(rest_client=QueueTransport(_algo_snapshot(quantity="1")))
    assert (await quantity_bad.create_algo_order(params)).is_success() is False
    assert (
        await DirectAdapter(rest_client=QueueTransport(Result.success({}))).cancel_algo_order("BTCUSDT", 1)
    ).is_success() is False

    event = {
        "e": "ORDER_TRADE_UPDATE",
        "E": 1,
        "o": {
            "i": "1",
            "c": "cid",
            "s": "BTCUSDT",
            "S": "BUY",
            "o": "MARKET",
            "X": "NEW",
            "x": "NEW",
            "q": "1",
            "z": "0",
            "l": "0",
            "L": "0",
            "ap": "0",
            "n": "0",
            "rp": "0",
        },
    }
    assert BinanceUsdmAdapter.parse_user_order_update({"e": "OTHER", "E": 1}).is_success() is False
    assert BinanceUsdmAdapter.parse_user_order_update(event | {"o": {"i": "1"}}).is_success() is False
    assert BinanceUsdmAdapter.parse_user_order_update(event | {"o": {**event["o"], "q": "bad"}}).is_success() is False
    lite = {
        "e": "TRADE_LITE",
        "E": 1,
        "i": "1",
        "c": "cid",
        "s": "BTCUSDT",
        "S": "BUY",
        "q": "1",
        "L": "100",
        "l": "1",
        "t": "2",
    }
    assert BinanceUsdmAdapter.parse_user_order_update(lite).is_success() is True
    assert BinanceUsdmAdapter.parse_user_order_update(lite | {"l": "0"}).is_success() is False

    account_event = {
        "e": "ACCOUNT_UPDATE",
        "E": 1,
        "a": {
            "B": [{"a": "USDT", "wb": "1", "cw": "1"}],
            "P": [{"s": "BTCUSDT", "pa": "0", "ep": "100", "bep": "100", "up": "0"}],
            "m": "ORDER",
        },
    }
    assert BinanceUsdmAdapter.parse_user_account_update({"e": "OTHER", "E": 1}).is_success() is False
    assert BinanceUsdmAdapter.parse_user_account_update({"e": "ACCOUNT_UPDATE", "E": 1, "a": {}}).is_success() is False
    assert (
        BinanceUsdmAdapter.parse_user_account_update(
            account_event | {"a": {**account_event["a"], "B": [None]}}
        ).is_success()
        is False
    )
    assert (
        BinanceUsdmAdapter.parse_user_account_update(
            account_event | {"a": {**account_event["a"], "B": [{"a": "", "wb": "1", "cw": "1"}]}}
        ).is_success()
        is False
    )
    assert (
        BinanceUsdmAdapter.parse_user_account_update(
            account_event | {"a": {**account_event["a"], "P": [None]}}
        ).is_success()
        is False
    )
    assert (
        BinanceUsdmAdapter.parse_user_account_update(
            account_event
            | {"a": {**account_event["a"], "P": [{"s": "", "pa": "0", "ep": "100", "bep": "100", "up": "0"}]}}
        ).is_success()
        is False
    )
    assert (
        BinanceUsdmAdapter.parse_user_account_update(
            account_event | {"a": {**account_event["a"], "m": ""}}
        ).is_success()
        is False
    )


def test_rest_client_lifecycle_clock_and_header_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    client = BinanceRESTClient("https://demo.example")
    assert client.is_circuit_breaker_open() is False
    client.close()

    class _Session:
        def __init__(self, *, fail: bool = False) -> None:
            self.fail = fail
            self.closed = False

        def close(self) -> None:
            self.closed = True
            if self.fail:
                raise RuntimeError("close failed")

    session = _Session()
    client._session = session
    client.close()
    assert session.closed is True and client._session is None
    client._session = _Session(fail=True)
    client._reset_transport_session()
    assert client._session is None
    client._reset_transport_session()

    async def server_time() -> Result[dict[str, Any]]:
        return Result.ok({"serverTime": 1_700_000_001_234})

    monkeypatch.setattr(client, "get_server_time", server_time)
    monkeypatch.setattr(rest_module.time, "time", lambda: 1_700_000_000.0)
    asyncio.run(client._resync_clock_offset())
    assert client._clock_offset_ms == 1_234

    client._update_rate_state_from_headers(
        {
            "X-MBX-USED-WEIGHT-1M": "bad",
            "X-MBX-ORDER-COUNT-1M": "bad;also-bad",
        }
    )
    client._rate_state.weight_limit = 100
    client._update_rate_state_from_headers(
        {
            "x-mbx-used-weight-1m": "90",
            "x-mbx-order-count-1m": "4;10",
        }
    )
    assert client._rate_state.weight_used == 90
    assert client._rate_state.order_count == 4
    assert client._rate_state.order_limit == 10


def test_rest_client_request_error_and_retry_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(rest_module.asyncio, "sleep", no_sleep)

    nonnumeric = BinanceRESTClient("https://demo.example", max_retries=1)
    monkeypatch.setattr(rest_module, "_sync_urlopen", lambda *_args: (b'{"code":"not-a-number"}', {}))
    assert asyncio.run(nonnumeric.get_ticker("BTCUSDT")).is_success() is True

    rate_client = BinanceRESTClient("https://demo.example", max_retries=2)
    retry_after_values: list[float] = []

    async def capture_sleep(seconds: float) -> None:
        retry_after_values.append(seconds)

    monkeypatch.setattr(rest_module.asyncio, "sleep", capture_sleep)

    def malformed_rate_limit(request: Request, _timeout: int, _session: Any = None) -> tuple[bytes, dict]:
        raise HTTPError(
            request.full_url,
            429,
            "too many",
            {"Retry-After": "not-a-number"},
            BytesIO(b'{"code":-1003,"msg":"limited"}'),
        )

    monkeypatch.setattr(rest_module, "_sync_urlopen", malformed_rate_limit)
    rate_result = asyncio.run(rate_client.get_ticker("BTCUSDT"))
    assert rate_result.error is not None and rate_result.error.category is ErrorCategory.RATE_LIMIT
    assert retry_after_values == [1.0]

    clock_client = BinanceRESTClient("https://demo.example", max_retries=2)
    resync_calls: list[str] = []

    async def resync() -> None:
        resync_calls.append("resync")

    monkeypatch.setattr(clock_client, "_resync_clock_offset", resync)

    def clock_error(request: Request, _timeout: int, _session: Any = None) -> tuple[bytes, dict]:
        raise HTTPError(
            request.full_url,
            400,
            "bad timestamp",
            {},
            BytesIO(b'{"code":-1021,"msg":"timestamp"}'),
        )

    monkeypatch.setattr(rest_module, "_sync_urlopen", clock_error)
    clock_result = asyncio.run(clock_client.get_account())
    assert clock_result.error is not None
    assert resync_calls == ["resync", "resync"]

    monkeypatch.setenv("BEIDOU_TERMINAL_WRITE_HOLD", "unknown-only")
    write_client = BinanceRESTClient("https://demo.example", max_retries=3)
    monkeypatch.setattr(rest_module, "_sync_urlopen", lambda *_args: (_ for _ in ()).throw(OSError("broken")))
    write_result = asyncio.run(write_client.create_order("BTCUSDT", "BUY", "MARKET", "1"))
    assert write_result.error is not None
    assert write_result.error.category is ErrorCategory.NETWORK
    assert write_result.error.retryable is False

    exhausted = BinanceRESTClient("https://demo.example", max_retries=0)
    exhausted_result = asyncio.run(exhausted.get_ticker("BTCUSDT"))
    assert exhausted_result.error is not None
    assert exhausted_result.error.message == "Max retries exhausted"

    invalid_body = BinanceRESTClient("https://demo.example", max_retries=1)

    def invalid_json(request: Request, _timeout: int, _session: Any = None) -> tuple[bytes, dict]:
        raise HTTPError(request.full_url, 400, "bad", {}, BytesIO(b"not-json"))

    monkeypatch.setattr(rest_module, "_sync_urlopen", invalid_json)
    invalid_result = asyncio.run(invalid_body.get_ticker("BTCUSDT"))
    assert invalid_result.error is not None and invalid_result.error.raw == {"body": "not-json"}

    invalid_code = BinanceRESTClient("https://demo.example", max_retries=1)

    def invalid_code_body(request: Request, _timeout: int, _session: Any = None) -> tuple[bytes, dict]:
        raise HTTPError(request.full_url, 400, "bad", {}, BytesIO(b'{"code":"bad","msg":"rejected"}'))

    monkeypatch.setattr(rest_module, "_sync_urlopen", invalid_code_body)
    invalid_code_result = asyncio.run(invalid_code.get_ticker("BTCUSDT"))
    assert invalid_code_result.error is not None


def test_rest_client_error_streak_opens_breaker_and_sync_close_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    client = BinanceRESTClient("https://demo.example", max_retries=1)
    calls = 0
    sync_urlopen = rest_module._sync_urlopen

    def unavailable(request: Request, _timeout: int, _session: Any = None) -> tuple[bytes, dict]:
        nonlocal calls
        calls += 1
        raise HTTPError(request.full_url, 503, "down", {}, BytesIO(b'{"msg":"down"}'))

    monkeypatch.setattr(rest_module, "_sync_urlopen", unavailable)
    for _ in range(5):
        result = asyncio.run(client.get_ticker("BTCUSDT"))
        assert result.is_success() is False
    assert client.is_circuit_breaker_open() is True
    assert calls == 5

    class _HttpxErrorClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def request(self, *_args: Any, **_kwargs: Any) -> Any:
            import httpx

            raise httpx.ConnectError("offline")

        def close(self) -> None:
            raise RuntimeError("close failed")

    import httpx

    monkeypatch.setattr(httpx, "Client", _HttpxErrorClient)
    request = Request("https://demo.example/ping", method="GET")
    with pytest.raises(httpx.HTTPError):
        sync_urlopen(request, 1)

    class _HttpxResponseClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def request(self, *_args: Any, **_kwargs: Any) -> Any:
            return type("Response", (), {"status_code": 200, "content": b"ok", "headers": {}})()

        def close(self) -> None:
            raise RuntimeError("close failed")

    monkeypatch.setattr(httpx, "Client", _HttpxResponseClient)
    body, headers = sync_urlopen(request, 1)
    assert body == b"ok" and headers == {}
