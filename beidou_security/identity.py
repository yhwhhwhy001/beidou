"""服务身份与短期凭据管理。分离市场只读、账户只读和交易密钥。"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from beidou_shared.types import ResultStatus, VenueId


class CredentialType(str, Enum):
    MARKET_READONLY = "MARKET_READONLY"
    ACCOUNT_READONLY = "ACCOUNT_READONLY"
    TRADING = "TRADING"


class KeyRotationStatus(str, Enum):
    ACTIVE = "ACTIVE"
    ROTATING = "ROTATING"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


@dataclass(frozen=True, slots=True)
class ServiceIdentity:
    service_name: str
    service_id: str
    roles: frozenset[str]
    allowed_ips: frozenset[str] = field(default_factory=frozenset)
    mTLS_cert_fingerprint: str | None = None


@dataclass
class Credential:
    credential_id: str
    credential_type: CredentialType
    venue_id: VenueId | None = None
    key_reference: str = ""  # 指向 Vault/KMS 的引用，非实际密钥
    status: KeyRotationStatus = KeyRotationStatus.ACTIVE
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc) + timedelta(days=90))
    last_rotated_at: datetime | None = None
    can_withdraw: bool = False  # 所有交易密钥必须禁用提款


class Permission:
    """RBAC 权限矩阵。"""

    PERMISSIONS = {
        "market:read": {CredentialType.MARKET_READONLY, CredentialType.ACCOUNT_READONLY, CredentialType.TRADING},
        "account:read": {CredentialType.ACCOUNT_READONLY, CredentialType.TRADING},
        "order:create": {CredentialType.TRADING},
        "order:cancel": {CredentialType.TRADING},
        "withdraw": set(),  # 无条件禁止
    }

    @classmethod
    def check(cls, action: str, credential_type: CredentialType, can_withdraw: bool = False) -> ResultStatus:
        if action == "withdraw":
            return ResultStatus.ERROR  # 提款权限无条件拒绝
        allowed_types = cls.PERMISSIONS.get(action)
        if allowed_types is None:
            return ResultStatus.UNSUPPORTED
        if credential_type not in allowed_types:
            return ResultStatus.ERROR
        return ResultStatus.SUCCESS

    @classmethod
    def can_create_order(cls, credential_type: CredentialType) -> bool:
        return cls.check("order:create", credential_type) == ResultStatus.SUCCESS


class SecretSanitizer:
    """自动脱敏 — 日志、指标、异常中不得出现明文密钥。"""

    SENSITIVE_KEYS = frozenset({
        "api_key", "api_secret", "secret_key", "private_key",
        "password", "token", "signature",
        "access_key", "secret", "passphrase",
    })

    @classmethod
    def sanitize(cls, data: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for k, v in data.items():
            if k.lower() in cls.SENSITIVE_KEYS:
                result[k] = "***REDACTED***"
            elif isinstance(v, dict):
                result[k] = cls.sanitize(v)
            elif isinstance(v, list):
                result[k] = [cls.sanitize(item) if isinstance(item, dict) else item for item in v]
            else:
                result[k] = v
        return result

    @classmethod
    def scan_for_secrets(cls, text: str) -> list[str]:
        """扫描文本中是否包含疑似密钥。返回发现的模式列表。"""
        found: list[str] = []
        import re
        for pattern in cls.SENSITIVE_KEYS:
            if re.search(pattern, text, re.IGNORECASE):
                found.append(pattern)
        return found


class KeyRotator:
    """密钥轮换管理器 — 不产生重复 Executor 或订单丢失。"""

    def __init__(self) -> None:
        self._credentials: dict[str, Credential] = {}

    def register(self, credential: Credential) -> None:
        self._credentials[credential.credential_id] = credential

    def start_rotation(self, credential_id: str) -> ResultStatus:
        cred = self._credentials.get(credential_id)
        if cred is None:
            return ResultStatus.UNKNOWN
        if cred.status == KeyRotationStatus.ROTATING:
            return ResultStatus.ERROR
        cred.status = KeyRotationStatus.ROTATING
        cred.last_rotated_at = datetime.now(timezone.utc)
        return ResultStatus.SUCCESS

    def complete_rotation(self, old_id: str, new_credential: Credential) -> ResultStatus:
        old = self._credentials.get(old_id)
        if old is None:
            return ResultStatus.UNKNOWN
        old.status = KeyRotationStatus.EXPIRED
        self.register(new_credential)
        return ResultStatus.SUCCESS

    def revoke(self, credential_id: str, reason: str = "") -> ResultStatus:
        cred = self._credentials.get(credential_id)
        if cred is None:
            return ResultStatus.UNKNOWN
        cred.status = KeyRotationStatus.REVOKED
        return ResultStatus.SUCCESS
