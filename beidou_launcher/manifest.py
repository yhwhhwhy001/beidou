"""Authoritative startup manifest for the Beidou runtime."""

from __future__ import annotations

from .registry import EXPECTED_ALPHA_COMPONENTS, EXPECTED_FACTORS, REQUIRED_ENGINE_ATTRIBUTES, REQUIRED_PACKAGES

SUPPORTED_MODES = ("research", "paper", "shadow", "testnet", "safety_only")
DEFAULT_MODE = "safety_only"
# Kept as an empty compatibility export for callers that imported the old
# manifest.  Startup no longer treats this as a symbol source; an omitted
# override is resolved from the read-only trading-pool discovery path.
DEFAULT_SYMBOLS: tuple[str, ...] = ()
HEALTH_PORT = 9090
STARTUP_TIMEOUT = 300.0
MONITOR_INTERVAL = 5.0
MAX_RESTARTS = 10
RESTART_WINDOW_SECONDS = 600
EXPECTED_FACTOR_COUNT = len(EXPECTED_FACTORS)

__all__ = [
    "DEFAULT_MODE",
    "DEFAULT_SYMBOLS",
    "EXPECTED_ALPHA_COMPONENTS",
    "EXPECTED_FACTORS",
    "EXPECTED_FACTOR_COUNT",
    "HEALTH_PORT",
    "MAX_RESTARTS",
    "MONITOR_INTERVAL",
    "REQUIRED_ENGINE_ATTRIBUTES",
    "REQUIRED_PACKAGES",
    "RESTART_WINDOW_SECONDS",
    "STARTUP_TIMEOUT",
    "SUPPORTED_MODES",
]
