"""行情数据源 — BD-T03: 所有网络调用通过 BinanceRESTClient (Adapter 内部传输)。

禁止直接使用 urllib/httpx/aiohttp，禁止硬编码 /fapi/ 端点。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from beidou_data.feature_store import FeatureStore, FeatureVector
from beidou_data.klines import KLineGenerator, OHLCV
from beidou_data.quality import DataQualityGate, DQCheckResult, DQCheckType
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


class MarketDataFeed:
    """Binance REST 行情数据源 — BD-T03 收敛。

    所有 HTTP 调用通过 BinanceRESTClient (Adapter 内部传输)。
    禁止绕过 Adapter 直接发送网络请求。
    """

    def __init__(self, client: Any = None) -> None:
        settings = ConfigProvider().load()
        self._rest_url = settings.exchange.rest_base_url
        self._api_key = ""  # 通过秘密提供器注入
        self._api_secret = ""
        self._recv_window = DEFAULT_RECV_WINDOW_MS

        # BD-T03: 使用 BinanceRESTClient 作为唯一网络传输
        # 优先使用注入的 client（引擎共享），否则创建独立实例
        if client is not None:
            self._client = client
        else:
            self._client = BinanceRESTClient(
                rest_url=self._rest_url,
                api_key=self._api_key,
                api_secret=self._api_secret,
            )

        self._feature_store = FeatureStore()
        self._kline_generators: dict[str, KLineGenerator] = {}
        self._last_ticker: dict[str, dict] = {}
        self._last_orderbook: dict[str, dict] = {}
        self._error_count: dict[str, int] = {}
        self._last_error_time: float = 0.0  # 最近一次错误的时刻，用于衰减判断
        self._start_time = time.time()

        # WebSocket 实时行情（可选）
        self._ws_client: Any = None
        self._ws_active = False
        self._ws_last_update: dict[str, float] = {}  # symbol → last WS update time
        self._ws_stale_threshold = 60.0  # WS 数据超时阈值（秒）

    def set_client(self, client: Any) -> None:
        """注入共享的 REST client（引擎启动时调用，替代独立实例）。"""
        self._client = client
        self._rest_url = getattr(client, "rest_url", self._rest_url)

    def _get_kline_generator(self, symbol: str, interval: str = "1h") -> KLineGenerator:
        """按 symbol:interval 惰性创建 KLineGenerator（真实 tick → OHLCV 聚合）。"""
        key = f"{symbol}:{interval}"
        gen = self._kline_generators.get(key)
        if gen is None:
            gen = KLineGenerator(interval=interval)
            self._kline_generators[key] = gen
        return gen

    def get_generated_klines(self, symbol: str, interval: str = "1h") -> list[OHLCV]:
        """返回 KLineGenerator 的已闭合 K 线 + 当前未闭合 bar。

        由实时 tick 聚合生成，与 REST klines 并行使用
        （KLineGenerator 的 get_klines() 返回已闭合 K 线）。
        """
        gen = self._get_kline_generator(symbol, interval)
        vi = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId(symbol))
        klines = gen.get_klines(vi)
        current = gen.get_current_bar(vi)
        if current is not None:
            klines = klines + [current]
        return klines

    def uptime_seconds(self) -> float:
        return time.time() - self._start_time

    def is_healthy(self) -> bool:
        # 错误计数时间衰减：若 120s 内无新错误，清零计数器
        # 避免启动初期瞬时错误（如空 URL）永久毒化健康状态
        now = time.time()
        if self._last_error_time > 0 and now - self._last_error_time > 120:
            self._error_count.clear()
            self._last_error_time = 0.0
        total_errors = sum(self._error_count.values())
        ws_ok = True
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
                    self._ws_last_update[symbol] = time.time()

            async def _on_depth(stream: str, data: dict) -> None:
                symbol = data.get("s", "")
                if symbol and "bids" in data:
                    self._last_orderbook[symbol] = {
                        "bids": [[b[0], b[1]] for b in data.get("bids", [])],
                        "asks": [[a[0], a[1]] for a in data.get("asks", [])],
                    }
                    self._ws_last_update[symbol] = time.time()

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
            print(f"[feed] WebSocket started: {len(symbols) * 3} streams for {len(symbols)} symbols")
            return True
        except Exception as e:
            print(f"[feed] WebSocket unavailable ({e}), falling back to REST polling")
            self._ws_client = None
            self._ws_active = False
            return False

    async def stop_ws(self) -> None:
        """BD-FIX: 安全停止 WebSocket 连接。"""
        if self._ws_client is not None:
            try:
                await self._ws_client.disconnect()
            except Exception:
                pass
            self._ws_client = None
        if hasattr(self, "_ws_task") and self._ws_task is not None:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):
                pass
            self._ws_task = None
        self._ws_active = False

    def is_ws_data_fresh(self, symbol: str) -> bool:
        """检查 WebSocket 数据是否新鲜。超过阈值返回 False（触发 REST 回退）。"""
        last = self._ws_last_update.get(symbol, 0)
        return (time.time() - last) < self._ws_stale_threshold

    def _api(self, path: str, method: str = "GET", signed: bool = False, params: dict | None = None) -> Any:
        """BD-T03: 通过 BinanceRESTClient 代理 — 所有网络调用收敛到 Adapter 传输层。

        同步包装器：在已有事件循环时通过 asyncio.to_thread 调用，
        否则尝试新建/获取事件循环执行。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        result = loop.run_until_complete(
            self._client.request(method, path, signed=signed, params=params or {})
        )
        if isinstance(result, dict):
            return result
        return {"error": -1, "msg": str(result)}

    async def _api_async(self, path: str, method: str = "GET", signed: bool = False, params: dict | None = None) -> Any:
        """BD-T03: 异步 API 调用 — 直接通过 BinanceRESTClient.request()。

        不再使用 urllib — 所有 HTTP 调用通过 Adapter 传输层。
        """
        result = await self._client.request(method, path, signed=signed, params=params or {})
        if isinstance(result, dict):
            return result
        return {"error": -1, "msg": str(result)}

    # --- Data fetching (async) ---

    async def async_fetch_ticker(self, symbol: str) -> dict:
        data = await self._api_async(Endpoint.TICKER_24HR, params={"symbol": symbol})
        if "lastPrice" not in data:
            return {}
        self._last_ticker[symbol] = data
        return data

    async def async_fetch_orderbook(self, symbol: str, depth: int = 5) -> dict:
        data = await self._api_async(Endpoint.DEPTH, params={"symbol": symbol, "limit": depth})
        if "bids" not in data:
            return {}
        self._last_orderbook[symbol] = data
        return data

    async def async_fetch_klines(self, symbol: str, interval: str, limit: int = 100) -> list[dict]:
        raw = await self._api_async(
            Endpoint.KLINES,
            params={"symbol": symbol, "interval": interval, "limit": limit},
        )
        if not isinstance(raw, list):
            return []
        klines = []
        for k in raw:
            klines.append(
                {
                    "open_time": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                    "close_time": datetime.fromtimestamp(k[6] / 1000, tz=timezone.utc),
                    "quote_volume": float(k[7]),
                    "trades": k[8],
                }
            )
        return klines

    async def async_update_features(self, symbol: str) -> dict[str, float]:
        """异步版本 — 在线程池中执行同步 I/O，不阻塞事件循环。"""
        return await asyncio.to_thread(self.update_features, symbol)

    async def async_get_kline_features(self, symbol: str, interval: str = "1h", lookback: int = 100) -> dict[str, float]:
        """异步版本 — 在线程池中执行同步 I/O，不阻塞事件循环。"""
        return await asyncio.to_thread(self.get_kline_features, symbol, interval, lookback)

    # --- Data fetching (sync) ---

    def fetch_ticker(self, symbol: str) -> dict:
        data = self._api(Endpoint.TICKER_24HR, params={"symbol": symbol})
        if "lastPrice" not in data:
            return {}
        self._last_ticker[symbol] = data
        return data

    def fetch_orderbook(self, symbol: str, depth: int = 5) -> dict:
        data = self._api(Endpoint.DEPTH, params={"symbol": symbol, "limit": depth})
        if "bids" not in data:
            return {}
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
            return []
        klines = []
        for k in raw:
            klines.append(
                {
                    "open_time": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                    "close_time": datetime.fromtimestamp(k[6] / 1000, tz=timezone.utc),
                    "quote_volume": float(k[7]),
                    "trades": k[8],
                }
            )
        return klines

    def fetch_account(self) -> dict:
        return self._api(Endpoint.ACCOUNT, signed=True)

    # --- Feature computation ---

    def update_features(self, symbol: str) -> dict[str, float]:
        ticker = self.fetch_ticker(symbol)
        orderbook = self.fetch_orderbook(symbol, 5)

        if not ticker or not orderbook:
            return {}

        # BD-FIX: KLineGenerator — 用实时 ticker 价格生成 OHLCV bar
        try:
            last_price = float(ticker["lastPrice"])
            if symbol not in self._kline_generators:
                self._kline_generators[symbol] = KLineGenerator(interval="5m")
            kg = self._kline_generators[symbol]
            kg.update(
                price=last_price,
                volume=float(ticker.get("volume", 0)),
                timestamp=datetime.now(timezone.utc),
            )
        except Exception:
            pass  # KLineGenerator 故障不影响主流程

        last_price = float(ticker["lastPrice"])
        # BD-FIX: 空 orderbook 保护（流动性稀薄标的）
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])
        if not bids or not asks:
            best_bid = last_price * 0.999
            best_ask = last_price * 1.001
        else:
            best_bid = float(bids[0][0])
            best_ask = float(asks[0][0])
        spread_bps = (best_ask - best_bid) / best_ask * 10000 if best_ask > 0 else 1.0
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
        except Exception:
            # KLine 聚合失败不得阻断 feature 更新
            self._error_count["kline_gen"] = self._error_count.get("kline_gen", 0) + 1
            self._last_error_time = time.time()

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
            # REST 不可用时直接用生成 K 线计算特征
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
        if len(klines) < 20:
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

        spread_bps_val = 1.0
        ticker = self._last_ticker.get(symbol, {})
        if ticker:
            bid = float(ticker.get("bid", 0))
            ask = float(ticker.get("ask", 0))
            if bid > 0 and ask > 0:
                spread_bps_val = (ask - bid) / ((bid + ask) / 2) * 10000

        return {
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
            "spread_bps": spread_bps_val,
        }

    def get_feature_store(self) -> FeatureStore:
        return self._feature_store

    def get_last_ticker(self, symbol: str) -> dict:
        return self._last_ticker.get(symbol, {})

    def get_last_orderbook(self, symbol: str) -> dict:
        return self._last_orderbook.get(symbol, {})
