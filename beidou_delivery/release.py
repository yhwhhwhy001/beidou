"""发布管理与回滚。失败发布不改变 Active 版本。"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from beidou_shared.types import ResultStatus

class ReleaseStatus(str, Enum):
    BUILDING = "BUILDING"
    TESTING = "TESTING"
    SHADOW = "SHADOW"
    CANARY = "CANARY"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    ROLLED_BACK = "ROLLED_BACK"
    FAILED = "FAILED"

class MigrationCompatibility(str, Enum):
    FORWARD_BACKWARD = "FORWARD_BACKWARD"
    FORWARD_ONLY = "FORWARD_ONLY"
    INCOMPATIBLE = "INCOMPATIBLE"

@dataclass(frozen=True, slots=True)
class ArtifactIdentity:
    artifact_id: str
    commit_sha: str
    schema_versions: dict[str, str]
    policy_versions: dict[str, str]
    model_versions: dict[str, str]
    rust_component_hashes: dict[str, str]
    built_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    signature: str | None = None
    sbom_hash: str | None = None

@dataclass
class ReleaseRecord:
    release_id: str
    artifact: ArtifactIdentity
    status: ReleaseStatus
    deployed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    canary_percentage: float = 0.0
    rollback_to: str | None = None
    migration_check: MigrationCompatibility | None = None


class ReleaseManager:
    """发布管理器 — 保证失败发布不改变 Active 版本。"""

    def __init__(self) -> None:
        self._releases: dict[str, ReleaseRecord] = {}
        self._active_release_id: str | None = None
        self._release_order: list[str] = []

    def register_build(self, artifact: ArtifactIdentity, migration: MigrationCompatibility) -> ReleaseRecord:
        if migration == MigrationCompatibility.INCOMPATIBLE:
            raise ValueError("Incompatible migration — cannot deploy")
        record = ReleaseRecord(
            release_id=f"release-{len(self._release_order)+1}",
            artifact=artifact,
            status=ReleaseStatus.BUILDING,
            migration_check=migration,
        )
        self._releases[record.release_id] = record
        self._release_order.append(record.release_id)
        return record

    def promote_to_active(self, release_id: str) -> ResultStatus:
        record = self._releases.get(release_id)
        if record is None:
            return ResultStatus.UNKNOWN
        if record.artifact.signature is None:
            return ResultStatus.ERROR  # 未签名制品不能部署
        if record.status not in (ReleaseStatus.CANARY, ReleaseStatus.TESTING):
            return ResultStatus.ERROR
        if record.migration_check == MigrationCompatibility.INCOMPATIBLE:
            return ResultStatus.ERROR

        old_active = self._active_release_id
        record.status = ReleaseStatus.ACTIVE
        self._active_release_id = release_id

        if old_active and old_active in self._releases:
            self._releases[old_active].status = ReleaseStatus.SUPERSEDED
        return ResultStatus.SUCCESS

    def rollback(self, target_release_id: str) -> ResultStatus:
        if target_release_id not in self._releases:
            return ResultStatus.UNKNOWN
        old_active = self._active_release_id
        self._active_release_id = target_release_id
        self._releases[target_release_id].status = ReleaseStatus.ACTIVE
        if old_active and old_active in self._releases:
            self._releases[old_active].status = ReleaseStatus.ROLLED_BACK
            self._releases[old_active].rollback_to = target_release_id
        return ResultStatus.SUCCESS

    def get_active(self) -> ReleaseRecord | None:
        if self._active_release_id is None:
            return None
        return self._releases.get(self._active_release_id)

    def sign_artifact(self, release_id: str, signature: str) -> ResultStatus:
        record = self._releases.get(release_id)
        if record is None:
            return ResultStatus.UNKNOWN
        object.__setattr__(record.artifact, "signature", signature)
        return ResultStatus.SUCCESS
