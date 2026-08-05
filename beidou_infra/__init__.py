"""北斗生产高可用基础设施。PostgreSQL/ClickHouse/Kafka、备份PITR与灾备。"""

__version__ = "2.0.0"
from .ha import (
    BackupVerification,
    DatabaseRole,
    DisasterRecoveryPlan,
    FactSource,
    InfrastructureTopology,
    LivenessProbe,
    ReadinessProbe,
    RPO_RTO_Target,
    StartupProbe,
)

__all__ = [
    "BackupVerification",
    "DatabaseRole",
    "DisasterRecoveryPlan",
    "FactSource",
    "InfrastructureTopology",
    "LivenessProbe",
    "RPO_RTO_Target",
    "ReadinessProbe",
    "StartupProbe",
]
