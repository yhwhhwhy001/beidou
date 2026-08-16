"""Binance USDⓈ-M 异步 WebSocket 行情客户端（纯标准库实现）。

不依赖第三方 websockets 库，自包含 RFC 6455 帧处理（asyncio + ssl），
与 rest_client.py 保持一致的无外部依赖风格，可直接用于生产。

- 生产 (fstream.binance.com) / 测试网 (stream.binancefuture.com) 双环境
- 组合流按流名分发回调；单连接最多 200 条流（可配置），超出自动拆分
- 指数退避重连 1s→2s→4s→…→30s，连接稳定后退避归零
- 服务端 ping 自动回 pong（Binance 每 3 分钟发送）；客户端每 60s 主动 ping 探测死链
- 状态机: CONNECTING / CONNECTED / RECONNECTING / DISCONNECTED
- 动态 subscribe/unsubscribe（组合流变更需重连，客户端自动触发）
- subscribe_sync/unsubscribe_sync 线程安全；同步回调在线程池执行，不阻塞事件循环

用法:
    client = BinanceUsdmWebSocketClient()
    await client.subscribe("btcusdt@ticker", on_ticker)
    await client.subscribe("btcusdt@depth5@100ms", on_depth)
    await client.subscribe(listen_key, on_user_event)  # 用户数据流
    await client.run()                                 # 阻塞直到 close()

    async def on_ticker(stream: str, data: dict) -> None:
        print(data["s"], data["c"])
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import inspect
import json
import logging
import os
import ssl
import threading
import urllib.parse
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)

# === 常量 ===
FSTREAM_PRODUCTION_URL = "wss://fstream.binance.com"
FSTREAM_TESTNET_URL = "wss://demo-fstream.binance.com"

MAX_STREAMS_PER_CONNECTION = 200  # Binance 单连接流上限
MAX_MESSAGE_SIZE = 8 * 1024 * 1024
CONNECT_TIMEOUT = 10.0
PING_INTERVAL = 60.0  # 客户端主动 ping 周期
PONG_TIMEOUT = 10.0  # ping 后等待 pong 超时，超时视为死链
INITIAL_RECONNECT_BACKOFF = 1.0
MAX_RECONNECT_BACKOFF = 30.0

# === WebSocket 帧 opcode (RFC 6455) ===
OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

Callback = Callable[[str, dict], Any]


class ConnectionState(Enum):
    """连接状态机。"""

    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    DISCONNECTED = "DISCONNECTED"


def split_stream(stream: str) -> tuple[str | None, str]:
    """拆分组合流名为 (symbol, stream_type)。

    'btcusdt@ticker' -> ('btcusdt', 'ticker')
    'btcusdt@depth5@100ms' -> ('btcusdt', 'depth5@100ms')
    '<listenKey>' -> (None, 'user')
    """
    if "@" in stream:
        symbol, _, kind = stream.partition("@")
        return symbol or None, kind
    return None, "user"


def _validate_stream(stream: str) -> None:
    if not stream or "/" in stream or any(c.isspace() for c in stream) or len(stream) > 1024:
        raise ValueError(f"invalid stream name: {stream!r}")


def _combined_stream_url(base_url: str, streams: list[str]) -> str:
    """构造组合流 URL: wss://host/stream?streams=a/b/c"""
    return f"{base_url}/stream?streams=" + "/".join(streams)


class WebSocketError(Exception):
    """WebSocket 协议错误。"""


class WebSocketClosedError(WebSocketError):
    """对端发送 close 帧。"""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(f"websocket closed: code={code} reason={reason!r}")
        self.code = code
        self.reason = reason


class WebSocketConnection:
    """最小化 RFC 6455 WebSocket 客户端（asyncio + 标准库）。

    支持 wss/ws、客户端掩码、分片重组、控制帧、close 握手。
    供 BinanceUsdmWebSocketClient 内部使用，也可独立测试。
    """

    def __init__(self, max_message_size: int = MAX_MESSAGE_SIZE) -> None:
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._max_message_size = max_message_size
        self._fragment_opcode: int | None = None
        self._fragment_chunks: list[bytes] | None = None

    # === 连接与握手 ===

    async def connect(self, url: str, timeout: float = CONNECT_TIMEOUT) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("ws", "wss"):
            raise WebSocketError(f"unsupported scheme: {parsed.scheme}")
        use_tls = parsed.scheme == "wss"
        port = parsed.port or (443 if use_tls else 80)
        ssl_ctx = ssl.create_default_context() if use_tls else None
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(parsed.hostname, port, ssl=ssl_ctx),
                timeout,
            )
        except asyncio.TimeoutError as exc:
            raise ConnectionError(f"connect timeout to {url}") from exc
        except Exception as exc:
            raise ConnectionError(f"TCP/TLS connect failed: {exc}") from exc
        await self._handshake(parsed, port)

    async def _handshake(self, parsed: urllib.parse.ParseResult, port: int) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {parsed.hostname}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        assert self._writer is not None, "connect() must precede handshake"
        assert self._writer is not None, "connect() must precede handshake"
        self._writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await self._writer.drain()

        status = await self._read_line()
        if not status.startswith(b"HTTP/1.1 101"):
            raise WebSocketError(f"handshake rejected: {status.decode('utf-8', 'replace').strip()}")
        accept: str | None = None
        while True:
            line = await self._read_line()
            if line in (b"", b"\r\n"):
                break
            if line.lower().startswith(b"sec-websocket-accept:"):
                accept = line.split(b":", 1)[1].strip().decode()
        # RFC 6455 要求 Sec-WebSocket-Accept = base64(SHA1(key + GUID)) — SHA1 系协议规定
        expected = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()  # noqa: S324
        ).decode()
        if accept != expected:
            raise WebSocketError("invalid Sec-WebSocket-Accept")

    # === 收发 ===

    async def _read_exact(self, n: int) -> bytes:
        assert self._reader is not None, "connect() must precede reads"
        try:
            return await self._reader.readexactly(n)
        except asyncio.IncompleteReadError as exc:
            raise ConnectionError("connection closed by peer") from exc

    async def _read_line(self) -> bytes:
        assert self._reader is not None, "connect() must precede reads"
        line = await self._reader.readline()
        if not line:
            raise ConnectionError("connection closed during handshake")
        return line

    async def _read_frame(self) -> tuple[bool, int, bytes]:
        b0, b1 = await self._read_exact(2)
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        length = b1 & 0x7F
        if length == 126:
            length = int.from_bytes(await self._read_exact(2), "big")
        elif length == 127:
            length = int.from_bytes(await self._read_exact(8), "big")
        if length > self._max_message_size:
            raise WebSocketError(f"frame too large: {length}")
        mask = await self._read_exact(4) if b1 & 0x80 else None
        payload = await self._read_exact(length) if length else b""
        if mask is not None:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    async def recv_message(self) -> tuple[int, bytes]:
        """读取一条消息，返回 (opcode, payload)。

        控制帧 (ping/pong/close) 立即返回；数据消息自动重组分片。
        若分片消息中插入控制帧，控制帧先返回，重组状态保留至下次调用。
        """
        if self._fragment_opcode is not None:
            while True:
                fin, opcode, payload = await self._read_frame()
                if opcode in (OP_PING, OP_PONG, OP_CLOSE):
                    return opcode, payload
                if opcode != OP_CONT:
                    raise WebSocketError(f"expected continuation, got opcode {opcode}")
                assert self._fragment_chunks is not None
                self._fragment_chunks.append(payload)
                if fin:
                    assert self._fragment_chunks is not None
                    opcode, payload = self._fragment_opcode, b"".join(self._fragment_chunks)
                    self._fragment_opcode = None
                    self._fragment_chunks = None
                    return opcode, payload
        fin, opcode, payload = await self._read_frame()
        if opcode in (OP_PING, OP_PONG, OP_CLOSE, OP_CONT):
            return opcode, payload
        if not fin:
            self._fragment_opcode = opcode
            self._fragment_chunks = [payload]
            return await self.recv_message()
        return opcode, payload

    async def send_frame(self, opcode: int, payload: bytes = b"") -> None:
        """发送一帧（客户端帧始终带掩码）。"""
        if len(payload) > self._max_message_size:
            raise WebSocketError("payload too large")
        mask = os.urandom(4)
        n = len(payload)
        header = bytearray([0x80 | opcode])
        if n < 126:
            header.append(0x80 | n)
        elif n <= 0xFFFF:
            header.append(0x80 | 126)
            header += n.to_bytes(2, "big")
        else:
            header.append(0x80 | 127)
            header += n.to_bytes(8, "big")
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        assert self._writer is not None, "connect() must precede writes"
        self._writer.write(bytes(header) + mask + masked)
        await self._writer.drain()

    async def send_ping(self, payload: bytes = b"") -> None:
        await self.send_frame(OP_PING, payload)

    async def send_pong(self, payload: bytes = b"") -> None:
        await self.send_frame(OP_PONG, payload)

    def abort(self) -> None:
        """立即断开底层连接（不发送 close 帧，供重连/停机使用）。"""
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()


def _parse_close_payload(payload: bytes) -> tuple[int, str]:
    code = int.from_bytes(payload[:2], "big") if len(payload) >= 2 else 1005
    reason = payload[2:].decode("utf-8", errors="replace")
    return code, reason


class _StreamGroup:
    """单条 WebSocket 连接组：承载 ≤ max_streams 条流，独立重连退避。"""

    def __init__(self, owner: "BinanceUsdmWebSocketClient", group_id: int) -> None:
        self.owner = owner
        self.group_id = group_id
        self._streams: dict[str, list[Callback]] = {}
        self._ws: WebSocketConnection | None = None
        self._task: asyncio.Task | None = None
        self._closed = False
        self._backoff = INITIAL_RECONNECT_BACKOFF
        self._ping_pending = False
        self._stable_pings = 0  # BD-FIX: 连续稳定 ping 计数（退避归零门槛）
        self.state = ConnectionState.DISCONNECTED

    # === 主循环 ===

    async def _run(self) -> None:
        while not self._closed:
            if not self._streams:
                await asyncio.sleep(0.5)
                continue
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if isinstance(exc, WebSocketClosedError) and exc.code in (1008, 1013):
                    self._backoff = max(self._backoff, 10.0)  # Binance 限流/过载断开
                logger.warning("ws group %d connection lost: %s", self.group_id, exc)
            await self._abort_ws()
            if self._closed:
                break
            self._set_state(ConnectionState.RECONNECTING)
            wait = min(self._backoff, MAX_RECONNECT_BACKOFF)
            self._backoff = min(self._backoff * 2, MAX_RECONNECT_BACKOFF)
            logger.info(
                "ws group %d reconnecting in %.1fs (streams=%d)",
                self.group_id,
                wait,
                len(self._streams),
            )
            await asyncio.sleep(wait)
        self._set_state(ConnectionState.DISCONNECTED)

    async def _connect_and_listen(self) -> None:
        self._set_state(ConnectionState.CONNECTING)
        url = _combined_stream_url(self.owner._base_url, sorted(self._streams))
        if len(url) > 6000:
            logger.warning("ws group %d combined URL very long (%d chars)", self.group_id, len(url))
        ws = WebSocketConnection()
        self._ws = ws
        await ws.connect(url)
        self._set_state(ConnectionState.CONNECTED)
        # BD-FIX: 新连接重置稳定计数（退避归零需本连接的连续稳定 ping）
        self._stable_pings = 0
        pinger = asyncio.create_task(self._ping_loop(ws))
        try:
            await self._listen(ws)
        finally:
            pinger.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pinger

    async def _listen(self, ws: WebSocketConnection) -> None:
        while True:
            opcode, payload = await ws.recv_message()
            if opcode == OP_PING:
                await ws.send_pong(payload)  # 响应服务端 ping
            elif opcode == OP_PONG:
                self._ping_pending = False
            elif opcode == OP_CLOSE:
                code, reason = _parse_close_payload(payload)
                raise WebSocketClosedError(code, reason)
            elif opcode == OP_CONT:
                raise WebSocketError("unexpected continuation frame")
            else:
                await self._handle_text(payload.decode("utf-8", errors="replace"))

    async def _ping_loop(self, ws: WebSocketConnection) -> None:
        """周期 ping 探测死链，pong 超时强制断开触发重连；连接稳定后退避归零。"""
        try:
            while True:
                await asyncio.sleep(self.owner.ping_interval)
                if self.state is not ConnectionState.CONNECTED:
                    return
                self._ping_pending = True
                try:
                    await ws.send_ping()
                    await asyncio.sleep(self.owner.pong_timeout)
                    if self._ping_pending:
                        raise ConnectionError("pong timeout")
                except Exception as exc:
                    logger.warning("ws group %d keepalive failed: %s", self.group_id, exc)
                    await self._abort_ws()
                    return
                # BD-FIX（M1 审查）: 单次 ping 成功即归零退避 —— demo
                # 抖动下连接存活 >70s 即永远停在 1s 退避，反复快速重连。
                # 要求连续多次稳定 ping 周期（约 3 分钟）才归零。
                self._stable_pings += 1
                if self._stable_pings >= 3:
                    self._backoff = INITIAL_RECONNECT_BACKOFF  # 连接持续稳定，退避归零
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("ws group %d ping loop exited: %s", self.group_id, exc)

    async def _handle_text(self, text: str) -> None:
        try:
            msg = json.loads(text)
        except (ValueError, TypeError):
            logger.warning("ws group %d invalid JSON: %.200s", self.group_id, text)
            return
        stream: str | None = None
        data: Any = msg
        if isinstance(msg, dict):
            stream = msg.get("stream")
            data = msg.get("data", msg)
        if not stream:
            stream = next(iter(self._streams), None)  # 裸流消息回退到组内唯一流
        if stream is None:
            return  # 无流可路由 —— 丢弃(消息不可归属)
        await self.owner._dispatch(stream, data)

    # === 状态与资源 ===

    def _set_state(self, state: ConnectionState) -> None:
        if self.state is state:
            return
        old = self.state
        self.state = state
        self.owner._on_group_state_changed(self, old, state)

    async def _abort_ws(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            ws.abort()

    async def _stop(self) -> None:
        """永久停止本组：断开、取消任务、置 DISCONNECTED。"""
        self._closed = True
        await self._abort_ws()
        if self._task is not None and self._task is not asyncio.current_task():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._set_state(ConnectionState.DISCONNECTED)


class BinanceUsdmWebSocketClient:
    """Binance USDⓈ-M 期货 WebSocket 行情客户端。

    自动将订阅按每连接上限拆分到多个连接组；回调签名 (stream, data)，
    async 回调在事件循环内按序执行（保证用户数据流事件顺序），
    同步回调在默认线程池执行（不阻塞行情接收）。
    """

    def __init__(
        self,
        base_url: str = FSTREAM_PRODUCTION_URL,
        max_streams_per_connection: int = MAX_STREAMS_PER_CONNECTION,
        ping_interval: float = PING_INTERVAL,
        pong_timeout: float = PONG_TIMEOUT,
    ) -> None:
        if max_streams_per_connection < 1:
            raise ValueError("max_streams_per_connection must be >= 1")
        self._base_url = base_url.rstrip("/")
        self.ping_interval = ping_interval
        self.pong_timeout = pong_timeout
        self._max_streams = max_streams_per_connection
        self._groups: list[_StreamGroup] = []
        self._global_callbacks: list[Callback] = []
        self._state_change_callbacks: list[Callable[[ConnectionState, ConnectionState, int], Any]] = []
        self._hooks_lock = threading.Lock()
        self._state_change_tasks: set[asyncio.Task] = set()
        self._stopped = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    # === 状态查询 ===

    @property
    def state(self) -> ConnectionState:
        """聚合状态：任一连接异常即 RECONNECTING，全部正常即 CONNECTED。"""
        states = {g.state for g in self._groups if g._streams}
        if not states:
            return ConnectionState.DISCONNECTED
        if any(s in (ConnectionState.CONNECTING, ConnectionState.RECONNECTING) for s in states):
            return ConnectionState.RECONNECTING
        if any(s is ConnectionState.CONNECTED for s in states):
            return ConnectionState.CONNECTED
        return ConnectionState.DISCONNECTED

    @property
    def streams(self) -> frozenset[str]:
        """当前全部已订阅流。"""
        return frozenset(s for g in self._groups for s in g._streams)

    @property
    def group_count(self) -> int:
        return len(self._groups)

    # === 订阅管理 ===

    def _pick_group(self, stream: str) -> _StreamGroup | None:
        for g in self._groups:
            if stream in g._streams:
                return g
        for g in self._groups:
            if len(g._streams) < self._max_streams:
                return g
        return None

    def _add_stream_callback(self, stream: str, callback: Callback) -> _StreamGroup | None:
        """注册回调（纯状态变更，可在任意线程调用）。

        新增流时返回所属组（需要重连或启动），仅追加回调时返回 None。
        """
        group = self._pick_group(stream)
        if group is None:
            group = _StreamGroup(self, len(self._groups) + 1)
            self._groups.append(group)
        callbacks = group._streams.setdefault(stream, [])
        if callback in callbacks:
            return None
        callbacks.append(callback)
        return group

    async def subscribe(self, stream: str, callback: Callback) -> None:
        """订阅一条流（如 'btcusdt@ticker'、'btcusdt@depth5@100ms' 或 listenKey）。

        新增流会使所在连接携带新 URL 自动重连；回调轻量即可，详见类文档。
        """
        _validate_stream(stream)
        group = self._add_stream_callback(stream, callback)
        if group is None:
            return
        logger.info("subscribe %s (group %d)", stream, group.group_id)
        self._ensure_group_task(group)
        if group.state in (ConnectionState.CONNECTING, ConnectionState.CONNECTED):
            await group._abort_ws()  # 组合流变更需重连生效（突发订阅会在下次连接一并生效）

    async def unsubscribe(self, stream: str, callback: Callback | None = None) -> bool:
        """取消订阅。callback 为 None 时移除该流全部回调；组空则关闭连接。"""
        group = next((g for g in self._groups if stream in g._streams), None)
        if group is None:
            return False
        if callback is not None:
            callbacks = group._streams[stream]
            if callback in callbacks:
                callbacks.remove(callback)
            if callbacks:
                return True
        del group._streams[stream]
        logger.info("unsubscribe %s (group %d)", stream, group.group_id)
        if group._streams:
            if group.state in (ConnectionState.CONNECTING, ConnectionState.CONNECTED):
                await group._abort_ws()
        else:
            await group._stop()
        return True

    def subscribe_sync(self, stream: str, callback: Callback, wait: bool = True) -> None:
        """线程安全订阅：可在任意线程调用（start() 之后）。"""
        if self._loop is None:
            self._add_stream_callback(stream, callback)
            return
        fut = asyncio.run_coroutine_threadsafe(self.subscribe(stream, callback), self._loop)
        if wait:
            fut.result(timeout=10.0)

    def unsubscribe_sync(self, stream: str, callback: Callback | None = None) -> bool:
        """线程安全取消订阅：可在任意线程调用。"""
        if self._loop is None:
            group = next((g for g in self._groups if stream in g._streams), None)
            if group is None:
                return False
            if callback is not None:
                callbacks = group._streams[stream]
                if callback in callbacks:
                    callbacks.remove(callback)
                if callbacks:
                    return True
            del group._streams[stream]
            return True
        fut = asyncio.run_coroutine_threadsafe(self.unsubscribe(stream, callback), self._loop)
        return fut.result(timeout=10.0)

    # === 全局钩子 ===

    def on_message(self, callback: Callback) -> None:
        """注册全局消息回调 (stream, data)，接收所有流。线程安全。"""
        with self._hooks_lock:
            self._global_callbacks.append(callback)

    def on_state_change(self, callback: Callable[[ConnectionState, ConnectionState, int], Any]) -> None:
        """注册状态变化回调 (old, new, group_id)。线程安全。"""
        with self._hooks_lock:
            self._state_change_callbacks.append(callback)

    # === 生命周期 ===

    def _ensure_group_task(self, group: _StreamGroup) -> None:
        group._closed = False
        if group._task is None or group._task.done():
            group._task = asyncio.create_task(group._run())

    async def start(self) -> None:
        """启动所有已注册订阅的连接（subscribe 已自动启动，此处保证幂等）。"""
        self._loop = asyncio.get_running_loop()
        for group in self._groups:
            self._ensure_group_task(group)

    async def run(self) -> None:
        """启动并持续运行，直到 close() 被调用。"""
        await self.start()
        try:
            await self._stopped.wait()
        finally:
            await self.close()

    async def close(self) -> None:
        """停止所有连接并释放资源。"""
        for group in self._groups:
            await group._stop()
        self._stopped.set()

    # === 回调分发 ===

    async def _dispatch(self, stream: str, data: Any) -> None:
        for group in self._groups:
            for cb in list(group._streams.get(stream, ())):
                await self._invoke_callback(cb, stream, data)
        with self._hooks_lock:
            global_cbs = list(self._global_callbacks)
        for cb in global_cbs:
            await self._invoke_callback(cb, stream, data)

    @staticmethod
    async def _invoke_callback(cb: Callback, stream: str, data: Any) -> None:
        try:
            if inspect.iscoroutinefunction(cb):
                await cb(stream, data)  # 在事件循环内按序执行
            else:
                await asyncio.get_running_loop().run_in_executor(None, cb, stream, data)
        except Exception:
            logger.exception("ws callback error for stream %s", stream)

    def _on_group_state_changed(self, group: _StreamGroup, old: ConnectionState, new: ConnectionState) -> None:
        with self._hooks_lock:
            cbs = list(self._state_change_callbacks)
        for cb in cbs:
            try:
                result = cb(old, new, group.group_id)
                if inspect.isawaitable(result):
                    _coro: Any = result
                    task = asyncio.create_task(_coro)
                    self._state_change_tasks.add(task)
                    task.add_done_callback(self._state_change_tasks.discard)
            except Exception:
                logger.exception("ws state-change callback error")


__all__ = [
    "FSTREAM_PRODUCTION_URL",
    "FSTREAM_TESTNET_URL",
    "BinanceUsdmWebSocketClient",
    "ConnectionState",
    "WebSocketConnection",
    "split_stream",
]
