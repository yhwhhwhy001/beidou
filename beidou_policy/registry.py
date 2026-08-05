"""Policy Registry 核心实现 — 版本化策略存储、签名验证与原子激活。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from beidou_shared.types import CorrelationId, PolicyId, ResultStatus, SchemaVersion


class PolicyCategory(str, Enum):
    HARD_SAFETY = "HARD_SAFETY"
    ADAPTIVE_BOUNDARY = "ADAPTIVE_BOUNDARY"
    MODEL_PARAMETER = "MODEL_PARAMETER"
    PROTOCOL_CONSTANT = "PROTOCOL_CONSTANT"


class PolicyLifecycle(str, Enum):
    PROPOSED = "PROPOSED"
    VALIDATED = "VALIDATED"
    SIGNED = "SIGNED"
    SCHEDULED = "SCHEDULED"
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


VALID_TRANSITIONS: dict[PolicyLifecycle, set[PolicyLifecycle]] = {
    PolicyLifecycle.PROPOSED: {PolicyLifecycle.VALIDATED, PolicyLifecycle.REVOKED},
    PolicyLifecycle.VALIDATED: {PolicyLifecycle.SIGNED, PolicyLifecycle.REVOKED},
    PolicyLifecycle.SIGNED: {PolicyLifecycle.SCHEDULED, PolicyLifecycle.REVOKED},
    PolicyLifecycle.SCHEDULED: {PolicyLifecycle.ACTIVE, PolicyLifecycle.REVOKED},
    PolicyLifecycle.ACTIVE: {PolicyLifecycle.REVOKED},
    PolicyLifecycle.REVOKED: set(),
}


@dataclass(frozen=True, slots=True)
class PolicyMetadata:
    policy_id: PolicyId
    name: str
    category: PolicyCategory
    schema_version: SchemaVersion
    author: str
    signature: str | None = None
    signing_key_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    scheduled_at: datetime | None = None
    expires_at: datetime | None = None
    environment: set[str] = field(default_factory=lambda: {"production", "staging", "testnet"})
    previous_version: PolicyId | None = None
    change_description: str = ""


@dataclass
class PolicyPackage:
    metadata: PolicyMetadata
    data: dict[str, Any]
    lifecycle: PolicyLifecycle = PolicyLifecycle.PROPOSED
    correlation_id: CorrelationId | None = None

    def _assert_transition(self, target: PolicyLifecycle) -> None:
        allowed = VALID_TRANSITIONS.get(self.lifecycle, set())
        if target not in allowed:
            raise ValueError(
                f"Invalid policy lifecycle transition: {self.lifecycle.value} -> {target.value}. "
                f"Allowed: {[a.value for a in allowed]}"
            )

    def transition(self, target: PolicyLifecycle) -> None:
        """通用生命周期转换 — 验证合法性后执行转换。"""
        self._assert_transition(target)
        self.lifecycle = target

    def validate(self, policy_schema_version: SchemaVersion) -> bool:
        if self.metadata.schema_version != policy_schema_version:
            return False
        return not (self.metadata.expires_at and self.metadata.expires_at < datetime.now(timezone.utc))

    def sign(self, signature: str, signing_key_id: str) -> None:
        self._assert_transition(PolicyLifecycle.SIGNED)
        object.__setattr__(self.metadata, "signature", signature)
        object.__setattr__(self.metadata, "signing_key_id", signing_key_id)
        self.lifecycle = PolicyLifecycle.SIGNED

    def schedule(self, scheduled_at: datetime) -> None:
        self._assert_transition(PolicyLifecycle.SCHEDULED)
        object.__setattr__(self.metadata, "scheduled_at", scheduled_at)
        self.lifecycle = PolicyLifecycle.SCHEDULED

    def activate(self) -> None:
        if self.lifecycle == PolicyLifecycle.SCHEDULED:
            if self.metadata.scheduled_at and self.metadata.scheduled_at > datetime.now(timezone.utc):
                raise ValueError("Cannot activate before scheduled_at")
        else:
            self._assert_transition(PolicyLifecycle.ACTIVE)
        self.lifecycle = PolicyLifecycle.ACTIVE

    def revoke(self, reason: str = "") -> None:
        self.lifecycle = PolicyLifecycle.REVOKED

    def is_effectively_active(self) -> bool:
        if self.lifecycle != PolicyLifecycle.ACTIVE:
            return False
        return not (self.metadata.expires_at and self.metadata.expires_at < datetime.now(timezone.utc))


class PolicyRegistry:
    """策略注册中心 — 管理所有 Policy 的版本、签名、激活和读取。

    并发读取者只能看到完整的旧版本或完整的新版本（原子切换）。
    """

    def __init__(self) -> None:
        self._policies: dict[PolicyId, PolicyPackage] = {}
        self._active_index: dict[str, PolicyId] = {}  # name -> active policy_id
        self._history: dict[str, list[PolicyId]] = {}  # name -> [policy_ids in order]

    def register(self, package: PolicyPackage) -> PolicyId:
        pid = package.metadata.policy_id
        if pid in self._policies:
            raise ValueError(f"Policy {pid} already registered")
        self._policies[pid] = deepcopy(package)
        name = package.metadata.name
        if name not in self._history:
            self._history[name] = []
        self._history[name].append(pid)
        return pid

    def get(self, policy_id: PolicyId) -> PolicyPackage | None:
        pkg = self._policies.get(policy_id)
        return deepcopy(pkg) if pkg else None

    def get_active(self, name: str) -> PolicyPackage | None:
        active_id = self._active_index.get(name)
        if active_id is None:
            return None
        pkg = self._policies.get(active_id)
        if pkg is None:
            return None
        if not pkg.is_effectively_active():
            return None
        return deepcopy(pkg)

    def activate_latest_valid(self, name: str) -> PolicyPackage | None:
        history = self._history.get(name, [])
        for pid in reversed(history):
            pkg = self._policies.get(pid)
            if pkg and pkg.lifecycle == PolicyLifecycle.SCHEDULED:
                if pkg.metadata.scheduled_at and pkg.metadata.scheduled_at <= datetime.now(timezone.utc):
                    pkg.activate()
                    self._active_index[name] = pid
                    return deepcopy(pkg)
        return None

    def set_active(self, policy_id: PolicyId) -> PolicyPackage:
        """原子激活 — 将指定 Policy 设为活跃版本。"""
        pkg = self._policies.get(policy_id)
        if pkg is None:
            raise ValueError(f"Policy {policy_id} not found")
        if pkg.metadata.signature is None:
            raise ValueError(f"Policy {policy_id} is not signed")
        if pkg.lifecycle == PolicyLifecycle.SIGNED:
            pkg.lifecycle = PolicyLifecycle.SCHEDULED
        if pkg.lifecycle not in (PolicyLifecycle.SCHEDULED, PolicyLifecycle.ACTIVE):
            raise ValueError(f"Policy {policy_id} is not in an activatable state: {pkg.lifecycle.value}")

        old_active_id = self._active_index.get(pkg.metadata.name)
        old_active = self._policies.get(old_active_id) if old_active_id else None

        pkg.activate()
        self._active_index[pkg.metadata.name] = policy_id

        if old_active and old_active != pkg:
            old_active.revoke("superseded")
        return deepcopy(pkg)

    def get_hard_safety_policy(self, name: str) -> PolicyPackage | None:
        pkg = self.get_active(name)
        if pkg and pkg.metadata.category == PolicyCategory.HARD_SAFETY:
            return pkg
        return None

    def list_active_policies(self) -> list[PolicyPackage]:
        result: list[PolicyPackage] = []
        for _name, pid in self._active_index.items():
            pkg = self._policies.get(pid)
            if pkg and pkg.is_effectively_active():
                result.append(deepcopy(pkg))
        return result

    def verify_signature(self, policy_id: PolicyId, public_key_pem: str) -> bool:
        pkg = self._policies.get(policy_id)
        if pkg is None or pkg.metadata.signature is None:
            return False
        import json

        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        try:
            key = serialization.load_pem_public_key(public_key_pem.encode())
            if not isinstance(key, rsa.RSAPublicKey):
                return False
            payload = json.dumps(
                {
                    "policy_id": pkg.metadata.policy_id,
                    "name": pkg.metadata.name,
                    "category": pkg.metadata.category.value,
                    "schema_version": pkg.metadata.schema_version,
                    "data": pkg.data,
                },
                sort_keys=True,
            ).encode()
            sig_bytes = bytes.fromhex(pkg.metadata.signature)
            key.verify(sig_bytes, payload, padding.PKCS1v15(), hashes.SHA256())
            return True
        except (InvalidSignature, ValueError, Exception):
            return False

    def fail_closed_check(self, name: str) -> ResultStatus:
        """Policy 不可用时返回 UNKNOWN 触发 Fail-Closed。"""
        pkg = self.get_active(name)
        if pkg is None:
            return ResultStatus.UNKNOWN
        if not pkg.is_effectively_active():
            return ResultStatus.UNKNOWN
        if pkg.metadata.signature is None:
            return ResultStatus.UNKNOWN
        return ResultStatus.SUCCESS
