
"""PKG-31: Release Management 测试。失败发布不改变 Active 版本。"""
from beidou_delivery.release import (
    ReleaseManager, ArtifactIdentity, MigrationCompatibility, ReleaseStatus,
)
from beidou_shared.types import ResultStatus

class TestReleaseManager:
    def make_artifact(self, signed=True):
        art = ArtifactIdentity(
            artifact_id="test-artifact-001",
            commit_sha="abc123def456",
            schema_versions={"policy": "2.0.0"},
            policy_versions={"risk": "1.0.0"},
            model_versions={},
            rust_component_hashes={},
        )
        if signed:
            object.__setattr__(art, "signature", "test_signature_hex")
        return art

    def test_unsigned_cannot_deploy(self):
        rm = ReleaseManager()
        art = self.make_artifact(signed=False)
        record = rm.register_build(art, MigrationCompatibility.FORWARD_BACKWARD)
        record.status = ReleaseStatus.TESTING
        result = rm.promote_to_active(record.release_id)
        assert result == ResultStatus.ERROR

    def test_signed_can_deploy(self):
        rm = ReleaseManager()
        art = self.make_artifact(signed=True)
        record = rm.register_build(art, MigrationCompatibility.FORWARD_BACKWARD)
        record.status = ReleaseStatus.CANARY
        result = rm.promote_to_active(record.release_id)
        assert result == ResultStatus.SUCCESS
        active = rm.get_active()
        assert active is not None
        assert active.status == ReleaseStatus.ACTIVE

    def test_incompatible_migration_blocked(self):
        rm = ReleaseManager()
        art = self.make_artifact()
        with __import__('pytest').raises(ValueError, match="Incompatible"):
            rm.register_build(art, MigrationCompatibility.INCOMPATIBLE)

    def test_rollback_changes_active(self):
        rm = ReleaseManager()
        art1 = self.make_artifact()
        record1 = rm.register_build(art1, MigrationCompatibility.FORWARD_BACKWARD)
        record1.status = ReleaseStatus.CANARY
        art2 = self.make_artifact()
        object.__setattr__(art2, "commit_sha", "new_commit")
        record2 = rm.register_build(art2, MigrationCompatibility.FORWARD_BACKWARD)
        record2.status = ReleaseStatus.CANARY

        rm.promote_to_active(record1.release_id)
        rm.promote_to_active(record2.release_id)
        assert rm.get_active().release_id == record2.release_id

        rm.rollback(record1.release_id)
        assert rm.get_active().release_id == record1.release_id
        assert rm._releases[record2.release_id].status == ReleaseStatus.ROLLED_BACK

    def test_rollback_unknown_fails(self):
        rm = ReleaseManager()
        assert rm.rollback("nonexistent") == ResultStatus.UNKNOWN

    def test_failed_release_does_not_change_active(self):
        rm = ReleaseManager()
        art = self.make_artifact()
        record = rm.register_build(art, MigrationCompatibility.FORWARD_BACKWARD)
        record.status = ReleaseStatus.BUILDING
        result = rm.promote_to_active(record.release_id)
        assert result == ResultStatus.ERROR
        assert rm.get_active() is None
