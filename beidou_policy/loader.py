"""Policy 参数加载与验证 — BD-05/BD-07。

所有策略参数由已签名 Policy 加载。Policy 不可用时 NO_ACTION。
Policy 绑定版本、签名和过期时间。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# Risk-increasing execution must not silently combine a signed subset with
# unsigned YAML defaults.  Keep this contract at the policy boundary so every
# caller (preflight and engine startup) applies the same completeness rule.
REQUIRED_RISK_PARAMETERS: frozenset[str] = frozenset(
    {
        "max_leverage",
        "max_concentration_pct",
        "max_position_notional",
        "max_total_leverage",
        "max_instruments",
        "drift_threshold",
        "max_drawdown_pct",
        "max_daily_loss_pct",
        "max_consecutive_losses",
        "risk_per_trade_pct",
        "min_sharpe_rolling",
    }
)


@dataclass(frozen=True, slots=True)
class PolicyEnvelope:
    """已签名 Policy Envelope — 不可变参数集。"""

    policy_id: str
    version: str
    parameters: dict[str, Any]
    signature: str
    issued_at: str
    expires_at: str | None = None
    signer: str = ""

    def is_expired(self) -> bool:
        if not self.expires_at:
            return False
        try:
            return datetime.now(timezone.utc) > datetime.fromisoformat(self.expires_at)
        except ValueError:
            return True

    def validate_risk_parameters(self) -> tuple[bool, str]:
        """Validate the complete signed parameter contract used by writes.

        A valid HMAC proves integrity, not that the envelope contains every
        value the execution engine needs.  Missing, non-finite, or out-of-
        bounds values therefore remain invalid instead of being filled from
        an unsigned environment YAML file.
        """

        missing = sorted(REQUIRED_RISK_PARAMETERS.difference(self.parameters))
        if missing:
            return False, f"missing required risk parameters: {','.join(missing)}"

        positive_float_keys = {
            "max_leverage",
            "max_position_notional",
            "max_total_leverage",
            "drift_threshold",
        }
        percentage_keys = {
            "max_concentration_pct",
            "max_drawdown_pct",
            "max_daily_loss_pct",
            "risk_per_trade_pct",
        }
        integer_keys = {"max_instruments", "max_consecutive_losses"}
        for key in REQUIRED_RISK_PARAMETERS:
            value = self.parameters.get(key)
            if value is None or isinstance(value, bool):
                return False, f"risk parameter {key} is not numeric"
            try:
                numeric = float(value)
            except (TypeError, ValueError, OverflowError):
                return False, f"risk parameter {key} is not numeric"
            if not math.isfinite(numeric):
                return False, f"risk parameter {key} is not finite"
            if key in integer_keys and (not numeric.is_integer() or numeric <= 0):
                return False, f"risk parameter {key} must be a positive integer"
            if key in positive_float_keys and numeric <= 0:
                return False, f"risk parameter {key} must be positive"
            if key in percentage_keys and not 0 < numeric <= 100:
                return False, f"risk parameter {key} must be in (0,100]"
        return True, "complete risk parameter contract"

    def verify(self, signing_key: str) -> bool:
        if not self.signature or not signing_key:
            return False
        payload = json.dumps(
            {
                "policy_id": self.policy_id,
                "version": self.version,
                "parameters": self.parameters,
                "issued_at": self.issued_at,
                "expires_at": self.expires_at,
            },
            sort_keys=True,
        )
        expected = hmac.new(signing_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(self.signature, expected)


class PolicyLoader:
    """Policy 加载器。

    从文件或环境加载已签名的 Policy Envelope。
    Policy 不可用或签名无效 → NO_ACTION（不允许使用默认值）。
    """

    def __init__(self, policy_dir: str = "config/policies", signing_key: str | None = None):
        self._policy_dir = policy_dir
        self._signing_key = signing_key or os.environ.get("BEIDOU_SIGNING_KEY", "")
        self._cache: dict[str, PolicyEnvelope] = {}

    def load(self, policy_id: str) -> PolicyEnvelope | None:
        """加载 Policy。缓存命中直接返回。"""
        if policy_id in self._cache:
            cached = self._cache[policy_id]
            if not cached.is_expired() and cached.verify(self._signing_key):
                return cached
            del self._cache[policy_id]

        path = os.path.join(self._policy_dir, f"{policy_id}.json")
        if not os.path.exists(path):
            return None

        try:
            with open(path) as f:
                data = json.load(f)
            if not isinstance(data, dict) or data.get("policy_id") != policy_id:
                return None
            parameters = data.get("parameters")
            if not isinstance(parameters, dict):
                return None
            envelope = PolicyEnvelope(
                policy_id=data["policy_id"],
                version=data["version"],
                parameters=parameters,
                signature=data.get("signature", ""),
                issued_at=data.get("issued_at", ""),
                expires_at=data.get("expires_at"),
                signer=data.get("signer", ""),
            )
            if not envelope.verify(self._signing_key):
                return None
            if not envelope.is_expired():
                self._cache[policy_id] = envelope
            return envelope
        except (KeyError, json.JSONDecodeError, FileNotFoundError):
            return None

    def get_param(self, policy_id: str, key: str, default: Any = None) -> Any | None:
        """获取单个参数。Policy 不可用时返回 None（不使用默认数值）。"""
        envelope = self.load(policy_id)
        if envelope is None:
            return None  # 不 fallback 到默认值
        return envelope.parameters.get(key)
