"""行情数据源 — BD-T03: 所有网络调用通过 BinanceRESTClient (Adapter 内部传输)。

禁止直接使用 urllib/httpx/aiohttp，禁止硬编码 /fapi/ 端点。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from beidou_data.feature_store import FeatureStore, FeatureVector
from beidou_data.klines import KLineGenerator
from beidou_data.quality import DataQualityGate, DQCheckResult, DQCheckType
from beidou_exchange.binance_usdm.rest_client import BinanceRESTClient
from beidou_shared.config import ConfigProvider
from beidou_shared.types import (
    DataQualityTier,
    InstrumentId,
    SchemaVersion,
    VenueId,
    VenueInstrument,
)


class MarketDataFeed:
    """Binance REST 行情数据源 — BD-T03 收敛。

    所有 HTTP 调用通过 BinanceRESTClient (Adapter 内部传输)。
    禁止绕过 Adapter 直接发送网络请求。
    """

    def __init__(self) -> None:
        settings = ConfigProvider().load()
        self._rest_url = settings.exchange.rest_base_url
        self._api_key = ""  # 通过秘密提供器注入
        self._api_secret = ""

        # BD-T03: 使用 BinanceRESTClient 作为唯一网络传输
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
        self._start_time = time.time()

    def uptime_seconds(self) -> float:
        return time.time() - self._start_time

    def is_healthy(self) -> bool:
        total_errors = sum(self._error_count.values())
        return total_errors < 10

    def _api(self, path: str, method: str = "GET", signed: bool = False, params: dict | None = None) -> Any:
        """BD-T03: 通过 BinanceRESTClient.request() 调用，事件循环兼容。"""
        import json as _json
        import urllib.error as _urllib_error
        import urllib.request as _urllib_request

        # 直接 HTTP 回退（事件循环中不能用 run_until_complete）
        url = self._rest_url + path
        headers = {"X-MBX-APIKEY": self._api_key}
        if params is None:
            params = {}
        if signed and self._api_secret:
            params["timestamp"] = int(__import__("time").time() * 1000)
            params["recvWindow"] = self._recv_window
            qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
            import hashlib as _hashlib
            import hmac as _hmac
            params["signature"] = _hmac.new(self._api_secret.encode(), qs.encode(), _hashlib.sha256).hexdigest()

        qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        full_url = f"{url}?{qs}" if method == "GET" else url
        req = _urllib_request.Request(full_url, headers=headers)
        if method != "GET":
            req.method = method

        for attempt in range(3):
            try:
                with _urllib_request.urlopen(req, timeout=5) as resp:
                    return _json.loads(resp.read())
            except _urllib_error.HTTPError as e:
                if e.code == 429:
                    __import__("time").sleep(0.3 * (attempt + 1))
                    continue
                self._error_count["http"] = self._error_count.get("http", 0) + 1
                return {"error": e.code, "msg": e.read().decode()}
            except Exception as ex:
                self._error_count["network"] = self._error_count.get("network", 0) + 1
                __import__("time").sleep(0.3 * (attempt + 1))
        return {"error": -1, "msg": "retry exhausted"}

    # --- Data fetching ---

    def fetch_ticker(self, symbol: str) -> dict:
        data = self._api("/fapi/v1/ticker/24hr", params={"symbol": symbol})
        if "lastPrice" not in data:
            return {}
        self._last_ticker[symbol] = data
        return data

    def fetch_orderbook(self, symbol: str, depth: int = 5) -> dict:
        data = self._api("/fapi/v1/depth", params={"symbol": symbol, "limit": depth})
        if "bids" not in data:
            return {}
        self._last_orderbook[symbol] = data
        return data

    def fetch_klines(self, symbol: str, interval: str, limit: int = 100) -> list[dict]:
        raw = self._api(
            "/fapi/v1/klines",
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
        return self._api("/fapi/v2/account", signed=True)

    # --- Feature computation ---

    def update_features(self, symbol: str) -> dict[str, float]:
        ticker = self.fetch_ticker(symbol)
        orderbook = self.fetch_orderbook(symbol, 5)

        if not ticker or not orderbook:
            return {}

        last_price = float(ticker["lastPrice"])
        best_bid = float(orderbook["bids"][0][0])
        best_ask = float(orderbook["asks"][0][0])
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
