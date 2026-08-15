"""PKG-06: Binance Adapter 测试。参考数据、健康监控、交易规则。"""

import pytest

from beidou_exchange.binance_usdm import (
    BinanceHealthMonitor,
    BinanceReferenceData,
    BinanceUsdmAdapter,
)
from beidou_exchange.binance_usdm.adapter import InstrumentStatus
from beidou_exchange.binance_usdm.endpoints import Endpoint
from beidou_exchange.core.error_taxonomy import ErrorCategory, Result
from beidou_exchange.core.protocol import Capability, OrderRequest
from beidou_exchange.core.user_stream import UserStreamSequencer, UserStreamStatus
from beidou_shared.types import (
    AccountId,
    AccountRef,
    HealthStatus,
    InstrumentId,
    OrderSide,
    OrderType,
    Quantity,
    VenueId,
    VenueInstrument,
)


class FakeRestClient:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[tuple[str, str, bool, dict | None]] = []
        self.reset_count = 0

    async def request(self, method: str, path: str, signed: bool = False, params: dict | None = None) -> Result:
        self.calls.append((method, path, signed, params))
        if isinstance(self.response, Result):
            return self.response
        return Result.success(self.response)

    def reset_circuit_breaker(self) -> None:
        self.reset_count += 1

    async def create_listen_key(self) -> Result:
        return Result.success({"listenKey": "listen-key-1"})

    async def keepalive_listen_key(self, listen_key: str) -> Result:
        return Result.success({"listenKey": listen_key})


class PostGateTestAdapter(BinanceUsdmAdapter):
    """Test-only seam for ACK/error parsing after the production hard hold."""

    async def request(
        self,
        method: str,
        path: str,
        signed: bool = False,
        params: dict | None = None,
        write_account_id: str | None = None,
    ) -> Result:
        del write_account_id
        return await self._rest_client.request(method, path, signed=signed, params=params)


def write_enabled_adapter(transport: FakeRestClient) -> BinanceUsdmAdapter:
    return PostGateTestAdapter(
        account_id=AccountId("dedicated-test-account"),
        rest_client=transport,
    )


class TestBinanceReferenceData:
    def test_no_guessing_tick_from_price_decimals(self):
        ref = BinanceReferenceData(venue_id=VenueId("BINANCE"))
        ref.instruments[InstrumentId("BTCUSDT")] = {
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                {"filterType": "MIN_NOTIONAL", "notional": "5.0"},
            ]
        }
        assert ref.get_tick_size(InstrumentId("BTCUSDT")) == "0.10"
        assert ref.get_step_size(InstrumentId("BTCUSDT")) == "0.001"
        assert ref.get_min_notional(InstrumentId("BTCUSDT")) == "5.0"

    def test_missing_instrument_returns_none(self):
        ref = BinanceReferenceData(venue_id=VenueId("BINANCE"))
        assert ref.get_tick_size(InstrumentId("UNKNOWN")) is None

    def test_detect_rule_change(self):
        old = BinanceReferenceData(venue_id=VenueId("BINANCE"))
        old.trading_rules[InstrumentId("BTCUSDT")] = {"minQty": "0.001"}
        new = BinanceReferenceData(venue_id=VenueId("BINANCE"))
        new.trading_rules[InstrumentId("BTCUSDT")] = {"minQty": "0.0001"}
        changes = new.detect_rule_changes(old)
        assert len(changes) == 1
        assert changes[0].instrument_id == InstrumentId("BTCUSDT")
        assert changes[0].field == "minQty"


class TestBinanceHealthMonitor:
    def test_venue_health_starts_unknown(self):
        monitor = BinanceHealthMonitor(VenueId("BINANCE"))
        assert monitor.venue_health == HealthStatus.UNKNOWN

    def test_unknown_not_safe_for_new_risk(self):
        monitor = BinanceHealthMonitor(VenueId("BINANCE"))
        assert not monitor.is_safe_for_new_risk()

    def test_healthy_is_safe(self):
        monitor = BinanceHealthMonitor(VenueId("BINANCE"))
        monitor.update_venue_health(HealthStatus.HEALTHY)
        assert monitor.is_safe_for_new_risk()

    def test_maintenance_not_safe(self):
        monitor = BinanceHealthMonitor(VenueId("BINANCE"))
        monitor.update_venue_health(HealthStatus.MAINTENANCE)
        assert not monitor.is_safe_for_new_risk()

    def test_instrument_exit_only_on_maintenance(self):
        monitor = BinanceHealthMonitor(VenueId("BINANCE"))
        monitor.set_instrument_health(InstrumentId("BTCUSDT"), HealthStatus.MAINTENANCE)
        actions = monitor.instruments_requiring_action()
        assert actions[InstrumentId("BTCUSDT")] == InstrumentStatus.EXIT_ONLY

    def test_instrument_quarantined_on_unavailable(self):
        monitor = BinanceHealthMonitor(VenueId("BINANCE"))
        monitor.set_instrument_health(InstrumentId("ETHUSDT"), HealthStatus.UNAVAILABLE)
        actions = monitor.instruments_requiring_action()
        assert actions[InstrumentId("ETHUSDT")] == InstrumentStatus.QUARANTINED


class TestBinanceAdapter:
    def test_capabilities_include_futures(self):
        adapter = BinanceUsdmAdapter()
        assert Capability.FUTURES_USD_M in adapter.capabilities

    def test_venue_id_is_binance(self):
        adapter = BinanceUsdmAdapter()
        assert adapter.venue_id == VenueId("BINANCE")

    def test_health_monitor_accessible(self):
        adapter = BinanceUsdmAdapter()
        assert adapter.health_monitor is not None
        assert adapter.health_monitor.venue_id == VenueId("BINANCE")

    @pytest.mark.asyncio
    async def test_user_listen_key_lifecycle_stays_inside_adapter_boundary(self):
        transport = FakeRestClient({})
        adapter = BinanceUsdmAdapter(rest_client=transport)

        created = await adapter.create_user_listen_key()
        renewed = await adapter.keepalive_user_listen_key("listen-key-1")
        missing = await adapter.keepalive_user_listen_key("")

        assert created.is_success() is True
        assert created.data == {"listenKey": "listen-key-1"}
        assert renewed.is_success() is True
        assert renewed.data == {"listenKey": "listen-key-1"}
        assert missing.is_success() is False

    @pytest.mark.asyncio
    async def test_account_info_uses_venue_permissions_and_rejects_withdrawal(self):
        transport = FakeRestClient({"canTrade": True, "canWithdraw": True, "canDeposit": True})
        adapter = BinanceUsdmAdapter(rest_client=transport)
        adapter.health_monitor.update_venue_health(HealthStatus.HEALTHY)

        info = await adapter.get_account_info(AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")))

        assert info.can_trade is True
        assert info.can_withdraw is True
        assert info.can_deposit is True
        assert transport.calls[-1][:3] == ("GET", Endpoint.ACCOUNT, True)

    @pytest.mark.asyncio
    async def test_account_info_unknown_permission_is_fail_closed(self):
        transport = FakeRestClient({"positions": []})
        adapter = BinanceUsdmAdapter(rest_client=transport)

        info = await adapter.get_account_info(AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")))

        assert info.can_trade is False
        assert info.can_withdraw is True

    def test_error_normalization(self):
        adapter = BinanceUsdmAdapter()
        error = adapter.normalize_error(-2015, "Invalid API-key")
        from beidou_shared.errors import ErrorCategory

        assert error.category == ErrorCategory.AUTH_FAILURE

    @pytest.mark.asyncio
    async def test_order_write_and_reduce_only_cross_adapter_boundary(self):
        transport = FakeRestClient(
            {
                "orderId": 17,
                "clientOrderId": "cid-1",
                "symbol": "BTCUSDT",
                "side": "SELL",
                "type": "MARKET",
                "origQty": "0.01",
                "status": "NEW",
                "executedQty": "0",
                "reduceOnly": True,
            }
        )
        adapter = write_enabled_adapter(transport)
        adapter.health_monitor.update_venue_health(HealthStatus.HEALTHY)
        request = OrderRequest(
            venue_instrument=VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT")),
            account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=Quantity(amount="0.01"),
            client_order_id="cid-1",
            reduce_only=True,
        )

        response = await adapter.create_order(request)

        assert response.order_id == "17"
        method, path, signed, params = transport.calls[-1]
        assert (method, path, signed) == ("POST", Endpoint.ORDER, True)
        assert params is not None and params["reduceOnly"] == "true"

    @pytest.mark.asyncio
    async def test_risk_increasing_order_submits_once_with_stable_identity_and_bound_ack(self):
        transport = FakeRestClient(
            {
                "orderId": 21,
                "clientOrderId": "cid-risk-1",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "type": "MARKET",
                "origQty": "0.01",
                "status": "NEW",
                "executedQty": "0",
                "reduceOnly": False,
            }
        )
        adapter = write_enabled_adapter(transport)
        adapter.health_monitor.update_venue_health(HealthStatus.HEALTHY)
        request = OrderRequest(
            venue_instrument=VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT")),
            account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(amount="0.01"),
            client_order_id="cid-risk-1",
        )

        response = await adapter.create_order(request)

        assert response.order_id == "21"
        assert response.client_order_id == "cid-risk-1"
        assert response.status.value == "NEW"
        assert len(transport.calls) == 1
        method, path, signed, params = transport.calls[0]
        assert (method, path, signed) == ("POST", Endpoint.ORDER, True)
        assert params is not None
        assert params["newClientOrderId"] == "cid-risk-1"
        assert params["symbol"] == "BTCUSDT"
        assert params["side"] == "BUY"
        assert params["type"] == "MARKET"
        assert params["quantity"] == "0.01"

    @pytest.mark.asyncio
    async def test_post_gate_exit_ack_parsing_is_independent_of_health_probe(self):
        transport = FakeRestClient(
            {
                "orderId": 18,
                "clientOrderId": "cid-exit-only-unknown-health",
                "symbol": "BTCUSDT",
                "side": "SELL",
                "type": "MARKET",
                "origQty": "0.01",
                "status": "NEW",
                "executedQty": "0",
                "reduceOnly": True,
            }
        )
        adapter = write_enabled_adapter(transport)
        request = OrderRequest(
            venue_instrument=VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT")),
            account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=Quantity(amount="0.01"),
            client_order_id="cid-exit-only-unknown-health",
            reduce_only=True,
        )

        response = await adapter.create_order(request)

        assert response.order_id == "18"
        assert transport.calls[-1][3] is not None and transport.calls[-1][3]["reduceOnly"] == "true"

    @pytest.mark.asyncio
    async def test_order_ack_mismatch_is_unknown_not_adopted(self):
        transport = FakeRestClient(
            {
                "orderId": 20,
                "clientOrderId": "another-client-id",
                "symbol": "BTCUSDT",
                "side": "SELL",
                "type": "MARKET",
                "origQty": "0.01",
                "status": "NEW",
                "executedQty": "0",
                "reduceOnly": True,
            }
        )
        adapter = write_enabled_adapter(transport)
        request = OrderRequest(
            venue_instrument=VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT")),
            account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=Quantity(amount="0.01"),
            client_order_id="expected-client-id",
            reduce_only=True,
        )

        adapter.health_monitor.update_venue_health(HealthStatus.HEALTHY)
        response = await adapter.create_order(request)

        assert response.status.value == "UNKNOWN"
        assert response.order_id == ""
        assert response.raw_response is not None and response.raw_response["reason"] == "ACK_CLIENT_ORDER_ID_MISMATCH"

    @pytest.mark.asyncio
    async def test_order_submission_failure_preserves_exchange_code_for_idempotent_recovery(self):
        transport = FakeRestClient(
            Result.failure(
                "Client order id is not unique.",
                http_status=400,
                category=ErrorCategory.ORDER_REJECTED,
                retryable=False,
                raw={"code": -4141, "msg": "Client order id is not unique."},
                source="binance_rest",
            )
        )
        adapter = write_enabled_adapter(transport)
        adapter.health_monitor.update_venue_health(HealthStatus.HEALTHY)
        request = OrderRequest(
            venue_instrument=VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT")),
            account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(amount="0.01"),
            client_order_id="cid-duplicate",
        )

        response = await adapter.create_order(request)

        assert response.status.value == "UNKNOWN"
        assert response.raw_response is not None
        assert response.raw_response["code"] == -4141
        assert response.raw_response["category"] == ErrorCategory.ORDER_REJECTED.value
        assert response.raw_response["retryable"] is False

    @pytest.mark.asyncio
    async def test_write_hold_precedes_unknown_venue_health(self):
        transport = FakeRestClient({"orderId": 19, "status": "NEW", "executedQty": "0"})
        adapter = BinanceUsdmAdapter(rest_client=transport)
        result = await adapter.request("POST", Endpoint.ORDER, signed=True, params={"symbol": "BTCUSDT"})

        assert result.is_success() is False
        assert transport.calls == []

    @pytest.mark.asyncio
    async def test_cancel_and_status_use_adapter_transport(self):
        transport = FakeRestClient(
            {
                "orderId": 17,
                "symbol": "BTCUSDT",
                "side": "BUY",
                "type": "MARKET",
                "status": "FILLED",
                "origQty": "0.01",
                "executedQty": "0.01",
            }
        )
        adapter = write_enabled_adapter(transport)
        adapter.health_monitor.update_venue_health(HealthStatus.HEALTHY)
        venue_instrument = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))

        cancel = await adapter.cancel_order("17", venue_instrument)
        status = await adapter.get_order_status("17", venue_instrument)

        # DELETE raced with a fill; the adapter must preserve the venue's
        # terminal status instead of claiming cancellation.
        assert cancel.status.value == "FILLED"
        assert status.value == "FILLED"
        assert transport.calls[0][1] == Endpoint.ORDER
        assert transport.calls[1][1] == Endpoint.ORDER

    @pytest.mark.asyncio
    async def test_cancel_ack_identity_or_terminal_status_mismatch_is_unknown(self):
        transport = FakeRestClient(
            {
                "orderId": 18,
                "symbol": "ETHUSDT",
                "side": "BUY",
                "type": "MARKET",
                "status": "CANCELED",
                "origQty": "0.01",
                "executedQty": "0",
            }
        )
        adapter = write_enabled_adapter(transport)
        adapter.health_monitor.update_venue_health(HealthStatus.HEALTHY)
        venue_instrument = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))

        response = await adapter.cancel_order("18", venue_instrument)

        assert response.status.value == "UNKNOWN"
        assert response.raw_response is not None
        assert response.raw_response["reason"] == "CANCEL_ACK_SYMBOL_MISMATCH"

        transport.response = {
            "orderId": 18,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "status": "NEW",
            "origQty": "0.01",
            "executedQty": "0",
        }
        response = await adapter.cancel_order("18", venue_instrument)
        assert response.status.value == "UNKNOWN"
        assert response.raw_response is not None
        assert response.raw_response["reason"] == "CANCEL_ACK_NOT_TERMINAL"

        transport.response = {
            "orderId": 18,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "status": "CANCELED",
            "origQty": "0.01",
            "executedQty": "0.005",
        }
        response = await adapter.cancel_order("18", venue_instrument)
        assert response.status.value == "UNKNOWN"
        assert response.raw_response is not None
        assert response.raw_response["reason"] == "CANCEL_ACK_PARTIAL_FILL_RECONCILIATION_REQUIRED"

    @pytest.mark.asyncio
    async def test_client_order_recovery_query_requires_bound_identity(self):
        transport = FakeRestClient(
            {
                "orderId": 21,
                "clientOrderId": "cid-recover",
                "symbol": "BTCUSDT",
                "side": "SELL",
                "type": "MARKET",
                "origQty": "0.01",
                "status": "FILLED",
                "executedQty": "0.01",
            }
        )
        adapter = BinanceUsdmAdapter(rest_client=transport)

        result = await adapter.query_order_by_client_id("BTCUSDT", "cid-recover")

        assert result.is_success()
        assert result.data is not None and result.data["orderId"] == 21
        assert transport.calls[-1][3] == {"symbol": "BTCUSDT", "origClientOrderId": "cid-recover"}

        transport.response = {"orderId": 21, "clientOrderId": "other", "symbol": "BTCUSDT", "status": "FILLED"}
        mismatch = await adapter.query_order_by_client_id("BTCUSDT", "cid-recover")
        assert not mismatch.is_success()

        transport.response = {
            "orderId": 21,
            "clientOrderId": "cid-recover",
            "symbol": "BTCUSDT",
            "status": "FILLED",
        }
        incomplete = await adapter.query_order_by_client_id("BTCUSDT", "cid-recover")
        assert not incomplete.is_success()
        assert incomplete.error is not None
        assert "incomplete" in incomplete.error.message

    @pytest.mark.asyncio
    async def test_position_query_preserves_signed_position_amount(self):
        transport = FakeRestClient(
            {
                "positions": [
                    {"symbol": "BTCUSDT", "positionAmt": "-0.25"},
                    {"symbol": "ETHUSDT", "positionAmt": "0"},
                ]
            }
        )
        adapter = BinanceUsdmAdapter(rest_client=transport)
        adapter.health_monitor.update_venue_health(HealthStatus.HEALTHY)

        status, positions = await adapter.get_positions(
            AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test"))
        )

        assert status.value == "SUCCESS"
        assert positions["BTCUSDT"].amount == "-0.25"
        assert "ETHUSDT" not in positions

    @pytest.mark.asyncio
    async def test_balance_query_rejects_non_finite_rows(self):
        transport = FakeRestClient([{"asset": "USDT", "balance": "NaN"}])
        adapter = BinanceUsdmAdapter(rest_client=transport)
        adapter.health_monitor.update_venue_health(HealthStatus.HEALTHY)

        status, balances = await adapter.get_balances(
            AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test"))
        )

        assert status.value == "UNKNOWN"
        assert balances == {}

    @pytest.mark.asyncio
    async def test_balance_query_preserves_verified_signed_rows(self):
        transport = FakeRestClient(
            [
                {"asset": "USDT", "balance": "100.50"},
                {"asset": "BTC", "balance": "-0.25"},
                {"asset": "ETH", "balance": "0"},
            ]
        )
        adapter = BinanceUsdmAdapter(rest_client=transport)
        adapter.health_monitor.update_venue_health(HealthStatus.HEALTHY)

        status, balances = await adapter.get_balances(
            AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test"))
        )

        assert status.value == "SUCCESS"
        assert balances["USDT"].amount == "100.50"
        assert balances["BTC"].amount == "-0.25"
        assert "ETH" not in balances

    @pytest.mark.asyncio
    async def test_missing_transport_is_unknown_not_success(self):
        adapter = BinanceUsdmAdapter()
        adapter.health_monitor.update_venue_health(HealthStatus.HEALTHY)
        request = OrderRequest(
            venue_instrument=VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT")),
            account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(amount="0.01"),
        )

        response = await adapter.create_order(request)

        assert response.status.value == "UNKNOWN"

    def test_algo_snapshot_requires_complete_venue_fields(self):
        account = AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test"))
        parsed = BinanceUsdmAdapter.parse_algo_order_snapshot(
            {
                "algoId": 99,
                "symbol": "BTCUSDT",
                "side": "SELL",
                "orderType": "STOP_MARKET",
                "quantity": "0.01",
                "triggerPrice": "95000",
                "algoStatus": "NEW",
                "updateTime": 123,
            },
            account,
        )
        assert parsed.is_success()
        assert parsed.data is not None and parsed.data.algo_id == "99"
        assert parsed.data.status == "NEW"

        incomplete = BinanceUsdmAdapter.parse_algo_order_snapshot(
            {"algoId": 99, "symbol": "BTCUSDT"},
            account,
        )
        assert not incomplete.is_success()

    def test_algo_snapshot_parses_boolean_strings_strictly(self):
        account = AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test"))
        parsed = BinanceUsdmAdapter.parse_algo_order_snapshot(
            {
                "algoId": 99,
                "symbol": "BTCUSDT",
                "side": "SELL",
                "orderType": "STOP_MARKET",
                "quantity": "0.01",
                "triggerPrice": "95000",
                "algoStatus": "NEW",
                "reduceOnly": "false",
                "closePosition": "0",
            },
            account,
        )
        assert parsed.is_success() and parsed.data is not None
        assert parsed.data.reduce_only is False
        assert parsed.data.close_position is False

        invalid = BinanceUsdmAdapter.parse_algo_order_snapshot(
            {
                "algoId": 99,
                "symbol": "BTCUSDT",
                "side": "SELL",
                "orderType": "STOP_MARKET",
                "quantity": "0.01",
                "triggerPrice": "95000",
                "algoStatus": "NEW",
                "reduceOnly": "sometimes",
            },
            account,
        )
        assert invalid.is_success() is False

    @pytest.mark.asyncio
    async def test_open_algo_inventory_is_typed_and_uses_adapter_boundary(self):
        transport = FakeRestClient(
            [
                {
                    "algoId": 99,
                    "symbol": "BTCUSDT",
                    "side": "SELL",
                    "orderType": "STOP_MARKET",
                    "quantity": "0.01",
                    "triggerPrice": "95000",
                    "algoStatus": "NEW",
                }
            ]
        )
        adapter = BinanceUsdmAdapter(rest_client=transport)
        result = await adapter.get_open_algo_orders()
        assert result.is_success()
        assert result.data and result.data[0].algo_id == "99"
        assert transport.calls[-1][1] == Endpoint.OPEN_ALGO_ORDERS

    @pytest.mark.asyncio
    async def test_create_algo_order_binds_reduce_only_ack_to_request(self):
        transport = FakeRestClient(
            {
                "algoId": 100,
                "symbol": "BTCUSDT",
                "side": "SELL",
                "orderType": "STOP_MARKET",
                "quantity": "0.01",
                "triggerPrice": "95000",
                "algoStatus": "NEW",
                "reduceOnly": True,
                "clientAlgoId": "bdp-fixed-id",
            }
        )
        adapter = write_enabled_adapter(transport)
        result = await adapter.create_algo_order(
            {
                "symbol": "BTCUSDT",
                "side": "SELL",
                "type": "STOP_MARKET",
                "quantity": "0.01",
                "triggerPrice": "95000",
                "reduceOnly": "true",
                "clientAlgoId": "bdp-fixed-id",
            }
        )
        assert result.is_success()
        assert result.data is not None and result.data.algo_id == "100"

    @pytest.mark.asyncio
    async def test_create_algo_order_rejects_mismatched_client_identity(self):
        transport = FakeRestClient(
            {
                "algoId": 102,
                "clientAlgoId": "other-id",
                "symbol": "BTCUSDT",
                "side": "SELL",
                "orderType": "STOP_MARKET",
                "quantity": "0.01",
                "triggerPrice": "95000",
                "algoStatus": "NEW",
                "reduceOnly": True,
            }
        )
        adapter = write_enabled_adapter(transport)

        result = await adapter.create_algo_order(
            {
                "symbol": "BTCUSDT",
                "side": "SELL",
                "type": "STOP_MARKET",
                "quantity": "0.01",
                "triggerPrice": "95000",
                "reduceOnly": "true",
                "clientAlgoId": "bdp-expected-id",
            }
        )

        assert result.is_success() is False
        assert result.error is not None
        assert "clientAlgoId" in result.error.message

    @pytest.mark.asyncio
    async def test_create_algo_order_rejects_mismatched_trigger_price(self):
        transport = FakeRestClient(
            {
                "algoId": 103,
                "clientAlgoId": "bdp-expected-id",
                "symbol": "BTCUSDT",
                "side": "SELL",
                "orderType": "STOP_MARKET",
                "quantity": "0.01",
                "triggerPrice": "94000",
                "algoStatus": "NEW",
                "reduceOnly": True,
            }
        )
        adapter = write_enabled_adapter(transport)

        result = await adapter.create_algo_order(
            {
                "symbol": "BTCUSDT",
                "side": "SELL",
                "type": "STOP_MARKET",
                "quantity": "0.01",
                "triggerPrice": "95000",
                "reduceOnly": "true",
                "clientAlgoId": "bdp-expected-id",
            }
        )

        assert result.is_success() is False
        assert result.error is not None
        assert "triggerPrice" in result.error.message

    @pytest.mark.asyncio
    async def test_create_algo_order_preserves_transport_failure_semantics(self):
        transport = FakeRestClient(
            Result.failure(
                "rate limited",
                http_status=429,
                category=ErrorCategory.RATE_LIMIT,
                retryable=True,
                raw={"code": -1003, "msg": "rate limited"},
                source="binance_rest",
                correlation_id="corr-algo-1",
            )
        )
        adapter = write_enabled_adapter(transport)

        result = await adapter.create_algo_order(
            {"symbol": "BTCUSDT", "quantity": "0.01", "reduceOnly": "true"}
        )

        assert result.is_success() is False
        assert result.error is not None
        assert result.error.category is ErrorCategory.RATE_LIMIT
        assert result.error.retryable is True
        assert result.error.http_status == 429
        assert result.error.raw == {"code": -1003, "msg": "rate limited"}
        assert result.source == "binance_rest"
        assert result.correlation_id == "corr-algo-1"

    @pytest.mark.asyncio
    async def test_create_algo_order_rejects_mismatched_or_unproven_ack(self):
        transport = FakeRestClient(
            {
                "algoId": 101,
                "symbol": "BTCUSDT",
                "side": "BUY",
                "orderType": "STOP_MARKET",
                "quantity": "0.01",
                "triggerPrice": "95000",
                "algoStatus": "NEW",
                "reduceOnly": False,
            }
        )
        adapter = write_enabled_adapter(transport)
        result = await adapter.create_algo_order(
            {
                "symbol": "BTCUSDT",
                "side": "SELL",
                "type": "STOP_MARKET",
                "quantity": "0.01",
                "triggerPrice": "95000",
                "reduceOnly": "true",
            }
        )
        assert result.is_success() is False
        assert result.error is not None
        assert "side" in result.error.message

    @pytest.mark.asyncio
    async def test_user_event_without_sequence_is_not_trusted(self):
        parsed = BinanceUsdmAdapter.parse_user_stream_event({"e": "ACCOUNT_UPDATE", "E": 1000})
        assert parsed.is_success() and parsed.data is not None
        observation = UserStreamSequencer().observe(parsed.data)
        assert observation.accepted is False
        assert observation.status is UserStreamStatus.SEQUENCE_UNAVAILABLE

    @pytest.mark.asyncio
    async def test_user_sequence_gap_is_blocked(self):
        sequencer = UserStreamSequencer()
        first = BinanceUsdmAdapter.parse_user_stream_event({"e": "ORDER_TRADE_UPDATE", "E": 1000, "u": 10})
        gap = BinanceUsdmAdapter.parse_user_stream_event({"e": "ORDER_TRADE_UPDATE", "E": 1001, "u": 12})
        assert first.data is not None and gap.data is not None
        assert sequencer.observe(first.data).accepted is True
        observation = sequencer.observe(gap.data)
        assert observation.accepted is False
        assert observation.status is UserStreamStatus.GAP
        after_gap = BinanceUsdmAdapter.parse_user_stream_event({"e": "ORDER_TRADE_UPDATE", "E": 1002, "u": 11})
        assert after_gap.data is not None
        assert sequencer.observe(after_gap.data).accepted is False
        sequencer.mark_replayed(10)
        replayed = BinanceUsdmAdapter.parse_user_stream_event({"e": "ORDER_TRADE_UPDATE", "E": 1003, "u": 11})
        assert replayed.data is not None
        assert sequencer.observe(replayed.data).accepted is True
