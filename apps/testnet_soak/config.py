"""Immutable bounds for the finite Testnet execution-probe campaign."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apps.testnet_verify.config import DEFAULT_TESTNET_ACCOUNT_ID, VerifierConfig


@dataclass(frozen=True, slots=True)
class SoakConfig:
    rest_url: str = "https://demo-fapi.binance.com"
    api_key: str = field(default="", repr=False)
    api_secret: str = field(default="", repr=False)
    account_id: str = DEFAULT_TESTNET_ACCOUNT_ID
    episodes: int = 30
    max_duration_seconds: float = 3600
    cycle_interval_seconds: float = 120
    target_notional: float = 100
    absolute_notional_ceiling: float = 500
    max_leverage: float = 3
    symbol: str = "BTCUSDT"
    confirm_testnet: bool = False
    interval: str = "1m"
    kline_limit: int = 1500
    order_poll_attempts: int = 5
    order_poll_interval_seconds: float = 1.0
    trace_path: Path = Path(".beidou/testnet_soak/decision-trace.jsonl")
    pool_state_path: Path = Path(".beidou/testnet_soak/pool-state.json")
    kill_switch_path: Path = Path(".beidou/testnet_verification/KILL_SWITCH")
    evidence_dir: Path = Path("evidence/testnet-soak")

    def __post_init__(self) -> None:
        self.validate()

    @classmethod
    def from_env(cls, **overrides: object) -> SoakConfig:
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
            "episodes": env_int("BEIDOU_SOAK_EPISODES", defaults.episodes),
            "max_duration_seconds": env_float("BEIDOU_SOAK_MAX_DURATION_SECONDS", defaults.max_duration_seconds),
            "cycle_interval_seconds": env_float("BEIDOU_SOAK_CYCLE_INTERVAL_SECONDS", defaults.cycle_interval_seconds),
            "target_notional": env_float("BEIDOU_SOAK_TARGET_NOTIONAL", defaults.target_notional),
            "absolute_notional_ceiling": env_float(
                "BEIDOU_SOAK_ABSOLUTE_NOTIONAL_CEILING", defaults.absolute_notional_ceiling
            ),
            "max_leverage": env_float("BEIDOU_SOAK_MAX_LEVERAGE", defaults.max_leverage),
            "symbol": os.getenv("BEIDOU_SOAK_SYMBOL", defaults.symbol),
            "trace_path": Path(os.getenv("BEIDOU_SOAK_TRACE_PATH", str(defaults.trace_path))),
            "pool_state_path": Path(os.getenv("BEIDOU_SOAK_POOL_STATE_PATH", str(defaults.pool_state_path))),
            "kill_switch_path": Path(os.getenv("BEIDOU_TESTNET_KILL_SWITCH_PATH", str(defaults.kill_switch_path))),
            "evidence_dir": Path(os.getenv("BEIDOU_SOAK_EVIDENCE_DIR", str(defaults.evidence_dir))),
        }
        values.update(overrides)
        return cls(**values)

    def validate(self) -> None:
        if type(self.episodes) is not int or not 1 <= self.episodes <= 30:
            raise ValueError("episodes must be within 1..30")
        numeric = (
            self.max_duration_seconds,
            self.cycle_interval_seconds,
            self.target_notional,
            self.absolute_notional_ceiling,
            self.max_leverage,
        )
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in numeric):
            raise ValueError("campaign numeric bounds must be real numbers")
        if any(not math.isfinite(float(value)) for value in numeric):
            raise ValueError("campaign numeric bounds must be finite")
        if not 0 < self.max_duration_seconds <= 3600:
            raise ValueError("max_duration_seconds must be within (0, 3600]")
        if self.cycle_interval_seconds < 60:
            raise ValueError("cycle_interval_seconds must be at least 60")
        if not 0 < self.target_notional <= 100:
            raise ValueError("target_notional must be within (0, 100]")
        if not self.target_notional <= self.absolute_notional_ceiling <= 500:
            raise ValueError("absolute_notional_ceiling must be between target_notional and 500")
        if not 0 < self.max_leverage <= 3:
            raise ValueError("max_leverage must be within (0, 3]")
        if not re.fullmatch(r"[A-Z0-9]{2,24}", self.symbol):
            raise ValueError("symbol must be a canonical uppercase Binance symbol")
        if type(self.confirm_testnet) is not bool:
            raise ValueError("confirm_testnet must be a boolean")
        if self.confirm_testnet and (
            not self.api_key or not self.api_secret or self.account_id == DEFAULT_TESTNET_ACCOUNT_ID
        ):
            raise ValueError("explicit Testnet credentials and dedicated account_id are required")

    def verifier_config(self) -> VerifierConfig:
        """Compose the sole existing verifier with stricter campaign bounds."""

        return VerifierConfig(
            rest_url=self.rest_url,
            api_key=self.api_key,
            api_secret=self.api_secret,
            account_id=self.account_id,
            max_notional=self.target_notional,
            max_leverage=self.max_leverage,
            max_account_exposure=self.absolute_notional_ceiling,
            max_instruments=1,
            interval=self.interval,
            kline_limit=self.kline_limit,
            order_poll_attempts=self.order_poll_attempts,
            order_poll_interval_seconds=self.order_poll_interval_seconds,
            confirm_testnet=self.confirm_testnet,
            once=True,
            close_after_verify=True,
            allowed_symbols=(self.symbol,),
            task_id="testnet-soak-campaign",
            entrypoint="apps.testnet_soak",
            trace_path=self.trace_path,
            pool_state_path=self.pool_state_path,
            kill_switch_path=self.kill_switch_path,
            evidence_dir=self.evidence_dir / "episodes",
        )

    def redacted_dict(self) -> dict[str, object]:
        return {
            "rest_url": self.rest_url,
            "account_id": self.account_id,
            "episodes": self.episodes,
            "max_duration_seconds": self.max_duration_seconds,
            "cycle_interval_seconds": self.cycle_interval_seconds,
            "cycle_interval_semantics": "START_TO_START",
            "target_notional": self.target_notional,
            "absolute_notional_ceiling": self.absolute_notional_ceiling,
            "max_leverage": self.max_leverage,
            "max_instruments": 1,
            "symbol": self.symbol,
            "confirm_testnet": self.confirm_testnet,
            "close_after_verify": True,
            "execution_mode": "EXECUTION_PROBE",
            "alpha_evidence": False,
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
