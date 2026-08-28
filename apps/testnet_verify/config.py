"""Configuration for the bounded local Binance Testnet verifier."""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class VerifierConfig:
    rest_url: str = "https://demo-fapi.binance.com"
    api_key: str = field(default="", repr=False)
    api_secret: str = field(default="", repr=False)
    account_id: str = "testnet-verification-account"
    max_notional: float = 25.0
    max_leverage: float = 3.0
    max_instruments: int = 5
    interval: str = "1m"
    kline_limit: int = 1500
    order_poll_attempts: int = 5
    order_poll_interval_seconds: float = 1.0
    confirm_testnet: bool = False
    once: bool = False
    close_after_verify: bool = False
    trace_path: Path = Path(".beidou/testnet_verification/decision-trace.jsonl")
    pool_state_path: Path = Path(".beidou/testnet_verification/pool-state.json")
    kill_switch_path: Path = Path(".beidou/testnet_verification/KILL_SWITCH")
    evidence_dir: Path = Path("evidence/testnet-verification")

    @classmethod
    def from_env(cls, **overrides: object) -> VerifierConfig:
        """Load non-secret defaults from the environment and explicit flags."""

        defaults = cls()

        def env_float(name: str, default: float) -> float:
            raw = os.getenv(name)
            if raw is None or raw == "":
                return default
            try:
                return float(raw)
            except ValueError as exc:
                raise ValueError(f"{name} must be numeric") from exc

        def env_int(name: str, default: int) -> int:
            raw = os.getenv(name)
            if raw is None or raw == "":
                return default
            try:
                return int(raw)
            except ValueError as exc:
                raise ValueError(f"{name} must be an integer") from exc

        values: dict[str, Any] = {
            "rest_url": os.getenv("BEIDOU_TESTNET_REST_URL", defaults.rest_url),
            "api_key": os.getenv("BEIDOU_TESTNET_API_KEY", ""),
            "api_secret": os.getenv("BEIDOU_TESTNET_API_SECRET", ""),
            "account_id": os.getenv("BEIDOU_TESTNET_ACCOUNT_ID", defaults.account_id),
            "max_notional": env_float("BEIDOU_TESTNET_MAX_NOTIONAL", defaults.max_notional),
            "max_leverage": env_float("BEIDOU_TESTNET_MAX_LEVERAGE", defaults.max_leverage),
            "max_instruments": env_int("BEIDOU_TESTNET_MAX_INSTRUMENTS", defaults.max_instruments),
            "interval": os.getenv("BEIDOU_TESTNET_INTERVAL", defaults.interval),
            "kline_limit": env_int("BEIDOU_TESTNET_KLINE_LIMIT", defaults.kline_limit),
            "order_poll_attempts": env_int("BEIDOU_TESTNET_ORDER_POLL_ATTEMPTS", defaults.order_poll_attempts),
            "order_poll_interval_seconds": env_float(
                "BEIDOU_TESTNET_ORDER_POLL_INTERVAL_SECONDS", defaults.order_poll_interval_seconds
            ),
            "trace_path": Path(os.getenv("BEIDOU_TESTNET_TRACE_PATH", str(defaults.trace_path))),
            "pool_state_path": Path(os.getenv("BEIDOU_TESTNET_POOL_STATE_PATH", str(defaults.pool_state_path))),
            "kill_switch_path": Path(os.getenv("BEIDOU_TESTNET_KILL_SWITCH_PATH", str(defaults.kill_switch_path))),
            "evidence_dir": Path(os.getenv("BEIDOU_TESTNET_EVIDENCE_DIR", str(defaults.evidence_dir))),
        }
        values.update(overrides)
        config = cls(**values)
        config.validate()
        return config

    def validate(self) -> None:
        if (
            not isinstance(self.max_notional, (int, float))
            or not isinstance(self.max_leverage, (int, float))
            or not math.isfinite(float(self.max_notional))
            or not math.isfinite(float(self.max_leverage))
            or self.max_notional <= 0
            or self.max_leverage <= 0
        ):
            raise ValueError("max_notional and max_leverage must be positive")
        if self.max_instruments <= 0 or self.max_instruments > 50:
            raise ValueError("max_instruments must be within 1..50")
        if self.kline_limit < 10 or self.kline_limit > 1500:
            raise ValueError("kline_limit must be within 10..1500")
        if self.interval not in {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}:
            raise ValueError("interval is not supported by the verifier")
        if self.order_poll_attempts < 1 or self.order_poll_attempts > 60:
            raise ValueError("order_poll_attempts must be within 1..60")
        if (
            not math.isfinite(float(self.order_poll_interval_seconds))
            or not 0 <= self.order_poll_interval_seconds <= 30
        ):
            raise ValueError("order_poll_interval_seconds must be within 0..30")
        if not str(self.account_id).strip() or str(self.account_id).upper() in {"UNKNOWN", "DEFAULT"}:
            raise ValueError("a non-default account_id is required")

    def redacted_dict(self) -> dict[str, object]:
        """Return configuration facts safe for an evidence manifest."""

        return {
            "rest_url": self.rest_url,
            "account_id": self.account_id,
            "max_notional": self.max_notional,
            "max_leverage": self.max_leverage,
            "max_instruments": self.max_instruments,
            "interval": self.interval,
            "kline_limit": self.kline_limit,
            "order_poll_attempts": self.order_poll_attempts,
            "order_poll_interval_seconds": self.order_poll_interval_seconds,
            "confirm_testnet": self.confirm_testnet,
            "once": self.once,
            "close_after_verify": self.close_after_verify,
            "trace_path": str(self.trace_path),
            "pool_state_path": str(self.pool_state_path),
            "kill_switch_path": str(self.kill_switch_path),
            "evidence_dir": str(self.evidence_dir),
            "api_key_present": bool(self.api_key),
            "api_secret_present": bool(self.api_secret),
        }

    def config_hash(self) -> str:
        payload = json.dumps(self.redacted_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
