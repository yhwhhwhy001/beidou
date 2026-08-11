"""BD-CV55: 分阶段认证阶梯。

Paper→Shadow→Testnet→72h→30d。
不可跳级，证书链完整。
G8 = Mainnet Candidate（不自动启用）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class GateLevel(str, Enum):
    G0 = "G0"  # CI/构建/供应链
    G1 = "G1"  # 数据 truth
    G2 = "G2"  # strategy/research parity
    G3 = "G3"  # risk/execution/ledger/recon
    G4 = "G4"  # Paper/Shadow
    G5 = "G5"  # minimal Testnet
    G6 = "G6"  # 72h unattended
    G7 = "G7"  # 30d unattended
    G8 = "G8"  # Mainnet candidate


GATE_ORDER = list(GateLevel)


@dataclass
class GateCertificate:
    gate: GateLevel
    certified: bool = False
    evidence_hash: str = ""
    commit_family: list[str] = field(default_factory=list)
    started_at: float = 0.0
    elapsed_seconds: float = 0.0
    p0_incidents: int = 0
    p1_incidents: int = 0
    restarts: int = 0
    recovery_events: int = 0
    unprotected_exposure_seconds: float = 0.0
    reconciliation_mismatches: int = 0


@dataclass
class StagedCertificationLadder:
    """BD-CV55: 分阶段认证阶梯。"""

    certificates: dict[GateLevel, GateCertificate] = field(default_factory=dict)
    current_gate: GateLevel = GateLevel.G0

    def certify(self, gate: GateLevel, evidence: GateCertificate) -> tuple[bool, str]:
        """BD-CV55 AC-55-01: 不可跳级。"""
        target_idx = GATE_ORDER.index(gate)
        current_idx = GATE_ORDER.index(self.current_gate)

        if target_idx > current_idx + 1:
            return False, f"CANNOT_SKIP:{self.current_gate.value}→{gate.value}"

        # 验证前置 gate 证书链
        for i in range(target_idx):
            prev_gate = GATE_ORDER[i]
            prev_cert = self.certificates.get(prev_gate)
            if prev_cert is None or not prev_cert.certified:
                return False, f"PREREQUISITE_MISSING:{prev_gate.value}"

        evidence.certified = True
        self.certificates[gate] = evidence
        if target_idx > current_idx:
            self.current_gate = gate
        return True, f"CERTIFIED:{gate.value}"

    def is_g8_mainnet_candidate(self) -> bool:
        """BD-CV55 AC-55-04: G8 不产生 Mainnet 写权限。"""
        g8 = self.certificates.get(GateLevel.G8)
        return g8 is not None and g8.certified

    def can_activate_mainnet(self) -> bool:
        """Mainnet write activation 必须独立显式操作。"""
        return False  # 始终返回 False — 需要包外人工作

    def chain_complete(self) -> bool:
        """BD-CV55 AC-55-01: 证书链完整。"""
        return all(
            self.certificates.get(g, GateCertificate(gate=g)).certified
            for g in GATE_ORDER
        )

    def start_gate_timer(self, gate: GateLevel) -> None:
        cert = self.certificates.get(gate)
        if cert is not None:
            cert.started_at = time.time()

    def update_elapsed(self, gate: GateLevel) -> float:
        cert = self.certificates.get(gate)
        if cert is not None and cert.started_at > 0:
            cert.elapsed_seconds = time.time() - cert.started_at
        return cert.elapsed_seconds if cert else 0.0

    def record_incident(self, severity: str) -> None:
        """BD-CV55: 任何 P0 立即 FAIL。"""
        current = self.certificates.get(self.current_gate)
        if current is None:
            return
        if severity == "P0":
            current.p0_incidents += 1
            current.certified = False  # P0 → FAIL
        elif severity == "P1":
            current.p1_incidents += 1
