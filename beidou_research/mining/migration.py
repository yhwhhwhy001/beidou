"""BF-12: 迁移、回归与发布。

迁移脚本:
1. 旧 factor records 迁移为 LEGACY_UNVERIFIED
2. 旧两个因子不得自动保留 ACTIVE
3. 旧策略作为 baseline challenger
4. 数据库 forward-only migration
5. Rollback 支持
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class MigrationStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    ROLLED_BACK = "ROLLED_BACK"
    FAILED = "FAILED"


@dataclass
class MigrationRecord:
    """单条迁移记录。"""

    old_factor_id: str
    new_factor_id: str
    old_status: str
    new_status: str
    evidence_hash: str
    migrated_at: datetime
    reason: str


LEGACY_UNVERIFIED = "LEGACY_UNVERIFIED"
BASELINE_CHALLENGER = "BASELINE_CHALLENGER"


class FactorMigration:
    """因子迁移管理器。

    Forward-only migration: 旧因子标记为 LEGACY_UNVERIFIED，
    不得自动保留 ACTIVE。旧策略作为 baseline challenger。
    """

    def __init__(self, store_path: str = "evidence/migration") -> None:
        self.store_path = store_path
        os.makedirs(store_path, exist_ok=True)
        self._records: list[MigrationRecord] = []
        self._status = MigrationStatus.PENDING

    def migrate_legacy_factors(
        self,
        legacy_factor_ids: list[str],
        reason: str = "BF-12 migration: legacy factors require re-evaluation",
    ) -> list[MigrationRecord]:
        """迁移旧因子到 LEGACY_UNVERIFIED。

        旧因子必须:
        1. 标记为 LEGACY_UNVERIFIED（不是 ACTIVE）
        2. 不能进入新策略
        3. 作为 baseline challenger 参考
        """
        self._status = MigrationStatus.IN_PROGRESS
        records = []

        for fid in legacy_factor_ids:
            record = MigrationRecord(
                old_factor_id=fid,
                new_factor_id=f"{fid}_legacy",
                old_status="ACTIVE",
                new_status=LEGACY_UNVERIFIED,
                evidence_hash=hashlib.sha256(f"{fid}:legacy_unverified:{reason}".encode()).hexdigest()[:16],
                migrated_at=datetime.now(timezone.utc),
                reason=reason,
            )
            records.append(record)

        self._records = records
        self._status = MigrationStatus.COMPLETED
        self._save_state()
        return records

    def rollback(self) -> list[MigrationRecord]:
        """回滚迁移。"""
        if self._status != MigrationStatus.COMPLETED:
            raise RuntimeError("Can only rollback from COMPLETED state")

        self._status = MigrationStatus.ROLLED_BACK
        # 保留迁移记录作为审计日志
        self._save_state()
        return self._records

    def verify_no_active_legacy(self, active_factors: list[str]) -> list[str]:
        """验证没有旧因子保留 ACTIVE 状态。"""
        violations = []
        legacy_ids = {r.old_factor_id for r in self._records}
        for fid in active_factors:
            if fid in legacy_ids:
                violations.append(f"Legacy factor {fid} is still ACTIVE")
        return violations

    def _save_state(self) -> None:
        """持久化迁移状态。"""
        path = os.path.join(self.store_path, "migration_state.json")
        state = {
            "status": self._status.value,
            "records": [
                {
                    "old_factor_id": r.old_factor_id,
                    "new_factor_id": r.new_factor_id,
                    "old_status": r.old_status,
                    "new_status": r.new_status,
                    "evidence_hash": r.evidence_hash,
                    "migrated_at": r.migrated_at.isoformat(),
                    "reason": r.reason,
                }
                for r in self._records
            ],
        }
        with open(path, "w") as f:
            json.dump(state, f, indent=2, default=str)

    @classmethod
    def from_state(cls, store_path: str = "evidence/migration") -> FactorMigration:
        """从持久化状态恢复。"""
        mig = cls(store_path)
        path = os.path.join(store_path, "migration_state.json")
        if os.path.exists(path):
            with open(path) as f:
                state = json.load(f)
            mig._status = MigrationStatus(state["status"])
            mig._records = [
                MigrationRecord(
                    old_factor_id=str(item["old_factor_id"]),
                    new_factor_id=str(item["new_factor_id"]),
                    old_status=str(item["old_status"]),
                    new_status=str(item["new_status"]),
                    evidence_hash=str(item["evidence_hash"]),
                    migrated_at=datetime.fromisoformat(str(item["migrated_at"])),
                    reason=str(item["reason"]),
                )
                for item in state.get("records", [])
            ]
        return mig
