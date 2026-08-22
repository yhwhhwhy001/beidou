"""Behavior-backed coverage for the remaining market-data boundary branches."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import beidou_core.feed as feed_module
from beidou_core.feed import MarketDataFeed, MarketDataUnknownError
from beidou_exchange.binance_usdm.endpoints import Endpoint
from beidou_exchange.core.error_taxonomy import Result


def _rows(count: int = 20) -> list[list[object]]:
    now = datetime.now(timezone.utc)
    rows: list[list[object]] = []
    for index in range(count):
        opened = now - timedelta(hours=count - index + 2)
        open_ms = int(opened.timestamp() * 1000)
        rows.append([open_ms, "100", "102", "99", str(100 + index / 10), "2", open_ms + 3_599_000, "200", 2])
    return rows


class _Client:
    def __init__(self, *, ticker: object | None = None, orderbook: object | None = None, klines: object = None) -> None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        self.ticker = (
            ticker
            if ticker is not None
            else {
                "lastPrice": "100",
                "lastQty": "1",
                "volume": "10",
                "highPrice": "101",
                "lowPrice": "99",
                "priceChangePercent": "1",
                "E": now_ms,
            }
        )
        self.orderbook = orderbook if orderbook is not None else {"bids": [["99", "1"]], "asks": [["101", "1"]]}
        self.klines = _rows() if klines is None else klines
        self._clock_offset_ms = 10

    async def request(self, method: str, path: str, *, signed: bool = False, params: dict | None = None) -> object:
        if path == Endpoint.TICKER_24HR:
            return Result.success(self.ticker)
        if path == Endpoint.DEPTH:
            return Result.success(self.orderbook)
        if path == Endpoint.KLINES:
            return Result.success(self.klines)
        return Result.failure("unexpected endpoint")


def _generated_bar(open_time: datetime, *, closed: bool = True) -> SimpleNamespace:
    def price(value: float) -> SimpleNamespace:
        return SimpleNamespace(amount=str(value))

    return SimpleNamespace(
        open_time=open_time,
        close_time=open_time + timedelta(hours=1),
        open=price(100),
        high=price(102),
        low=price(99),
        close=price(101),
        volume=SimpleNamespace(amount="2"),
        quote_volume=SimpleNamespace(amount="200"),
        trade_count=2,
        is_closed=closed,
    )


class _BadGenerator:
    def update(self, **kwargs: object) -> None:
        raise RuntimeError("update failed")

    def process_tick(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError("process failed")

    def get_klines(self, venue_instrument: object) -> list[object]:
        return []

    def get_current_bar(self, venue_instrument: object) -> None:
        return None


def test_feed_time_gate_and_parser_reject_all_untrusted_shapes() -> None:
    assert MarketDataFeed._parse_event_time([]) is None
    assert MarketDataFeed._parse_event_time({"E": 1e300}) is None
    assert MarketDataFeed._parse_event_time({"E": "not-a-number"}) is None

    feed = MarketDataFeed(client=_Client())
    now = datetime.now(timezone.utc)
    vi = feed_module.VenueInstrument(
        venue_id=feed_module.VenueId("BINANCE"), instrument_id=feed_module.InstrumentId("BTCUSDT")
    )
    gate = feed._build_live_dq_gate(vi, {}, 10, now - timedelta(seconds=200))
    assert any(check.tier is feed_module.DataQualityTier.CONDITIONAL for check in gate.checks)

    class _BadBool:
        def __bool__(self) -> bool:
            raise ValueError("untrusted flag")

    valid_open = int((now - timedelta(minutes=2)).timestamp() * 1000)
    valid_close = int((now - timedelta(minutes=1)).timestamp() * 1000)
    base = [valid_open, "100", "102", "99", "101", "2", valid_close, "200", 2]
    assert MarketDataFeed._parse_rest_kline([True, *base[1:]], now, include_closed=True) is None
    impossible_time = 10**15
    impossible_row = [impossible_time, *base[1:6], impossible_time + 1000, *base[7:]]
    assert MarketDataFeed._parse_rest_kline(impossible_row, now, include_closed=True) is None
    negative_volume = list(base)
    negative_volume[5] = -1
    assert MarketDataFeed._parse_rest_kline(negative_volume, now, include_closed=True) is None
    negative_quote = list(base)
    negative_quote[7] = -1
    assert MarketDataFeed._parse_rest_kline(negative_quote, now, include_closed=True) is None
    bad_flag = [*base, "x", "x", _BadBool()]
    assert MarketDataFeed._parse_rest_kline(bad_flag, now, include_closed=True) is not None

    parsed = MarketDataFeed._parse_rest_kline(base, now, include_closed=True)
    assert parsed is not None
    for field, value in (("open", True), ("close", "bad"), ("close", float("nan")), ("open", 0)):
        invalid = dict(parsed)
        invalid[field] = value
        assert MarketDataFeed._is_valid_feature_bar(invalid) is False
    invalid = dict(parsed)
    invalid["high"] = 105
    invalid["low"] = 103
    assert MarketDataFeed._is_valid_feature_bar(invalid) is False
    invalid = dict(parsed)
    invalid["quote_volume"] = "bad"
    assert MarketDataFeed._is_valid_feature_bar(invalid) is False
    invalid["quote_volume"] = float("nan")
    assert MarketDataFeed._is_valid_feature_bar(invalid) is False


def test_feed_generated_bar_and_health_boundaries() -> None:
    feed = MarketDataFeed(client=_Client())
    current = _generated_bar(datetime.now(timezone.utc), closed=True)
    historical = _generated_bar(current.open_time - timedelta(hours=1), closed=True)

    class _Generator:
        def get_klines(self, venue_instrument: object) -> list[object]:
            return [historical]

        def get_current_bar(self, venue_instrument: object) -> object:
            return current

    feed._get_kline_generator = lambda symbol, interval="1h": _Generator()  # type: ignore[method-assign]
    assert feed.get_generated_klines("BTCUSDT", include_current=True) == [historical, current]

    feed._ws_client = SimpleNamespace(state=SimpleNamespace(value="CLOSED"))
    assert feed.is_healthy() is False


@pytest.mark.asyncio
async def test_feed_websocket_event_time_fallback_and_async_generator_failures() -> None:
    import beidou_exchange.binance_usdm as binance_usdm

    class _WebSocket:
        instance: _WebSocket | None = None

        def __init__(self, *, base_url: str) -> None:
            self.callbacks: dict[str, object] = {}
            self.closed = False
            _WebSocket.instance = self

        async def subscribe(self, stream: str, callback: object) -> None:
            self.callbacks[stream] = callback

        async def run(self) -> None:
            await asyncio.Event().wait()

        async def close(self) -> None:
            self.closed = True

    original = binance_usdm.BinanceUsdmWebSocketClient
    binance_usdm.BinanceUsdmWebSocketClient = _WebSocket  # type: ignore[assignment]
    try:
        feed = MarketDataFeed(client=_Client())
        assert await feed.start_ws(["BTCUSDT"]) is True
        ws = _WebSocket.instance
        assert ws is not None
        await ws.callbacks["btcusdt@ticker"]("ticker", {"s": "BTCUSDT", "c": "100", "Q": "1"})  # type: ignore[misc]
        assert feed._clock_fallback_ticks == 1
        await feed.stop_ws()
    finally:
        binance_usdm.BinanceUsdmWebSocketClient = original  # type: ignore[assignment]

    feed = MarketDataFeed(client=_Client())
    feed._kline_generators["BTCUSDT:5m"] = _BadGenerator()  # type: ignore[assignment]
    feed._kline_generators["BTCUSDT:1h"] = _BadGenerator()  # type: ignore[assignment]
    features = await feed.async_update_features("BTCUSDT")
    assert features["price"] == 100.0
    assert feed._error_count["kline_gen"] == 2

    class _UnknownClient(_Client):
        async def request(self, method: str, path: str, **kwargs: object) -> object:
            return Result.failure("unknown")

    with pytest.raises(MarketDataUnknownError, match="unknown"):
        await MarketDataFeed(client=_UnknownClient()).async_update_features("BTCUSDT")

    incomplete = MarketDataFeed(client=_Client(ticker=[]))
    incomplete._client.orderbook = {"bids": [["99", "1"]], "asks": [["101", "1"]]}
    with pytest.raises(MarketDataUnknownError, match="incomplete"):
        await incomplete.async_update_features("BTCUSDT")

    error_client = _Client()
    error_client.ticker = {"error": "bad"}
    with pytest.raises(MarketDataUnknownError, match="contains an error"):
        await MarketDataFeed(client=error_client).async_update_features("BTCUSDT")


@pytest.mark.asyncio
async def test_feed_async_kline_merge_fallback_dq_and_insufficient_boundaries() -> None:
    now = datetime.now(timezone.utc)
    feed = MarketDataFeed(client=_Client())
    latest = _generated_bar(now, closed=True)
    feed.get_generated_klines = lambda symbol, interval="1h": [latest]  # type: ignore[method-assign]
    feed._last_ticker["BTCUSDT"] = dict(feed._client.ticker)
    assert (await feed.async_get_kline_features("BTCUSDT", lookback=20))["n_candles"] == 20

    fallback = MarketDataFeed(client=_Client(klines=[]))
    generated = [_generated_bar(now - timedelta(hours=20 - index), closed=True) for index in range(20)]
    fallback.get_generated_klines = lambda symbol, interval="1h": generated  # type: ignore[method-assign]
    assert (await fallback.async_get_kline_features("BTCUSDT", lookback=20))["n_candles"] == 20

    insufficient = MarketDataFeed(client=_Client(klines=[]))
    with pytest.raises(MarketDataUnknownError, match="insufficient"):
        await insufficient.async_get_kline_features("BTCUSDT", lookback=20)

    conditional = MarketDataFeed(client=_Client())
    conditional._last_ticker["BTCUSDT"] = dict(conditional._client.ticker)
    conditional._last_ticker["BTCUSDT"]["E"] = int((now - timedelta(seconds=200)).timestamp() * 1000)
    assert (await conditional.async_get_kline_features("BTCUSDT", lookback=20))["dq_tier"] == "CONDITIONAL"


def test_feed_sync_boundary_errors_pagination_and_feature_merges(monkeypatch: pytest.MonkeyPatch) -> None:
    feed = MarketDataFeed(client=_Client())
    feed._api = lambda *args, **kwargs: {"bad": 1}  # type: ignore[method-assign]
    with pytest.raises(MarketDataUnknownError, match="ticker"):
        feed.fetch_ticker("BTCUSDT")
    with pytest.raises(MarketDataUnknownError, match="order book"):
        feed.fetch_orderbook("BTCUSDT")

    feed._api = lambda *args, **kwargs: []  # type: ignore[method-assign]
    with pytest.raises(MarketDataUnknownError, match="no closed valid rows"):
        feed.fetch_klines("BTCUSDT", "1h", start_time=1)

    feed._api = lambda *args, **kwargs: [[1, 2]]  # type: ignore[method-assign]
    with pytest.raises(MarketDataUnknownError, match="no closed valid rows"):
        feed.fetch_klines("BTCUSDT", "1h", start_time=1)

    good_client = _Client()
    feed = MarketDataFeed(client=good_client)
    feed.fetch_ticker = lambda symbol: {}  # type: ignore[method-assign]
    feed.fetch_orderbook = lambda symbol, depth=5: {}  # type: ignore[method-assign]
    with pytest.raises(MarketDataUnknownError, match="incomplete"):
        feed.update_features("BTCUSDT")

    good_client.ticker = {"lastPrice": "100", "volume": "10", "highPrice": "101", "lowPrice": "99"}
    good_client.orderbook = {"bids": [["99", "1"]], "asks": [["101", "1"]]}
    feed = MarketDataFeed(client=good_client)
    feed._kline_generators["BTCUSDT:5m"] = _BadGenerator()  # type: ignore[assignment]
    feed._kline_generators["BTCUSDT:1h"] = _BadGenerator()  # type: ignore[assignment]
    assert feed.update_features("BTCUSDT")["dq_tier"] == "CONDITIONAL"

    empty = MarketDataFeed(client=_Client(orderbook={"bids": [], "asks": []}))
    with pytest.raises(MarketDataUnknownError, match="incomplete"):
        empty.update_features("BTCUSDT")
    invalid = MarketDataFeed(client=_Client(orderbook={"bids": [["102", "1"]], "asks": [["101", "1"]]}))
    with pytest.raises(MarketDataUnknownError, match="invalid"):
        invalid.update_features("BTCUSDT")

    now = datetime.now(timezone.utc)
    row = {
        "open_time": now - timedelta(hours=2),
        "close_time": now - timedelta(hours=1),
        "open": 100,
        "high": 102,
        "low": 99,
        "close": 101,
        "volume": 2,
        "quote_volume": 200,
        "is_closed": True,
    }
    generated = _generated_bar(now, closed=True)
    feed = MarketDataFeed(client=_Client())
    feed.fetch_klines = lambda *args, **kwargs: [dict(row) for _ in range(20)]  # type: ignore[method-assign]
    feed.get_generated_klines = lambda symbol, interval="1h": [generated]  # type: ignore[method-assign]
    feed._last_ticker["BTCUSDT"] = {"bid": "99", "ask": "101"}
    assert feed.get_kline_features("BTCUSDT", lookback=20)["n_candles"] == 20
    assert feed._compute_kline_features("BTCUSDT", "1h", [dict(row) for _ in range(20)])["spread_bps"] > 0

    fallback = MarketDataFeed(client=_Client())
    fallback.fetch_klines = lambda *args, **kwargs: []  # type: ignore[method-assign]
    fallback.get_generated_klines = lambda symbol, interval="1h": [generated] * 20  # type: ignore[method-assign]
    assert fallback.get_kline_features("BTCUSDT", lookback=20)["n_candles"] == 20
    empty_features = MarketDataFeed(client=_Client())
    empty_features.fetch_klines = lambda *args, **kwargs: []  # type: ignore[method-assign]
    empty_features.get_generated_klines = lambda symbol, interval="1h": []  # type: ignore[method-assign]
    with pytest.raises(MarketDataUnknownError, match="insufficient"):
        empty_features.get_kline_features("BTCUSDT", lookback=20)
