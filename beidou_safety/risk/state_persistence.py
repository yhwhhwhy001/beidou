"""BD-CV33: RiskStateAuthority 持久化。

状态损坏/缺失/非法值 → NO_NEW_RISK。
Approval 绑定 RiskSnapshot + PortfolioTarget + Intent hash，有 TTL。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class RiskSnapshot:
    """BD-CV33: 不可变风险快照。"""

    snapshot_id: str = ""
    input_fact_hashes: dict[str, str] = field(default_factory=dict)
    rule_outcomes: dict[str, str] = field(default_factory=dict)
    aggregate_state: str = "CORRUPT"  # NORMAL/WARNING/CRITICAL/CORRUPT
    reason: str = ""
    expires_at: str = ""
    policy_version: str = ""
    config_version: str = ""
    snapshot_hash: str = ""

    def compute_hash(self) -> str:
        data = {
            "snapshot_id": self.snapshot_id,
            "input_fact_hashes": self.input_fact_hashes,
            "rule_outcomes": self.rule_outcomes,
            "aggregate_state": self.aggregate_state,
            "reason": self.reason,
            "expires_at": self.expires_at,
            "policy_version": self.policy_version,
            "config_version": self.config_version,
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def is_valid(self) -> bool:
        """状态损坏/缺失/非法值 → False。"""
        if self.aggregate_state in ("CORRUPT", ""):
            return False
        if not self.snapshot_hash:
            return False
        return self.compute_hash() == self.snapshot_hash

    def is_expired(self) -> bool:
        if not self.expires_at:
            return True
        try:
            expires = datetime.fromisoformat(self.expires_at)
            # P2 修复: 无时区的 expires_at 与 aware now 比较会抛 TypeError
            # 并被当成"恒过期"——fail-closed 方向安全但语义错误。补默认
            # 时区后正常比较。
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) > expires
        except (ValueError, TypeError):
            return True


@dataclass
class RiskStatePersistence:
    """BD-CV33: 风险状态持久化。

    手工 reset/自动恢复不直接写 NORMAL。
    """

    _state_file: str = "evidence/BD-01/risk_snapshot.json"

    def save(self, snapshot: RiskSnapshot) -> bool:
        os.makedirs(os.path.dirname(self._state_file), exist_ok=True)
        try:
            snapshot.snapshot_hash = snapshot.compute_hash()
            with open(self._state_file, "w") as f:
                json.dump(
                    {
                        "snapshot_id": snapshot.snapshot_id,
                        "input_fact_hashes": snapshot.input_fact_hashes,
                        "rule_outcomes": snapshot.rule_outcomes,
                        "aggregate_state": snapshot.aggregate_state,
                        "reason": snapshot.reason,
                        "expires_at": snapshot.expires_at,
                        "policy_version": snapshot.policy_version,
                        "config_version": snapshot.config_version,
                        "snapshot_hash": snapshot.snapshot_hash,
                        "saved_at": datetime.now(timezone.utc).isoformat(),
                    },
                    f,
                    indent=2,
                )
            return True
        except Exception:
            return False

    def load(self) -> RiskSnapshot:
        if not os.path.exists(self._state_file):
            return RiskSnapshot(aggregate_state="CORRUPT", reason="NO_PERSISTED_STATE")
        try:
            with open(self._state_file) as f:
                data = json.load(f)
            snap = RiskSnapshot(
                snapshot_id=data.get("snapshot_id", ""),
                input_fact_hashes=data.get("input_fact_hashes", {}),
                rule_outcomes=data.get("rule_outcomes", {}),
                aggregate_state=data.get("aggregate_state", "CORRUPT"),
                reason=data.get("reason", ""),
                expires_at=data.get("expires_at", ""),
                policy_version=data.get("policy_version", ""),
                config_version=data.get("config_version", ""),
                snapshot_hash=data.get("snapshot_hash", ""),
            )
            if not snap.is_valid() or snap.is_expired():
                return RiskSnapshot(aggregate_state="CORRUPT", reason="PERSISTED_STATE_INVALID_OR_EXPIRED")
            return snap
        except (json.JSONDecodeError, KeyError):
            return RiskSnapshot(aggregate_state="CORRUPT", reason="STATE_FILE_CORRUPTED")

    def reset_requires_new_snapshot(self) -> RiskSnapshot:
        """重置必须生成新 RiskSnapshot，不可直接写 NORMAL。"""
        return RiskSnapshot(aggregate_state="CORRUPT", reason="RESET_REQUIRES_NEW_SNAPSHOT")
