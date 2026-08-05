
"""北斗生产高可用基础设施。PostgreSQL/ClickHouse/Kafka、备份PITR与灾备。"""
__version__ = "2.0.0"
from .ha import (
    InfrastructureTopology, FactSource, DatabaseRole,
    RPO_RTO_Target, BackupVerification, DisasterRecoveryPlan,
    StartupProbe, LivenessProbe, ReadinessProbe,
)
__all__ = [
    "InfrastructureTopology", "FactSource", "DatabaseRole",
    "RPO_RTO_Target", "BackupVerification", "DisasterRecoveryPlan",
    "StartupProbe", "LivenessProbe", "ReadinessProbe",
]
