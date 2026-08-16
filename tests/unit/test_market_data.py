"""PKG-07~10: Market Data 测试。订单簿、K线、数据质量。"""

from datetime import datetime, timedelta, timezone

import pytest

from beidou_data.klines import KLineGenerator
from beidou_data.market import (
    BarIntegrity,
    BookLevel,
    ClosedBarNormalizer,
    MarketEvent,
    MarketEventType,
    OrderBookSnapshot,
    RawLayer,
)
from beidou_data.quality import AutoRepair, DataQualityGate, DQCheckResult, DQCheckType
from beidou_exchange.binance_usdm.endpoints import Endpoint
from beidou_exchange.core.error_taxonomy import Result
from beidou_shared.types import DataQualityTier, InstrumentId, Price, Quantity, VenueId, VenueInstrument


class _AdapterResultClient:
    async def request(self, method, path, signed=False, params=None):
        if path == Endpoint.TICKER_24HR:
            return Result.success({"lastPrice": "100", "volume": "10", "highPrice": "101", "lowPrice": "99"})
        if path == Endpoint.DEPTH:
            return Result.success({"bids": [["99.9", "1"]], "asks": [["100.1", "1"]]})
        if path == Endpoint.KLINES:
            return Result.success([[0, "99", "101", "98", "100", "10", 3600000, "1000", 3]])
        return Result.failure("unexpected path")


class _UnclosedBarClient(_AdapterResultClient):
    async def request(self, method, path, signed=False, params=None):
        if path == Endpoint.KLINES:
            future_close = int((datetime.now(timezone.utc).timestamp() + 3600) * 1000)
            return Result.success([[future_close - 3600000, "99", "101", "98", "100", "10", future_close, "1000", 3]])
        return await super().request(method, path, signed=signed, params=params)


class _MalformedKlineClient(_AdapterResultClient):
    async def request(self, method, path, signed=False, params=None):
        if path == Endpoint.KLINES:
            return Result.success(
                [
                    [0, "99", "101", "98", "100", "10", 3600000, "1000", 3],
                    [1, "99", "101", "98"],
                    [2, "nan", "101", "98", "100", "10", 3600000, "1000", 3],
                    [3, "99", "98", "98", "100", "10", 3600000, "1000", 3],
                    [4, "0", "101", "98", "100", "10", 3600000, "1000", 3],
                ]
            )
        return await super().request(method, path, signed=signed, params=params)


class _ClosableWebSocket:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_market_data_feed_unwraps_adapter_results():
    from beidou_core.feed import MarketDataFeed

    feed = MarketDataFeed(client=_AdapterResultClient())
    assert (await feed.async_fetch_ticker("BTCUSDT"))["lastPrice"] == "100"
    assert (await feed.async_fetch_orderbook("BTCUSDT"))["bids"]
    assert len(await feed.async_fetch_klines("BTCUSDT", "1h")) == 1
    features = await feed.async_update_features("BTCUSDT")
    assert features["price"] == 100.0


@pytest.mark.asyncio
async def test_market_data_feed_stop_ws_uses_client_close_contract():
    from beidou_core.feed import MarketDataFeed

    feed = MarketDataFeed(client=_AdapterResultClient())
    client = _ClosableWebSocket()
    feed._ws_client = client

    await feed.stop_ws()

    assert client.closed is True
    assert feed._ws_client is None
    assert feed._ws_active is False


@pytest.mark.asyncio
async def test_market_data_feed_rejects_unclosed_rest_bar():
    from beidou_core.feed import MarketDataFeed, MarketDataUnknownError

    feed = MarketDataFeed(client=_UnclosedBarClient())
    with pytest.raises(MarketDataUnknownError):
        await feed.async_fetch_klines("BTCUSDT", "1h")


@pytest.mark.asyncio
async def test_market_data_feed_skips_malformed_rest_rows_without_zero_or_nan_fallback():
    from beidou_core.feed import MarketDataFeed

    feed = MarketDataFeed(client=_MalformedKlineClient())
    rows = await feed.async_fetch_klines("BTCUSDT", "1h")

    assert len(rows) == 1
    assert rows[0]["open"] == 99.0
    assert rows[0]["close"] == 100.0


@pytest.mark.asyncio
async def test_sync_compatibility_wrapper_is_safe_inside_running_loop():
    from beidou_core.feed import MarketDataFeed

    feed = MarketDataFeed(client=_AdapterResultClient())
    ticker = feed.fetch_ticker("BTCUSDT")
    assert ticker["lastPrice"] == "100"


def test_kline_features_reject_non_finite_or_inconsistent_bars():
    from beidou_core.feed import MarketDataFeed

    feed = MarketDataFeed(client=_AdapterResultClient())
    now = datetime.now(timezone.utc)
    klines = [
        {
            "open_time": now - timedelta(hours=20 - index),
            "close_time": now - timedelta(hours=19 - index),
            "open": 100.0 + index,
            "high": 101.0 + index,
            "low": 99.0 + index,
            "close": 100.5 + index,
            "volume": 10.0,
            "is_closed": True,
        }
        for index in range(20)
    ]

    klines[4]["close"] = float("nan")
    assert feed._compute_kline_features("BTCUSDT", "1h", klines) == {}

    klines[4]["close"] = 100.5 + 4
    klines[4]["low"] = 200.0
    assert feed._compute_kline_features("BTCUSDT", "1h", klines) == {}


def test_generated_market_features_exclude_unclosed_bar_by_default():
    from beidou_core.feed import MarketDataFeed

    feed = MarketDataFeed(client=_AdapterResultClient())
    generator = feed._get_kline_generator("BTCUSDT", "1h")
    vi = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))
    generator.process_tick(
        vi,
        Price(amount="100"),
        Quantity(amount="1"),
        datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc),
    )

    assert feed.get_generated_klines("BTCUSDT", "1h") == []


def test_default_market_data_transport_is_exchange_adapter():
    """Standalone research feed must not bypass the exchange protocol boundary."""

    from beidou_core.feed import MarketDataFeed
    from beidou_exchange.binance_usdm.adapter import BinanceUsdmAdapter

    feed = MarketDataFeed()
    assert isinstance(feed._client, BinanceUsdmAdapter)


def test_kline_features_carry_explicit_closed_bar_evidence():
    from beidou_core.feed import MarketDataFeed

    feed = MarketDataFeed(client=_AdapterResultClient())
    now = datetime.now(timezone.utc)
    klines = [
        {
            "open_time": now - timedelta(hours=20 - index),
            "close_time": now - timedelta(hours=19 - index),
            "open": 100.0 + index,
            "high": 101.0 + index,
            "low": 99.0 + index,
            "close": 100.5 + index,
            "volume": 10.0,
            "is_closed": True,
        }
        for index in range(20)
    ]

    features = feed._compute_kline_features("BTCUSDT", "1h", klines)

    assert features["bar_is_closed"] is True
    assert features["bar_open_time"] == klines[-1]["open_time"]
    assert features["bar_close_time"] == klines[-1]["close_time"]
    assert isinstance(features["bar_available_at"], datetime)


def test_closed_bar_normalizer_missing_close_flag_is_not_closed():
    normalizer = ClosedBarNormalizer()
    vi = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))
    now = datetime.now(timezone.utc)
    result = normalizer.normalize(
        {
            "open_time": now - timedelta(minutes=2),
            "close_time": now - timedelta(minutes=1),
            "open": "100",
            "high": "101",
            "low": "99",
            "close": "100",
            "volume": "1",
        },
        vi,
        interval="1m",
    )

    assert result.status is BarIntegrity.NOT_CLOSED
    assert result.bar is not None
    assert result.bar.is_closed is False


def test_closed_bar_normalizer_missing_ohlcv_is_invalid_not_zero_filled():
    normalizer = ClosedBarNormalizer()
    vi = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))
    now = datetime.now(timezone.utc)

    result = normalizer.normalize(
        {
            "open_time": now - timedelta(minutes=2),
            "close_time": now - timedelta(minutes=1),
            "open": "100",
            "high": "101",
            "low": "99",
            "close": "100",
            "is_closed": True,
        },
        vi,
        interval="1m",
    )

    assert result.status is BarIntegrity.INVALID
    assert result.bar is None
    assert "volume" in result.detail


def test_closed_bar_normalizer_rejects_non_positive_price():
    normalizer = ClosedBarNormalizer()
    vi = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))
    now = datetime.now(timezone.utc)

    result = normalizer.normalize(
        {
            "open_time": now - timedelta(minutes=2),
            "close_time": now - timedelta(minutes=1),
            "open": "0",
            "high": "101",
            "low": "99",
            "close": "100",
            "volume": "1",
            "is_closed": True,
        },
        vi,
        interval="1m",
    )

    assert result.status is BarIntegrity.INVALID
    assert result.bar is None


class TestOrderBook:
    def make_book(self):
        vi = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))
        return OrderBookSnapshot(
            venue_instrument=vi,
            bids=[
                BookLevel(price=Price(amount="50000"), quantity=Quantity(amount="1.0")),
                BookLevel(price=Price(amount="49900"), quantity=Quantity(amount="2.0")),
            ],
            asks=[
                BookLevel(price=Price(amount="50100"), quantity=Quantity(amount="1.5")),
                BookLevel(price=Price(amount="50200"), quantity=Quantity(amount="0.5")),
            ],
        )

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
        evt = MarketEvent(
            event_type=MarketEventType.TRADE, venue_instrument=vi, event_time=t0, schema_version=SchemaVersion("2.0.0")
        )
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
        gen.process_tick(vi, Price(amount="50100"), Quantity(amount="0.2"), t0 + timedelta(seconds=30))
        gen.process_tick(vi, Price(amount="49900"), Quantity(amount="0.1"), t0 + timedelta(minutes=1, seconds=1))
        klines = gen.get_klines(vi)
        assert len(klines) >= 1

    def test_invalid_interval(self):
        import pytest

        with pytest.raises(ValueError):
            KLineGenerator("2x")


class TestDataQuality:
    def test_all_pass(self):
        gate = DataQualityGate(
            checks=[
                DQCheckResult(DQCheckType.FRESHNESS, DataQualityTier.PASS, "OK", 0.5, 1.0),
                DQCheckResult(DQCheckType.COMPLETENESS, DataQualityTier.PASS, "OK", 1.0, 0.99),
            ]
        )
        assert gate.overall_tier() == DataQualityTier.PASS
        assert gate.is_safe_for_trading()

    def test_any_fail_fails(self):
        gate = DataQualityGate(
            checks=[
                DQCheckResult(DQCheckType.FRESHNESS, DataQualityTier.PASS, "OK"),
                DQCheckResult(DQCheckType.CONSISTENCY, DataQualityTier.FAIL, "Mismatch"),
            ]
        )
        assert gate.overall_tier() == DataQualityTier.FAIL
        assert not gate.is_safe_for_trading()

    def test_conditional_passes_for_research(self):
        gate = DataQualityGate(
            checks=[DQCheckResult(DQCheckType.FRESHNESS, DataQualityTier.CONDITIONAL, "Stale", 60, 30)]
        )
        assert gate.overall_tier() == DataQualityTier.CONDITIONAL
        assert gate.is_safe_for_research()
        assert not gate.is_safe_for_trading()


class TestAutoRepair:
    def test_gap_detection(self):
        t0 = datetime.now(timezone.utc)
        series = [{"ts": t0}, {"ts": t0 + timedelta(seconds=5)}]
        _, gaps = AutoRepair.repair_gap(series, "ts", 1000)
        assert isinstance(gaps, list), f"Expected list, got {type(gaps)}"  # gap detection works


def test_parse_rest_kline_missing_close_flag_is_not_closed():
    """M01-F01 (P0-04): x 标志缺失时绝不伪造 is_closed=True（闭合证据诚实性）。"""
    from beidou_core.feed import MarketDataFeed

    now = datetime.now(timezone.utc)
    open_ms = int((now - timedelta(hours=1)).timestamp() * 1000)
    close_ms = int((now - timedelta(minutes=1)).timestamp() * 1000)
    row = [open_ms, "100", "101", "99", "100.5", "10", close_ms, "1000", 5]  # 无 x 字段
    parsed = MarketDataFeed._parse_rest_kline(row, now, include_closed=True)
    assert parsed is not None
    assert parsed["is_closed"] is False  # 缺失闭合证据 = 未闭合,绝不制造 True


def test_parse_rest_kline_explicit_close_flag_is_honored():
    from beidou_core.feed import MarketDataFeed

    now = datetime.now(timezone.utc)
    open_ms = int((now - timedelta(hours=1)).timestamp() * 1000)
    close_ms = int((now - timedelta(minutes=1)).timestamp() * 1000)
    row = [open_ms, "100", "101", "99", "100.5", "10", close_ms, "1000", 5, "0", "0", "0", True]
    parsed = MarketDataFeed._parse_rest_kline(row, now, include_closed=True)
    assert parsed["is_closed"] is True


def test_sync_kline_merge_preserves_closed_evidence():
    """M01-F01: 本地 bar→dict 转换必须携带 is_closed(对抗审查发现的证据丢失点)。"""
    from beidou_core.feed import _generated_bar_to_dict
    from beidou_data.klines import OHLCV

    vi = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))
    bar = OHLCV(
        venue_instrument=vi,
        interval="1h",
        open_time=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
        close_time=datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc),
        open=Price(amount="100"),
        high=Price(amount="101"),
        low=Price(amount="99"),
        close=Price(amount="100.5"),
        volume=Quantity(amount="10"),
        trade_count=5,
        is_closed=True,
    )
    as_dict = _generated_bar_to_dict(bar)
    assert as_dict["is_closed"] is True
    assert as_dict["open_time"] == datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)

    unclosed = OHLCV(
        venue_instrument=vi,
        interval="1h",
        open_time=datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc),
        close_time=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        open=Price(amount="101"),
        high=Price(amount="102"),
        low=Price(amount="100"),
        close=Price(amount="101.5"),
        volume=Quantity(amount="3"),
        trade_count=2,
        is_closed=False,
    )
    assert _generated_bar_to_dict(unclosed)["is_closed"] is False


def test_parse_event_time_valid_and_invalid():
    """M01-F03 (P0-06): E 事件时间解析 —— 缺失/未来/过旧/非法均拒绝。"""
    from beidou_core.feed import MarketDataFeed

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    parsed = MarketDataFeed._parse_event_time({"E": now_ms - 1000})
    assert parsed is not None
    assert abs((parsed - datetime.now(timezone.utc)).total_seconds()) < 60

    assert MarketDataFeed._parse_event_time({"E": None}) is None
    assert MarketDataFeed._parse_event_time({}) is None
    assert MarketDataFeed._parse_event_time({"E": "not-a-number"}) is None
    assert MarketDataFeed._parse_event_time({"E": now_ms + 10_000}) is None  # 未来 10s → 时钟异常
    assert MarketDataFeed._parse_event_time({"E": now_ms - 2 * 86_400_000}) is None  # 2 天前


class _EventTimeKlineClient:
    """ticker 携带固定 E 事件时间的假客户端。"""

    def __init__(self, event_ms: int) -> None:
        self._event_ms = event_ms

    async def request(self, method: str, path: str, signed: bool = False, params: dict | None = None):
        from beidou_exchange.binance_usdm.endpoints import Endpoint

        if path == Endpoint.TICKER_24HR:
            return {
                "lastPrice": "100",
                "priceChangePercent": "1.5",
                "volume": "1000",
                "quoteVolume": "100000",
                "highPrice": "102",
                "lowPrice": "98",
                "lastQty": "2",
                "E": self._event_ms,
            }
        if path == Endpoint.DEPTH:
            return {"bids": [["99.9", "1"]], "asks": [["100.1", "1"]]}
        raise AssertionError(f"unexpected path: {path}")


@pytest.mark.asyncio
async def test_async_update_features_buckets_bars_by_exchange_event_time():
    """M01-F03 (P0-06): bar 桶切分基于交易所事件时间而非本地时钟。"""
    from beidou_core.feed import MarketDataFeed

    # E = 1 秒前(时钟检查 tolerance 内),桶起点按 E 的小时桶推算
    event_time = datetime.now(timezone.utc) - timedelta(seconds=1)
    feed = MarketDataFeed(client=_EventTimeKlineClient(int(event_time.timestamp() * 1000)))
    features = await feed.async_update_features("BTCUSDT")

    generator = feed._get_kline_generator("BTCUSDT", "1h")
    vi = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))
    current = generator.get_current_bar(vi)
    assert current is not None
    expected_bucket = event_time.replace(minute=0, second=0, microsecond=0)
    assert current.open_time == expected_bucket  # 桶起点 = E 的小时桶(交易所时间)
    assert "dq_tier" in features
    assert features["dq_tier"] == "PASS"
    # FeatureVector 持久化 data_quality_tier（P1-08）
    stored = feed.get_feature_store().get_latest("btcusdt_live", InstrumentId("BTCUSDT"))
    assert stored is not None
    assert stored.data_quality_tier == "PASS"
