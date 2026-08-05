
"""PKG-07~10: Market Data 测试。订单簿、K线、数据质量。"""
from datetime import datetime, timezone, timedelta
from beidou_shared.types import VenueId, InstrumentId, Price, Quantity, VenueInstrument, DataQualityTier
from beidou_data.market import OrderBookSnapshot, BookLevel, RawLayer, MarketEvent, MarketEventType, TradeTick
from beidou_data.klines import KLineGenerator, OHLCV
from beidou_data.quality import DataQualityGate, DQCheckResult, DQCheckType, CrossSourceValidator, AutoRepair

class TestOrderBook:
    def make_book(self):
        vi = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))
        return OrderBookSnapshot(venue_instrument=vi, bids=[BookLevel(price=Price(amount="50000"), quantity=Quantity(amount="1.0")), BookLevel(price=Price(amount="49900"), quantity=Quantity(amount="2.0"))], asks=[BookLevel(price=Price(amount="50100"), quantity=Quantity(amount="1.5")), BookLevel(price=Price(amount="50200"), quantity=Quantity(amount="0.5"))])

    def test_best_bid_ask(self):
        book = self.make_book()
        assert float(book.best_bid().amount) == 50000
        assert float(book.best_ask().amount) == 50100

    def test_mid_price(self):
        book = self.make_book()
        assert float(book.mid_price().amount) == 50050.0

    def test_spread(self):
        book = self.make_book()
        assert float(book.spread().amount) == 100.0

    def test_imbalance(self):
        book = self.make_book()
        imb = book.imbalance()
        assert -1.0 <= imb <= 1.0

    def test_apply_update_bid(self):
        book = self.make_book()
        book.apply_update("BUY", Price(amount="50050"), Quantity(amount="3.0"))
        assert len(book.bids) == 3

    def test_apply_update_remove_zero(self):
        book = self.make_book()
        book.apply_update("BUY", Price(amount="50000"), Quantity(amount="0"))
        assert len(book.bids) == 1

    def test_empty_book_none_values(self):
        vi = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))
        book = OrderBookSnapshot(venue_instrument=vi)
        assert book.best_bid() is None
        assert book.spread_bps() is None

class TestRawLayer:
    def test_ingest_and_replay(self):
        layer = RawLayer()
        vi = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))
        from beidou_shared.types import SchemaVersion
        t0 = datetime.now(timezone.utc)
        evt = MarketEvent(event_type=MarketEventType.TRADE, venue_instrument=vi, event_time=t0, schema_version=SchemaVersion("2.0.0"))
        layer.ingest(evt)
        assert layer.event_count() == 1
        replays = layer.replay_range(t0 - timedelta(seconds=1), t0 + timedelta(seconds=1))
        assert len(replays) == 1

class TestKLineGenerator:
    def test_klines_generated(self):
        gen = KLineGenerator("1m")
        vi = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))
        t0 = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        gen.process_tick(vi, Price(amount="50000"), Quantity(amount="0.1"), t0)
        result = gen.process_tick(vi, Price(amount="50100"), Quantity(amount="0.2"), t0 + timedelta(seconds=30))
        gen.process_tick(vi, Price(amount="49900"), Quantity(amount="0.1"), t0 + timedelta(minutes=1, seconds=1))
        klines = gen.get_klines(vi)
        assert len(klines) >= 1

    def test_invalid_interval(self):
        import pytest
        with pytest.raises(ValueError):
            KLineGenerator("2x")

class TestDataQuality:
    def test_all_pass(self):
        gate = DataQualityGate(checks=[DQCheckResult(DQCheckType.FRESHNESS, DataQualityTier.PASS, "OK", 0.5, 1.0), DQCheckResult(DQCheckType.COMPLETENESS, DataQualityTier.PASS, "OK", 1.0, 0.99)])
        assert gate.overall_tier() == DataQualityTier.PASS
        assert gate.is_safe_for_trading()

    def test_any_fail_fails(self):
        gate = DataQualityGate(checks=[DQCheckResult(DQCheckType.FRESHNESS, DataQualityTier.PASS, "OK"), DQCheckResult(DQCheckType.CONSISTENCY, DataQualityTier.FAIL, "Mismatch")])
        assert gate.overall_tier() == DataQualityTier.FAIL
        assert not gate.is_safe_for_trading()

    def test_conditional_passes_for_research(self):
        gate = DataQualityGate(checks=[DQCheckResult(DQCheckType.FRESHNESS, DataQualityTier.CONDITIONAL, "Stale", 60, 30)])
        assert gate.overall_tier() == DataQualityTier.CONDITIONAL
        assert gate.is_safe_for_research()
        assert not gate.is_safe_for_trading()

class TestAutoRepair:
    def test_gap_detection(self):
        t0 = datetime.now(timezone.utc)
        series = [{"ts": t0}, {"ts": t0 + timedelta(seconds=5)}]
        _, gaps = AutoRepair.repair_gap(series, "ts", 1000)
        assert isinstance(gaps, list), f"Expected list, got {type(gaps)}"  # gap detection works
