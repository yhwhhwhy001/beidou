"""Authoritative startup manifest for the Beidou runtime."""

from __future__ import annotations

from .registry import EXPECTED_ALPHA_COMPONENTS, EXPECTED_FACTORS, REQUIRED_ENGINE_ATTRIBUTES, REQUIRED_PACKAGES

SUPPORTED_MODES = ("research", "paper", "shadow", "testnet", "safety_only")
DEFAULT_MODE = "testnet"
DEFAULT_SYMBOLS = ("DEFAULT",)
HEALTH_PORT = 9090
EXPECTED_FACTOR_COUNT = len(EXPECTED_FACTORS)

__all__ = [
    "DEFAULT_MODE",
    "DEFAULT_SYMBOLS",
    "EXPECTED_ALPHA_COMPONENTS",
    "EXPECTED_FACTOR_COUNT",
    "EXPECTED_FACTORS",
    "HEALTH_PORT",
    "REQUIRED_ENGINE_ATTRIBUTES",
    "REQUIRED_PACKAGES",
    "SUPPORTED_MODES",
]
