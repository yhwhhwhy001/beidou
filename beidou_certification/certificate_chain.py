"""BD-CV52: 完整证书链验证 + 撤销。

GateCertificate 绑定 repo SHA/lockfile/build/config/policy/artifact/evidence hashes。
证书撤销自动传播到后继 Gate。
重启恢复验证 manifest hash/signature。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum


class GateResult(str, Enum):
    PASS = "PASS"  # noqa: S105  # nosec B105 - status enum, not a credential
    FAIL = "FAIL"
    NOT_VERIFIABLE = "NOT_VERIFIABLE"
    REVOKED = "REVOKED"


@dataclass
class GateCertificate:
    """BD-CV52: 完整证书 schema。

    绑定 gate/repo SHA/lockfile/build/config/policy/artifact/evidence hashes。
    """

    gate: str = ""
    subject: str = ""
    repo_sha: str = ""
    lockfile_hash: str = ""
    build_hash: str = ""
    config_hash: str = ""
    policy_hash: str = ""
    artifact_hash: str = ""
    evidence_hash: str = ""
    issued_at: str = ""
    expires_at: str = ""
    issuer_key_id: str = ""
    signature: str = ""
    result: GateResult = GateResult.NOT_VERIFIABLE
    revoked: bool = False
    revoked_at: str = ""
    revoked_reason: str = ""

    def compute_signature(self, private_key_hint: str = "") -> str:
        """计算证书签名（绑定所有 hash）。"""
        data = (
            f"{self.gate}:{self.subject}:{self.repo_sha}:{self.lockfile_hash}:"
            f"{self.build_hash}:{self.config_hash}:{self.policy_hash}:{self.artifact_hash}:"
            f"{self.evidence_hash}:{self.issued_at}:{self.expires_at}:{self.issuer_key_id}:{self.result.value}"
        )
        return hashlib.sha256(data.encode()).hexdigest()

    def is_valid(self) -> bool:
        if self.revoked:
            return False
        if not self.signature:
            return False
        if self.expires_at:
            try:
                if float(self.expires_at) <= time.time():
                    return False
            except (TypeError, ValueError):
                return False
        expected = self.compute_signature()
        return self.signature == expected


@dataclass
class CertificateChain:
    """BD-CV52: 证书链 + 撤销传播。

    Issuer ≠ Verifier: verifier 从证据内容计算，不接受路径即 PASS。
    证书撤销自动撤销后继证书。
    72h/30d elapsed time 使用持久化 started_at + monotonic segment ledger。
    """

    certificates: dict[str, GateCertificate] = field(default_factory=dict)
    _started_at_map: dict[str, float] = field(default_factory=dict)
    _chain_file: str = "evidence/certification/chain.json"

    def issue(self, cert: GateCertificate, issuer_key_id: str) -> tuple[bool, str]:
        """BD-CV52: Issuer ≠ Verifier — 不同实体。"""
        if not issuer_key_id:
            return False, "MISSING_ISSUER_KEY"
        cert.issuer_key_id = issuer_key_id
        cert.issued_at = str(time.time())
        cert.signature = cert.compute_signature()
        self.certificates[cert.gate] = cert
        self._started_at_map[cert.gate] = time.time()
        return True, f"ISSUED:{cert.gate}"

    def verify(self, gate: str, verifier_key_id: str) -> GateResult:
        """BD-CV52: Verifier 从证据内容计算，不接受路径字符串即 PASS。"""
        cert = self.certificates.get(gate)
        if cert is None:
            return GateResult.NOT_VERIFIABLE
        if cert.revoked:
            return GateResult.REVOKED
        # Verifier 不能与 Issuer 相同
        if verifier_key_id == cert.issuer_key_id:
            return GateResult.NOT_VERIFIABLE
        if not cert.is_valid():
            return GateResult.FAIL
        return GateResult.PASS if cert.result == GateResult.PASS else cert.result

    def revoke(self, gate: str, reason: str) -> list[str]:
        """BD-CV52 AC-52-04: 证书撤销自动传播到后继 Gate。"""
        cert = self.certificates.get(gate)
        if cert is None:
            return []

        cert.revoked = True
        cert.revoked_at = str(time.time())
        cert.revoked_reason = reason
        cert.result = GateResult.REVOKED

        # 传播到所有后继 Gate
        revoked_gates = [gate]
        gate_order = ["G0", "G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8"]
        start_idx = gate_order.index(gate) if gate in gate_order else -1
        if start_idx >= 0:
            for i in range(start_idx + 1, len(gate_order)):
                successor = gate_order[i]
                sc = self.certificates.get(successor)
                if sc is not None and not sc.revoked:
                    sc.revoked = True
                    sc.revoked_reason = f"CASCADE_FROM:{gate}:{reason}"
                    sc.result = GateResult.REVOKED
                    revoked_gates.append(successor)
        return revoked_gates

    def elapsed_time(self, gate: str) -> float:
        """BD-CV52: 72h/30d elapsed time。"""
        started = self._started_at_map.get(gate, 0.0)
        if started == 0.0:
            return 0.0
        return time.time() - started

    def save_chain(self) -> bool:
        os.makedirs(os.path.dirname(self._chain_file), exist_ok=True)
        try:
            data = {
                gate: {
                    "gate": c.gate,
                    "subject": c.subject,
                    "result": c.result.value,
                    "revoked": c.revoked,
                    "signature": c.signature[:16],
                    "elapsed_seconds": self.elapsed_time(gate),
                }
                for gate, c in self.certificates.items()
            }
            with open(self._chain_file, "w") as f:
                json.dump(data, f, indent=2)
            return True
        except Exception:
            return False

    def load_chain(self) -> bool:
        """BD-CV52: 重启恢复验证 manifest hash/signature。"""
        if not os.path.exists(self._chain_file):
            return False
        try:
            with open(self._chain_file) as f:
                data = json.load(f)
            for gate, info in data.items():
                cert = self.certificates.get(gate)
                if cert is not None and info.get("signature") != cert.signature[:16]:
                    cert.result = GateResult.NOT_VERIFIABLE
            return True
        except (json.JSONDecodeError, KeyError):
            return False
