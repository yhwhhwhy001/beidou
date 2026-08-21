"""Semantic coverage for the feed's transport, websocket and DQ boundaries."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import beidou_core.feed as feed_module
import beidou_exchange.binance_usdm as binance_usdm
from beidou_core.feed import MarketDataFeed, MarketDataUnknownError
from beidou_exchange.binance_usdm.endpoints import Endpoint
from beidou_exchange.core.error_taxonomy import Result


def _closed_kline_rows(count: int = 20) -> list[list[object]]:
    now = datetime.now(timezone.utc)
    rows: list[list[object]] = []
    for index in range(count):
        open_time = now - timedelta(hours=count - index + 2)
        open_ms = int(open_time.timestamp() * 1000)
        close_ms = open_ms + 3_599_000
        rows.append([open_ms, "100", "102", "99", str(100 + index / 10), "10", close_ms, "1000", 3])
    return rows


class _FeedClient:
    def __init__(self, *, ticker: dict | None = None, orderbook: dict | None = None, klines: object = None) -> None:
        self.rest_url = "https://feed.test"
        self.ticker = ticker or {
            "lastPrice": "100",
            "lastQty": "2",
            "priceChangePercent": "1.5",
            "volume": "10",
            "highPrice": "101",
            "lowPrice": "99",
            "E": int(datetime.now(timezone.utc).timestamp() * 1000),
        }
        self.orderbook = orderbook or {"bids": [["99.9", "1"]], "asks": [["100.1", "1"]]}
        self.klines = _closed_kline_rows() if klines is None else klines
        self.calls: list[tuple[str, str, dict]] = []
        self._clock_offset_ms = 25

    async def request(self, method: str, path: str, *, signed: bool = False, params: dict | None = None) -> object:
        self.calls.append((method, path, params or {}))
        if path == Endpoint.TICKER_24HR:
            return Result.success(self.ticker)
        if path == Endpoint.DEPTH:
            return Result.success(self.orderbook)
        if path == Endpoint.KLINES:
            return Result.success(self.klines)
        if path == Endpoint.ACCOUNT:
            return Result.success({"account": "snapshot"})
        return Result.failure("unexpected endpoint")


def test_feed_transport_helpers_and_lifecycle_boundaries() -> None:
    client = _FeedClient()
    feed = MarketDataFeed(client=client)
    replacement = SimpleNamespace(
        rest_url="https://replacement.test", _rest_client=SimpleNamespace(_clock_offset_ms="-30")
    )

    feed.set_client(replacement)
    assert feed._rest_url == "https://replacement.test"
    assert feed._clock_offset_ms() == -30
    assert feed.get_canonical_builder("15m") is feed.get_canonical_builder("15m")
    assert feed.get_generated_klines("BTCUSDT") == []
    assert feed.uptime_seconds() >= 0
    assert feed.is_healthy() is True
    assert feed.is_ws_data_fresh("BTCUSDT") is False
    feed._ws_last_update["BTCUSDT"] = time.monotonic()
    assert feed.is_ws_data_fresh("BTCUSDT") is True

    assert MarketDataFeed._unwrap_result({"ok": True}) == {"ok": True}
    assert MarketDataFeed._unwrap_result([1]) == [1]
    with pytest.raises(MarketDataUnknownError, match="empty"):
        MarketDataFeed._unwrap_result(Result.success(None))
    with pytest.raises(MarketDataUnknownError, match="failed"):
        MarketDataFeed._unwrap_result(Result.failure("failed"))
    with pytest.raises(MarketDataUnknownError, match="unsupported"):
        MarketDataFeed._unwrap_result(object())

    feed._error_count["transport"] = 10
    feed._ws_failure = "closed"
    assert feed.is_healthy() is False
    feed._last_error_time = time.monotonic() - 121
    feed._ws_failure = None
    assert feed.is_healthy() is True
    assert feed._error_count == {}


@pytest.mark.asyncio
async def test_feed_websocket_callbacks_preserve_quote_and_generate_auditable_ticks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeWebSocket:
        instance: FakeWebSocket | None = None

        def __init__(self, *, base_url: str) -> None:
            self.base_url = base_url
            self.callbacks: dict[str, object] = {}
            self.closed = False
            FakeWebSocket.instance = self

        async def subscribe(self, stream: str, callback: object) -> None:
            self.callbacks[stream] = callback

        async def run(self) -> None:
            await asyncio.Event().wait()

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(binance_usdm, "BinanceUsdmWebSocketClient", FakeWebSocket)
    feed = MarketDataFeed(client=_FeedClient())
    feed._last_ticker["BTCUSDT"] = {"bid": "99.8", "ask": "100.2"}

    assert await feed.start_ws(["BTCUSDT"], testnet=True) is True
    ws = FakeWebSocket.instance
    assert ws is not None
    assert set(ws.callbacks) == {"btcusdt@ticker", "btcusdt@depth5@100ms", "btcusdt@markPrice@1s"}

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    await ws.callbacks["btcusdt@ticker"](
        "ticker", {"s": "BTCUSDT", "c": "100", "P": "2", "v": "20", "h": "110", "l": "90", "Q": "3", "E": now_ms}
    )  # type: ignore[misc]
    await ws.callbacks["btcusdt@depth5@100ms"]("depth", {"s": "BTCUSDT", "b": [["99.9", "2"]], "a": [["100.1", "3"]]})  # type: ignore[misc]
    await ws.callbacks["btcusdt@markPrice@1s"]("mark", {"s": "BTCUSDT", "p": "100.05"})  # type: ignore[misc]
    await ws.callbacks["btcusdt@depth5@100ms"]("depth", {})  # type: ignore[misc]

    assert feed._last_ticker["BTCUSDT"]["bid"] == "99.9"
    assert feed._last_ticker["BTCUSDT"]["markPrice"] == "100.05"
    assert feed._last_orderbook["BTCUSDT"]["asks"] == [["100.1", "3"]]
    assert "BTCUSDT:5m" in feed._kline_generators
    assert feed._clock_fallback_ticks == 0

    await ws.callbacks["btcusdt@ticker"]("ticker", {"s": "BTCUSDT", "c": "bad", "Q": "1"})  # type: ignore[misc]
    assert feed._error_count["kline_gen_ws"] == 1

    await feed.stop_ws()
    assert ws.closed is True
    assert feed._ws_client is None
    assert feed._ws_active is False


@pytest.mark.asyncio
async def test_feed_websocket_constructor_and_close_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_constructor(*, base_url: str) -> object:
        raise RuntimeError(f"cannot connect to {base_url}")

    monkeypatch.setattr(binance_usdm, "BinanceUsdmWebSocketClient", fail_constructor)
    feed = MarketDataFeed(client=_FeedClient())
    assert await feed.start_ws(["BTCUSDT"], testnet=False) is False
    assert feed._ws_client is None
    assert feed._ws_failure and "RuntimeError" in feed._ws_failure
    assert feed._error_count["websocket"] == 1

    class BrokenClose:
        def close(self) -> None:
            raise OSError("close failed")

    feed._ws_client = BrokenClose()
    await feed.stop_ws()
    assert feed._ws_client is None
    assert feed._ws_failure and "OSError" in feed._ws_failure
    assert feed._ws_active is False

    class DisconnectOnly:
        def __init__(self) -> None:
            self.disconnected = False

        async def disconnect(self) -> None:
            self.disconnected = True

    fallback = DisconnectOnly()
    feed._ws_client = fallback
    await feed.stop_ws()
    assert fallback.disconnected is True


@pytest.mark.parametrize(
    ("raw", "include_closed", "valid"),
    [
        (None, True, False),
        ([1, 2], True, False),
        (["bad", 0, 1, 0, 1, 1, 2, 1, 1], True, False),
        ([0, 1, 2, 0, 1, 1, -1, 1, 1], True, False),
        ([0, 1, 2, 0, 1, 1, 2, -1, 1], True, False),
        ([0, 1, 0, 0, 1, 1, 2, 1, 1], True, False),
        ([0, 1, 2, 2, 1, 1, 2, 1, 1], True, False),
        ([0, 1, 2, 0, 1, -1, 2, 1, 1], True, False),
    ],
)
def test_feed_rest_parser_rejects_untrusted_rows(raw: object, include_closed: bool, valid: bool) -> None:
    parsed = MarketDataFeed._parse_rest_kline(raw, datetime.now(timezone.utc), include_closed=include_closed)
    assert (parsed is not None) is valid


def test_feed_rest_parser_derives_close_state_and_feature_bar_validation() -> None:
    now = datetime.now(timezone.utc)
    raw = [
        int((now - timedelta(minutes=2)).timestamp() * 1000),
        "100",
        "102",
        "99",
        "101",
        "1",
        int((now - timedelta(minutes=1)).timestamp() * 1000),
        "1000",
        3,
        "x",
        "x",
        "0",
    ]
    parsed = MarketDataFeed._parse_rest_kline(raw, now, include_closed=True)
    assert parsed is not None and parsed["is_closed"] is True
    assert MarketDataFeed._is_valid_feature_bar(parsed) is True
    assert MarketDataFeed._is_valid_feature_bar("not-a-bar") is False
    for field, value in (("open_time", now), ("close_time", now - timedelta(minutes=3))):
        invalid = dict(parsed)
        invalid[field] = value
        assert MarketDataFeed._is_valid_feature_bar(invalid) is False
    for field, value in (
        ("open", float("nan")),
        ("volume", -1),
        ("high", 1),
        ("quote_volume", "bad"),
        ("is_closed", "yes"),
    ):
        invalid = dict(parsed)
        invalid[field] = value
        assert MarketDataFeed._is_valid_feature_bar(invalid) is False

    invalid_close = dict(parsed)
    invalid_close["close_time"] = invalid_close["open_time"]
    assert MarketDataFeed._is_valid_feature_bar(invalid_close) is False
    invalid_low = dict(parsed)
    invalid_low["low"] = 103
    assert MarketDataFeed._is_valid_feature_bar(invalid_low) is False


def test_feed_indicator_helpers_and_clock_offset_fail_closed() -> None:
    assert feed_module._wilder_smooth_last([], 14) == 0.0
    assert feed_module._wilder_smooth_last([1.0, 3.0], 14) == 2.0
    assert feed_module._ema_series([], 3) == []
    assert feed_module._ema_series([1.0, 3.0], 3) == [2.0, 2.0]

    feed = MarketDataFeed(client=SimpleNamespace(_rest_client=SimpleNamespace(_clock_offset_ms="bad")))
    assert feed._clock_offset_ms() is None
    feed._client._rest_client._clock_offset_ms = 3_600_001
    assert feed._clock_offset_ms() is None

    now = datetime.now(timezone.utc)
    assert MarketDataFeed._parse_event_time({"E": float("nan")}) is None
    assert MarketDataFeed._parse_event_time({"E": now.timestamp() * 1000}) is not None
    gate = feed._build_live_dq_gate(
        feed_module.VenueInstrument(
            venue_id=feed_module.VenueId("BINANCE"), instrument_id=feed_module.InstrumentId("BTCUSDT")
        ),
        {},
        100.0,
        now - timedelta(seconds=400),
    )
    assert gate.overall_tier().value == "FAIL"


@pytest.mark.asyncio
async def test_feed_async_fetch_and_update_rejects_bad_boundary_facts() -> None:
    cases = [
        _FeedClient(ticker={"bad": 1}),
        _FeedClient(orderbook={"bids": []}),
        _FeedClient(klines={"error": "bad"}),
    ]
    with pytest.raises(MarketDataUnknownError, match="ticker"):
        await MarketDataFeed(client=cases[0]).async_fetch_ticker("BTCUSDT")
    with pytest.raises(MarketDataUnknownError, match="order book"):
        await MarketDataFeed(client=cases[1]).async_fetch_orderbook("BTCUSDT")
    with pytest.raises(MarketDataUnknownError, match="not a list"):
        await MarketDataFeed(client=cases[2]).async_fetch_klines("BTCUSDT", "1h")

    class ExplodingClient:
        async def request(self, method: str, path: str, **kwargs: object) -> object:
            raise OSError("network down")

    with pytest.raises(MarketDataUnknownError, match="request failed"):
        await MarketDataFeed(client=ExplodingClient()).async_update_features("BTCUSDT")

    incomplete = MarketDataFeed(client=_FeedClient(ticker={"lastPrice": "100"}, orderbook={"asks": [["101", "1"]]}))
    with pytest.raises(MarketDataUnknownError, match="incomplete"):
        await incomplete.async_update_features("BTCUSDT")

    empty_book = MarketDataFeed(client=_FeedClient(orderbook={"bids": [], "asks": []}))
    with pytest.raises(MarketDataUnknownError, match="two-sided order book"):
        await empty_book.async_update_features("BTCUSDT")

    invalid_book = MarketDataFeed(client=_FeedClient(orderbook={"bids": [["101", "1"]], "asks": [["100", "1"]]}))
    with pytest.raises(MarketDataUnknownError, match="invalid"):
        await invalid_book.async_update_features("BTCUSDT")

    stale_ticker = dict(_FeedClient().ticker)
    stale_ticker["E"] = int((datetime.now(timezone.utc) - timedelta(seconds=400)).timestamp() * 1000)
    stale = MarketDataFeed(client=_FeedClient(ticker=stale_ticker))
    with pytest.raises(MarketDataUnknownError, match="data quality gate FAIL"):
        await stale.async_update_features("BTCUSDT")

    for client, message in [
        (
            _FeedClient(ticker={"lastPrice": "100"}, orderbook={"bids": [["99", "1"]], "asks": [["100", "1"]]}),
            "contains an error",
        ),
        (
            _FeedClient(ticker={"lastPrice": "100"}, orderbook={"bids": [["99", "1"]], "asks": [["100", "1"]]}),
            "not a list",
        ),
    ]:
        client.klines = {"error": "bad"} if message == "contains an error" else "bad"
        target = MarketDataFeed(client=client)
        with pytest.raises(MarketDataUnknownError):
            await target.async_get_klines_raw("BTCUSDT")


@pytest.mark.asyncio
async def test_feed_async_kline_features_and_raw_history_use_closed_deduplicated_bars() -> None:
    feed = MarketDataFeed(client=_FeedClient())
    features = await feed.async_get_kline_features("BTCUSDT", "1h", 20)
    assert features["n_candles"] == 20
    assert features["dq_tier"] == "CONDITIONAL"

    raw = await feed.async_get_klines_raw("BTCUSDT", "1h", 20)
    assert len(raw) == 20
    assert [row["open_time"] for row in raw] == sorted(row["open_time"] for row in raw)

    for client, message in [
        (_FeedClient(klines={"error": "bad"}), "contains an error"),
        (_FeedClient(klines="bad"), "not a list"),
    ]:
        with pytest.raises(MarketDataUnknownError, match=message):
            await MarketDataFeed(client=client).async_get_kline_features("BTCUSDT")

    stale_ticker = dict(_FeedClient().ticker)
    stale_ticker["E"] = int((datetime.now(timezone.utc) - timedelta(seconds=400)).timestamp() * 1000)
    stale = MarketDataFeed(client=_FeedClient(ticker=stale_ticker))
    stale._last_ticker["BTCUSDT"] = stale_ticker
    with pytest.raises(MarketDataUnknownError, match="kline DQ gate FAIL"):
        await stale.async_get_kline_features("BTCUSDT")


def test_feed_sync_fetch_account_and_feature_accessors() -> None:
    feed = MarketDataFeed(client=_FeedClient())
    assert feed.fetch_account() == {"account": "snapshot"}
    assert feed.get_last_ticker("missing") == {}
    assert feed.get_last_orderbook("missing") == {}

    features = feed.update_features("BTCUSDT")
    assert features["price"] == 100.0
    assert features["dq_tier"] in {"PASS", "CONDITIONAL"}
    assert feed.get_last_ticker("BTCUSDT")["lastPrice"] == "100"
    assert feed.get_last_orderbook("BTCUSDT")["bids"]
    assert feed.get_kline_features("BTCUSDT", "1h", 20)["n_candles"] == 20

    with pytest.raises(MarketDataUnknownError, match="not a list"):
        MarketDataFeed(client=_FeedClient(klines={"error": "bad"})).fetch_klines("BTCUSDT", "1h")
    with pytest.raises(MarketDataUnknownError, match="no closed valid rows"):
        MarketDataFeed(client=_FeedClient(klines=[])).fetch_klines("BTCUSDT", "1h")

    stale_ticker = dict(_FeedClient().ticker)
    stale_ticker["E"] = int((datetime.now(timezone.utc) - timedelta(seconds=400)).timestamp() * 1000)
    with pytest.raises(MarketDataUnknownError, match="data quality gate FAIL"):
        MarketDataFeed(client=_FeedClient(ticker=stale_ticker)).update_features("BTCUSDT")
