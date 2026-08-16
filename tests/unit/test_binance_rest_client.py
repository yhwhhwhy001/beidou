"""Deterministic contract tests for the Binance REST adapter boundary."""

from __future__ import annotations

import asyncio
import json
from io import BytesIO
from typing import ClassVar
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from beidou_exchange.binance_usdm import rest_client as rest_module
from beidou_exchange.binance_usdm.endpoints import Endpoint
from beidou_exchange.binance_usdm.rest_client import BinanceRESTClient
from beidou_exchange.core.error_taxonomy import ErrorCategory, Result


def test_sync_urlopen_handles_large_response_and_preserves_http_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        def __init__(self, status_code: int, content: bytes) -> None:
            self.status_code = status_code
            self.reason_phrase = "Service Unavailable"
            self.headers = {"content-type": "application/json"}
            self.content = content

    class FakeClient:
        responses: ClassVar[list[FakeResponse]] = [
            FakeResponse(200, b'{"ok":true}'),
            FakeResponse(503, b'{"msg":"unavailable"}'),
        ]

        def __init__(self, **_kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def request(self, method, url, *, headers, content):
            assert method == "POST"
            assert url == "https://demo.example/order"
            assert {key.lower(): value for key, value in headers.items()}["x-mbx-apikey"] == "api-key"
            assert content == b"symbol=BTCUSDT"
            return self.responses.pop(0)

    import httpx

    monkeypatch.setattr(httpx, "Client", FakeClient)
    request = Request(
        "https://demo.example/order",
        data=b"symbol=BTCUSDT",
        headers={"X-MBX-APIKEY": "api-key"},
        method="POST",
    )
    body, _headers = rest_module._sync_urlopen(request, 3)
    assert body == b'{"ok":true}'
    with pytest.raises(HTTPError) as raised:
        rest_module._sync_urlopen(request, 3)
    assert raised.value.code == 503
    assert raised.value.read() == b'{"msg":"unavailable"}'
    raised.value.close()


def test_convenience_methods_share_one_request_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    client = BinanceRESTClient("https://demo.example", api_secret="secret")  # noqa: S106 - deterministic test key
    calls: list[tuple[str, str, bool, dict]] = []

    async def fake_request(method: str, path: str, signed: bool = False, params: dict | None = None) -> Result:
        calls.append((method, path, signed, dict(params or {})))
        return Result.ok({"ok": True})

    monkeypatch.setattr(client, "_request", fake_request)
    asyncio.run(client.get_server_time())
    asyncio.run(client.get_exchange_info("BTCUSDT"))
    asyncio.run(client.get_ticker("BTCUSDT"))
    asyncio.run(client.get_depth("BTCUSDT", 5))
    asyncio.run(client.get_klines("BTCUSDT", "1m", 3))
    asyncio.run(client.get_position_mode())
    asyncio.run(client.get_account())
    asyncio.run(client.get_open_orders("BTCUSDT"))
    asyncio.run(client.get_order("BTCUSDT", 7))
    asyncio.run(client.create_order("BTCUSDT", "BUY", "LIMIT", "1", "100", "GTC", "true", "cid"))
    asyncio.run(client.cancel_order("BTCUSDT", 7))
    asyncio.run(client.get_open_algo_orders("BTCUSDT"))
    asyncio.run(client.create_algo_order({"algoType": "STOP"}))
    asyncio.run(client.cancel_algo_order("BTCUSDT", 9))
    asyncio.run(client.create_listen_key())
    asyncio.run(client.keepalive_listen_key("listen-key-1"))
    missing_key = asyncio.run(client.keepalive_listen_key())
    asyncio.run(client.request("GET", Endpoint.TICKER_24HR, params={"symbol": "BTCUSDT"}))

    assert calls[0] == ("GET", Endpoint.SERVER_TIME, False, {})
    assert calls[1] == ("GET", Endpoint.EXCHANGE_INFO, False, {"symbol": "BTCUSDT"})
    assert calls[5][2] is True
    assert calls[9][0:3] == ("POST", Endpoint.ORDER, True)
    assert calls[9][3]["reduceOnly"] == "true"
    assert any(
        method == "PUT" and path == Endpoint.LISTEN_KEY and params == {"listenKey": "listen-key-1"}
        for method, path, _signed, params in calls
    )
    assert missing_key.is_success() is False
    assert calls[-1][1] == Endpoint.TICKER_24HR


def test_keepalive_transport_preserves_put_method(monkeypatch: pytest.MonkeyPatch) -> None:
    """The convenience boundary is insufficient: the actual transport must send PUT."""

    client = BinanceRESTClient(
        "https://demo.example",
        api_key="api-key",
        api_secret="secret",  # noqa: S106 - deterministic test key
        max_retries=1,
    )
    methods: list[str] = []

    def fake_urlopen(request, timeout, _session=None):
        methods.append(request.get_method())
        return b'{"listenKey":"listen-key-1"}', {}

    monkeypatch.setattr(rest_module, "_sync_urlopen", fake_urlopen)
    result = asyncio.run(client.keepalive_listen_key("listen-key-1"))

    assert result.is_success() is True
    assert methods == ["PUT"]


def test_signed_success_adds_timestamp_signature_and_auth_header(monkeypatch: pytest.MonkeyPatch) -> None:
    client = BinanceRESTClient(
        "https://demo.example",
        api_key="api-key",
        api_secret="secret",  # noqa: S106 - deterministic test key
        max_retries=1,
    )
    captured: list[object] = []

    def fake_urlopen(request, timeout, _session=None):
        captured.append((request, timeout))
        return json.dumps({"serverTime": 123}).encode(), {}

    monkeypatch.setattr(rest_module, "_sync_urlopen", fake_urlopen)
    result = asyncio.run(client.get_server_time())
    assert result.is_success() is True
    assert result.data == {"serverTime": 123}
    request, timeout = captured[0]
    assert timeout > 0
    assert request.get_header("X-mbx-apikey") == "api-key"

    signed = asyncio.run(client.get_order("BTCUSDT", 12))
    assert signed.is_success() is True
    signed_request, _ = captured[1]
    assert "timestamp=" in signed_request.full_url
    assert "recvWindow=" in signed_request.full_url
    assert "signature=" in signed_request.full_url


def test_binance_business_error_is_classified_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    client = BinanceRESTClient("https://demo.example", max_retries=3)
    calls = 0

    def fake_urlopen(request, timeout, _session=None):
        nonlocal calls
        calls += 1
        return b'{"code":-2015,"msg":"invalid api-key"}', {}

    monkeypatch.setattr(rest_module, "_sync_urlopen", fake_urlopen)
    result = asyncio.run(client.get_account())
    assert result.is_success() is False
    assert result.error is not None
    assert result.error.category is ErrorCategory.AUTH_FAILURE
    assert result.error.retryable is False
    assert result.error.raw == {"code": -2015, "msg": "invalid api-key"}
    assert calls == 1


def test_network_failure_retries_then_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    client = BinanceRESTClient("https://demo.example", max_retries=2)
    calls = 0

    def fail_urlopen(request, timeout, _session=None):
        nonlocal calls
        calls += 1
        raise OSError("network down")

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(rest_module, "_sync_urlopen", fail_urlopen)
    monkeypatch.setattr(rest_module.asyncio, "sleep", no_sleep)
    result = asyncio.run(client.get_ticker("BTCUSDT"))
    assert result.is_success() is False
    assert result.error is not None
    assert result.error.category is ErrorCategory.NETWORK
    assert calls == 2


def test_rate_limit_retries_and_circuit_breaker_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    client = BinanceRESTClient("https://demo.example", max_retries=1)
    errors = 0

    def rate_limited(request, timeout, _session=None):
        nonlocal errors
        errors += 1
        raise HTTPError(
            request.full_url,
            429,
            "too many",
            {"Retry-After": "0"},
            BytesIO(b'{"code":-1003,"msg":"rate limited"}'),
        )

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(rest_module, "_sync_urlopen", rate_limited)
    monkeypatch.setattr(rest_module.asyncio, "sleep", no_sleep)
    for _ in range(5):
        result = asyncio.run(client.get_ticker("BTCUSDT"))
        assert result.is_success() is False
        assert result.error is not None
        assert result.error.category is ErrorCategory.RATE_LIMIT
    assert client._rate_state.circuit_open is True
    blocked = asyncio.run(client.get_ticker("BTCUSDT"))
    assert blocked.error is not None
    assert blocked.error.category is ErrorCategory.RATE_LIMIT
    assert blocked.error.retryable is False
    assert errors == 5
    client.reset_circuit_breaker()
    assert client._rate_state.circuit_open is False


def test_http_5xx_retryable_error_and_business_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    client = BinanceRESTClient("https://demo.example", max_retries=2)
    calls = 0

    def unavailable(request, timeout, _session=None):
        nonlocal calls
        calls += 1
        raise HTTPError(request.full_url, 503, "unavailable", {}, BytesIO(b"gateway"))

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(rest_module, "_sync_urlopen", unavailable)
    monkeypatch.setattr(rest_module.asyncio, "sleep", no_sleep)
    result = asyncio.run(client.get_ticker("BTCUSDT"))
    assert result.error is not None
    assert result.error.category is ErrorCategory.EXCHANGE_UNAVAILABLE
    assert calls == 2


def test_ambiguous_order_503_is_unknown_and_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A venue write may have succeeded despite a generic 503; query by client id first."""

    client = BinanceRESTClient("https://demo.example", api_secret="secret", max_retries=3)  # noqa: S106
    calls = 0

    def unavailable(request, timeout, _session=None):
        nonlocal calls
        calls += 1
        raise HTTPError(
            request.full_url,
            503,
            "unknown execution status",
            {},
            BytesIO(b'{"msg":"Unknown error, please check your request or try again later."}'),
        )

    monkeypatch.setattr(rest_module, "_sync_urlopen", unavailable)
    result = asyncio.run(
        client.create_order(
            "BTCUSDT",
            "BUY",
            "MARKET",
            "0.001",
            client_order_id="beidou-ambiguous-1",
        )
    )

    assert result.is_success() is False
    assert result.error is not None
    assert result.error.category is ErrorCategory.UNKNOWN
    assert result.error.retryable is False
    assert result.error.raw["write_safety"] == "QUERY_BEFORE_RETRY_REQUIRED"
    assert calls == 1


def test_account_capability_requires_all_independent_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    client = BinanceRESTClient("https://demo.example")

    async def server_time() -> Result:
        return Result.ok({"serverTime": int(__import__("time").time() * 1000)})

    async def account() -> Result:
        return Result.ok({"totalWalletBalance": "100", "positions": [], "canTrade": True, "canWithdraw": False})

    async def orders() -> Result:
        return Result.ok([])

    async def exchange_info() -> Result:
        return Result.ok({"symbols": []})

    monkeypatch.setattr(client, "get_server_time", server_time)
    monkeypatch.setattr(client, "get_account", account)
    monkeypatch.setattr(client, "get_open_orders", orders)
    monkeypatch.setattr(client, "get_exchange_info", exchange_info)
    result = asyncio.run(client.check_account_capability())
    assert result.is_success() is True
    assert result.data["can_trade"] is True

    async def bad_account() -> Result:
        return Result.ok({"positions": []})

    monkeypatch.setattr(client, "get_account", bad_account)
    failed = asyncio.run(client.check_account_capability())
    assert failed.is_success() is False
    assert failed.error is not None
    assert failed.error.category is ErrorCategory.UNKNOWN


def test_account_capability_rejects_venue_withdrawal_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    client = BinanceRESTClient("https://demo.example")

    async def server_time() -> Result:
        return Result.ok({"serverTime": int(__import__("time").time() * 1000)})

    async def account() -> Result:
        return Result.ok({"totalWalletBalance": "100", "positions": [], "canTrade": True, "canWithdraw": True})

    async def orders() -> Result:
        return Result.ok([])

    async def exchange_info() -> Result:
        return Result.ok({"symbols": []})

    monkeypatch.setattr(client, "get_server_time", server_time)
    monkeypatch.setattr(client, "get_account", account)
    monkeypatch.setattr(client, "get_open_orders", orders)
    monkeypatch.setattr(client, "get_exchange_info", exchange_info)
    result = asyncio.run(client.check_account_capability())
    assert result.is_success() is False
    assert result.error is not None
    assert result.error.category is ErrorCategory.UNKNOWN


def test_high_weight_get_response_cached_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """openAlgoOrders/openOrders 是高权重端点（各 40w）：TTL 内重复
    调用必须命中本地缓存，不再发起第二次传输。"""
    client = BinanceRESTClient(
        "https://demo.example",
        api_key="api-key",
        api_secret="secret",  # noqa: S106 - deterministic test key
        max_retries=1,
    )
    calls: list[str] = []

    def fake_urlopen(request, timeout, _session=None):
        calls.append(request.get_method())
        return b'[{"algoId": 1, "symbol": "BTCUSDT"}]', {}

    monkeypatch.setattr(rest_module, "_sync_urlopen", fake_urlopen)
    first = asyncio.run(client.get_open_algo_orders())
    second = asyncio.run(client.get_open_algo_orders())
    assert first.is_success() is True
    assert second.is_success() is True
    assert second.data == first.data
    assert calls == ["GET"]


def test_high_weight_get_cache_keyed_by_params(monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存键必须包含请求参数：带 symbol 与不带 symbol 是两个事实。"""
    client = BinanceRESTClient(
        "https://demo.example",
        api_key="api-key",
        api_secret="secret",  # noqa: S106 - deterministic test key
        max_retries=1,
    )
    calls: list[str] = []

    def fake_urlopen(request, timeout, _session=None):
        calls.append(request.full_url)
        return b'[]', {}

    monkeypatch.setattr(rest_module, "_sync_urlopen", fake_urlopen)
    asyncio.run(client.get_open_orders())
    asyncio.run(client.get_open_orders("BTCUSDT"))
    assert len(calls) == 2


def test_failed_high_weight_get_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """失败结果不缓存：下一轮必须重新发起传输（否则把瞬时失败
    固化为持久 UNKNOWN）。"""
    client = BinanceRESTClient(
        "https://demo.example",
        api_key="api-key",
        api_secret="secret",  # noqa: S106 - deterministic test key
        max_retries=1,
    )
    calls: list[str] = []

    def failing_urlopen(request, timeout, _session=None):
        calls.append(request.get_method())
        raise ConnectionError("boom")

    monkeypatch.setattr(rest_module, "_sync_urlopen", failing_urlopen)
    first = asyncio.run(client.get_open_algo_orders())
    second = asyncio.run(client.get_open_algo_orders())
    assert first.is_success() is False
    assert second.is_success() is False
    assert calls == ["GET", "GET"]


def test_cached_get_serves_during_open_circuit_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    """熔断窗口内，TTL 内的缓存结果仍可直接返回——supervisor 保护
    覆盖探针依赖这一行为在熔断期间保持 ok=True。"""
    client = BinanceRESTClient(
        "https://demo.example",
        api_key="api-key",
        api_secret="secret",  # noqa: S106 - deterministic test key
        max_retries=1,
    )
    calls: list[str] = []

    def fake_urlopen(request, timeout, _session=None):
        calls.append(request.get_method())
        return b'[{"algoId": 2, "symbol": "ETHUSDT"}]', {}

    monkeypatch.setattr(rest_module, "_sync_urlopen", fake_urlopen)
    first = asyncio.run(client.get_open_algo_orders())
    assert first.is_success() is True

    client._rate_state.circuit_open = True
    client._rate_state.circuit_open_until = rest_module.time.monotonic() + 60
    cached = asyncio.run(client.get_open_algo_orders())
    assert cached.is_success() is True
    assert cached.data == first.data
    assert calls == ["GET"]
