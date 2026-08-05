"""Policy 参数加载与验证 — BD-05/BD-07。

所有策略参数由已签名 Policy 加载。Policy 不可用时 NO_ACTION。
Policy 绑定版本、签名和过期时间。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


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
            envelope = PolicyEnvelope(
                policy_id=data["policy_id"],
                version=data["version"],
                parameters=data["parameters"],
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
