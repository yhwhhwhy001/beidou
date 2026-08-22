"""Deterministic RFC-6455 and subscription-state tests for the WS boundary."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections import deque

import pytest

from beidou_exchange.binance_usdm import ws_client as ws_module
from beidou_exchange.binance_usdm.ws_client import (
    OP_CLOSE,
    OP_CONT,
    OP_PING,
    OP_TEXT,
    WS_GUID,
    BinanceUsdmWebSocketClient,
    ConnectionState,
    WebSocketClosedError,
    WebSocketConnection,
    WebSocketError,
    _combined_stream_url,
    _parse_close_payload,
    _validate_stream,
    split_stream,
)


class _Reader:
    def __init__(self, data: bytes = b"", lines: list[bytes] | None = None) -> None:
        self._data = bytearray(data)
        self._lines = deque(lines or [])

    async def readexactly(self, n: int) -> bytes:
        if len(self._data) < n:
            raise asyncio.IncompleteReadError(bytes(self._data), n)
        value = bytes(self._data[:n])
        del self._data[:n]
        return value

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.popleft()


class _Writer:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def _frame(opcode: int, payload: bytes, *, fin: bool = True, mask: bytes | None = None) -> bytes:
    first = (0x80 if fin else 0) | opcode
    length = len(payload)
    if length < 126:
        header = bytes([first, (0x80 if mask else 0) | length])
    elif length <= 0xFFFF:
        header = bytes([first, (0x80 if mask else 0) | 126]) + length.to_bytes(2, "big")
    else:
        header = bytes([first, (0x80 if mask else 0) | 127]) + length.to_bytes(8, "big")
    if mask is None:
        return header + payload
    return header + mask + bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))


def test_stream_helpers_validate_and_build_deterministic_urls() -> None:
    assert split_stream("btcusdt@ticker") == ("btcusdt", "ticker")
    assert split_stream("btcusdt@depth5@100ms") == ("btcusdt", "depth5@100ms")
    assert split_stream("listen-key") == (None, "user")
    assert _combined_stream_url("wss://demo.example", ["b@ticker", "e@ticker"]) == (
        "wss://demo.example/stream?streams=b@ticker/e@ticker"
    )
    for invalid in ("", "bad stream", "bad/name", "x" * 1025):
        with pytest.raises(ValueError):
            _validate_stream(invalid)


def test_read_frame_supports_lengths_and_masking() -> None:
    payload = b"hello"
    connection = WebSocketConnection()
    connection._reader = _Reader(_frame(OP_TEXT, payload, mask=b"abcd"))
    assert asyncio.run(connection._read_frame()) == (True, OP_TEXT, payload)

    large = b"x" * 126
    connection._reader = _Reader(_frame(OP_TEXT, large))
    fin, opcode, received = asyncio.run(connection._read_frame())
    assert (fin, opcode, len(received)) == (True, OP_TEXT, 126)

    very_large = b"y" * 126
    oversized = WebSocketConnection(max_message_size=10)
    oversized._reader = _Reader(_frame(OP_TEXT, very_large))
    with pytest.raises(WebSocketError):
        asyncio.run(oversized._read_frame())


def test_recv_message_reassembles_fragments_and_preserves_control_frames() -> None:
    data = _frame(OP_TEXT, b"hel", fin=False) + _frame(OP_PING, b"ping") + _frame(OP_CONT, b"lo")
    connection = WebSocketConnection()
    connection._reader = _Reader(data)
    assert asyncio.run(connection.recv_message()) == (OP_PING, b"ping")
    assert asyncio.run(connection.recv_message()) == (OP_TEXT, b"hello")

    connection._reader = _Reader(_frame(OP_TEXT, b"a", fin=False) + _frame(OP_TEXT, b"unexpected"))
    with pytest.raises(WebSocketError):
        asyncio.run(connection.recv_message())


def test_send_frame_masks_payload_and_supports_extended_lengths(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = _Writer()
    connection = WebSocketConnection(max_message_size=70_000)
    connection._writer = writer
    monkeypatch.setattr(ws_module.os, "urandom", lambda n: b"abcd" if n == 4 else b"x" * n)
    asyncio.run(connection.send_frame(OP_TEXT, b"hello"))
    assert writer.buffer[:2] == bytes([0x81, 0x85])
    assert writer.buffer[2:6] == b"abcd"
    assert len(writer.buffer) == 11

    writer.buffer.clear()
    asyncio.run(connection.send_frame(OP_TEXT, b"x" * 126))
    assert writer.buffer[1] == 126 | 0x80
    assert int.from_bytes(writer.buffer[2:4], "big") == 126

    with pytest.raises(WebSocketError):
        asyncio.run(WebSocketConnection(max_message_size=2).send_frame(OP_TEXT, b"123"))


def test_handshake_validates_status_and_accept_header(monkeypatch: pytest.MonkeyPatch) -> None:
    key_bytes = b"0123456789abcdef"
    key = base64.b64encode(key_bytes).decode()
    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()  # noqa: S324
    writer = _Writer()
    reader = _Reader(
        lines=[
            b"HTTP/1.1 101 Switching Protocols\r\n",
            b"Sec-WebSocket-Accept: " + accept.encode() + b"\r\n",
            b"\r\n",
        ]
    )
    connection = WebSocketConnection()
    connection._writer = writer
    connection._reader = reader
    monkeypatch.setattr(ws_module.os, "urandom", lambda n: key_bytes)
    parsed = ws_module.urllib.parse.urlparse("ws://demo.example/stream?streams=a")
    asyncio.run(connection._handshake(parsed, 80))
    assert b"GET /stream?streams=a HTTP/1.1" in writer.buffer

    rejected = WebSocketConnection()
    rejected._writer = _Writer()
    rejected._reader = _Reader(lines=[b"HTTP/1.1 400 Bad Request\r\n"])
    with pytest.raises(WebSocketError, match="handshake rejected"):
        asyncio.run(rejected._handshake(parsed, 80))


def test_close_payload_and_connection_abort() -> None:
    assert _parse_close_payload((1000).to_bytes(2, "big") + b"done") == (1000, "done")
    assert _parse_close_payload(b"") == (1005, "")
    error = WebSocketClosedError(1008, "policy")
    assert error.code == 1008
    assert "policy" in str(error)
    writer = _Writer()
    connection = WebSocketConnection()
    connection._writer = writer
    connection.abort()
    assert writer.closed is True
    assert connection._writer is None


def test_subscription_groups_dispatch_callbacks_and_unsubscribe() -> None:
    async def scenario() -> tuple[list[tuple[str, dict]], list[tuple[str, dict]], BinanceUsdmWebSocketClient]:
        client = BinanceUsdmWebSocketClient("ws://demo.example", max_streams_per_connection=1)
        async_calls: list[tuple[str, dict]] = []
        sync_calls: list[tuple[str, dict]] = []

        async def async_cb(stream: str, data: dict) -> None:
            async_calls.append((stream, data))

        def sync_cb(stream: str, data: dict) -> None:
            sync_calls.append((stream, data))

        await client.subscribe("btcusdt@ticker", async_cb)
        await client.subscribe("ethusdt@ticker", sync_cb)
        assert client.group_count == 2
        assert client.state is ConnectionState.DISCONNECTED
        await client._dispatch("btcusdt@ticker", {"price": 1})
        await client._dispatch("ethusdt@ticker", {"price": 2})
        await client.unsubscribe("btcusdt@ticker", async_cb)
        assert "btcusdt@ticker" not in client.streams
        assert async_calls == [("btcusdt@ticker", {"price": 1})]
        assert sync_calls == [("ethusdt@ticker", {"price": 2})]
        return async_calls, sync_calls, client

    _, _, client = asyncio.run(scenario())
    asyncio.run(client.close())


def test_group_text_dispatch_state_hooks_and_invalid_messages() -> None:
    async def scenario() -> tuple[list[tuple[str, dict]], list[tuple[ConnectionState, ConnectionState, int]]]:
        client = BinanceUsdmWebSocketClient("ws://demo.example", max_streams_per_connection=2)
        received: list[tuple[str, dict]] = []
        states: list[tuple[ConnectionState, ConnectionState, int]] = []
        client.on_message(lambda stream, data: received.append((stream, data)))
        client.on_state_change(lambda old, new, group: states.append((old, new, group)))
        await client.subscribe("btcusdt@ticker", lambda stream, data: received.append((stream, data)))
        group = client._groups[0]
        await group._handle_text('{"stream":"btcusdt@ticker","data":{"p":"1"}}')
        await group._handle_text('{"p":"2"}')
        await group._handle_text("not json")
        assert received[:2] == [
            ("btcusdt@ticker", {"p": "1"}),
            ("btcusdt@ticker", {"p": "1"}),
        ]
        group._set_state(ConnectionState.CONNECTING)
        group._set_state(ConnectionState.CONNECTED)
        assert states[-1] == (ConnectionState.CONNECTING, ConnectionState.CONNECTED, 1)
        await client.close()
        return received, states

    asyncio.run(scenario())


def test_sync_subscription_and_callback_exceptions_are_bounded() -> None:
    client = BinanceUsdmWebSocketClient("ws://demo.example")
    calls: list[tuple[str, dict]] = []

    def callback(stream: str, data: dict) -> None:
        calls.append((stream, data))

    client.subscribe_sync("btcusdt@ticker", callback)
    assert client.streams == frozenset({"btcusdt@ticker"})
    assert client.unsubscribe_sync("missing", callback) is False
    assert client.unsubscribe_sync("btcusdt@ticker", callback) is True
    assert client.unsubscribe_sync("btcusdt@ticker", callback) is False

    async def scenario() -> None:
        async def bad(_stream: str, _data: dict) -> None:
            raise RuntimeError("callback failure")

        await BinanceUsdmWebSocketClient._invoke_callback(bad, "s", {})

    asyncio.run(scenario())


def test_connection_transport_and_frame_edge_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    original_handshake = WebSocketConnection._handshake
    with pytest.raises(WebSocketError, match="unsupported scheme"):
        asyncio.run(WebSocketConnection().connect("http://demo.example"))

    async def successful_connection(*_args, **_kwargs):
        return _Reader(), _Writer()

    async def no_handshake(self, _parsed, _port) -> None:
        return None

    monkeypatch.setattr(ws_module.asyncio, "open_connection", successful_connection)
    monkeypatch.setattr(WebSocketConnection, "_handshake", no_handshake)
    asyncio.run(WebSocketConnection().connect("ws://demo.example"))
    monkeypatch.setattr(WebSocketConnection, "_handshake", original_handshake)

    async def timeout_connection(*_args, **_kwargs):
        raise asyncio.TimeoutError

    monkeypatch.setattr(ws_module.asyncio, "open_connection", timeout_connection)
    with pytest.raises(ConnectionError, match="connect timeout"):
        asyncio.run(WebSocketConnection().connect("ws://demo.example"))

    async def failed_connection(*_args, **_kwargs):
        raise OSError("refused")

    monkeypatch.setattr(ws_module.asyncio, "open_connection", failed_connection)
    with pytest.raises(ConnectionError, match="TCP/TLS connect failed"):
        asyncio.run(WebSocketConnection().connect("wss://demo.example"))

    key_bytes = b"0123456789abcdef"
    parsed = ws_module.urllib.parse.urlparse("ws://demo.example/stream")
    rejected = WebSocketConnection()
    rejected._writer = _Writer()
    rejected._reader = _Reader(
        lines=[b"HTTP/1.1 101 Switching Protocols\r\n", b"Sec-WebSocket-Accept: wrong\r\n", b"\r\n"]
    )
    monkeypatch.setattr(ws_module.os, "urandom", lambda n: key_bytes)
    with pytest.raises(WebSocketError, match="invalid Sec-WebSocket-Accept"):
        asyncio.run(rejected._handshake(parsed, 80))

    incomplete = WebSocketConnection()
    incomplete._reader = _Reader(b"\x81")
    with pytest.raises(ConnectionError, match="connection closed by peer"):
        asyncio.run(incomplete._read_frame())
    empty_line = WebSocketConnection()
    empty_line._reader = _Reader(lines=[])
    with pytest.raises(ConnectionError, match="connection closed during handshake"):
        asyncio.run(empty_line._read_line())

    extended = b"z" * 65_536
    extended_connection = WebSocketConnection(max_message_size=len(extended))
    extended_connection._reader = _Reader(_frame(OP_TEXT, extended))
    fin, opcode, payload = asyncio.run(extended_connection._read_frame())
    assert (fin, opcode, len(payload)) == (True, OP_TEXT, len(extended))

    continuation = WebSocketConnection()
    continuation._reader = _Reader(_frame(OP_CONT, b"continuation"))
    assert asyncio.run(continuation.recv_message()) == (OP_CONT, b"continuation")
    final = WebSocketConnection()
    final._reader = _Reader(_frame(OP_TEXT, b"final"))
    assert asyncio.run(final.recv_message()) == (OP_TEXT, b"final")

    writer = _Writer()
    sender = WebSocketConnection(max_message_size=70_000)
    sender._writer = writer
    monkeypatch.setattr(ws_module.os, "urandom", lambda n: b"abcd" if n == 4 else b"x" * n)
    asyncio.run(sender.send_frame(OP_TEXT, b"x" * 65_536))
    asyncio.run(sender.send_ping(b"ping"))
    asyncio.run(sender.send_pong(b"pong"))
    assert writer.buffer


@pytest.mark.asyncio
async def test_group_and_client_lifecycle_edge_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    original_sleep = ws_module.asyncio.sleep
    client = BinanceUsdmWebSocketClient("ws://demo.example", max_streams_per_connection=1)
    group = client._groups[0] if client._groups else None
    assert client.state is ConnectionState.DISCONNECTED
    group = group or ws_module._StreamGroup(client, 1)

    sleeps = 0

    async def stop_after_sleep(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        group._closed = True

    monkeypatch.setattr(ws_module.asyncio, "sleep", stop_after_sleep)
    await group._run()
    assert sleeps == 1 and group.state is ConnectionState.DISCONNECTED

    group._streams["x" * 1000] = [lambda _s, _d: None]

    class _FailingWs:
        async def connect(self, _url: str) -> None:
            raise RuntimeError("connect failed")

        def abort(self) -> None:
            return None

    monkeypatch.setattr(ws_module, "WebSocketConnection", _FailingWs)
    with pytest.raises(RuntimeError):
        await group._connect_and_listen()
    await group._abort_ws()

    group._streams = {f"x{index}" + "y" * 1000: [lambda _s, _d: None] for index in range(7)}
    with pytest.raises(RuntimeError):
        await group._connect_and_listen()

    reconnect_group = ws_module._StreamGroup(client, 9)
    reconnect_group._streams["reconnect"] = [lambda _s, _d: None]

    async def fail_connect() -> None:
        reconnect_group._closed = True
        raise RuntimeError("reconnect failed")

    async def close_after_wait(_seconds: float) -> None:
        reconnect_group._closed = True

    monkeypatch.setattr(reconnect_group, "_connect_and_listen", fail_connect)
    monkeypatch.setattr(ws_module.asyncio, "sleep", close_after_wait)
    await reconnect_group._run()

    group._streams.clear()
    group._streams["btcusdt@ticker"] = [lambda _s, _d: None]

    class _ContinuationWs:
        async def recv_message(self) -> tuple[int, bytes]:
            return OP_CONT, b"bad"

        async def send_pong(self, _payload: bytes) -> None:
            return None

    with pytest.raises(WebSocketError, match="unexpected continuation"):
        await group._listen(_ContinuationWs())

    class _TextThenCloseWs:
        def __init__(self) -> None:
            self.messages = iter([(OP_TEXT, b'{"value": 1}'), (OP_CLOSE, (1000).to_bytes(2, "big"))])

        async def recv_message(self) -> tuple[int, bytes]:
            return next(self.messages)

    with pytest.raises(WebSocketClosedError):
        await group._listen(_TextThenCloseWs())

    group.state = ConnectionState.DISCONNECTED

    async def immediate_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(ws_module.asyncio, "sleep", immediate_sleep)
    await group._ping_loop(_ContinuationWs())

    async def failing_sleep(_seconds: float) -> None:
        raise RuntimeError("timer failed")

    monkeypatch.setattr(ws_module.asyncio, "sleep", failing_sleep)
    await group._ping_loop(_ContinuationWs())
    monkeypatch.setattr(ws_module.asyncio, "sleep", original_sleep)

    client = BinanceUsdmWebSocketClient("ws://demo.example", max_streams_per_connection=1)

    def callback(_s, _d):
        return None

    client._add_stream_callback("first", callback)

    def second_callback(_s, _d):
        return None

    client._add_stream_callback("first", second_callback)
    assert client._pick_group("second") is None
    group = client._groups[0]
    group.state = ConnectionState.CONNECTING
    assert client.state is ConnectionState.RECONNECTING
    await client.subscribe("first", callback)
    await client.subscribe("second", callback)
    await client.unsubscribe("first", callback)
    assert "first" in client.streams
    await client.unsubscribe("first")
    await client.close()

    client = BinanceUsdmWebSocketClient("ws://demo.example", max_streams_per_connection=2)

    def first(_s, _d):
        return None

    def second(_s, _d):
        return None

    await client.subscribe("a", first)
    await client.subscribe("a", second)
    client._groups[0].state = ConnectionState.CONNECTED
    aborted: list[str] = []

    async def record_abort() -> None:
        aborted.append("abort")

    monkeypatch.setattr(client._groups[0], "_abort_ws", record_abort)
    await client.subscribe("b", first)
    await client.unsubscribe("a", first)
    assert "a" in client.streams
    await client.unsubscribe("a")
    assert aborted == ["abort", "abort"]

    client = BinanceUsdmWebSocketClient("ws://demo.example")
    await client.start()
    await asyncio.to_thread(client.subscribe_sync, "threaded", callback, True)
    await asyncio.to_thread(client.subscribe_sync, "threaded-no-wait", callback, False)
    assert await asyncio.to_thread(client.unsubscribe_sync, "threaded", callback) is True
    assert await asyncio.to_thread(client.unsubscribe_sync, "threaded-no-wait") is True
    await client.close()

    client = BinanceUsdmWebSocketClient("ws://demo.example")
    client.subscribe_sync("sync-stream", callback)
    client.subscribe_sync("sync-stream", second_callback)
    assert client.unsubscribe_sync("sync-stream", callback) is True
    assert "sync-stream" in client.streams
    assert client.unsubscribe_sync("sync-stream") is True

    client = BinanceUsdmWebSocketClient("ws://demo.example")
    run_task = asyncio.create_task(client.run())
    await asyncio.sleep(0)
    await client.close()
    await run_task

    errors: list[str] = []

    def bad_state(_old: ConnectionState, _new: ConnectionState, _group: int) -> None:
        errors.append("bad")
        raise RuntimeError("state callback failed")

    client.on_state_change(bad_state)
    group = ws_module._StreamGroup(client, 7)
    group._set_state(ConnectionState.CONNECTING)
    assert errors == ["bad"]
