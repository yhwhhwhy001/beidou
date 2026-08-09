"""PKG-32: Infrastructure 测试。事实源、RPO/RTO、灾备恢复、健康探针。"""

from beidou_core.store import PersistentStore
from beidou_infra import (
    BackupVerification,
    DisasterRecoveryPlan,
    FactSource,
    InfrastructureTopology,
    LivenessProbe,
    ReadinessProbe,
    RPO_RTO_Target,
    SQLiteBackupManager,
    StartupProbe,
)
from beidou_lifecycle import DegradationLevel


class TestFactSource:
    def test_authoritative_sources(self):
        assert FactSource.POSTGRESQL.is_authoritative()
        assert FactSource.KAFKA.is_authoritative()

    def test_clickhouse_not_authoritative(self):
        assert not FactSource.CLICKHOUSE.is_authoritative()

    def test_analytics_not_authoritative(self):
        assert not FactSource.OBJECT_STORE.is_authoritative()

    def test_order_fact_source(self):
        from beidou_infra.ha import FACT_SOURCE_AUTHORITY

        assert FACT_SOURCE_AUTHORITY["orders"] == FactSource.POSTGRESQL

    def test_analytics_fact_source(self):
        from beidou_infra.ha import FACT_SOURCE_AUTHORITY

        assert FACT_SOURCE_AUTHORITY["analytics"] == FactSource.CLICKHOUSE


class TestRPO_RTO:
    def test_target_creation(self):
        t = RPO_RTO_Target(data_domain="orders", rpo_seconds=60, rto_seconds=300, measurement_method="pitr_test")
        assert t.rpo_seconds == 60
        assert t.rto_seconds == 300


class TestBackupVerification:
    def test_valid_backup(self):
        bv = BackupVerification(
            backup_id="bkp-001",
            data_domain="ledger",
            restore_successful=True,
            ledger_balanced=True,
            intent_integrity=True,
            reconciliation_passed=True,
            invariants_check={"ledger_sum": True},
        )
        assert bv.is_valid()

    def test_invalid_backup_missing_ledger(self):
        bv = BackupVerification(
            backup_id="bkp-002",
            data_domain="ledger",
            restore_successful=True,
            ledger_balanced=False,
            intent_integrity=True,
        )
        assert not bv.is_valid()

    def test_backup_without_reconciliation_is_not_valid(self):
        bv = BackupVerification(
            backup_id="bkp-003",
            data_domain="account_facts",
            restore_successful=True,
            ledger_balanced=True,
            intent_integrity=True,
            reconciliation_passed=False,
            invariants_check={"account": True},
        )
        assert not bv.is_valid()

    def test_sqlite_backup_is_verified_and_encryption_requires_explicit_key(self, tmp_path):
        source = tmp_path / "source.db"
        PersistentStore(str(source))
        backup = tmp_path / "backup.db"
        manager = SQLiteBackupManager()
        manifest = manager.create(source, backup, backup_id="bkp-local-1")
        assert manifest.integrity_check is True
        verified = manager.verify(backup)
        assert verified.passed is True
        assert verified.row_counts["user_stream_events"] == 0

        encrypted = tmp_path / "backup.db.enc"
        key = b"k" * 32
        encrypted_manifest = manager.encrypt(backup, encrypted, key=key)
        assert encrypted_manifest.plaintext_size_bytes == backup.stat().st_size
        restored = tmp_path / "restored.db"
        manager.decrypt(encrypted, restored, key=key)
        assert manager.verify(restored).passed is True

        import pytest

        with pytest.raises(ValueError, match="32 key bytes"):
            manager.encrypt(backup, tmp_path / "bad.enc", key=b"short")


class TestDisasterRecovery:
    def test_default_no_new_risk(self):
        dr = DisasterRecoveryPlan(plan_id="dr-001", topology=InfrastructureTopology(environment="prod"))
        assert dr.default_degradation_level() == DegradationLevel.NO_NEW_RISK

    def test_cannot_trade_without_account_facts(self):
        dr = DisasterRecoveryPlan(plan_id="dr-001", topology=InfrastructureTopology(environment="prod"))
        assert not dr.can_resume_trading()

    def test_can_trade_after_account_restore(self):
        dr = DisasterRecoveryPlan(plan_id="dr-001", topology=InfrastructureTopology(environment="prod"))
        dr.backup_verifications.append(
            BackupVerification(
                backup_id="bkp-001",
                data_domain="account_facts",
                restore_successful=True,
                ledger_balanced=True,
                intent_integrity=True,
                reconciliation_passed=True,
                invariants_check={"account": True},
            )
        )
        assert dr.can_resume_trading()


class TestHealthProbes:
    def test_startup_ready(self):
        probe = StartupProbe(dependencies_ready=True, config_loaded=True, migrations_applied=True)
        assert probe.is_ready()

    def test_startup_not_ready(self):
        probe = StartupProbe(dependencies_ready=False, config_loaded=True, migrations_applied=True)
        assert not probe.is_ready()

    def test_liveness_alive(self):
        probe = LivenessProbe(process_alive=True, event_loop_responsive=True, memory_within_bounds=True)
        assert probe.is_alive()

    def test_readiness_trading_eligibility_separate(self):
        probe = ReadinessProbe(
            can_connect_db=True,
            can_connect_kafka=True,
            can_connect_exchange=True,
            account_facts_verified=False,
            trading_eligible=False,
        )
        assert probe.is_ready()
        assert not probe.is_trading_eligible()

    def test_trading_eligible_when_all_ok(self):
        probe = ReadinessProbe(
            can_connect_db=True,
            can_connect_kafka=True,
            can_connect_exchange=True,
            account_facts_verified=True,
            trading_eligible=True,
        )
        assert probe.is_trading_eligible()
