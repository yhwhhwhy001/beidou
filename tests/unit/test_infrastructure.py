"""PKG-32: Infrastructure 测试。事实源、RPO/RTO、灾备恢复、健康探针。"""

from beidou_infra import (
    BackupVerification,
    DisasterRecoveryPlan,
    FactSource,
    InfrastructureTopology,
    LivenessProbe,
    ReadinessProbe,
    RPO_RTO_Target,
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
