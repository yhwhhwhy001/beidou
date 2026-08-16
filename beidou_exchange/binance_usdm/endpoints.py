"""Binance USDⓈ-M Futures API 端点路径常量。

集中管理所有 API 端点路径，禁止在代码中硬编码路径字符串。
使用方式: from beidou_exchange.binance_usdm.endpoints import Endpoint
"""

from __future__ import annotations


class Endpoint:
    """Binance USDⓈ-M Futures REST API 端点路径。

    所有 Binance API 访问必须使用这些常量，不得在业务代码中
    硬编码 "/fapi/v1/..." 字符串。
    """

    # 公共查询（无需签名）
    SERVER_TIME: str = "/fapi/v1/time"
    EXCHANGE_INFO: str = "/fapi/v1/exchangeInfo"
    TICKER_24HR: str = "/fapi/v1/ticker/24hr"
    DEPTH: str = "/fapi/v1/depth"
    KLINES: str = "/fapi/v1/klines"
    OPEN_INTEREST: str = "/fapi/v1/openInterest"
    FUNDING_RATE: str = "/fapi/v1/fundingRate"

    # 账户与持仓（需签名）
    POSITION_SIDE_DUAL: str = "/fapi/v1/positionSide/dual"
    POSITION_RISK: str = "/fapi/v2/positionRisk"
    LEVERAGE: str = "/fapi/v1/leverage"
    ACCOUNT: str = "/fapi/v2/account"
    BALANCE: str = "/fapi/v2/balance"

    # 订单管理（需签名）
    ORDER: str = "/fapi/v1/order"
    ALL_ORDERS: str = "/fapi/v1/allOrders"
    OPEN_ORDERS: str = "/fapi/v1/openOrders"
    ALGO_ORDER: str = "/fapi/v1/algoOrder"
    OPEN_ALGO_ORDERS: str = "/fapi/v1/openAlgoOrders"

    # 用户数据流
    LISTEN_KEY: str = "/fapi/v1/listenKey"


class WSEndpoint:
    """Binance USDⓈ-M Futures WebSocket 端点。"""

    # 公开行情流 (wss://)
    WS_BASE: str = "wss://fstream.binance.com"
    WS_BASE_TESTNET: str = "wss://demo-fstream.binance.com"

    # 组合流前缀
    COMBINED_STREAM: str = "/stream?streams="

    # 单流后缀 (小写)
    TICKER: str = "@ticker"
    MINI_TICKER: str = "@miniTicker"
    BOOK_TICKER: str = "@bookTicker"
    DEPTH5: str = "@depth5@100ms"
    DEPTH10: str = "@depth10@100ms"
    DEPTH20: str = "@depth20@100ms"
    KLINE_1M: str = "@kline_1m"
    KLINE_5M: str = "@kline_5m"
    KLINE_1H: str = "@kline_1h"
    AGG_TRADE: str = "@aggTrade"
    MARK_PRICE: str = "@markPrice@1s"
    FUNDING_RATE_WS: str = "@fundingRate"

    # 用户数据流 (需要 listenKey)
    USER_DATA: str = "/ws/"


# 客户端默认参数
DEFAULT_RECV_WINDOW_MS: int = 60_000
DEFAULT_MAX_RETRIES: int = 5
DEFAULT_HTTP_TIMEOUT: int = 30
DEFAULT_WEIGHT_LIMIT: int = 1200
DEFAULT_ORDER_LIMIT: int = 10
# BD-FIX (rate-budget): openOrders/openAlgoOrders 各消耗 40 权重。
# supervisor 每 30s 探测 + 近线每周期调用都会打这两个端点，短 TTL
# 响应缓存让同一事实在 TTL 内只消耗一次配额。
HIGH_WEIGHT_GET_CACHE_TTL: float = 10.0
CIRCUIT_BREAKER_THRESHOLD: int = 5
CIRCUIT_BREAKER_COOLDOWN: int = 60
