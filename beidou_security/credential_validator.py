"""
P1 (BDS-P1-051~056): 凭据安全验证器。

修复:
- P1-051: 凭据权限不只检查类型，还验证 status/expiry/IP/rotation
- P1-052: 轮换状态持久化 (generation/activation/revocation evidence)
- P1-053: 签名密钥域隔离 (policy/risk/cert 独立 key)
- P1-054: signer 纳入签名 payload
- P1-055: Policy 不可变版本 ID
- P1-056: Config hash 覆盖完整（嵌套 secrets 深度脱敏）
- P1-017: IP 白名单 CIDR 匹配支持
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import ClassVar

_logger = logging.getLogger(__name__)


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


class SigningDomain(str, Enum):
    """P1-053: 签名密钥域隔离。"""
    POLICY = "policy"
    RISK = "risk"
    CERTIFICATION = "certification"
    GENERAL = "general"
    EXCHANGE = "exchange"


# P1-053: 凭据类型 → 签名域映射
CREDENTIAL_TO_DOMAIN: dict[CredentialType, SigningDomain] = {
    CredentialType.POLICY_KEY: SigningDomain.POLICY,
    CredentialType.RISK_KEY: SigningDomain.RISK,
    CredentialType.CERT_KEY: SigningDomain.CERTIFICATION,
    CredentialType.SIGNING_KEY: SigningDomain.GENERAL,
    CredentialType.API_KEY: SigningDomain.EXCHANGE,
}


# ---------------------------------------------------------------------------
# IP Whitelist (BDS-P1-017)
# ---------------------------------------------------------------------------


def _is_ip_in_cidr(ip: str, cidr_or_ip: str) -> bool:
    """P1-017: CIDR-aware IP 匹配。

    支持:
    - 精确 IP: "192.168.1.1"
    - CIDR 网段: "10.0.0.0/8", "172.16.0.0/12"
    """
    try:
        addr = ipaddress.ip_address(ip)
        # Try as a single IP/host first
        try:
            net = ipaddress.ip_network(cidr_or_ip, strict=False)
        except ValueError:
            return False
        return addr in net
    except ValueError:
        return False


def verify_ip_whitelist(ip: str, allowed_ips: tuple[str, ...]) -> bool:
    """P1-017: 验证 IP 是否在白名单内。

    空白名单 = 所有 IP 允许。
    """
    if not allowed_ips:
        return True
    for allowed in allowed_ips:
        if _is_ip_in_cidr(ip, allowed) or ip == allowed:
            return True
    return False


# ---------------------------------------------------------------------------
# Credential
# ---------------------------------------------------------------------------


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
    def signing_domain(self) -> SigningDomain:
        """P1-053: 凭据所属签名域。"""
        return CREDENTIAL_TO_DOMAIN.get(self.credential_type, SigningDomain.GENERAL)

    @property
    def is_active(self) -> bool:
        """P1-051: 综合验证 status + expiry。"""
        if self.status != CredentialStatus.ACTIVE:
            return False
        if self.expiry_time > 0 and time.time() > self.expiry_time:
            return False
        return True

    def is_allowed_ip(self, ip: str) -> bool:
        """P1-017: IP 白名单检查（CIDR 支持）。"""
        return verify_ip_whitelist(ip, self.allowed_ips)


# ---------------------------------------------------------------------------
# Rotation Record (BDS-P1-052)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RotationRecord:
    """P1-052: 持久化轮换记录。"""

    old_credential_id: str
    new_credential_id: str
    old_key_id: str
    new_key_id: str
    rotated_at: float
    evidence_hash: str = ""


# ---------------------------------------------------------------------------
# Credential Registry
# ---------------------------------------------------------------------------


class CredentialRegistry:
    """P1-051~056: 凭据注册表 — 完整生命周期管理 + 持久化。"""

    # P1-053: 签名密钥域隔离
    KEY_DOMAINS: ClassVar[dict[CredentialType, str]] = {
        CredentialType.POLICY_KEY: "policy",
        CredentialType.RISK_KEY: "risk",
        CredentialType.CERT_KEY: "certification",
        CredentialType.SIGNING_KEY: "general",
        CredentialType.API_KEY: "exchange",
    }

    def __init__(self, evidence_dir: str = "") -> None:
        self._credentials: dict[str, Credential] = {}
        self._revoked: set[str] = set()
        self._generation_log: list[dict] = []  # 内存缓存
        self._rotation_records: list[RotationRecord] = []
        self._evidence_dir = evidence_dir or os.path.join("evidence", "BD-01")
        self._rotation_log_path = os.path.join(self._evidence_dir, "credential_rotations.jsonl")
        self._revocation_log_path = os.path.join(self._evidence_dir, "credential_revocations.jsonl")
        self._domain_keys: dict[SigningDomain, str] = {}  # P1-053

    # ------------------------------------------------------------------
    # Registration & Validation
    # ------------------------------------------------------------------

    def register(self, cred: Credential) -> None:
        existing = self._credentials.get(cred.credential_id)
        if existing and existing.generation >= cred.generation:
            raise ValueError(f"Credential {cred.credential_id} generation must increase")
        self._credentials[cred.credential_id] = cred

    def validate(
        self, credential_id: str, required_type: CredentialType,
        client_ip: str = "", required_domain: SigningDomain | None = None,
    ) -> bool:
        """P1-051/053: 完整验证 — type + status + expiry + IP + domain。"""
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
        # P1-053: 签名域验证
        if required_domain is not None and cred.signing_domain != required_domain:
            return False
        return True

    # ------------------------------------------------------------------
    # Rotation (BDS-P1-052)
    # ------------------------------------------------------------------

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
            allowed_ips=old.allowed_ips,
        )
        self._credentials[credential_id] = Credential(
            **{**old.__dict__, "status": CredentialStatus.ROTATING}
        )
        self._credentials[new_cred.credential_id] = new_cred

        log_entry = {
            "action": "rotate", "old_id": credential_id,
            "new_id": new_cred.credential_id, "timestamp": time.time(),
        }
        self._generation_log.append(log_entry)

        # P1-052: 持久化到 JSONL
        self._persist_rotation_event(log_entry)

        return new_cred

    def rotate_with_evidence(
        self, credential_id: str, new_key_hash: str, evidence: dict | None = None,
    ) -> tuple[Credential, RotationRecord]:
        """P1-052: 带证据哈希的轮换。"""
        new_cred = self.rotate(credential_id, new_key_hash)
        record = RotationRecord(
            old_credential_id=credential_id,
            new_credential_id=new_cred.credential_id,
            old_key_id=self._credentials[credential_id].key_id,
            new_key_id=new_cred.key_id,
            rotated_at=time.time(),
            evidence_hash=hashlib.sha256(
                json.dumps(evidence or {}, sort_keys=True).encode()
            ).hexdigest(),
        )
        self._rotation_records.append(record)
        self._persist_rotation_event(record.__dict__)
        return new_cred, record

    def revoke(self, credential_id: str) -> None:
        """P1-052: 撤销凭据（持久化）。"""
        self._revoked.add(credential_id)
        self._persist_revocation_event({
            "action": "revoke",
            "credential_id": credential_id,
            "timestamp": time.time(),
        })

    def get_generation_log(self) -> list[dict]:
        return list(self._generation_log)

    def get_rotation_records(self) -> list[RotationRecord]:
        return list(self._rotation_records)

    def restore_from_log(self) -> int:
        """P1-052: 从 JSONL 恢复 rotation/revocation 状态。"""
        restored = 0
        for path in (self._rotation_log_path, self._revocation_log_path):
            try:
                if os.path.exists(path):
                    with open(path) as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                event = json.loads(line)
                                action = event.get("action", "")
                                if action == "revoke":
                                    cid = event.get("credential_id", "")
                                    if cid:
                                        self._revoked.add(cid)
                                        restored += 1
                                elif action == "rotate":
                                    self._generation_log.append(event)
                                    restored += 1
                            except (json.JSONDecodeError, KeyError):
                                _logger.warning("Invalid rotation log line: %s", line[:100])
                else:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
            except OSError as exc:
                _logger.warning("Cannot read rotation log %s: %s", path, exc)
        return restored

    def _persist_rotation_event(self, event: dict) -> None:
        try:
            os.makedirs(os.path.dirname(self._rotation_log_path), exist_ok=True)
            with open(self._rotation_log_path, "a") as f:
                f.write(json.dumps(event, sort_keys=True) + "\n")
        except OSError as exc:
            _logger.error("Failed to persist rotation event: %s", exc)

    def _persist_revocation_event(self, event: dict) -> None:
        try:
            os.makedirs(os.path.dirname(self._revocation_log_path), exist_ok=True)
            with open(self._revocation_log_path, "a") as f:
                f.write(json.dumps(event, sort_keys=True) + "\n")
        except OSError as exc:
            _logger.error("Failed to persist revocation event: %s", exc)

    # ------------------------------------------------------------------
    # Domain key management (BDS-P1-053)
    # ------------------------------------------------------------------

    def set_domain_key(self, domain: SigningDomain, key_id: str) -> None:
        """P1-053: 注册域签名密钥。"""
        self._domain_keys[domain] = key_id

    def get_domain_key(self, domain: SigningDomain) -> str:
        """P1-053: 获取域签名密钥 ID。"""
        return self._domain_keys.get(domain, "")

    def verify_domain_membership(self, credential_id: str, expected_domain: SigningDomain) -> bool:
        """P1-053: 验证凭据属于指定签名域。"""
        cred = self._credentials.get(credential_id)
        if cred is None:
            return False
        return cred.signing_domain == expected_domain


# ---------------------------------------------------------------------------
# Immutable Policy Version (BDS-P1-055)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyVersion:
    """P1-055: 不可变策略版本 — content-addressed。"""

    policy_id: str
    version: str
    content_hash: str  # SHA-256 of canonical policy content
    created_at: float = field(default_factory=time.time)
    signer: str = ""
    signature: str = ""


class PolicyRegistry:
    """P1-055: 策略注册表 — 检测同 ID 变更。"""

    def __init__(self) -> None:
        self._policies: dict[str, PolicyVersion] = {}
        self._id_history: dict[str, set[str]] = {}  # policy_id → {content_hash, ...}

    def register(self, policy: PolicyVersion) -> tuple[bool, str]:
        """注册策略。同 ID 不同 hash 将被拒绝（不可变版本）。

        Returns:
            (accepted, reason)
        """
        existing = self._policies.get(policy.policy_id)
        if existing is not None:
            if existing.content_hash != policy.content_hash:
                return False, (
                    f"Policy {policy.policy_id} is immutable: "
                    f"existing hash {existing.content_hash[:16]} != new {policy.content_hash[:16]}"
                )
            return True, "already_registered"

        self._policies[policy.policy_id] = policy
        self._id_history.setdefault(policy.policy_id, set()).add(policy.content_hash)
        return True, "registered"

    def get(self, policy_id: str) -> PolicyVersion | None:
        """获取策略版本。返回 None 若不存在。"""
        return self._policies.get(policy_id)

    def is_valid(self, policy_id: str, content_hash: str) -> bool:
        """验证 policy_id + hash 组合是否与注册版本一致。"""
        existing = self._policies.get(policy_id)
        if existing is None:
            return False
        return existing.content_hash == content_hash

    def validate_or_reject(self, policy_id: str, content_hash: str) -> tuple[bool, str]:
        """验证策略版本。若同 ID 不同 hash → 拒绝（可能被篡改）。"""
        existing = self._policies.get(policy_id)
        if existing is None:
            return False, f"unknown_policy:{policy_id}"
        if existing.content_hash != content_hash:
            return False, (
                f"policy_hash_mismatch:{policy_id} "
                f"(registered={existing.content_hash[:16]} provided={content_hash[:16]})"
            )
        return True, "policy_valid"


# ---------------------------------------------------------------------------
# Config Hash (BDS-P1-056)
# ---------------------------------------------------------------------------

_SENSITIVE_KEYS = frozenset({
    "secrets", "api_key", "api_secret", "signing_key",
    "secret", "password", "token", "private_key", "access_key",
    "secret_key", "credential", "credentials", "key",
})


def _deep_strip_secrets(obj: object, depth: int = 0) -> object:
    """P1-056: 递归脱敏 — 移除嵌套级别的 secrets。

    深度限制防止递归炸弹。
    敏感 key 的值被完全移除（不替换为占位符），
    以确保同一配置的不同 secret 值产生相同的 hash。
    """
    if depth > 10:
        return obj
    if isinstance(obj, dict):
        return {
            k: _deep_strip_secrets(v, depth + 1)
            for k, v in obj.items()
            if k not in _SENSITIVE_KEYS
        }
    if isinstance(obj, list):
        return [_deep_strip_secrets(item, depth + 1) for item in obj]
    return obj


def compute_full_config_hash(
    config: dict,
    secrets_version: str = "",
    policy_version: str = "",
) -> str:
    """P1-056: 对 resolved config 做 canonical full hash。

    - 递归脱敏嵌套 secrets
    - 绑定 secrets_version 和 policy_version
    - canonical JSON + SHA-256
    """
    safe_config = _deep_strip_secrets(dict(config))
    if not isinstance(safe_config, dict):
        safe_config = {}

    # 绑定版本信息（非敏感）
    safe_config["_secrets_version"] = secrets_version
    if policy_version:
        safe_config["_policy_version"] = policy_version

    canonical = json.dumps(safe_config, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Signing (BDS-P1-054)
# ---------------------------------------------------------------------------


def sign_with_signer(
    payload: str, key: bytes, signer: str, key_id: str,
    algorithm: str = "HMAC-SHA256", domain: str = "",
) -> str:
    """P1-054: signer/key_id/algorithm 纳入签名 payload。

    签名 canonical payload = signer|key_id|algorithm|domain|payload。
    """
    parts = [signer, key_id, algorithm]
    if domain:
        parts.append(domain)
    parts.append(payload)
    canonical = "|".join(parts)
    return hmac.new(key, canonical.encode(), hashlib.sha256).hexdigest()
