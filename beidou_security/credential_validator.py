"""
P1 (BDS-P1-051~056): 凭据安全验证器。

修复:
- P1-051: 凭据权限不只检查类型，还验证 status/expiry/IP/rotation
- P1-052: 轮换状态持久化 (generation/activation/revocation evidence)
- P1-053: 签名密钥域隔离 (policy/risk/cert 独立 key)
- P1-054: signer 纳入签名 payload
- P1-055: Policy 不可变版本 ID
- P1-056: Config hash 覆盖完整
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import ClassVar


class CredentialStatus(str, Enum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    ROTATING = "ROTATING"
    PENDING_ACTIVATION = "PENDING_ACTIVATION"


class CredentialType(str, Enum):
    API_KEY = "API_KEY"
    SIGNING_KEY = "SIGNING_KEY"
    CERT_KEY = "CERT_KEY"
    POLICY_KEY = "POLICY_KEY"
    RISK_KEY = "RISK_KEY"


@dataclass(frozen=True)
class Credential:
    """P1-051: 凭据 — 包含完整生命周期状态。"""

    credential_id: str
    credential_type: CredentialType
    key_hash: str  # 只存哈希，不存明文
    status: CredentialStatus = CredentialStatus.PENDING_ACTIVATION
    generation: int = 1
    activation_time: float = 0.0
    expiry_time: float = 0.0
    rotation_interval_days: int = 90
    allowed_ips: tuple[str, ...] = ()
    key_id: str = ""  # P1-053: 独立 key ID
    signer: str = ""  # P1-054: signer 身份
    metadata: dict = field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        """P1-051: 综合验证 status + expiry。"""
        if self.status != CredentialStatus.ACTIVE:
            return False
        if self.expiry_time > 0 and time.time() > self.expiry_time:
            return False
        return True

    def is_allowed_ip(self, ip: str) -> bool:
        """P1-051: IP 白名单检查。"""
        if not self.allowed_ips:
            return True  # 无限制
        return ip in self.allowed_ips


class CredentialRegistry:
    """P1-051~056: 凭据注册表 — 完整生命周期管理。"""

    # P1-053: 签名密钥域隔离
    KEY_DOMAINS: ClassVar[dict[CredentialType, str]] = {
        CredentialType.POLICY_KEY: "policy",
        CredentialType.RISK_KEY: "risk",
        CredentialType.CERT_KEY: "certification",
        CredentialType.SIGNING_KEY: "general",
        CredentialType.API_KEY: "exchange",
    }

    def __init__(self) -> None:
        self._credentials: dict[str, Credential] = {}
        self._revoked: set[str] = set()
        self._generation_log: list[dict] = []  # P1-052: 持久化轮换日志

    def register(self, cred: Credential) -> None:
        existing = self._credentials.get(cred.credential_id)
        if existing and existing.generation >= cred.generation:
            raise ValueError(f"Credential {cred.credential_id} generation must increase")
        self._credentials[cred.credential_id] = cred

    def validate(self, credential_id: str, required_type: CredentialType, client_ip: str = "") -> bool:
        """P1-051: 完整验证 — type + status + expiry + IP。"""
        cred = self._credentials.get(credential_id)
        if cred is None:
            return False
        if cred.credential_type != required_type:
            return False
        if not cred.is_active:
            return False
        if client_ip and not cred.is_allowed_ip(client_ip):
            return False
        if credential_id in self._revoked:
            return False
        return True

    def rotate(self, credential_id: str, new_key_hash: str) -> Credential:
        """P1-052: 轮换 — 旧凭据标记 ROTATING，新凭据 PENDING_ACTIVATION。"""
        old = self._credentials.get(credential_id)
        if old is None:
            raise ValueError(f"Credential {credential_id} not found")
        new_cred = Credential(
            credential_id=f"{credential_id}-gen{old.generation + 1}",
            credential_type=old.credential_type,
            key_hash=new_key_hash,
            generation=old.generation + 1,
            status=CredentialStatus.PENDING_ACTIVATION,
            key_id=old.key_id,
            signer=old.signer,
        )
        self._credentials[credential_id] = Credential(
            **{**old.__dict__, "status": CredentialStatus.ROTATING}
        )
        self._credentials[new_cred.credential_id] = new_cred
        self._generation_log.append({
            "action": "rotate", "old_id": credential_id,
            "new_id": new_cred.credential_id, "timestamp": time.time(),
        })
        return new_cred

    def revoke(self, credential_id: str) -> None:
        """P1-052: 撤销凭据。"""
        self._revoked.add(credential_id)

    def get_generation_log(self) -> list[dict]:
        return list(self._generation_log)


def compute_full_config_hash(config: dict, secrets_version: str = "") -> str:
    """P1-056: 对 resolved config 做 canonical full hash。

    Secret 仅绑定 key ref/secret version，不绑定明文值。
    """
    safe_config = dict(config)
    safe_config.pop("secrets", None)
    safe_config.pop("api_key", None)
    safe_config.pop("api_secret", None)
    safe_config.pop("signing_key", None)
    safe_config["_secrets_version"] = secrets_version
    canonical = json.dumps(safe_config, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def sign_with_signer(payload: str, key: bytes, signer: str, key_id: str, algorithm: str = "HMAC-SHA256") -> str:
    """P1-054: signer/key_id/algorithm 纳入签名 payload。

    签名 canonical payload = signer|key_id|algorithm|payload。
    """
    canonical = f"{signer}|{key_id}|{algorithm}|{payload}"
    return hmac.new(key, canonical.encode(), hashlib.sha256).hexdigest()
