"""PKG-06: Binance Adapter 测试。参考数据、健康监控、交易规则。"""

import pytest

from beidou_exchange.binance_usdm import (
    BinanceHealthMonitor,
    BinanceReferenceData,
    BinanceUsdmAdapter,
)
from beidou_exchange.binance_usdm.adapter import InstrumentStatus
from beidou_exchange.binance_usdm.endpoints import Endpoint
from beidou_exchange.core.error_taxonomy import Result
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
        return Result.success(self.response)

    def reset_circuit_breaker(self) -> None:
        self.reset_count += 1


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

    def test_error_normalization(self):
        adapter = BinanceUsdmAdapter()
        error = adapter.normalize_error(-2015, "Invalid API-key")
        from beidou_shared.errors import ErrorCategory

        assert error.category == ErrorCategory.AUTH_FAILURE

    @pytest.mark.asyncio
    async def test_order_write_and_reduce_only_cross_adapter_boundary(self):
        transport = FakeRestClient({"orderId": 17, "status": "NEW", "executedQty": "0"})
        adapter = BinanceUsdmAdapter(rest_client=transport)
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
    async def test_cancel_and_status_use_adapter_transport(self):
        transport = FakeRestClient({"orderId": 17, "status": "FILLED", "origQty": "0.01", "executedQty": "0.01"})
        adapter = BinanceUsdmAdapter(rest_client=transport)
        adapter.health_monitor.update_venue_health(HealthStatus.HEALTHY)
        venue_instrument = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))

        cancel = await adapter.cancel_order("17", venue_instrument)
        status = await adapter.get_order_status("17", venue_instrument)

        assert cancel.status.value == "CANCELED"
        assert status.value == "FILLED"
        assert transport.calls[0][1] == Endpoint.ORDER
        assert transport.calls[1][1] == Endpoint.ORDER

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
