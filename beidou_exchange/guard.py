"""Local write guard: exact host allow-list, kill-switch file, reduce-only pass-through.

This is not an authorization system.  It is the last local check before a
mutating request leaves the process: mainnet hosts are refused outright, and
while the kill-switch file exists only risk-reducing writes (cancel,
reduce-only orders, leverage/session control) are allowed.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ALLOWED_HOSTS: frozenset[str] = frozenset({"demo-fapi.binance.com", "testnet.binancefuture.com"})
MUTATING_METHODS: frozenset[str] = frozenset({"POST", "PUT", "DELETE"})
ORDER_PATHS: frozenset[str] = frozenset({"/fapi/v1/order", "/fapi/v1/batchOrders"})


class GuardError(PermissionError):
    pass


def normalize_host(rest_url: str) -> str:
    parsed = urlsplit(str(rest_url or "").strip())
    if parsed.scheme.lower() != "https":
        raise GuardError("REST URL must use https")
    if not parsed.hostname:
        raise GuardError("REST URL must contain a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise GuardError("credentials in the REST URL are forbidden")
    if parsed.port not in (None, 443):
        raise GuardError("REST URL must use port 443")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise GuardError("REST base URL must not carry a path, query or fragment")
    host = parsed.hostname.lower()
    if host not in ALLOWED_HOSTS:
        raise GuardError(f"host {host!r} is not an allowed demo/testnet host")
    return host


def is_risk_reducing(method: str, path: str, params: Mapping[str, Any]) -> bool:
    if method == "DELETE":
        return True
    if path in ORDER_PATHS:
        flag = str(params.get("reduceOnly", "false")).lower()
        return flag == "true" or str(params.get("closePosition", "false")).lower() == "true"
    return path not in ORDER_PATHS  # leverage / margin type / listen-key style session control


class WriteGuard:
    def __init__(self, rest_url: str, kill_switch_path: str | Path | None = None) -> None:
        self.host = normalize_host(rest_url)
        self.kill_switch_path = None if kill_switch_path is None else Path(kill_switch_path)

    def kill_switch_engaged(self) -> bool:
        return self.kill_switch_path is not None and self.kill_switch_path.exists()

    def engage_kill_switch(self, reason: str = "") -> Path:
        if self.kill_switch_path is None:
            raise GuardError("no kill switch path configured")
        self.kill_switch_path.parent.mkdir(parents=True, exist_ok=True)
        self.kill_switch_path.write_text(reason or "engaged\n", encoding="utf-8")
        return self.kill_switch_path

    def authorize(self, method: str, path: str, params: Mapping[str, Any]) -> None:
        method = method.upper()
        if method not in MUTATING_METHODS:
            return
        if self.kill_switch_engaged() and not is_risk_reducing(method, path, params):
            raise GuardError(f"kill switch engaged at {self.kill_switch_path}; risk-increasing write refused")
