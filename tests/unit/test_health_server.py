"""HTTP-level contracts for the four independent health layers."""

from __future__ import annotations

import json
import socket
from urllib.error import HTTPError
from urllib.request import urlopen

from beidou_core.health import HealthServer, HealthState


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get(base: str, path: str) -> tuple[int, bytes]:
    try:
        with urlopen(base + path, timeout=2) as response:  # noqa: S310 - fixed localhost HTTP test endpoint
            return response.status, response.read()
    except HTTPError as exc:
        try:
            return exc.code, exc.read()
        finally:
            exc.close()


def test_health_server_exposes_distinct_default_layers() -> None:
    server = HealthServer(port=_free_port())
    server.start()
    base = f"http://127.0.0.1:{server._port}"
    try:
        status, body = _get(base, "/health")
        assert status == 200
        assert json.loads(body)["status"] == "HEALTHY"

        status, body = _get(base, "/ready")
        assert status == 200
        assert json.loads(body)["ready"] is True

        status, body = _get(base, "/trading-ready")
        assert status == 503
        assert json.loads(body) == {
            "trading_ready": False,
            "reason": "NO_CERTIFICATE",
            "uptime_seconds": json.loads(body)["uptime_seconds"],
        }

        status, body = _get(base, "/exit-ready")
        assert status == 503
        assert json.loads(body)["reason"] == "EXIT_READINESS_NOT_CONFIGURED"

        status, body = _get(base, "/factors")
        assert status == 200
        assert json.loads(body) == {"factors": [], "count": 0}

        status, body = _get(base, "/metrics")
        assert status == 200
        assert b"beidou_uptime_seconds" in body

        status, body = _get(base, "/not-found")
        assert status == 404
        assert json.loads(body)["error"] == "not found"
    finally:
        server.stop()


def test_health_server_callbacks_preserve_degraded_vs_unhealthy_semantics() -> None:
    server = HealthServer(port=_free_port())
    server.set_liveness_check(lambda: HealthState.DEGRADED)
    server.set_readiness_check(lambda: False)
    server.set_trading_readiness(lambda: (True, "SUPERVISOR_VALIDATED"))
    server.set_exit_readiness(lambda: (True, "READY"))
    server.set_metrics_collector(lambda: {"queue-depth": 3, "nested": {"state": 1}})
    server.set_status_info(lambda: {"supervisor": {"state": "RUNNING"}})
    server.set_factor_provider(lambda: [{"factor_id": "f1", "lifecycle": "ACTIVE"}])
    server.start()
    base = f"http://127.0.0.1:{server._port}"
    try:
        status, body = _get(base, "/health")
        assert status == 200
        assert json.loads(body)["status"] == "DEGRADED"

        status, body = _get(base, "/ready")
        assert status == 503
        assert json.loads(body) == {"ready": False, "supervisor": {"state": "RUNNING"}}

        status, body = _get(base, "/trading-ready")
        assert status == 200
        assert json.loads(body)["reason"] == "SUPERVISOR_VALIDATED"

        status, body = _get(base, "/exit-ready")
        assert status == 200
        assert json.loads(body)["exit_ready"] is True

        status, body = _get(base, "/status")
        payload = json.loads(body)
        assert status == 200
        assert payload["liveness"] == "DEGRADED"
        assert payload["trading_ready"] is True
        assert payload["exit_ready"] is True

        status, body = _get(base, "/factors")
        assert status == 200
        assert json.loads(body)["count"] == 1

        status, body = _get(base, "/metrics")
        assert status == 200
        assert b"beidou_queue_depth" in body
        assert b'beidou_nested{key="state"} 1' in body
    finally:
        server.stop()


def test_unhealthy_liveness_returns_http_503() -> None:
    server = HealthServer(port=_free_port())
    server.set_liveness_check(lambda: HealthState.UNHEALTHY)
    server.start()
    try:
        status, body = _get(f"http://127.0.0.1:{server._port}", "/health")
        assert status == 503
        assert json.loads(body)["status"] == "UNHEALTHY"
    finally:
        server.stop()
