"""Async Binance USDⓈ-M REST client: HMAC signing, clock resync, retry/backoff, ambiguity semantics.

Ambiguity rule: a *read* that fails is retried; a *write* whose request was
sent but whose response is unknown (timeout after send, 5xx) raises
:class:`OrderOutcomeUnknown` so the caller queries by client id instead of
re-submitting.  Deterministic venue rejections raise :class:`VenueError`.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import random
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

import httpx

from beidou_exchange.guard import WriteGuard
from beidou_shared.types import OrderOutcomeUnknown, VenueError

logger = logging.getLogger(__name__)

RETRYABLE_HTTP = frozenset({418, 429, 500, 502, 503, 504})
RETRYABLE_CODES = frozenset({-1003, -1015, -1021, -1001, -1007, -1008})
CLOCK_SKEW_CODE = -1021


class BinanceRestClient:
    def __init__(
        self,
        rest_url: str,
        api_key: str,
        api_secret: str,
        *,
        guard: WriteGuard | None = None,
        recv_window_ms: int = 10_000,
        timeout_seconds: float = 15.0,
        max_retries: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.rest_url = rest_url.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret.encode("utf-8")
        self._guard = guard
        self._recv_window = int(recv_window_ms)
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(
            base_url=self.rest_url,
            timeout=timeout_seconds,
            headers={"X-MBX-APIKEY": api_key, "User-Agent": "beidou-v5"},
            transport=transport,
        )
        self.clock_offset_ms = 0
        self.used_weight = 0
        self.consecutive_transport_failures = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- clock ------------------------------------------------------------
    async def sync_clock(self) -> int:
        payload = await self.get("/fapi/v1/time")
        server = int(payload["serverTime"])
        self.clock_offset_ms = server - int(time.time() * 1000)
        return self.clock_offset_ms

    def _timestamp_ms(self) -> int:
        return int(time.time() * 1000) + self.clock_offset_ms

    def _sign(self, params: Mapping[str, Any]) -> dict[str, Any]:
        signed = {k: v for k, v in params.items() if v is not None}
        signed["timestamp"] = self._timestamp_ms()
        signed["recvWindow"] = self._recv_window
        query = urlencode(signed, doseq=True)
        signed["signature"] = hmac.new(self._api_secret, query.encode("utf-8"), hashlib.sha256).hexdigest()
        return signed

    # --- public API ---------------------------------------------------------
    async def get(self, path: str, params: Mapping[str, Any] | None = None, *, signed: bool = False) -> Any:
        return await self._request("GET", path, dict(params or {}), signed=signed)

    async def post(self, path: str, params: Mapping[str, Any], *, signed: bool = True) -> Any:
        return await self._request("POST", path, dict(params), signed=signed)

    async def delete(self, path: str, params: Mapping[str, Any], *, signed: bool = True) -> Any:
        return await self._request("DELETE", path, dict(params), signed=signed)

    # --- core ---------------------------------------------------------------
    async def _request(self, method: str, path: str, params: dict[str, Any], *, signed: bool) -> Any:
        method = method.upper()
        mutating = method in {"POST", "PUT", "DELETE"}
        if mutating and self._guard is not None:
            self._guard.authorize(method, path, params)
        delay = 0.5
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            payload = self._sign(params) if signed else {k: v for k, v in params.items() if v is not None}
            try:
                if method == "GET":
                    response = await self._client.get(path, params=payload)
                elif method == "DELETE":
                    response = await self._client.delete(path, params=payload)
                else:
                    response = await self._client.request(method, path, data=payload)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # nothing was sent: safe to retry for reads and writes alike
                last_error = exc
                self.consecutive_transport_failures += 1
            except httpx.TransportError as exc:
                self.consecutive_transport_failures += 1
                if mutating:
                    raise OrderOutcomeUnknown(
                        f"{type(exc).__name__} after sending {method} {path}",
                        client_order_id=str(params.get("newClientOrderId") or params.get("origClientOrderId") or ""),
                        symbol=str(params.get("symbol", "")),
                    ) from exc
                last_error = exc
            else:
                self.consecutive_transport_failures = 0
                self._read_rate_headers(response.headers)
                outcome = self._interpret(response, method, path, params)
                if outcome.retry_after is None:
                    return outcome.data
                if outcome.resync_clock:
                    await self.sync_clock()
                last_error = outcome.error
                delay = max(delay, outcome.retry_after)
            if attempt >= self._max_retries:
                break
            await asyncio.sleep(delay + random.uniform(0.0, 0.25))
            delay = min(delay * 2.0, 20.0)
        if isinstance(last_error, VenueError):
            raise last_error
        raise VenueError(f"{method} {path} failed after {self._max_retries + 1} attempts: {last_error}", retryable=True)

    class _Outcome:
        __slots__ = ("data", "error", "resync_clock", "retry_after")

        def __init__(
            self,
            data: Any = None,
            error: VenueError | None = None,
            retry_after: float | None = None,
            resync_clock: bool = False,
        ) -> None:
            self.data = data
            self.error = error
            self.retry_after = retry_after
            self.resync_clock = resync_clock

    def _interpret(self, response: httpx.Response, method: str, path: str, params: Mapping[str, Any]) -> _Outcome:
        mutating = method in {"POST", "PUT", "DELETE"}
        status = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = None
        code = (
            int(body.get("code", 0))
            if isinstance(body, dict) and str(body.get("code", "")).lstrip("-").isdigit()
            else 0
        )
        message = str(body.get("msg", "")) if isinstance(body, dict) else response.text[:200]
        if status in RETRYABLE_HTTP:
            retry_after = float(response.headers.get("Retry-After", "0") or 0) or 2.0
            if status >= 500 and mutating:
                raise OrderOutcomeUnknown(
                    f"HTTP {status} for {method} {path}",
                    client_order_id=str(params.get("newClientOrderId") or params.get("origClientOrderId") or ""),
                    symbol=str(params.get("symbol", "")),
                )
            return self._Outcome(
                error=VenueError(f"HTTP {status}: {message}", code=code or status, retryable=True),
                retry_after=retry_after,
            )
        if status >= 400 or code < 0:
            if code == CLOCK_SKEW_CODE:
                return self._Outcome(
                    error=VenueError(message, code=code, retryable=True), retry_after=0.2, resync_clock=True
                )
            if code in RETRYABLE_CODES:
                return self._Outcome(error=VenueError(message, code=code, retryable=True), retry_after=2.0)
            raise VenueError(
                f"{message or 'venue rejected request'} (code {code}, http {status})",
                code=code or status,
                retryable=False,
            )
        return self._Outcome(data=body)

    def _read_rate_headers(self, headers: httpx.Headers) -> None:
        for key, value in headers.items():
            if key.lower().startswith("x-mbx-used-weight-"):
                try:
                    self.used_weight = int(value)
                except ValueError:
                    continue
