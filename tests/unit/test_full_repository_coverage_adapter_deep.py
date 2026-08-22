"""Exhaustive typed boundary cases for the Binance USD-M adapter."""

from __future__ import annotations

import pytest

from beidou_exchange.binance_usdm.adapter import BinanceUsdmAdapter
from beidou_exchange.core.error_taxonomy import Result
from beidou_exchange.core.protocol import OrderRequest
from beidou_shared.errors import ErrorCategory
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


class _Transport:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple] = []

    async def request(self, method, path, *, signed=False, params=None):
        self.calls.append((method, path, signed, params))
        if self.error:
            raise self.error
        return self.response if isinstance(self.response, Result) else Result.success(self.response)

    async def create_listen_key(self):
        return self.response if isinstance(self.response, Result) else Result.success(self.response)

    async def keepalive_listen_key(self, _key):
        return self.response if isinstance(self.response, Result) else Result.success(self.response)


def _account() -> AccountRef:
    return AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("acct"))


def _instrument() -> VenueInstrument:
    return VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))


def _request(*, reduce_only=False, order_type=OrderType.MARKET, client_order_id="cid") -> OrderRequest:
    return OrderRequest(
        venue_instrument=_instrument(),
        account_ref=_account(),
        side=OrderSide.BUY,
        order_type=order_type,
        quantity=Quantity(amount="1"),
        client_order_id=client_order_id,
        reduce_only=reduce_only,
    )


@pytest.mark.asyncio
async def test_adapter_request_and_account_read_paths_cover_transport_failures(monkeypatch) -> None:
    monkeypatch.setenv("BEIDOU_TERMINAL_WRITE_HOLD", "unknown-only")
    transport = _Transport({"ok": True})
    adapter = BinanceUsdmAdapter(rest_client=transport)
    # An unverified health state blocks risk-increasing transport writes but
    # still allows an explicit exit-only request.
    blocked = await adapter.request("POST", "/fapi/v1/order", params={})
    assert blocked.error is not None and blocked.error.category is ErrorCategory.EXCHANGE_UNAVAILABLE
    exit_result = await adapter.request("POST", "/fapi/v1/order", params={"reduceOnly": "true"})
    assert exit_result.is_success()
    transport.response = Result.failure("down", category=ErrorCategory.NETWORK)
    failed = await adapter.request("GET", "/fapi/v1/time")
    assert failed.is_success() is False
    transport.response = Result.success({"ok": True})
    assert (await adapter.request("GET", "/fapi/v1/time")).is_success()
    assert adapter.health_monitor.venue_health is HealthStatus.HEALTHY

    adapter = BinanceUsdmAdapter(rest_client=_Transport({}, error=OSError("offline")))
    info = await adapter.get_account_info(_account())
    assert info.can_trade is False and info.can_withdraw is True

    transport = _Transport([{"asset": "", "balance": "1"}])
    adapter = BinanceUsdmAdapter(rest_client=transport)
    adapter.health_monitor.update_venue_health(HealthStatus.HEALTHY)
    status, _ = await adapter.get_balances(_account())
    assert status is not None and status.value == "UNKNOWN"
    for response in (
        [{"asset": "USDT"}],
        [{"asset": "USDT", "balance": "bad"}],
        [{"asset": "USDT", "balance": "NaN"}],
        {"not": "list"},
    ):
        transport.response = response
        status, _ = await adapter.get_balances(_account())
        assert status.value == "UNKNOWN"

    transport.response = {"positions": [{"symbol": "BTCUSDT", "positionAmt": "bad"}]}
    status, _ = await adapter.get_positions(_account())
    assert status.value == "UNKNOWN"
    transport.response = {"positions": [{"symbol": "BTCUSDT", "positionAmt": "NaN"}]}
    status, _ = await adapter.get_positions(_account())
    assert status.value == "UNKNOWN"
    transport.response = {"positions": "not-list"}
    status, _ = await adapter.get_positions(_account())
    assert status.value == "UNKNOWN"


def test_adapter_order_and_cancel_ack_validation_matrix() -> None:
    request = _request(reduce_only=True)
    valid = {
        "orderId": "1",
        "status": "FILLED",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "type": "MARKET",
        "origQty": "1",
        "executedQty": "1",
        "clientOrderId": "cid",
        "reduceOnly": True,
    }
    assert BinanceUsdmAdapter._validate_order_ack(request, valid) == (True, "OK")
    cases = [
        (None, "ACK_NOT_OBJECT"),
        ({"orderId": "1"}, "ACK_IDENTITY_MISSING"),
        ({**valid, "executedQty": ""}, "ACK_EXECUTED_QTY_MISSING"),
        ({**valid, "symbol": "ETHUSDT"}, "ACK_SYMBOL_MISMATCH"),
        ({**valid, "side": "SELL"}, "ACK_SIDE_MISMATCH"),
        ({**valid, "type": "LIMIT"}, "ACK_ORDER_TYPE_MISMATCH"),
        ({**valid, "status": "BOGUS"}, "ACK_STATUS_UNKNOWN"),
        ({**valid, "clientOrderId": ""}, "ACK_CLIENT_ORDER_ID_MISSING"),
        ({**valid, "clientOrderId": "other"}, "ACK_CLIENT_ORDER_ID_MISMATCH"),
        ({**valid, "origQty": "bad"}, "ACK_QUANTITY_INVALID"),
        ({**valid, "origQty": "2"}, "ACK_QUANTITY_MISMATCH"),
        ({**valid, "reduceOnly": False}, "ACK_REDUCE_ONLY_UNPROVEN"),
    ]
    for response, reason in cases:
        assert BinanceUsdmAdapter._validate_order_ack(request, response) == (False, reason)
    truncated = {**valid, "origQty": "0.5", "executedQty": "0.5"}
    assert BinanceUsdmAdapter._validate_order_ack(request, truncated) == (True, "OK")

    cancel_valid = {
        "orderId": "1",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "type": "MARKET",
        "origQty": "1",
        "executedQty": "0",
        "status": "CANCELED",
    }
    assert BinanceUsdmAdapter._validate_cancel_ack("1", _instrument(), cancel_valid) == (True, "OK")
    cancel_cases = [
        (None, "CANCEL_ACK_NOT_OBJECT"),
        ({"orderId": "1"}, "CANCEL_ACK_IDENTITY_MISSING"),
        ({**cancel_valid, "orderId": "2"}, "CANCEL_ACK_ORDER_ID_MISMATCH"),
        ({**cancel_valid, "symbol": "ETHUSDT"}, "CANCEL_ACK_SYMBOL_MISMATCH"),
        ({**cancel_valid, "status": "NEW"}, "CANCEL_ACK_NOT_TERMINAL"),
        ({**cancel_valid, "status": "BAD"}, "CANCEL_ACK_STATUS_UNKNOWN"),
        ({**cancel_valid, "origQty": "bad"}, "CANCEL_ACK_QUANTITY_INVALID"),
        ({**cancel_valid, "executedQty": "2"}, "CANCEL_ACK_EXECUTED_QTY_INVALID"),
        ({**cancel_valid, "executedQty": "0.2"}, "CANCEL_ACK_PARTIAL_FILL_RECONCILIATION_REQUIRED"),
        ({**cancel_valid, "side": "BAD"}, "CANCEL_ACK_ORDER_SEMANTICS_UNKNOWN"),
    ]
    for response, reason in cancel_cases:
        assert BinanceUsdmAdapter._validate_cancel_ack("1", _instrument(), response) == (False, reason)


@pytest.mark.asyncio
async def test_adapter_create_cancel_algo_and_recovery_semantics(monkeypatch) -> None:
    monkeypatch.setenv("BEIDOU_TERMINAL_WRITE_HOLD", "unknown-only")
    order_ack = {
        "orderId": "1",
        "clientOrderId": "cid",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "type": "MARKET",
        "origQty": "1",
        "status": "NEW",
        "executedQty": "0",
        "reduceOnly": False,
    }
    transport = _Transport(order_ack)
    adapter = BinanceUsdmAdapter(rest_client=transport)
    adapter.health_monitor.update_venue_health(HealthStatus.HEALTHY)
    response = await adapter.create_order(_request(reduce_only=False, client_order_id=""))
    assert response.status is OrderStatus.NEW
    assert "newClientOrderId" not in transport.calls[-1][3]

    transport.response = Result.failure("no margin", category=ErrorCategory.INSUFFICIENT_MARGIN, raw={"code": -2019})
    response = await adapter.create_order(_request())
    assert response.status is OrderStatus.REJECTED
    transport.response = Result.failure("rate", category=ErrorCategory.TIMEOUT, retryable=True)
    response = await adapter.create_order(_request())
    assert response.status is OrderStatus.REJECTED
    transport.response = Result.success({"unexpected": True})
    response = await adapter.create_order(_request())
    assert response.status is OrderStatus.UNKNOWN

    transport.response = Result.success({"code": 200})
    canceled = await adapter.cancel_algo_order("BTCUSDT", 1)
    assert canceled.is_success() is False
    transport.response = Result.success({"algoId": "1"})
    assert (await adapter.cancel_algo_order("BTCUSDT", 1)).is_success()

    # A complete conditional ACK is accepted; each mismatch is preserved as
    # UNKNOWN and cannot become an ACTIVE protection fact.
    params = {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "type": "STOP_MARKET",
        "quantity": "1",
        "triggerPrice": "90",
        "clientAlgoId": "cid",
        "workingType": "CONTRACT_PRICE",
        "positionSide": "BOTH",
        "reduceOnly": "true",
        "closePosition": "false",
    }
    algo = {
        "algoId": "7",
        "symbol": "BTCUSDT",
        "side": "SELL",
        "orderType": "STOP_MARKET",
        "triggerPrice": "90",
        "quantity": "1",
        "algoStatus": "NEW",
        "clientAlgoId": "cid",
        "workingType": "CONTRACT_PRICE",
        "positionSide": "BOTH",
        "reduceOnly": True,
        "closePosition": False,
    }
    transport.response = Result.success(algo)
    assert (await adapter.create_algo_order(params)).is_success()
    for key, value in (
        ("symbol", "ETHUSDT"),
        ("side", "BUY"),
        ("type", "TAKE_PROFIT_MARKET"),
        ("clientAlgoId", "other"),
        ("triggerPrice", "bad"),
        ("workingType", "MARK_PRICE"),
        ("positionSide", "LONG"),
        ("closePosition", "true"),
        ("quantity", "2"),
    ):
        changed = dict(params)
        changed[key] = value
        result = await adapter.create_algo_order(changed)
        assert result.is_success() is False, key
    transport.response = Result.success({**algo, "reduceOnly": False})
    assert (await adapter.create_algo_order(params)).is_success() is False


@pytest.mark.asyncio
async def test_adapter_algo_inventory_and_user_stream_parser_error_paths() -> None:
    adapter = BinanceUsdmAdapter()
    assert (await adapter.get_open_algo_orders()).is_success() is False
    transport = _Transport([{}])
    adapter = BinanceUsdmAdapter(rest_client=transport)
    adapter.health_monitor.update_venue_health(HealthStatus.HEALTHY)
    assert (await adapter.get_open_algo_orders()).is_success() is False

    event = {
        "e": "TRADE_LITE",
        "E": 1,
        "i": "1",
        "c": "cid",
        "s": "BTCUSDT",
        "S": "BUY",
        "q": "1",
        "L": "100",
        "l": "0.1",
        "t": "2",
    }
    assert BinanceUsdmAdapter.parse_user_order_update(event).is_success()
    assert BinanceUsdmAdapter.parse_user_order_update({**event, "l": "bad"}).is_success() is False
    order_event = {
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
    assert BinanceUsdmAdapter.parse_user_order_update(order_event).is_success()
    assert (
        BinanceUsdmAdapter.parse_user_order_update({"e": "ORDER_TRADE_UPDATE", "E": 1, "o": {}}).is_success() is False
    )
    account = {
        "e": "ACCOUNT_UPDATE",
        "E": 1,
        "a": {
            "B": [{"a": "USDT", "wb": "1", "cw": "1"}],
            "P": [{"s": "BTCUSDT", "pa": "0", "ep": "100", "bep": "100", "up": "0"}],
            "m": "ORDER",
        },
    }
    assert BinanceUsdmAdapter.parse_user_account_update(account).is_success()
    assert (
        BinanceUsdmAdapter.parse_user_account_update({**account, "a": {**account["a"], "m": ""}}).is_success() is False
    )
