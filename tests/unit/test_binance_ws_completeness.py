"""Semantic coverage for WebSocket reconnect and lifecycle branches."""

from __future__ import annotations

import asyncio

import pytest

from beidou_exchange.binance_usdm import ws_client as ws_module
from beidou_exchange.binance_usdm.ws_client import (
    OP_CLOSE,
    OP_PING,
    OP_PONG,
    BinanceUsdmWebSocketClient,
    ConnectionState,
    WebSocketClosedError,
    _StreamGroup,
)


@pytest.mark.asyncio
async def test_stream_group_routes_control_frames_and_unowned_messages() -> None:
    client = BinanceUsdmWebSocketClient("ws://example", ping_interval=999, pong_timeout=0)
    group = _StreamGroup(client, 1)
    pongs: list[bytes] = []

    class _Ws:
        def __init__(self) -> None:
            self.messages = iter([(OP_PING, b"ping"), (OP_PONG, b"pong"), (OP_CLOSE, (1000).to_bytes(2, "big"))])

        async def recv_message(self) -> tuple[int, bytes]:
            return next(self.messages)

        async def send_pong(self, payload: bytes) -> None:
            pongs.append(payload)

    with pytest.raises(WebSocketClosedError):
        await group._listen(_Ws())
    assert pongs == [b"ping"]
    await group._handle_text('{"value": 1}')
    await group._handle_text("not json")
    group._streams.clear()
    await group._handle_text('{"stream":"orphan","data":{}}')


@pytest.mark.asyncio
async def test_stream_group_ping_success_requires_stable_cycles(monkeypatch: pytest.MonkeyPatch) -> None:
    client = BinanceUsdmWebSocketClient("ws://example", ping_interval=0, pong_timeout=0)
    group = _StreamGroup(client, 1)
    group.state = ConnectionState.CONNECTED
    sleeps = 0

    async def fake_sleep(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps > 6:
            raise asyncio.CancelledError

    class _Ws:
        async def send_ping(self) -> None:
            group._ping_pending = False

    monkeypatch.setattr(ws_module.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await group._ping_loop(_Ws())
    assert group._stable_pings >= 3
    assert group._backoff == ws_module.INITIAL_RECONNECT_BACKOFF


@pytest.mark.asyncio
async def test_stream_group_ping_timeout_aborts_connection() -> None:
    client = BinanceUsdmWebSocketClient("ws://example", ping_interval=0, pong_timeout=0)
    group = _StreamGroup(client, 1)
    group.state = ConnectionState.CONNECTED
    aborted: list[str] = []

    class _Ws:
        async def send_ping(self) -> None:
            return None

        def abort(self) -> None:
            aborted.append("abort")

    group._ws = _Ws()
    await group._ping_loop(_Ws())
    assert aborted == ["abort"]


@pytest.mark.asyncio
async def test_stream_group_connect_listen_and_run_reconnect_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    client = BinanceUsdmWebSocketClient("ws://example", ping_interval=999, pong_timeout=0)
    group = _StreamGroup(client, 1)
    group._streams["btcusdt@ticker"] = [lambda _s, _d: None]
    states: list[ConnectionState] = []
    client.on_state_change(lambda _old, new, _gid: states.append(new))

    class _FakeWs:
        def __init__(self) -> None:
            self.aborted = False

        async def connect(self, _url: str) -> None:
            return None

        async def recv_message(self) -> tuple[int, bytes]:
            raise WebSocketClosedError(1013, "busy")

        def abort(self) -> None:
            self.aborted = True

    fake_ws = _FakeWs()
    monkeypatch.setattr(ws_module, "WebSocketConnection", lambda: fake_ws)
    with pytest.raises(WebSocketClosedError):
        await group._connect_and_listen()
    assert group.state is ConnectionState.CONNECTED

    async def fake_sleep(_seconds: float) -> None:
        group._closed = True

    monkeypatch.setattr(ws_module.asyncio, "sleep", fake_sleep)
    group._closed = False
    await group._run()
    assert group.state is ConnectionState.DISCONNECTED
    assert ConnectionState.RECONNECTING in states
    assert fake_ws.aborted is True


@pytest.mark.asyncio
async def test_client_subscription_sync_lifecycle_and_async_state_hook() -> None:
    with pytest.raises(ValueError):
        BinanceUsdmWebSocketClient(max_streams_per_connection=0)
    client = BinanceUsdmWebSocketClient("ws://example", max_streams_per_connection=1)

    def cb(_s: object, _d: object) -> None:
        return None

    group = client._add_stream_callback("btcusdt@ticker", cb)
    assert group is not None
    assert client._add_stream_callback("btcusdt@ticker", cb) is None
    assert client.state is ConnectionState.DISCONNECTED
    group.state = ConnectionState.CONNECTED
    assert client.state is ConnectionState.CONNECTED

    events: list[tuple[str, str]] = []

    async def state_hook(old: ConnectionState, new: ConnectionState, _group: int) -> None:
        events.append((old.value, new.value))

    client.on_state_change(state_hook)
    group._set_state(ConnectionState.RECONNECTING)
    await asyncio.sleep(0)
    assert events[-1] == ("CONNECTED", "RECONNECTING")
    assert await client.unsubscribe("btcusdt@ticker", cb) is True
    assert client.streams == frozenset()
    assert await client.unsubscribe("missing") is False
    await client.start()
    await client.close()
    assert client.state is ConnectionState.DISCONNECTED
