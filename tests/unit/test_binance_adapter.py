"""PKG-06: Binance Adapter 测试。参考数据、健康监控、交易规则。"""

from beidou_exchange.binance_usdm import (
    BinanceHealthMonitor,
    BinanceReferenceData,
    BinanceUsdmAdapter,
)
from beidou_exchange.binance_usdm.adapter import InstrumentStatus
from beidou_exchange.core.protocol import Capability
from beidou_shared.types import HealthStatus, InstrumentId, VenueId


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
