"""健康检查 HTTP 服务器 — 四层健康状态 (BD-P1-15)。

端点:
  /health        — Liveness (进程存活)
  /ready         — Readiness (可接受请求)
  /trading-ready — Trading Readiness (可接受交易操作)
  /exit-ready    — Exit Readiness (可执行退出订单)
  /metrics       — Prometheus 指标
  /status        — 完整状态信息

BD-P1-15 AC-15-03: 四种健康状态独立可查询。
"""

from __future__ import annotations

import json
import socket
import threading
import time
from enum import Enum
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable


class HealthState(str, Enum):
    """BD-P1-15: 四层健康状态。"""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"
    UNKNOWN = "UNKNOWN"


class HealthServer:
    """BD-P1-15: 四层健康检查 HTTP 服务器。

    每层有独立回调，可独立查询。
    """

    def __init__(self, port: int = 9090, bind_host: str = "127.0.0.1") -> None:
        self._port = port
        self._bind_host = bind_host
        self._start_time = time.time()
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

        # BD-P1-15: 四层健康检查回执
        self._liveness_check: Callable[[], HealthState] = lambda: HealthState.HEALTHY
        self._readiness_check: Callable[[], bool] = lambda: True
        self._trading_readiness_check: Callable[[], tuple[bool, str]] = lambda: (False, "NO_CERTIFICATE")
        self._exit_readiness_check: Callable[[], tuple[bool, str]] = lambda: (
            False,
            "EXIT_READINESS_NOT_CONFIGURED",
        )  # BD-FIX: 默认返回不可退出，需显式设置

        # 其他回执
        self._metrics_collector: Callable[[], dict] = lambda: {}
        self._status_info: Callable[[], dict] = lambda: {}

    # --- Setters ---

    def set_readiness_check(self, fn: Callable[[], bool]) -> None:
        self._readiness_check = fn

    def set_liveness_check(self, fn: Callable[[], HealthState]) -> None:
        """BD-P1-15: 设置 Liveness 检查回执。"""
        self._liveness_check = fn

    def set_trading_readiness(self, fn: Callable[[], tuple[bool, str]]) -> None:
        """BD-P1-15: 设置 Trading Readiness 检查回执。
        返回 (ready: bool, reason: str)"""
        self._trading_readiness_check = fn

    def set_exit_readiness(self, fn: Callable[[], tuple[bool, str]]) -> None:
        """BD-P1-15: 设置 Exit Readiness 检查回执。"""
        self._exit_readiness_check = fn

    def set_metrics_collector(self, fn: Callable[[], dict]) -> None:
        self._metrics_collector = fn

    def set_status_info(self, fn: Callable[[], dict]) -> None:
        self._status_info = fn

    def set_factor_provider(self, fn: Callable[[], list[dict]]) -> None:
        self._factor_provider = fn

    def uptime_seconds(self) -> float:
        return time.time() - self._start_time

    def start(self) -> None:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass  # Suppress access logs

            def do_GET(self):
                if self.path == "/health":
                    # BD-P1-15: Liveness — 进程存活
                    liveness = server._liveness_check()
                    code = 200 if liveness in (HealthState.HEALTHY, HealthState.DEGRADED) else 503
                    self._send_json(
                        code,
                        {
                            "status": liveness.value,
                            "uptime_seconds": round(server.uptime_seconds(), 1),
                            "version": "2.0.0",
                        },
                    )
                elif self.path == "/ready":
                    ready = server._readiness_check()
                    status_info = server._status_info()
                    self._send_json(
                        200 if ready else 503,
                        {"ready": ready, **status_info},
                    )
                elif self.path == "/trading-ready":
                    # BD-P1-15: Trading Readiness — 可接受交易操作
                    trading_ready, reason = server._trading_readiness_check()
                    self._send_json(
                        200 if trading_ready else 503,
                        {
                            "trading_ready": trading_ready,
                            "reason": reason,
                            "uptime_seconds": round(server.uptime_seconds(), 1),
                        },
                    )
                elif self.path == "/exit-ready":
                    # BD-P1-15: Exit Readiness — 可执行退出订单
                    exit_ready, reason = server._exit_readiness_check()
                    self._send_json(
                        200 if exit_ready else 503,
                        {
                            "exit_ready": exit_ready,
                            "reason": reason,
                        },
                    )
                elif self.path == "/metrics":
                    metrics = server._metrics_collector()
                    self._send_prometheus(metrics)
                elif self.path == "/status":
                    status_info = server._status_info()
                    liveness = server._liveness_check()
                    trading_ready, tr_reason = server._trading_readiness_check()
                    exit_ready, ex_reason = server._exit_readiness_check()
                    self._send_json(
                        200,
                        {
                            "uptime_seconds": round(server.uptime_seconds(), 1),
                            "liveness": liveness.value,
                            **status_info,
                            "trading_ready": trading_ready,
                            "trading_ready_reason": tr_reason,
                            "exit_ready": exit_ready,
                            "exit_ready_reason": ex_reason,
                        },
                    )
                elif self.path == "/factors":
                    provider = getattr(server, "_factor_provider", None)
                    factors = provider() if provider else []
                    self._send_json(200, {"factors": factors, "count": len(factors)})
                else:
                    self._send_json(404, {"error": "not found"})

            def _send_json(self, code: int, data: dict) -> None:
                body = json.dumps(data, indent=2).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_prometheus(self, metrics: dict) -> None:
                lines = [
                    "# HELP beidou_uptime_seconds Engine uptime in seconds",
                    "# TYPE beidou_uptime_seconds gauge",
                    f"beidou_uptime_seconds {server.uptime_seconds()}",
                ]
                for name, value in metrics.items():
                    safe_name = name.replace("-", "_").replace(" ", "_")
                    if isinstance(value, (int, float)):
                        lines.append(f"# HELP beidou_{safe_name} {name}")
                        lines.append(f"# TYPE beidou_{safe_name} gauge")
                        lines.append(f"beidou_{safe_name} {value}")
                    elif isinstance(value, dict):
                        for k, v in value.items():
                            lines.append(f'beidou_{safe_name}{{key="{k}"}} {v}')

                body = "\n".join(lines).encode() + b"\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        # BD-FIX: allow_reuse_address + SO_REUSEADDR 防止 TIME_WAIT 导致的端口冲突。
        # 启动时如果端口被占用（可能是残留的 TIME_WAIT），最多重试 3 次，间隔 2s。
        import time as _time

        last_err = None
        for attempt in range(3):
            try:
                self._server = HTTPServer((self._bind_host, self._port), Handler)
                self._server.allow_reuse_address = True
                self._server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                break
            except OSError as exc:
                last_err = exc
                if attempt < 2:
                    _time.sleep(2.0)
        else:
            raise OSError(f"HealthServer port {self._port} unavailable after 3 retries: {last_err}") from last_err

        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None
