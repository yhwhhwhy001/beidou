"""Deterministic RFC-6455 and subscription-state tests for the WS boundary."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections import deque

import pytest

from beidou_exchange.binance_usdm import ws_client as ws_module
from beidou_exchange.binance_usdm.ws_client import (
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
