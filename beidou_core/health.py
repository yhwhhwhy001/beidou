"""健康检查 HTTP 服务器 — /health /ready /metrics 端点。

基于 http.server，零外部依赖。端口 9090 与 config 中 prometheus_port 一致。
"""

from __future__ import annotations

import json
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Callable


class HealthServer:
    """轻量 HTTP 健康检查服务器。"""

    def __init__(self, port: int = 9090) -> None:
        self._port = port
        self._start_time = time.time()
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

        # 可注册的回调
        self._readiness_check: Callable[[], bool] = lambda: True
        self._metrics_collector: Callable[[], dict] = lambda: {}
        self._status_info: Callable[[], dict] = lambda: {}

    def set_readiness_check(self, fn: Callable[[], bool]) -> None:
        self._readiness_check = fn

    def set_metrics_collector(self, fn: Callable[[], dict]) -> None:
        self._metrics_collector = fn

    def set_status_info(self, fn: Callable[[], dict]) -> None:
        self._status_info = fn

    def uptime_seconds(self) -> float:
        return time.time() - self._start_time

    def start(self) -> None:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass  # Suppress access logs

            def do_GET(self):
                if self.path == "/health":
                    self._send_json(200, {
                        "status": "ok",
                        "uptime_seconds": round(server.uptime_seconds(), 1),
                        "version": "2.0.0",
                    })
                elif self.path == "/ready":
                    ready = server._readiness_check()
                    status_info = server._status_info()
                    self._send_json(200 if ready else 503, {
                        "ready": ready,
                        **status_info,
                    })
                elif self.path == "/metrics":
                    metrics = server._metrics_collector()
                    self._send_prometheus(metrics)
                elif self.path == "/status":
                    status_info = server._status_info()
                    self._send_json(200, {
                        "uptime_seconds": round(server.uptime_seconds(), 1),
                        **status_info,
                    })
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
                            lines.append(f"beidou_{safe_name}{{key=\"{k}\"}} {v}")

                body = "\n".join(lines).encode() + b"\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = HTTPServer(("0.0.0.0", self._port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None
