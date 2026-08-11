"""行情数据源 — BD-T03: 所有网络调用通过 BinanceUsdmAdapter 传输。

禁止建立绕过适配器的底层 HTTP 连接，也禁止硬编码交易所端点。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from beidou_data.canonical_bars import CanonicalBarBuilder, get_canonical_bar_builder  # BD-CV11
from beidou_data.feature_store import FeatureStore, FeatureVector
from beidou_data.klines import OHLCV, KLineGenerator
from beidou_data.quality import DataQualityGate, DQCheckResult, DQCheckType
from beidou_exchange.binance_usdm.adapter import BinanceUsdmAdapter
from beidou_exchange.binance_usdm.endpoints import DEFAULT_RECV_WINDOW_MS, Endpoint
from beidou_exchange.binance_usdm.rest_client import BinanceRESTClient
from beidou_shared.config import ConfigProvider
from beidou_shared.types import (
    DataQualityTier,
    InstrumentId,
    Price,
    Quantity,
    SchemaVersion,
    VenueId,
    VenueInstrument,
)

logger = logging.getLogger(__name__)


class MarketDataUnknownError(RuntimeError):
    """Raised when the feed cannot prove a usable market-data fact."""


class MarketDataFeed:
    """Binance REST 行情数据源 — BD-T03 收敛。

    所有 HTTP 调用通过 BinanceUsdmAdapter；REST client 只作为 Adapter 的
    内部 transport。禁止行情层直接持有或调用底层 REST transport。
    """

    def __init__(self, client: Any = None) -> None:
        settings = ConfigProvider().load()
        self._rest_url = settings.exchange.rest_base_url
        self._api_key = ""  # 通过秘密提供器注入
        self._api_secret = ""
        self._recv_window = DEFAULT_RECV_WINDOW_MS

        # BD-T03: 使用注入的 Adapter 作为唯一网络传输。独立研究工具没有
        # 注入 client 时也必须先构造 Adapter，不能让行情层绕过协议边界。
        if client is not None:
            self._client = client
        else:
            self._client = BinanceUsdmAdapter(
                rest_client=BinanceRESTClient(
                    rest_url=self._rest_url,
                    api_key=self._api_key,
                    api_secret=self._api_secret,
                )
            )

        self._feature_store = FeatureStore()
        self._kline_generators: dict[str, KLineGenerator] = {}
        # BD-CV11: 统一 CanonicalBarBuilder — 运行时/回测/replay 共用
        self._canonical_builders: dict[str, CanonicalBarBuilder] = {}
        self._last_ticker: dict[str, dict] = {}
        self._last_orderbook: dict[str, dict] = {}
        self._error_count: dict[str, int] = {}
        self._last_error_time: float = 0.0  # 最近一次错误的时刻，用于衰减判断
        # Health/freshness is a duration domain; wall-clock corrections must
        # not manufacture uptime or stale-data recovery.
        self._start_time = time.monotonic()

        # WebSocket 实时行情（可选）
        self._ws_client: Any = None
        self._ws_active = False
        self._ws_failure: str | None = None
        self._ws_last_update: dict[str, float] = {}  # symbol → last WS update time
        self._ws_stale_threshold = 60.0  # WS 数据超时阈值（秒）

    def set_client(self, client: Any) -> None:
        """注入共享的 REST client（引擎启动时调用，替代独立实例）。"""
        self._client = client
        self._rest_url = getattr(client, "rest_url", self._rest_url)

    def _get_kline_generator(self, symbol: str, interval: str = "1h") -> KLineGenerator:
        """按 symbol:interval 惰性创建 KLineGenerator（真实 tick → OHLCV 聚合）。

        BD-CV11: 同时注册到 CanonicalBarBuilder 注册表。
        """
        key = f"{symbol}:{interval}"
        gen = self._kline_generators.get(key)
        if gen is None:
            gen = KLineGenerator(interval=interval)
            self._kline_generators[key] = gen
            # BD-CV11: 确保运行时使用共享 CanonicalBarBuilder
        if interval not in self._canonical_builders:
            self._canonical_builders[interval] = get_canonical_bar_builder(interval)
        return gen

    def get_canonical_builder(self, interval: str = "5m") -> CanonicalBarBuilder:
        """BD-CV11: 获取共享 CanonicalBarBuilder。回到/研究/replay 统一调用。"""
        if interval not in self._canonical_builders:
            self._canonical_builders[interval] = get_canonical_bar_builder(interval)
        return self._canonical_builders[interval]

    def get_generated_klines(self, symbol: str, interval: str = "1h", *, include_current: bool = False) -> list[OHLCV]:
        """Return generated bars; research features use closed bars by default."""
        gen = self._get_kline_generator(symbol, interval)
        vi = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId(symbol))
        klines = gen.get_klines(vi)
        current = gen.get_current_bar(vi)
        if include_current and current is not None and current.is_closed:
            klines = [*klines, current]
        return klines

    def uptime_seconds(self) -> float:
        return time.monotonic() - self._start_time

    def is_healthy(self) -> bool:
        # 错误计数时间衰减：若 120s 内无新错误，清零计数器
        # 避免启动初期瞬时错误（如空 URL）永久毒化健康状态
        now = time.monotonic()
        if self._last_error_time > 0 and now - self._last_error_time > 120:
            self._error_count.clear()
            self._last_error_time = 0.0
        total_errors = sum(self._error_count.values())
        ws_ok = self._ws_failure is None
        if self._ws_client is not None:
            ws_ok = self._ws_client.state.value in ("CONNECTED", "RECONNECTING")
        return total_errors < 10 and ws_ok

    # --- WebSocket 实时行情 ---

    async def start_ws(self, symbols: list[str], testnet: bool = True) -> bool:
        """启动 WebSocket 实时行情流。

        订阅所有标的的 ticker + depth5 + mark_price 流。
        失败时回退到 REST 轮询（静默降级）。
        """
        try:
            from beidou_exchange.binance_usdm import (
                FSTREAM_PRODUCTION_URL,
                FSTREAM_TESTNET_URL,
                BinanceUsdmWebSocketClient,
            )

            self._ws_client = BinanceUsdmWebSocketClient(
                base_url=FSTREAM_TESTNET_URL if testnet else FSTREAM_PRODUCTION_URL
            )

            # 注册回调：更新本地缓存（新API: callback(stream_name, data)）
            async def _on_ticker(stream: str, data: dict) -> None:
                symbol = data.get("s", "")
                if symbol:
                    self._last_ticker[symbol] = {
                        "lastPrice": data.get("c", "0"),
                        "priceChangePercent": data.get("P", "0"),
                        "volume": data.get("v", "0"),
                        "highPrice": data.get("h", "0"),
                        "lowPrice": data.get("l", "0"),
                        "bid": data.get("b", "0"),
                        "ask": data.get("a", "0"),
                    }
                    self._ws_last_update[symbol] = time.monotonic()
                    # 用 WebSocket 实时 ticker 驱动 K 线生成
                    try:
                        last_price = float(data.get("c", 0))
                        # PKG22 (BDS-P1-040): 使用 lastQty 作为逐笔成交量，
                        # 而非 24h 累计 volume (data["v"])
                        per_tick_volume = float(data.get("l", data.get("lastQty", 0)))
                        if last_price > 0 and per_tick_volume > 0:
                            kg_key = f"{symbol}:5m"
                            if kg_key not in self._kline_generators:
                                self._kline_generators[kg_key] = KLineGenerator(interval="5m")
                            self._kline_generators[kg_key].update(
                                price=last_price,
                                volume=per_tick_volume,
                                timestamp=datetime.now(timezone.utc),
                                symbol=symbol,
                            )
                    except Exception as exc:
                        # PKG22 (BDS-P1-041): 不再静默跳过，记录错误
                        logger.warning(
                            "WS kline aggregation failed for %s: %s: %s",
                            symbol,
                            type(exc).__name__,
                            str(exc)[:120],
                        )
                        self._error_count["kline_gen_ws"] = self._error_count.get("kline_gen_ws", 0) + 1
                        self._last_error_time = time.monotonic()

            async def _on_depth(stream: str, data: dict) -> None:
                symbol = data.get("s", "")
                if symbol and "bids" in data:
                    self._last_orderbook[symbol] = {
                        "bids": [[b[0], b[1]] for b in data.get("bids", [])],
                        "asks": [[a[0], a[1]] for a in data.get("asks", [])],
                    }
                    self._ws_last_update[symbol] = time.monotonic()

            async def _on_mark_price(stream: str, data: dict) -> None:
                symbol = data.get("s", "")
                if symbol and symbol in self._last_ticker:
                    self._last_ticker[symbol]["markPrice"] = data.get("p", "0")

            # 构建订阅流列表
            for sym in symbols:
                sym_lower = sym.lower()
                await self._ws_client.subscribe(f"{sym_lower}@ticker", _on_ticker)
                await self._ws_client.subscribe(f"{sym_lower}@depth5@100ms", _on_depth)
                await self._ws_client.subscribe(f"{sym_lower}@markPrice@1s", _on_mark_price)

            # 启动连接（后台任务）BD-FIX: 保存任务引用以便停止追踪
            import asyncio as _asyncio

            self._ws_task = _asyncio.create_task(self._ws_client.run())
            self._ws_active = True
            self._ws_failure = None
            logger.info("WebSocket started: %s streams for %s symbols", len(symbols) * 3, len(symbols))
            return True
        except Exception as e:
            logger.warning("WebSocket unavailable; market-data health remains UNKNOWN: %s", type(e).__name__)
            self._ws_client = None
            self._ws_active = False
            self._ws_failure = f"{type(e).__name__}: {e}"
            self._error_count["websocket"] = self._error_count.get("websocket", 0) + 1
            self._last_error_time = time.monotonic()
            return False

    async def stop_ws(self) -> None:
        """BD-FIX: 安全停止 WebSocket 连接。"""
        if self._ws_client is not None:
            # BinanceUsdmWebSocketClient exposes ``close``.  Keep a
            # compatibility fallback for injected test transports, but make
            # close failures observable instead of silently discarding them.
            try:
                close = getattr(self._ws_client, "close", None)
                if callable(close):
                    result = close()
                else:
                    disconnect = getattr(self._ws_client, "disconnect", None)
                    result = disconnect() if callable(disconnect) else None
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                self._ws_failure = f"{type(exc).__name__}: {exc}"
                self._error_count["websocket"] = self._error_count.get("websocket", 0) + 1
                self._last_error_time = time.monotonic()
                logger.error("WebSocket close failed: %s", type(exc).__name__)
            self._ws_client = None
        if hasattr(self, "_ws_task") and self._ws_task is not None:
            self._ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._ws_task
            self._ws_task = None
        self._ws_active = False

    def is_ws_data_fresh(self, symbol: str) -> bool:
        """检查 WebSocket 数据是否新鲜。超过阈值返回 False（触发 REST 回退）。"""
        last = self._ws_last_update.get(symbol, 0)
        return (time.monotonic() - last) < self._ws_stale_threshold

    def _api(self, path: str, method: str = "GET", signed: bool = False, params: dict | None = None) -> Any:
        """BD-T03: 通过 BinanceRESTClient 代理 — 所有网络调用收敛到 Adapter 传输层。

        同步包装器：在已有事件循环时通过 asyncio.to_thread 调用，
        否则尝试新建/获取事件循环执行。
        """

        async def request() -> Any:
            return await self._client.request(method, path, signed=signed, params=params or {})

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            result = asyncio.run(request())
        else:
            # A synchronous compatibility caller may be nested in an active
            # event loop.  Run the coroutine in a short-lived worker loop
            # instead of calling run_until_complete on the active loop.
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="beidou-feed-sync") as executor:
                result = executor.submit(asyncio.run, request()).result()
        return self._unwrap_result(result)

    async def _api_async(self, path: str, method: str = "GET", signed: bool = False, params: dict | None = None) -> Any:
        """BD-T03: 异步 API 调用统一通过 BinanceRESTClient.request()。"""
        result = await self._client.request(method, path, signed=signed, params=params or {})
        return self._unwrap_result(result)

    @staticmethod
    def _unwrap_result(result: Any) -> Any:
        """Normalize Adapter ``Result`` and legacy dict transports at the feed boundary."""

        if hasattr(result, "is_success"):
            if result.is_success():
                if result.data is None:
                    raise MarketDataUnknownError("market data response is empty")
                return result.data
            error = getattr(result, "error", None)
            raise MarketDataUnknownError(str(error or "market data request failed"))
        if isinstance(result, (dict, list)):
            return result
        raise MarketDataUnknownError(f"market data response has unsupported type: {type(result).__name__}")

    @staticmethod
    def _parse_rest_kline(raw: Any, now: datetime, *, include_closed: bool) -> dict[str, Any] | None:
        """Parse one Binance REST kline without manufacturing market facts.

        REST responses are an external trust boundary.  A malformed row must
        not become a zero/NaN feature, and a still-forming row must not enter a
        strategy observation set.  Callers deliberately skip invalid rows;
        the downstream feature gate still requires a sufficient, valid sample.
        """

        if not isinstance(raw, (list, tuple)) or len(raw) < 9:
            return None

        def _finite(value: Any) -> float | None:
            if isinstance(value, bool):
                return None
            try:
                parsed = float(value)
            except (TypeError, ValueError, OverflowError):
                return None
            return parsed if math.isfinite(parsed) else None

        open_ms = _finite(raw[0])
        close_ms = _finite(raw[6])
        if open_ms is None or close_ms is None or open_ms < 0 or close_ms <= open_ms:
            return None

        try:
            open_time = datetime.fromtimestamp(open_ms / 1000, tz=timezone.utc)
            close_time = datetime.fromtimestamp(close_ms / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

        if close_time > now:
            return None

        values: dict[str, float] = {}
        for name, index in (("open", 1), ("high", 2), ("low", 3), ("close", 4), ("volume", 5)):
            parsed = _finite(raw[index])
            if parsed is None:
                return None
            if name == "volume":
                if parsed < 0:
                    return None
            elif parsed <= 0:
                return None
            values[name] = parsed

        if values["high"] < max(values["open"], values["close"], values["low"]):
            return None
        if values["low"] > min(values["open"], values["close"], values["high"]):
            return None

        quote_volume = _finite(raw[7])
        if quote_volume is None or quote_volume < 0:
            return None

        parsed_row: dict[str, Any] = {
            "open_time": open_time,
            "open": values["open"],
            "high": values["high"],
            "low": values["low"],
            "close": values["close"],
            "volume": values["volume"],
            "close_time": close_time,
            "quote_volume": quote_volume,
            "trades": raw[8],
        }
        if include_closed:
            parsed_row["is_closed"] = True
        return parsed_row

    @staticmethod
    def _is_valid_feature_bar(bar: Any) -> bool:
        """Validate bars from REST or the local generator before indicators."""

        if not isinstance(bar, dict):
            return False
        open_time = bar.get("open_time")
        close_time = bar.get("close_time")
        if not isinstance(open_time, datetime) or not isinstance(close_time, datetime) or close_time <= open_time:
            return False

        values: dict[str, float] = {}
        for name in ("open", "high", "low", "close", "volume"):
            value = bar.get(name)
            if isinstance(value, bool):
                return False
            try:
                parsed = float(value)
            except (TypeError, ValueError, OverflowError):
                return False
            if not math.isfinite(parsed):
                return False
            if name == "volume" and parsed < 0:
                return False
            if name != "volume" and parsed <= 0:
                return False
            values[name] = parsed
        if values["high"] < max(values["open"], values["close"], values["low"]):
            return False
        if values["low"] > min(values["open"], values["close"], values["high"]):
            return False
        if "is_closed" in bar and not isinstance(bar["is_closed"], bool):
            return False
        quote_volume = bar.get("quote_volume")
        if quote_volume is not None:
            try:
                quote_value = float(quote_volume)
            except (TypeError, ValueError, OverflowError):
                return False
            if not math.isfinite(quote_value) or quote_value < 0:
                return False
        return True

    # --- Data fetching (async) ---

    async def async_fetch_ticker(self, symbol: str) -> dict:
        data = await self._api_async(Endpoint.TICKER_24HR, params={"symbol": symbol})
        if not isinstance(data, dict) or "lastPrice" not in data:
            raise MarketDataUnknownError(f"ticker for {symbol} is incomplete")
        self._last_ticker[symbol] = data
        return data

    async def async_fetch_orderbook(self, symbol: str, depth: int = 5) -> dict:
        data = await self._api_async(Endpoint.DEPTH, params={"symbol": symbol, "limit": depth})
        if not isinstance(data, dict) or "bids" not in data or "asks" not in data:
            raise MarketDataUnknownError(f"order book for {symbol} is incomplete")
        self._last_orderbook[symbol] = data
        return data

    async def async_fetch_klines(self, symbol: str, interval: str, limit: int = 100) -> list[dict]:
        raw = await self._api_async(
            Endpoint.KLINES,
            params={"symbol": symbol, "interval": interval, "limit": limit},
        )
        if not isinstance(raw, list):
            raise MarketDataUnknownError(f"klines for {symbol} are not a list")
        klines = []
        now = datetime.now(timezone.utc)
        for k in raw:
            parsed = self._parse_rest_kline(k, now, include_closed=False)
            if parsed is not None:
                klines.append(parsed)
        if not klines:
            raise MarketDataUnknownError(f"klines for {symbol} contain no closed valid rows")
        return klines

    async def async_update_features(self, symbol: str) -> dict[str, float]:
        """异步版本 — 直接使用共享 REST client，零线程池开销。

        替换原来的 asyncio.to_thread(update_features)，避免嵌套事件循环
        和线程池耗尽。所有网络 I/O 通过共享 client 的 asyncio.to_thread
        在单层事件循环中执行。
        """
        try:
            ticker_result = await self._api_async(Endpoint.TICKER_24HR, params={"symbol": symbol})
            orderbook_result = await self._api_async(Endpoint.DEPTH, params={"symbol": symbol, "limit": 5})
        except Exception as exc:
            logger.warning("async_update_features request failed for %s: %s", symbol, exc)
            if isinstance(exc, MarketDataUnknownError):
                raise
            raise MarketDataUnknownError(f"market data request failed for {symbol}") from exc

        if not isinstance(ticker_result, dict) or not isinstance(orderbook_result, dict):
            raise MarketDataUnknownError(f"market data response for {symbol} is incomplete")
        if "error" in ticker_result or "error" in orderbook_result:
            raise MarketDataUnknownError(f"market data response for {symbol} contains an error")

        ticker = ticker_result
        orderbook = orderbook_result

        if "lastPrice" not in ticker or "bids" not in orderbook:
            logger.warning(
                "async_update_features incomplete data for %s: has_lastPrice=%s has_bids=%s",
                symbol,
                "lastPrice" in ticker,
                "bids" in orderbook,
            )
            raise MarketDataUnknownError(f"ticker/order book for {symbol} is incomplete")

        self._last_ticker[symbol] = ticker
        self._last_orderbook[symbol] = orderbook

        # BD-FIX: KLineGenerator — 用实时 ticker 价格生成 OHLCV bar
        try:
            last_price = float(ticker["lastPrice"])
            # PKG22 (BDS-P1-040): 使用 lastQty 作为逐笔成交量
            per_tick_volume = float(ticker.get("lastQty", 0))
            kg_key = f"{symbol}:5m"
            if kg_key not in self._kline_generators:
                self._kline_generators[kg_key] = KLineGenerator(interval="5m")
            kg = self._kline_generators[kg_key]
            kg.update(
                price=last_price,
                volume=per_tick_volume if per_tick_volume > 0 else float(ticker.get("volume", 0)),
                timestamp=datetime.now(timezone.utc),
            )
        except Exception as exc:
            logger.warning("5m kline aggregation failed for %s: %s: %s", symbol, type(exc).__name__, str(exc)[:120])
            self._error_count["kline_gen"] = self._error_count.get("kline_gen", 0) + 1
            self._last_error_time = time.monotonic()

        last_price = float(ticker["lastPrice"])
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])
        if not bids or not asks:
            logger.warning("async_update_features missing two-sided order book for %s", symbol)
            raise MarketDataUnknownError(f"two-sided order book for {symbol} is incomplete")
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        if not (0 < best_bid <= best_ask):
            logger.warning("async_update_features invalid two-sided order book for %s", symbol)
            raise MarketDataUnknownError(f"two-sided order book for {symbol} is invalid")
        spread_bps = (best_ask - best_bid) / best_ask * 10000
        change_pct = float(ticker.get("priceChangePercent", 0))

        features = {
            "price": last_price,
            "bid": best_bid,
            "ask": best_ask,
            "spread_bps": spread_bps,
            "change_24h_pct": change_pct,
            "volume_24h": float(ticker.get("volume", 0)),
            "high_24h": float(ticker.get("highPrice", 0)),
            "low_24h": float(ticker.get("lowPrice", 0)),
        }

        instrument_id = InstrumentId(symbol)
        venue_id = VenueId("BINANCE")
        vi = VenueInstrument(venue_id=venue_id, instrument_id=instrument_id)

        # 将实时 tick 喂入 KLineGenerator
        try:
            gen = self._get_kline_generator(symbol, "1h")
            gen.process_tick(
                vi,
                Price(amount=str(last_price)),
                Quantity(amount=str(ticker.get("lastQty", "0")) or "0"),
                datetime.now(timezone.utc),
                is_taker_buy=True,
            )
        except Exception as exc:
            logger.warning("kline generator update failed for %s: %s", symbol, type(exc).__name__)
            self._error_count["kline_gen"] = self._error_count.get("kline_gen", 0) + 1
            self._last_error_time = time.monotonic()

        gate = DataQualityGate(venue_instrument=vi)
        gate.checks.append(
            DQCheckResult(
                check_type=DQCheckType.FRESHNESS,
                tier=DataQualityTier.PASS,
                detail="live_ticker",
            )
        )
        if spread_bps < 100:
            gate.checks.append(
                DQCheckResult(
                    check_type=DQCheckType.COMPLETENESS,
                    tier=DataQualityTier.PASS,
                    detail=f"spread={spread_bps:.1f}bps",
                )
            )

        self._feature_store.store(
            FeatureVector(
                name=f"{symbol.lower()}_live",
                values=features,
                timestamp=datetime.now(timezone.utc),
                instrument_id=instrument_id,
                venue_id=venue_id,
                version=SchemaVersion("2.0.0"),
            )
        )

        return features

    async def async_get_kline_features(self, symbol: str, interval: str = "1h", lookback: int = 100) -> dict[str, Any]:
        """异步版本 — 经 Adapter 统一边界获取闭合 K 线。"""
        raw = await self._api_async(
            Endpoint.KLINES,
            params={"symbol": symbol, "interval": interval, "limit": lookback},
        )
        if isinstance(raw, dict) and "error" in raw:
            raise MarketDataUnknownError(f"kline response for {symbol} contains an error")
        if not isinstance(raw, list):
            raise MarketDataUnknownError(f"kline response for {symbol} is not a list")

        klines = []
        now = datetime.now(timezone.utc)
        for k in raw:
            parsed = self._parse_rest_kline(k, now, include_closed=True)
            if parsed is not None:
                klines.append(parsed)

        # 合并 KLineGenerator 实时聚合的 K 线
        gen_klines = self.get_generated_klines(symbol, interval)
        if gen_klines and klines:
            gen_last = gen_klines[-1]
            if gen_last.open_time >= klines[-1]["open_time"]:
                klines[-1] = {
                    "open_time": gen_last.open_time,
                    "open": float(gen_last.open.amount),
                    "high": float(gen_last.high.amount),
                    "low": float(gen_last.low.amount),
                    "close": float(gen_last.close.amount),
                    "volume": float(gen_last.volume.amount),
                    "close_time": gen_last.close_time,
                    "quote_volume": float(gen_last.quote_volume.amount) if gen_last.quote_volume else 0.0,
                    "trades": gen_last.trade_count,
                    "is_closed": bool(gen_last.is_closed),
                }
        elif not klines and len(gen_klines) >= 20:
            klines = [
                {
                    "open_time": k.open_time,
                    "open": float(k.open.amount),
                    "high": float(k.high.amount),
                    "low": float(k.low.amount),
                    "close": float(k.close.amount),
                    "volume": float(k.volume.amount),
                    "close_time": k.close_time,
                    "quote_volume": float(k.quote_volume.amount) if k.quote_volume else 0.0,
                    "trades": k.trade_count,
                    "is_closed": bool(k.is_closed),
                }
                for k in gen_klines
            ]
        features = self._compute_kline_features(symbol, interval, klines)
        if not features:
            raise MarketDataUnknownError(f"insufficient valid closed bars for {symbol}")
        return features

    def fetch_ticker(self, symbol: str) -> dict:
        data = self._api(Endpoint.TICKER_24HR, params={"symbol": symbol})
        if not isinstance(data, dict) or "lastPrice" not in data:
            raise MarketDataUnknownError(f"ticker for {symbol} is incomplete")
        self._last_ticker[symbol] = data
        return data

    def fetch_orderbook(self, symbol: str, depth: int = 5) -> dict:
        data = self._api(Endpoint.DEPTH, params={"symbol": symbol, "limit": depth})
        if not isinstance(data, dict) or "bids" not in data or "asks" not in data:
            raise MarketDataUnknownError(f"order book for {symbol} is incomplete")
        self._last_orderbook[symbol] = data
        return data

    def fetch_klines(self, symbol: str, interval: str, limit: int = 100) -> list[dict]:
        raw = self._api(
            Endpoint.KLINES,
            params={
                "symbol": symbol,
                "interval": interval,
                "limit": limit,
            },
        )
        if not isinstance(raw, list):
            raise MarketDataUnknownError(f"klines for {symbol} are not a list")
        klines = []
        now = datetime.now(timezone.utc)
        for k in raw:
            parsed = self._parse_rest_kline(k, now, include_closed=True)
            if parsed is not None:
                klines.append(parsed)
        if not klines:
            raise MarketDataUnknownError(f"klines for {symbol} contain no closed valid rows")
        return klines

    def fetch_account(self) -> dict:
        return self._api(Endpoint.ACCOUNT, signed=True)

    # --- Feature computation ---

    def update_features(self, symbol: str) -> dict[str, float]:
        ticker = self.fetch_ticker(symbol)
        orderbook = self.fetch_orderbook(symbol, 5)

        if not ticker or not orderbook:
            raise MarketDataUnknownError(f"ticker/order book for {symbol} is incomplete")

        # BD-FIX: KLineGenerator — 用实时 ticker 价格生成 OHLCV bar
        try:
            last_price = float(ticker["lastPrice"])
            # PKG22 (BDS-P1-040): 使用 lastQty 作为逐笔成交量
            per_tick_volume = float(ticker.get("lastQty", 0))
            kg_key = f"{symbol}:5m"
            if kg_key not in self._kline_generators:
                self._kline_generators[kg_key] = KLineGenerator(interval="5m")
            kg = self._kline_generators[kg_key]
            kg.update(
                price=last_price,
                volume=per_tick_volume if per_tick_volume > 0 else float(ticker.get("volume", 0)),
                timestamp=datetime.now(timezone.utc),
            )
        except Exception as exc:
            logger.warning("5m kline aggregation failed for %s: %s", symbol, type(exc).__name__)
            self._error_count["kline_gen"] = self._error_count.get("kline_gen", 0) + 1
            self._last_error_time = time.monotonic()

        last_price = float(ticker["lastPrice"])
        # BD-FIX: 空 orderbook 保护（流动性稀薄标的）
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])
        if not bids or not asks:
            logger.warning("update_features missing two-sided order book for %s", symbol)
            raise MarketDataUnknownError(f"two-sided order book for {symbol} is incomplete")
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        if not (0 < best_bid <= best_ask):
            logger.warning("update_features invalid two-sided order book for %s", symbol)
            raise MarketDataUnknownError(f"two-sided order book for {symbol} is invalid")
        spread_bps = (best_ask - best_bid) / best_ask * 10000
        change_pct = float(ticker.get("priceChangePercent", 0))

        features = {
            "price": last_price,
            "bid": best_bid,
            "ask": best_ask,
            "spread_bps": spread_bps,
            "change_24h_pct": change_pct,
            "volume_24h": float(ticker.get("volume", 0)),
            "high_24h": float(ticker.get("highPrice", 0)),
            "low_24h": float(ticker.get("lowPrice", 0)),
        }

        instrument_id = InstrumentId(symbol)
        venue_id = VenueId("BINANCE")
        vi = VenueInstrument(venue_id=venue_id, instrument_id=instrument_id)

        # 将实时 tick 喂入 KLineGenerator（真实 tick → OHLCV 聚合）
        try:
            gen = self._get_kline_generator(symbol, "1h")
            gen.process_tick(
                vi,
                Price(amount=str(last_price)),
                Quantity(amount=str(ticker.get("lastQty", "0")) or "0"),
                datetime.now(timezone.utc),
                is_taker_buy=True,
            )
        except Exception as exc:
            # KLine 聚合失败不得阻断 feature 更新
            logger.warning("kline generator update failed for %s: %s", symbol, type(exc).__name__)
            self._error_count["kline_gen"] = self._error_count.get("kline_gen", 0) + 1
            self._last_error_time = time.monotonic()

        gate = DataQualityGate(venue_instrument=vi)
        gate.checks.append(
            DQCheckResult(
                check_type=DQCheckType.FRESHNESS,
                tier=DataQualityTier.PASS,
                detail="live_ticker",
            )
        )
        if spread_bps < 100:
            gate.checks.append(
                DQCheckResult(
                    check_type=DQCheckType.COMPLETENESS,
                    tier=DataQualityTier.PASS,
                    detail=f"spread={spread_bps:.1f}bps",
                )
            )

        self._feature_store.store(
            FeatureVector(
                name=f"{symbol.lower()}_live",
                values=features,
                timestamp=datetime.now(timezone.utc),
                instrument_id=instrument_id,
                venue_id=venue_id,
                version=SchemaVersion("2.0.0"),
            )
        )

        return features

    def get_kline_features(self, symbol: str, interval: str = "1h", lookback: int = 100) -> dict[str, float]:
        klines = self.fetch_klines(symbol, interval, lookback)

        # 合并 KLineGenerator 实时聚合的 K 线（tick 驱动的当前 bar 优先于 REST 最后一条）
        gen_klines = self.get_generated_klines(symbol, interval)
        if gen_klines and klines:
            gen_last = gen_klines[-1]
            if gen_last.open_time >= klines[-1]["open_time"]:
                klines[-1] = {
                    "open_time": gen_last.open_time,
                    "open": float(gen_last.open.amount),
                    "high": float(gen_last.high.amount),
                    "low": float(gen_last.low.amount),
                    "close": float(gen_last.close.amount),
                    "volume": float(gen_last.volume.amount),
                    "close_time": gen_last.close_time,
                    "quote_volume": float(gen_last.quote_volume.amount) if gen_last.quote_volume else 0.0,
                    "trades": gen_last.trade_count,
                }
        elif not klines and len(gen_klines) >= 20:
            klines = [
                {
                    "open_time": k.open_time,
                    "open": float(k.open.amount),
                    "high": float(k.high.amount),
                    "low": float(k.low.amount),
                    "close": float(k.close.amount),
                    "volume": float(k.volume.amount),
                    "close_time": k.close_time,
                    "quote_volume": float(k.quote_volume.amount) if k.quote_volume else 0.0,
                    "trades": k.trade_count,
                }
                for k in gen_klines
            ]
        features = self._compute_kline_features(symbol, interval, klines)
        if not features:
            raise MarketDataUnknownError(f"insufficient valid closed bars for {symbol}")
        return features

    def _compute_kline_features(self, symbol: str, interval: str, klines: list[dict]) -> dict[str, Any]:
        """从 K 线数据计算技术指标特征。同步和异步路径共享。"""
        if len(klines) < 20 or any(not self._is_valid_feature_bar(bar) for bar in klines):
            return {}

        closes = [k["close"] for k in klines]
        highs = [k["high"] for k in klines]
        lows = [k["low"] for k in klines]
        volumes = [k["volume"] for k in klines]
        n = len(closes)

        returns = [(closes[i] / closes[i - 1] - 1) for i in range(1, n)]

        sma_5 = sum(closes[-5:]) / 5
        sma_20 = sum(closes[-20:]) / 20
        sma_50 = sum(closes[-50:]) / min(50, n) if n >= 50 else sma_20

        recent_ret = returns[-20:] if len(returns) >= 20 else returns
        vol_20 = (sum(r**2 for r in recent_ret) / len(recent_ret)) ** 0.5
        ann_vol = vol_20 * (365 * 24) ** 0.5 if interval == "1h" else vol_20 * (365) ** 0.5

        tr_list = []
        for i in range(1, min(15, len(highs))):
            tr = max(
                highs[-i] - lows[-i],
                abs(highs[-i] - closes[-i - 1]),
                abs(lows[-i] - closes[-i - 1]),
            )
            tr_list.append(tr)
        atr = sum(tr_list) / len(tr_list) if tr_list else 0
        atr_pct = atr / closes[-1] * 100 if closes[-1] > 0 else 0

        vol_short = sum(volumes[-5:]) / 5
        vol_long = sum(volumes[-20:]) / 20
        vol_ratio = vol_short / vol_long if vol_long > 0 else 1.0

        trend_5 = closes[-1] / closes[-5] - 1 if n >= 5 else 0
        trend_20 = closes[-1] / closes[-20] - 1 if n >= 20 else 0

        gains = [max(r, 0) for r in returns[-15:]]
        losses = [abs(min(r, 0)) for r in returns[-15:]]
        avg_gain = sum(gains[-14:]) / 14 if len(gains) >= 14 else sum(gains) / max(len(gains), 1)
        avg_loss = sum(losses[-14:]) / 14 if len(losses) >= 14 else sum(losses) / max(len(losses), 1)
        rsi = 100 - (100 / (1 + avg_gain / avg_loss)) if avg_loss > 0 else 100

        bb_std = (sum((c - sma_20) ** 2 for c in closes[-20:]) / 20) ** 0.5
        bb_upper = sma_20 + 2 * bb_std
        bb_lower = sma_20 - 2 * bb_std

        highest_20 = max(highs[-20:])
        lowest_20 = min(lows[-20:])

        def _ema(values: list[float], period: int) -> float:
            if len(values) < period:
                return sum(values) / len(values)
            k = 2.0 / (period + 1)
            ema = sum(values[:period]) / period
            for v in values[period:]:
                ema = v * k + ema * (1 - k)
            return ema

        ema_12 = _ema(closes, 12)
        ema_26 = _ema(closes, 26)
        macd = ema_12 - ema_26
        macd_signal = ema_12 * 0.2 + ema_26 * 0.8 - (ema_12 - ema_26) * 0.2

        spread_bps_val: float | None = None
        ticker = self._last_ticker.get(symbol, {})
        if ticker:
            bid = float(ticker.get("bid", 0))
            ask = float(ticker.get("ask", 0))
            if bid > 0 and ask > 0:
                spread_bps_val = (ask - bid) / ((bid + ask) / 2) * 10000

        latest = klines[-1]
        features: dict[str, Any] = {
            "close": closes[-1],
            "prices": closes,  # 完整收盘价序列 — 供 alpha 因子（z-score / half-life）使用
            "sma_5": sma_5,
            "sma_20": sma_20,
            "sma_50": sma_50,
            "ann_volatility": ann_vol,
            "atr_pct": atr_pct,
            "vol_ratio": vol_ratio,
            "trend_5_pct": trend_5 * 100,
            "trend_20_pct": trend_20 * 100,
            "rsi_14": rsi,
            "n_candles": n,
            "bb_upper": bb_upper,
            "bb_mid": sma_20,
            "bb_lower": bb_lower,
            "highest_20": highest_20,
            "lowest_20": lowest_20,
            "ema_12": ema_12,
            "ema_26": ema_26,
            "macd": macd,
            "macd_signal": macd_signal,
            "spread_bps": spread_bps_val if spread_bps_val is not None else 2.0,  # 默认 2bps，不阻断信号生成
        }
        # Strategy execution is allowed only from an explicitly closed bar.
        # These fields are first-class evidence, not inferred defaults.  The
        # retrieval time is the earliest local claim for data availability; a
        # caller with stronger venue evidence may replace it before hashing.
        available_at = datetime.now(timezone.utc)
        features.update(
            {
                "bar_open_time": latest.get("open_time"),
                "bar_close_time": latest.get("close_time"),
                "bar_available_at": available_at,
                "bar_is_closed": latest.get("is_closed") is True,
            }
        )
        return features

    def get_feature_store(self) -> FeatureStore:
        return self._feature_store

    def get_last_ticker(self, symbol: str) -> dict:
        return self._last_ticker.get(symbol, {})

    def get_last_orderbook(self, symbol: str) -> dict:
        return self._last_orderbook.get(symbol, {})
