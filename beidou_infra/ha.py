"""生产高可用配置。事实源、RPO/RTO、灾备恢复与健康探针。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from beidou_lifecycle import DegradationLevel


class DatabaseRole(str, Enum):
    PRIMARY = "PRIMARY"
    REPLICA = "REPLICA"
    ANALYTICS = "ANALYTICS"
    DR = "DR"


class FactSource(str, Enum):
    POSTGRESQL = "POSTGRESQL"
    KAFKA = "KAFKA"
    CLICKHOUSE = "CLICKHOUSE"
    OBJECT_STORE = "OBJECT_STORE"

    def is_authoritative(self) -> bool:
        return self in (FactSource.POSTGRESQL, FactSource.KAFKA)


FACT_SOURCE_AUTHORITY = {
    "orders": FactSource.POSTGRESQL,
    "ledger": FactSource.POSTGRESQL,
    "risk_approval": FactSource.POSTGRESQL,
    "account_facts": FactSource.POSTGRESQL,
    "market_data": FactSource.KAFKA,
    "analytics": FactSource.CLICKHOUSE,
    "backups": FactSource.OBJECT_STORE,
}


@dataclass(frozen=True, slots=True)
class RPO_RTO_Target:
    data_domain: str
    rpo_seconds: int
    rto_seconds: int
    measurement_method: str
    last_verified: datetime | None = None


@dataclass
class BackupVerification:
    backup_id: str
    data_domain: str
    verified_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    restore_successful: bool = False
    invariants_check: dict[str, bool] = field(default_factory=dict)
    ledger_balanced: bool = False
    intent_integrity: bool = False
    reconciliation_passed: bool = False

    def is_valid(self) -> bool:
        return (
            self.restore_successful
            and self.ledger_balanced
            and self.intent_integrity
            and all(self.invariants_check.values())
        )


@dataclass
class InfrastructureTopology:
    environment: str
    postgresql_instances: dict[DatabaseRole, list[str]] = field(default_factory=dict)
    kafka_brokers: list[str] = field(default_factory=list)
    clickhouse_nodes: list[str] = field(default_factory=list)
    object_store_bucket: str = ""
    multi_az: bool = False
    cross_region_dr: bool = False

    def get_authoritative_source(self, data_domain: str) -> FactSource:
        return FACT_SOURCE_AUTHORITY.get(data_domain, FactSource.POSTGRESQL)


@dataclass
class DisasterRecoveryPlan:
    plan_id: str
    topology: InfrastructureTopology
    rpo_rto_targets: list[RPO_RTO_Target] = field(default_factory=list)
    backup_verifications: list[BackupVerification] = field(default_factory=list)
    last_drill_at: datetime | None = None
    active: bool = False

    def default_degradation_level(self) -> DegradationLevel:
        """灾备恢复默认 NO_NEW_RISK，完成账户事实恢复后才能交易。"""
        return DegradationLevel.NO_NEW_RISK

    def can_resume_trading(self) -> bool:
        account_facts_restored = any(
            bv.data_domain == "account_facts" and bv.is_valid() for bv in self.backup_verifications
        )
        return account_facts_restored


@dataclass(frozen=True, slots=True)
class StartupProbe:
    dependencies_ready: bool
    config_loaded: bool
    migrations_applied: bool

    def is_ready(self) -> bool:
        return all([self.dependencies_ready, self.config_loaded, self.migrations_applied])


@dataclass(frozen=True, slots=True)
class LivenessProbe:
    process_alive: bool
    event_loop_responsive: bool
    memory_within_bounds: bool

    def is_alive(self) -> bool:
        return all([self.process_alive, self.event_loop_responsive])


@dataclass(frozen=True, slots=True)
class ReadinessProbe:
    can_connect_db: bool
    can_connect_kafka: bool
    can_connect_exchange: bool
    account_facts_verified: bool
    trading_eligible: bool = False

    def is_ready(self) -> bool:
        return self.can_connect_db and self.can_connect_kafka

    def is_trading_eligible(self) -> bool:
        """交易执行资格与 K8s Readiness 分离。"""
        return self.is_ready() and self.account_facts_verified and self.trading_eligible
