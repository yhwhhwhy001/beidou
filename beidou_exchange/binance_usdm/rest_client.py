"""Binance USDⓈ-M 异步 REST 客户端 — BD-02 Adapter 边界。

替代 engine.py 中的 urllib 直连，提供统一错误分类、限频退避和账户能力检查。
所有 Binance API 访问必须通过此客户端。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, cast

from beidou_exchange.binance_usdm.endpoints import (
    CIRCUIT_BREAKER_COOLDOWN,
    CIRCUIT_BREAKER_THRESHOLD,
    DEFAULT_HTTP_TIMEOUT,
    DEFAULT_MAX_RETRIES,
    DEFAULT_ORDER_LIMIT,
    DEFAULT_RECV_WINDOW_MS,
    DEFAULT_WEIGHT_LIMIT,
    HIGH_WEIGHT_GET_CACHE_TTL,
    Endpoint,
)
from beidou_exchange.binance_usdm.write_guard import classify_terminal_write
from beidou_exchange.core.error_taxonomy import (
    ErrorCategory,
    Result,
    classify_http_error,
)
from beidou_exchange.core.write_authority import TerminalWriteKind

logger = logging.getLogger(__name__)


@dataclass
class RateLimitState:
    """端点级限频状态。"""

    weight_used: int = 0
    weight_limit: int = DEFAULT_WEIGHT_LIMIT
    order_count: int = 0
    order_limit: int = DEFAULT_ORDER_LIMIT
    last_reset: float = field(default_factory=time.monotonic)
    consecutive_failures: int = 0
    circuit_open: bool = False
    circuit_open_until: float = 0.0


class BinanceRESTClient:
    """Binance USDⓈ-M 异步 REST 客户端。

    特性:
    - 统一 Result[T] 返回类型
    - 自动签名（HMAC-SHA256）
    - 指数退避 + Retry-After
    - 端点级熔断
    - 时钟偏差检测
    - 密钥自动脱敏
    """

    def __init__(  # nosec B107 - empty credentials are injected by the secret provider
        self,
        rest_url: str,
        api_key: str = "",
        api_secret: str = "",
        recv_window: int = DEFAULT_RECV_WINDOW_MS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        account_id: str = "UNKNOWN",
    ):
        self._rest_url = rest_url.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret
        self._recv_window = recv_window
        self._max_retries = max_retries
        self._account_id = str(account_id or "UNKNOWN")
        self._rate_state = RateLimitState()
        self._clock_offset_ms: int = 0  # 时钟偏差（服务端时间 - 本地时间）
        self._session: Any = None  # P1-019: 持久 httpx.Client
        # P2 修复: 懒创建的 session 会被多个 to_thread 调用并发首建 —— 加锁
        # 避免重复构造/泄漏 client。
        self._session_lock = threading.Lock()
        # BD-FIX (rate-budget): 高权重 GET 响应缓存（TTL 内一次传输）。
        # 缓存成功结果；失败不缓存。熔断窗口内缓存命中直接返回，
        # 使 supervisor 保护覆盖探针在熔断期间仍可消费最近的成功事实。
        self._get_cache: dict[tuple[str, tuple], tuple[float, Result]] = {}
        self._cacheable_gets = {Endpoint.OPEN_ORDERS, Endpoint.OPEN_ALGO_ORDERS}

    def reset_circuit_breaker(self) -> None:
        """重置客户端熔断器（启动恢复等关键阶段调用）。"""
        self._rate_state.circuit_open = False
        self._rate_state.consecutive_failures = 0
        self._rate_state.circuit_open_until = 0.0

    def is_circuit_breaker_open(self) -> bool:
        """检查断路器是否因连续失败而打开。

        用于区分「旧 session 残留的熔断」和「当前 session 的真实限频」。
        只有前者才应无条件重置；后者代表 venue 保护，不应清除。
        """
        if not self._rate_state.circuit_open:
            return False
        return time.monotonic() < self._rate_state.circuit_open_until

    def close(self) -> None:
        """Release the persistent HTTP client, if one was created."""
        if self._session is None:
            return
        session, self._session = self._session, None
        session.close()

    def _reset_transport_session(self) -> None:
        """丢弃持久 httpx.Client,下一次请求将以全新连接池重建。

        BD-FIX (transport outage self-heal): 2026-08-19 18:09 网络抖动
        (VPN/TUN 路径中断)后,旧进程内持久连接池整体失效 —— 所有请求
        handshake timeout / 空消息 PoolTimeout,持续 80+ 分钟;同一时刻
        新进程相同请求全部成功。半死连接无法自愈,必须在故障连击时
        主动重建 session,否则只能靠整进程重启恢复。
        """
        if self._session is None:
            return
        with contextlib.suppress(Exception):
            self._session.close()
        self._session = None

    def _mark_transport_failure(self) -> None:
        """记录一次传输层失败;达到熔断阈值时打开熔断并重建 session。

        BD-FIX (transport outage self-heal): 网络类异常连击说明传输层
        已不可信(连接池半死/解析陈旧),打开熔断冷却 60s 并丢弃
        session,冷却结束后第一次请求即用全新池重建 —— 免重启恢复。
        5xx 等 venue 已应答的错误不经过此路径,不误重建。
        """
        self._rate_state.consecutive_failures += 1
        if self._rate_state.consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
            self._rate_state.circuit_open = True
            self._rate_state.circuit_open_until = time.monotonic() + CIRCUIT_BREAKER_COOLDOWN
            self._reset_transport_session()

    # === 公共查询（无需签名）===

    async def get_server_time(self) -> Result[dict]:
        """获取服务端时间，用于时钟偏差校准。"""
        return await self._request("GET", Endpoint.SERVER_TIME)

    async def _resync_clock_offset(self) -> None:
        """-1021 后重取 server time 校正偏移（BD-FIX，I1 审查）。

        demo 服务器时钟落后本机超过 recvWindow 时，所有签名请求
        确定性 -1021；旧逻辑用同一偏移重新签名必然再败。
        """
        time_result = await self.get_server_time()
        if time_result.is_success():
            server_time = int(cast(Any, (time_result.data or {}).get("serverTime", 0)))
            local_time = int(time.time() * 1000)
            if server_time > 0:
                self._clock_offset_ms = server_time - local_time

    async def get_exchange_info(self, symbol: str | None = None) -> Result[dict]:
        """获取交易规则和交易对信息。"""
        params = {"symbol": symbol} if symbol else {}
        return await self._request("GET", Endpoint.EXCHANGE_INFO, params=params)

    async def get_ticker(self, symbol: str) -> Result[dict]:
        """获取24小时价格统计。"""
        return await self._request("GET", Endpoint.TICKER_24HR, params={"symbol": symbol})

    async def get_depth(self, symbol: str, limit: int = 20) -> Result[dict]:
        """获取订单簿深度。"""
        return await self._request("GET", Endpoint.DEPTH, params={"symbol": symbol, "limit": limit})

    async def get_klines(self, symbol: str, interval: str, limit: int = 500) -> Result[list]:
        """获取K线数据。"""
        return await self._request(
            "GET",
            Endpoint.KLINES,
            params={
                "symbol": symbol,
                "interval": interval,
                "limit": limit,
            },
        )

    # === 签名请求 ===

    async def get_position_mode(self) -> Result[dict]:
        """获取当前持仓模式 (ONE_WAY / HEDGE)。

        GET /fapi/v1/positionSide/dual (USER_DATA)
        返回: {"dualSidePosition": true} → HEDGE, false → ONE_WAY
        """
        return await self._request("GET", Endpoint.POSITION_SIDE_DUAL, signed=True)

    async def get_account(self) -> Result[dict]:
        """获取账户信息（余额、仓位）。"""
        return await self._request("GET", Endpoint.ACCOUNT, signed=True)

    async def get_open_orders(self, symbol: str | None = None) -> Result[list]:
        """获取当前挂单。"""
        params = {"symbol": symbol} if symbol else {}
        return await self._request("GET", Endpoint.OPEN_ORDERS, signed=True, params=params)

    async def get_order(self, symbol: str, order_id: int) -> Result[dict]:
        """查询单个订单状态。"""
        return await self._request(
            "GET",
            Endpoint.ORDER,
            signed=True,
            params={
                "symbol": symbol,
                "orderId": order_id,
            },
        )

    async def create_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: str,
        price: str | None = None,
        time_in_force: str | None = None,
        reduce_only: str | None = None,
        client_order_id: str | None = None,
        iceberg_qty: str | None = None,
    ) -> Result[dict]:
        """创建订单。

        iceberg_qty:ICEBERG 可见切片量(Binance USDⓈ-M 支持,LIMIT+GTC;
        仅显示 icebergQty 在盘口,每笔最多成交该量 —— 部分成交确定性来源,
        Ruling-14);传入时写入 params["icebergQty"],不传不影响既有调用。
        """
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "quantity": quantity,
        }
        if price:
            params["price"] = price
            params["timeInForce"] = time_in_force or "GTC"
        if reduce_only:
            params["reduceOnly"] = reduce_only
        if client_order_id:
            params["newClientOrderId"] = client_order_id
        if iceberg_qty:
            params["icebergQty"] = iceberg_qty
        return await self._request("POST", Endpoint.ORDER, signed=True, params=params)

    async def cancel_order(self, symbol: str, order_id: int) -> Result[dict]:
        """取消订单。"""
        return await self._request(
            "DELETE",
            Endpoint.ORDER,
            signed=True,
            params={
                "symbol": symbol,
                "orderId": order_id,
            },
        )

    async def get_open_algo_orders(self, symbol: str | None = None) -> Result[list]:
        """查询当前条件单；返回非 list 时由 Adapter 判为 UNKNOWN。"""

        params = {"symbol": symbol} if symbol else {}
        return await self._request("GET", Endpoint.OPEN_ALGO_ORDERS, signed=True, params=params)

    async def create_algo_order(self, params: dict[str, Any]) -> Result[dict]:
        """创建 Algo 单；调用者必须提供已校验的完整参数。"""

        return await self._request("POST", Endpoint.ALGO_ORDER, signed=True, params=dict(params))

    async def cancel_algo_order(self, symbol: str, algo_id: int) -> Result[dict]:
        """撤销单个 Algo 单，不使用 cancel-all 旁路。"""

        return await self._request(
            "DELETE",
            Endpoint.ALGO_ORDER,
            signed=True,
            params={"symbol": symbol, "algoId": algo_id},
        )

    # === 用户数据流 ===

    async def create_listen_key(self) -> Result[dict]:
        """创建用户数据流 listenKey。"""
        return await self._request("POST", Endpoint.LISTEN_KEY, signed=True)

    async def keepalive_listen_key(self, listen_key: str | None = None) -> Result[dict]:
        """续期 listenKey；缺少 key 时显式 UNKNOWN，不发送无效请求。"""

        if not str(listen_key or "").strip():
            return Result.failure(
                "listenKey is required for keepalive",
                category=ErrorCategory.UNKNOWN,
                source="binance_user_stream",
            )
        return await self._request(
            "PUT",
            Endpoint.LISTEN_KEY,
            signed=True,
            params={"listenKey": str(listen_key)},
        )

    # === 通用请求（兼容遗留 _api 调用） ===

    async def request(self, method: str, path: str, signed: bool = False, params: dict | None = None) -> Result:
        """通用异步请求 — 替代 engine._api() 的直连 urllib。

        所有 Binance API 端点统一通过此方法访问，确保错误分类、限频和时钟偏差一致。
        """
        return await self._request(method, path, signed, params)

    # === 账户能力检查 ===

    async def check_account_capability(self) -> Result[dict]:
        """账户能力检查。

        验证: 读余额、读仓位、读挂单、交易权限、无提款权限、IP白名单、时钟偏差。
        任一失败返回 FAIL。
        """
        capabilities: dict[str, Any] = {
            "can_read_balance": False,
            "can_read_positions": False,
            "can_read_orders": False,
            "can_trade": False,
            "can_withdraw": None,
            "exchange_info_available": False,
            "ip_whitelisted": False,
            "clock_skew_ms": 0,
        }

        # 1. 时钟偏差
        time_result = await self.get_server_time()
        if time_result.is_success():
            server_time = (time_result.data or {}).get("serverTime", 0)
            local_time = int(time.time() * 1000)
            skew = server_time - local_time
            capabilities["clock_skew_ms"] = skew
            # P1-018: 闭环写入时钟偏差，后续签名请求使用校准后的时间戳
            self._clock_offset_ms = skew

        # 2. 账户信息
        account = await self.get_account()
        if account.is_success():
            account_data = account.data if isinstance(account.data, dict) else {}
            capabilities["can_read_balance"] = "totalWalletBalance" in account_data or "assets" in account_data
            capabilities["can_read_positions"] = "positions" in account_data
            venue_can_trade = account_data.get("canTrade")
            if isinstance(venue_can_trade, bool):
                capabilities["can_trade"] = venue_can_trade
            venue_can_withdraw = account_data.get("canWithdraw")
            # Missing/non-boolean permission facts are UNKNOWN, never safe.
            capabilities["can_withdraw"] = venue_can_withdraw if isinstance(venue_can_withdraw, bool) else None

        # 3. 挂单查询
        orders = await self.get_open_orders()
        capabilities["can_read_orders"] = isinstance(orders.data, list)

        # 4. 交易规则可用性（账户 canTrade 仍是权威权限事实）
        exchange_info = await self.get_exchange_info()
        if exchange_info.is_success():
            capabilities["exchange_info_available"] = True

        all_ok = all(
            [
                capabilities["can_read_balance"],
                capabilities["can_read_positions"],
                capabilities["can_read_orders"],
                capabilities["can_trade"] is True,
                capabilities["can_withdraw"] is False,
                capabilities.get("exchange_info_available") is True,
                abs(capabilities["clock_skew_ms"]) < 5000,
            ]
        )

        return (
            Result.ok(capabilities)
            if all_ok
            else Result.failure(
                f"Account capability check failed: {capabilities}",
                category=ErrorCategory.UNKNOWN,
                raw=capabilities,
                source="binance_account_capability",
            )
        )

    # === 内部实现 ===

    def _update_rate_state_from_headers(self, headers: dict) -> None:
        """P1-016: 从 Binance 响应头解析并更新限频状态。

        X-MBX-USED-WEIGHT-(intervalNum)(intervalLetter) 返回当前已用权重。
        例如 X-MBX-USED-WEIGHT-1M 表示1分钟窗口内已用权重。
        """
        for key, value in headers.items():
            key_lower = key.lower()
            if key_lower.startswith("x-mbx-used-weight-"):
                try:
                    self._rate_state.weight_used = int(value)
                except (TypeError, ValueError):
                    logger.warning("Invalid exchange rate-limit header %s", key)
            elif key_lower == "x-mbx-order-count-1m":
                try:
                    parts = value.split(";")
                    used = int(parts[0]) if parts else 0
                    limit = int(parts[1]) if len(parts) > 1 else self._rate_state.order_limit
                    self._rate_state.order_count = used
                    self._rate_state.order_limit = limit
                except (TypeError, ValueError, IndexError):
                    pass
        # 如果 weight_used 接近 limit，记录告警
        if self._rate_state.weight_limit > 0:
            ratio = self._rate_state.weight_used / self._rate_state.weight_limit
            if ratio > 0.8:
                import logging

                _logger = logging.getLogger(__name__)
                _logger.warning(
                    "Binance rate limit near capacity: %d/%d (%.0f%%)",
                    self._rate_state.weight_used,
                    self._rate_state.weight_limit,
                    ratio * 100,
                )

    async def _request(self, method: str, path: str, signed: bool = False, params: dict | None = None) -> Result:
        """发送 HTTP 请求并返回 Result[T]。HTTP 调用在线程池中执行，不阻塞事件循环。"""
        base_params = dict(params or {})
        method = str(method).upper()

        # BD-FIX (rate-budget): 高权重 GET 的短 TTL 响应缓存。缓存命中
        # 时直接返回最近的成功事实，不产生网络请求——包括熔断窗口内，
        # 让保护覆盖探针在熔断期间仍可消费最近一次成功快照。
        cache_key: tuple[str, tuple] | None = None
        if method == "GET" and path in self._cacheable_gets:
            cache_key = (path, tuple(sorted(base_params.items())))
            cached = self._get_cache.get(cache_key)
            if cached is not None and time.monotonic() - cached[0] <= HIGH_WEIGHT_GET_CACHE_TTL:
                return cached[1]
        write_request = classify_terminal_write(
            method,
            path,
            base_params,
            account_id=self._account_id,
        )
        # 合并语义(codex/full-system-optimization + main testnet 实装):
        # - 默认 HARD_HOLD(opt 安全语义):所有 terminal write 在 transport
        #   层拦截,等待 write authority 接线(registry baseline_policy)
        # - BEIDOU_TERMINAL_WRITE_HOLD=unknown-only(main testnet 实装):
        #   仅未分类突变端点(UNKNOWN)hold,已知 kind 放行 —— 写能力
        #   不退化(下单/取消/减仓),authority 接线属后续演进
        _hold_mode = os.environ.get("BEIDOU_TERMINAL_WRITE_HOLD", "hard")
        producer_session_write = (
            os.environ.get("BEIDOU_G5_PRODUCER") == "1"
            and write_request is not None
            and write_request.kind is TerminalWriteKind.SESSION_CONTROL
        )
        if write_request is not None and not producer_session_write and (
            _hold_mode == "hard" or write_request.kind is TerminalWriteKind.UNKNOWN
        ):
            reason = (
                "UNCLASSIFIED_TERMINAL_WRITE"
                if write_request.kind is TerminalWriteKind.UNKNOWN
                else "WRITE_CAPABILITY_REGISTRY_INCOMPLETE"
            )
            return Result.failure(
                "Terminal writes are held until the capability registry is complete",
                category=ErrorCategory.PERMISSION_DENIED,
                retryable=False,
                raw={"reason": reason, "kind": write_request.kind.value},
                source="binance_rest_write_hold",
            )

        # A writable Testnet request has the same ambiguity and rate-limit
        # semantics as any other venue write.  Environment labels must never
        # bypass the shared transport circuit.
        if self._rate_state.circuit_open:
            if time.monotonic() < self._rate_state.circuit_open_until:
                return Result.failure(
                    "Circuit breaker open",
                    category=ErrorCategory.RATE_LIMIT,
                    retryable=False,
                    raw={"reason": "CIRCUIT_BREAKER_OPEN"},
                    source="binance_rest",
                )
            self._rate_state.circuit_open = False
            self._rate_state.consecutive_failures = 0

        url = self._rest_url + path

        for attempt in range(self._max_retries):
            try:
                import urllib.error
                import urllib.request

                # Rebuild and re-sign every attempt.  A timestamp/signature is
                # a per-attempt venue fact and must not be reused after backoff.
                request_params = dict(base_params)
                if signed:
                    request_params["timestamp"] = int(time.time() * 1000) + self._clock_offset_ms
                    request_params["recvWindow"] = self._recv_window
                    signing_qs = "&".join(f"{k}={v}" for k, v in sorted(request_params.items()))
                    request_params["signature"] = hmac.new(
                        self._api_secret.encode(),
                        signing_qs.encode(),
                        hashlib.sha256,
                    ).hexdigest()
                qs = "&".join(f"{k}={v}" for k, v in sorted(request_params.items()))

                if method in {"POST", "PUT"}:
                    req = urllib.request.Request(url, data=qs.encode(), method=method)
                else:
                    full_url = url + "?" + qs if qs else url
                    req = urllib.request.Request(full_url, method=method)

                req.add_header("X-MBX-APIKEY", self._api_key)

                # 在线程池中执行同步 HTTP，不阻塞事件循环
                # P1-019: 使用持久 httpx session 避免每次新建连接
                if self._session is None:
                    with self._session_lock:
                        if self._session is None:
                            import httpx

                            self._session = httpx.Client(
                                timeout=DEFAULT_HTTP_TIMEOUT, follow_redirects=False, http2=False
                            )
                body, resp_headers = await asyncio.to_thread(_sync_urlopen, req, DEFAULT_HTTP_TIMEOUT, self._session)
                data = json.loads(body)
                self._rate_state.consecutive_failures = 0

                # P1-016: 从响应头解析限频状态
                self._update_rate_state_from_headers(resp_headers)

                if isinstance(data, dict) and "code" in data:
                    # BD-FIX: 交易所部分端点(如 algoOrder 取消)的成功
                    # 响应里 code 是字符串("200"),旧代码 `code < 0` 触发
                    # str/int TypeError 并被误报 WRITE_UNKNOWN(交易所侧
                    # 实际已生效)。先规范化为 int 再比较。
                    try:
                        binance_code = int(data.get("code") or 0)
                    except (TypeError, ValueError):
                        binance_code = 0
                    if binance_code < 0:
                        category, retryable = classify_http_error(200, "", binance_code)
                        return Result.failure(
                            data.get("msg", str(data)),
                            http_status=200,
                            category=category,
                            retryable=retryable,
                            raw=dict(data),
                            source="binance_rest",
                        )

                success_result = Result.ok(data)
                if cache_key is not None:
                    self._get_cache[cache_key] = (time.monotonic(), success_result)
                return success_result

            except urllib.error.HTTPError as e:
                http_status = e.code
                try:
                    error_body = e.read().decode() if e.fp else ""
                finally:
                    # HTTPError owns the temporary response file.  Close it
                    # on every retry/failure path so a long-running worker
                    # cannot accumulate unclosed 4xx/5xx response handles.
                    e.close()

                binance_code = 0
                err_data: Any = None
                try:
                    err_data = json.loads(error_body)
                    raw_code = err_data.get("code", 0) if isinstance(err_data, dict) else 0
                    try:
                        binance_code = int(raw_code or 0)
                    except (TypeError, ValueError):
                        binance_code = 0
                except Exception:
                    binance_code = 0

                raw_error = dict(err_data) if isinstance(err_data, dict) else {"body": error_body[:200]}
                error_message = (
                    str(err_data.get("msg", ""))
                    if isinstance(err_data, dict) and err_data.get("msg")
                    else error_body[:200]
                )

                category, retryable = classify_http_error(http_status, "", binance_code)

                # A generic venue 5xx on a write is not a confirmed failure:
                # the request may already have reached the matching engine.
                # Preserve UNKNOWN and require an exact client-id/readback
                # reconciliation before any new write attempt.
                if method in {"POST", "PUT", "DELETE"} and http_status >= 500:
                    raw_error.update(
                        {
                            "method": method,
                            "path": path,
                            "write_safety": "QUERY_BEFORE_RETRY_REQUIRED",
                        }
                    )
                    self._rate_state.consecutive_failures += 1
                    # M11-F03: 调试 print 改结构化 logger(原 locals() 取变量脆弱)
                    logger.warning(
                        "[rest] FAIL x%d: %s %s -> %s",
                        self._rate_state.consecutive_failures,
                        method,
                        path,
                        str(locals().get("error_message", "n/a"))[:100],
                    )
                    return Result.failure(
                        f"WRITE_UNKNOWN: {error_message or f'HTTP {http_status}'}",
                        http_status=http_status,
                        category=ErrorCategory.UNKNOWN,
                        retryable=False,
                        raw=raw_error,
                        source="binance_rest",
                    )

                if category == ErrorCategory.RATE_LIMIT:
                    # Retry-After is advisory; malformed values must not turn
                    # a venue rate-limit response into a generic NETWORK
                    # result after the retry budget is exhausted.
                    retry_after_raw = e.headers.get("Retry-After", attempt + 1)
                    try:
                        # BD-FIX: Retry-After 封顶 30s（M2 审查：超大值
                        # 如 86400 会让每次重试睡一天级）
                        retry_after = min(30.0, max(0.0, float(retry_after_raw)))
                    except (TypeError, ValueError):
                        retry_after = float(attempt + 1)
                    if binance_code == -1021:
                        with contextlib.suppress(Exception):
                            await self._resync_clock_offset()
                    if attempt < self._max_retries - 1:
                        await asyncio.sleep(retry_after)
                        continue
                    self._rate_state.consecutive_failures += 1
                    # M11-F03: 调试 print 改结构化 logger(原 locals() 取变量脆弱)
                    logger.warning(
                        "[rest] FAIL x%d: %s %s -> %s",
                        self._rate_state.consecutive_failures,
                        method,
                        path,
                        str(locals().get("error_message", "n/a"))[:100],
                    )
                    if self._rate_state.consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
                        self._rate_state.circuit_open = True
                        self._rate_state.circuit_open_until = time.monotonic() + CIRCUIT_BREAKER_COOLDOWN
                    return Result.failure(
                        error_message or "Rate limit response",
                        http_status=http_status,
                        category=category,
                        retryable=retryable,
                        raw=raw_error,
                        source="binance_rest",
                    )

                if retryable and attempt < self._max_retries - 1:
                    # BD-FIX: -1021（时钟偏差）先重取 server time 校正
                    # 偏移再重试 —— 旧逻辑用同一偏移重新签名必然再败
                    # （I1 审查：demo 服务器时钟落后超 recvWindow 时
                    # 所有签名请求确定性 -1021）
                    wait = 0.5 * (2**attempt)
                    await asyncio.sleep(wait)
                    continue

                # BD-FIX (rate-budget): 业务拒绝（订单不存在 -2013、参数
                # 拒绝、余额/保证金不足等）是确定性的业务事实，不是 venue
                # 故障 —— 不得计入熔断计数。启动解析遗留 UNKNOWN 意图时
                # 每笔查询都是业务拒绝，旧逻辑 5 连败即打开熔断器并短路
                # 全部请求（8-16 实测恶性循环）。熔断只针对 venue 侧
                # 可恢复错误（5xx/网络/限频）。
                if category == ErrorCategory.EXCHANGE_UNAVAILABLE:
                    self._rate_state.consecutive_failures += 1
                    # M11-F03: 调试 print 改结构化 logger(原 locals() 取变量脆弱)
                    logger.warning(
                        "[rest] FAIL x%d: %s %s -> %s",
                        self._rate_state.consecutive_failures,
                        method,
                        path,
                        str(locals().get("error_message", "n/a"))[:100],
                    )
                    if self._rate_state.consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
                        self._rate_state.circuit_open = True
                        self._rate_state.circuit_open_until = time.monotonic() + CIRCUIT_BREAKER_COOLDOWN

                return Result.failure(
                    error_message or f"HTTP {http_status}",
                    http_status=http_status,
                    category=category,
                    retryable=retryable,
                    raw=raw_error,
                    source="binance_rest",
                )

            except Exception as e:
                # PKG11 (BDS-P0-012): 写请求盲重试防护
                # POST/DELETE 在超时/网络故障时不得盲重试 — 可能导致重复订单。
                # 标记为 UNKNOWN 状态，调用方必须查询订单状态 (queryOrder) 后决定 adopt/retry。
                is_write = method in ("POST", "PUT", "DELETE")
                if is_write and attempt >= 0:  # 写请求第一次失败即停止
                    self._mark_transport_failure()
                    # M11-F03: 调试 print 改结构化 logger(原 locals() 取变量脆弱)
                    logger.warning(
                        "[rest] FAIL x%d: %s %s -> %s",
                        self._rate_state.consecutive_failures,
                        method,
                        path,
                        str(locals().get("error_message", "n/a"))[:100],
                    )
                    return Result.failure(
                        f"WRITE_UNKNOWN: {str(e)[:180]}",
                        category=ErrorCategory.NETWORK,
                        retryable=False,  # 写请求不自动重试 — 调用方先查询再决定
                        raw={
                            "exception_type": type(e).__name__,
                            "method": method,
                            "path": path,
                            "write_safety": "QUERY_BEFORE_RETRY_REQUIRED",
                        },
                        source="binance_rest",
                    )
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
                self._mark_transport_failure()
                # M11-F03: 调试 print 改结构化 logger(原 locals() 取变量脆弱)
                logger.warning(
                    "[rest] FAIL x%d: %s %s -> %s",
                    self._rate_state.consecutive_failures,
                    method,
                    path,
                    str(locals().get("error_message", "n/a"))[:100],
                )
                return Result.failure(
                    str(e)[:200],
                    category=ErrorCategory.NETWORK,
                    retryable=True,
                    raw={"exception_type": type(e).__name__},
                    source="binance_rest",
                )

        return Result.failure(
            "Max retries exhausted",
            category=ErrorCategory.NETWORK,
            retryable=True,
            source="binance_rest",
        )


def _sync_urlopen(req: urllib.request.Request, timeout: int, _session: Any = None) -> tuple[bytes, dict]:
    """同步 HTTP 请求 — 供 asyncio.to_thread 在线程池中调用。

    P1-019: 接受可选的持久 httpx.Client 避免每次新建连接。
    P1-016: 返回 (content, headers) 以支持解析限频头。
    """

    import httpx

    headers = dict(req.header_items())
    method = req.get_method().upper()
    body = req.data
    client = _session
    close_after = client is None
    if client is None:
        client = httpx.Client(timeout=timeout, follow_redirects=False, http2=False)
    try:
        response = client.request(method, req.full_url, headers=headers, content=body)
    except httpx.HTTPError:
        if close_after and client is not None:
            try:
                client.close()
            except Exception as close_exc:
                logger.warning("Temporary HTTP client close failed after request error: %s", type(close_exc).__name__)
        raise
    if response.status_code >= 400:
        err = urllib.error.HTTPError(
            req.full_url,
            response.status_code,
            response.reason_phrase,
            cast(Any, dict(response.headers)),
            BytesIO(response.content),
        )
        if close_after and client is not None:
            try:
                client.close()
            except Exception as close_exc:
                logger.warning("Temporary HTTP client close failed after HTTP error: %s", type(close_exc).__name__)
        raise err
    content = response.content
    resp_headers = dict(response.headers)
    if close_after and client is not None:
        try:
            client.close()
        except Exception as close_exc:
            logger.warning("Temporary HTTP client close failed: %s", type(close_exc).__name__)
    return content, resp_headers
