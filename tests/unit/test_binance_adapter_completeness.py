"""Semantic coverage for Binance adapter boundaries.

These tests use an in-process transport double only.  They exercise the
adapter's typed parsing, health, and fail-closed branches without contacting
an exchange or authorizing a terminal write.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from beidou_exchange.binance_usdm.adapter import (
    BinanceHealthMonitor,
    BinanceReferenceData,
    BinanceUsdmAdapter,
    InstrumentStatus,
    _format_decimal,
    _safe_enum,
    _sanitized_adapter_error,
    _strict_bool,
)
from beidou_exchange.core.error_taxonomy import Result
from beidou_shared.errors import ErrorCategory
from beidou_shared.types import (
    AccountId,
    AccountRef,
    HealthStatus,
    InstrumentId,
    OrderStatus,
    OrderType,
    VenueId,
    VenueInstrument,
)


class _Transport:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[tuple[str, str, bool, dict[str, object] | None]] = []
        self.reset_count = 0
        self.open = True

    async def request(
        self,
        method: str,
        path: str,
        *,
        signed: bool = False,
        params: dict[str, object] | None = None,
    ) -> Result[object]:
        self.calls.append((method, path, signed, params))
        if isinstance(self.response, Result):
            return self.response
        return Result.success(self.response)

    def reset_circuit_breaker(self) -> None:
        self.reset_count += 1

    def is_circuit_breaker_open(self) -> bool:
        return self.open

    async def create_listen_key(self) -> Result[object]:
        return Result.success(self.response)

    async def keepalive_listen_key(self, listen_key: str) -> Result[object]:
        del listen_key
        return Result.success(self.response)


def _account_ref() -> AccountRef:
    return AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test"))


def _instrument() -> VenueInstrument:
    return VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))


def test_adapter_scalar_helpers_and_sanitized_error() -> None:
    assert _format_decimal("100.000") == "100"
    assert _format_decimal("0.0012300") == "0.00123"
    assert _format_decimal("not-a-number") == "not-a-number"
    assert _safe_enum(OrderType, "NOT_A_VENUE_TYPE") is OrderType.UNKNOWN
    assert _strict_bool(True, field_name="x") is True
    assert _strict_bool(0, field_name="x") is False
    assert _strict_bool(" yes ", field_name="x") is True
    assert _strict_bool("no", field_name="x") is False
    with pytest.raises(ValueError, match="not a valid boolean"):
        _strict_bool("maybe", field_name="reduceOnly")

    error = SimpleNamespace(
        raw={"code": -1003, "msg": "venue detail"},
        category=ErrorCategory.NETWORK,
        message="fallback",
        retryable=True,
        http_status=429,
    )
    assert _sanitized_adapter_error(error, fallback="unused") == {
        "code": -1003,
        "msg": "venue detail",
        "category": "NETWORK",
        "retryable": True,
        "http_status": 429,
    }
    assert _sanitized_adapter_error(SimpleNamespace(), fallback="safe") == {
        "code": -1,
        "msg": "safe",
        "category": "UNKNOWN",
        "retryable": False,
        "http_status": 0,
    }


def test_reference_data_missing_and_changed_rules_are_explicit() -> None:
    old = BinanceReferenceData(venue_id=VenueId("BINANCE"))
    new = BinanceReferenceData(venue_id=VenueId("BINANCE"))
    instrument = InstrumentId("BTCUSDT")
    new.instruments[instrument] = {"filters": [{"filterType": "OTHER"}]}
    new.trading_rules[instrument] = {"minQty": "0.01", "newRule": "x"}
    old.trading_rules[instrument] = {"minQty": "0.001"}

    assert new.get_tick_size(instrument) is None
    assert new.get_step_size(instrument) is None
    assert new.get_min_notional(instrument) is None
    changes = new.detect_rule_changes(old)
    assert [(item.field_name, item.old_value, item.new_value) for item in changes] == [("minQty", "0.001", "0.01")]

    monitor = BinanceHealthMonitor(VenueId("BINANCE"))
    monitor.record_transport_failure()
    monitor.record_transport_failure()
    assert monitor.venue_health is HealthStatus.UNKNOWN
    monitor.record_transport_failure()
    assert monitor.venue_health is HealthStatus.UNAVAILABLE
    monitor.record_error("/fapi/v1/order", "NETWORK")
    monitor.record_transport_success()
    assert monitor.venue_health is HealthStatus.HEALTHY
    monitor.set_instrument_health(InstrumentId("X"), HealthStatus.UNKNOWN)
    assert monitor.get_instrument_health(InstrumentId("X")) is HealthStatus.UNKNOWN
    assert monitor.instruments_requiring_action()[InstrumentId("X")] is InstrumentStatus.EXIT_ONLY


@pytest.mark.asyncio
async def test_adapter_transport_health_and_listen_key_fail_closed_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    missing = BinanceUsdmAdapter()
    assert (await missing.create_user_listen_key()).is_success() is False
    assert (await missing.keepalive_user_listen_key(" ")).is_success() is False
    assert missing.is_circuit_breaker_open() is False

    malformed = _Transport({})
    adapter = BinanceUsdmAdapter(rest_client=malformed)
    assert (await adapter.create_user_listen_key()).is_success() is False
    assert (await adapter.keepalive_user_listen_key("key")).is_success() is True
    adapter.reset_circuit_breaker()
    assert malformed.reset_count == 1
    assert adapter.is_circuit_breaker_open() is True
    malformed.open = False
    assert adapter.is_circuit_breaker_open() is False

    monkeypatch.setenv("BEIDOU_TERMINAL_WRITE_HOLD", "unknown-only")
    adapter.health_monitor.update_venue_health(HealthStatus.HEALTHY)
    ok = await adapter.request("GET", "/fapi/v1/time")
    assert ok.is_success() is True
    assert malformed.calls[-1][0] == "GET"
    held = await adapter.request("POST", "/unregistered/mutation", params={})
    assert held.is_success() is False
    assert malformed.calls[-1][0] == "GET"


@pytest.mark.asyncio
async def test_adapter_account_balance_position_and_snapshot_boundaries() -> None:
    adapter = BinanceUsdmAdapter(rest_client=_Transport({}))
    account = _account_ref()
    status, balances = await adapter.get_balances(account)
    assert status.value == "UNKNOWN" and balances == {}
    status, positions = await adapter.get_positions(account)
    assert status.value == "UNKNOWN" and positions == {}

    transport = _Transport({"canTrade": True, "canWithdraw": False, "canDeposit": "bad"})
    adapter = BinanceUsdmAdapter(rest_client=transport)
    adapter.health_monitor.update_venue_health(HealthStatus.HEALTHY)
    info = await adapter.get_account_info(account)
    assert info.can_trade is True and info.can_withdraw is False and info.can_deposit is False

    transport.response = [{"asset": "USDT", "balance": "1.25"}, {"asset": "BTC", "balance": "0"}]
    status, balances = await adapter.get_balances(account)
    assert status.value == "SUCCESS" and balances["USDT"].amount == "1.25"
    transport.response = [{"asset": "USDT", "balance": "NaN"}]
    status, _ = await adapter.get_balances(account)
    assert status.value == "UNKNOWN"

    transport.response = {
        "positions": [{"symbol": "BTCUSDT", "positionAmt": "-0.2"}, {"symbol": "ETHUSDT", "positionAmt": "0"}]
    }
    status, positions = await adapter.get_positions(account)
    assert status.value == "SUCCESS" and positions["BTCUSDT"].amount == "-0.2"
    transport.response = {"positions": [{"symbol": "BTCUSDT"}]}
    status, _ = await adapter.get_positions(account)
    assert status.value == "UNKNOWN"

    adapter.reference_data.instruments[InstrumentId("BTCUSDT")] = {
        "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.001"}]
    }
    assert adapter.sync_rule_snapshots() == 1
    assert adapter.rule_snapshot_version == 1
    assert adapter.rule_snapshot_count == 1
    assert adapter.get_rule_snapshot("UNKNOWN").symbol == "UNKNOWN"


@pytest.mark.asyncio
async def test_adapter_order_recovery_and_cancel_status_unknowns() -> None:
    adapter = BinanceUsdmAdapter()
    assert (await adapter.query_order_by_client_id("BTCUSDT", "cid")).is_success() is False
    instrument = _instrument()
    assert (await adapter.cancel_order("1", instrument)).status.value == "UNKNOWN"
    assert await adapter.get_order_status("1", instrument) == OrderStatus.UNKNOWN

    transport = _Transport({"orderId": "1", "symbol": "BTCUSDT", "status": "FILLED"})
    adapter = BinanceUsdmAdapter(rest_client=transport)
    adapter.health_monitor.update_venue_health(HealthStatus.HEALTHY)
    assert await adapter.get_order_status("2", instrument) == OrderStatus.UNKNOWN
    transport.response = {"orderId": "1", "symbol": "BTCUSDT", "status": "NEW"}
    assert await adapter.get_order_status("1", instrument) == OrderStatus.NEW

    transport.response = Result.failure("not found", category=ErrorCategory.EXCHANGE_UNAVAILABLE, raw={"code": -2013})
    result = await adapter.query_order_by_client_id("BTCUSDT", "cid")
    assert result.is_success() is False and result.error is not None


def test_adapter_typed_parsers_reject_incomplete_and_invalid_facts() -> None:
    account = _account_ref()
    assert BinanceUsdmAdapter.parse_algo_order_snapshot([], account).is_success() is False
    assert BinanceUsdmAdapter.parse_algo_order_snapshot({}, account).is_success() is False
    assert (
        BinanceUsdmAdapter.parse_algo_order_snapshot(
            {
                "algoId": "1",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "orderType": "STOP",
                "triggerPrice": "0",
                "algoStatus": "NEW",
            },
            account,
        ).is_success()
        is False
    )
    assert BinanceUsdmAdapter.parse_user_stream_event({}).is_success() is False
    assert BinanceUsdmAdapter.parse_user_stream_event({"e": "x", "E": "bad"}).is_success() is False
    assert BinanceUsdmAdapter.parse_user_order_update({"e": "x", "E": 1}).is_success() is False
    assert BinanceUsdmAdapter.parse_user_account_update({"e": "ACCOUNT_UPDATE", "E": 1, "a": {}}).is_success() is False

    valid_event = {"e": "ACCOUNT_UPDATE", "E": 1, "a": {"B": [], "P": [], "m": "ORDER"}}
    parsed = BinanceUsdmAdapter.parse_user_account_update(valid_event)
    assert parsed.is_success() is True
    malformed = {
        "e": "ACCOUNT_UPDATE",
        "E": 1,
        "a": {"B": [{"a": "USDT", "wb": "NaN", "cw": "0"}], "P": [], "m": "ORDER"},
    }
    assert BinanceUsdmAdapter.parse_user_account_update(malformed).is_success() is False

    order_event = {
        "e": "ORDER_TRADE_UPDATE",
        "E": 1,
        "T": 2,
        "u": 3,
        "o": {
            "i": 7,
            "c": "cid",
            "s": "BTCUSDT",
            "S": "BUY",
            "o": "LIMIT",
            "X": "FILLED",
            "x": "TRADE",
            "q": "1",
            "z": "1",
            "l": "1",
            "L": "100",
            "ap": "100",
            "t": 9,
            "n": "0.1",
            "N": "USDT",
            "rp": "0.5",
        },
    }
    parsed_order = BinanceUsdmAdapter.parse_user_order_update(order_event)
    assert parsed_order.is_success() is True
    assert parsed_order.data is not None and parsed_order.data.order_status is OrderStatus.FILLED
    assert BinanceUsdmAdapter.parse_user_order_update({**order_event, "o": {"i": 7}}).is_success() is False
    bad_numeric = {**order_event, "o": {**order_event["o"], "l": "-1"}}
    assert BinanceUsdmAdapter.parse_user_order_update(bad_numeric).is_success() is False

    lite_event = {
        "e": "TRADE_LITE",
        "E": 1,
        "i": 8,
        "c": "lite-cid",
        "s": "BTCUSDT",
        "S": "SELL",
        "q": "1",
        "L": "101",
        "l": "1",
        "t": 10,
        "n": "0.01",
        "N": "USDT",
    }
    parsed_lite = BinanceUsdmAdapter.parse_user_order_update(lite_event)
    assert parsed_lite.is_success() is True
    assert parsed_lite.data is not None and parsed_lite.data.order_status is OrderStatus.UNKNOWN
    assert BinanceUsdmAdapter.parse_user_order_update({**lite_event, "l": "0"}).is_success() is False
    assert BinanceUsdmAdapter.parse_user_order_update({**lite_event, "q": "bad"}).is_success() is False

    complete_account = {
        "e": "ACCOUNT_UPDATE",
        "E": 10,
        "a": {
            "m": "ORDER",
            "B": [{"a": "USDT", "wb": "10", "cw": "9", "ab": "8"}],
            "P": [
                {
                    "s": "BTCUSDT",
                    "pa": "0.1",
                    "ep": "100",
                    "bep": "100",
                    "up": "1",
                    "mt": "cross",
                    "ps": "BOTH",
                }
            ],
        },
    }
    parsed_account = BinanceUsdmAdapter.parse_user_account_update(complete_account)
    assert parsed_account.is_success() is True
    assert parsed_account.data is not None and len(parsed_account.data.positions) == 1
    no_available = {**complete_account, "a": {**complete_account["a"], "B": [{"a": "USDT", "wb": "10", "cw": "9"}]}}
    assert BinanceUsdmAdapter.parse_user_account_update(no_available).is_success() is True
    missing_reason = {**complete_account, "a": {**complete_account["a"], "m": ""}}
    assert BinanceUsdmAdapter.parse_user_account_update(missing_reason).is_success() is False
