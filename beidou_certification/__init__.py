"""北斗实盘认证框架 — G5 Testnet 到 G8 无人值守。
Gate 独立发证，P0 立即停止并回退。每个场景独立 PASS/FAIL/NOT_VERIFIABLE。
"""

from .engine import (
    CertificationFramework,
    CertificationGate,
    CertificationManager,
    CertificationScenario,
    G5TestnetCertification,
    G6ShadowCertification,
    G7LiveCertification,
    G8UnattendedCertification,
    GateCertificate,
    ScenarioResult,
    ScenarioStatus,
    create_l2_canary_certification,
    create_l3_ramp_certification,
    create_l4_normal_certification,
    create_l5_champion_certification,
)
from .gate_verifier import GateVerification, verify_g5_certificate, verify_g7_certificate

__all__ = [
    "CertificationFramework",
    "CertificationGate",
    "CertificationManager",
    "CertificationScenario",
    "G5TestnetCertification",
    "G6ShadowCertification",
    "G7LiveCertification",
    "G8UnattendedCertification",
    "GateCertificate",
    "GateVerification",
    "ScenarioResult",
    "ScenarioStatus",
    "create_l2_canary_certification",
    "create_l3_ramp_certification",
    "create_l4_normal_certification",
    "create_l5_champion_certification",
    "verify_g5_certificate",
    "verify_g7_certificate",
]
