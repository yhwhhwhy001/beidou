"""北斗生产高可用基础设施。PostgreSQL/ClickHouse/Kafka、备份PITR与灾备。"""

__version__ = "2.0.0"
from .backup import BackupManifest, BackupVerificationResult, EncryptedBackupManifest, SQLiteBackupManager
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
    "BackupManifest",
    "BackupVerification",
    "BackupVerificationResult",
    "DatabaseRole",
    "DisasterRecoveryPlan",
    "EncryptedBackupManifest",
    "FactSource",
    "InfrastructureTopology",
    "LivenessProbe",
    "RPO_RTO_Target",
    "ReadinessProbe",
    "SQLiteBackupManager",
    "StartupProbe",
]
