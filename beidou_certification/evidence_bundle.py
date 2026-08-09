"""EvidenceBundle 与 Gate Runner — BD-13。

不可伪造认证：证书绑定 repository、commit、dependency lock、container digest、
policy、dataset、account/environment 和有效期。
结果仅允许 PASS/FAIL/NOT_VERIFIABLE。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class ScenarioStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_VERIFIABLE = "NOT_VERIFIABLE"


class GateResult(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_VERIFIABLE = "NOT_VERIFIABLE"


@dataclass
class EvidenceItem:
    """单条证据。"""

    name: str
    source_uri: str
    checksum: str
    producer: str
    commit: str
    schema_version: str = "2.0.0"


@dataclass
class EvidenceBundle:
    """证据包。"""

    bundle_id: str
    manifest: dict = field(default_factory=dict)
    evidence_items: list[EvidenceItem] = field(default_factory=list)
    scenario_results: dict[str, ScenarioStatus] = field(default_factory=dict)
    scenario_evidence_refs: dict[str, tuple[str, ...]] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def add_evidence(self, item: EvidenceItem) -> None:
        if any(
            not str(value).strip()
            for value in (item.name, item.source_uri, item.checksum, item.producer, item.commit, item.schema_version)
        ):
            raise ValueError("evidence item metadata must be complete")
        self.evidence_items.append(item)

    def record_scenario(self, scenario_id: str, status: ScenarioStatus, evidence_refs: list[str] | None = None) -> None:
        if not str(scenario_id).strip():
            raise ValueError("scenario_id is required")
        self.scenario_results[scenario_id] = status
        refs = tuple(str(ref).strip() for ref in (evidence_refs or []) if str(ref).strip())
        self.scenario_evidence_refs[scenario_id] = refs

    def validate(self) -> tuple[bool, str]:
        """Validate provenance before a PASS can become a certificate."""

        if not self.bundle_id.strip() or not self.created_at.strip():
            return False, "bundle_identity_missing"
        if not isinstance(self.manifest, dict) or not self.manifest:
            return False, "manifest_missing"
        required_manifest = (
            "repository",
            "commit",
            "dependency_lock_hash",
            "container_digest",
            "policy_version",
            "dataset_manifest_hash",
            "account_environment",
        )
        if any(not str(self.manifest.get(key, "")).strip() for key in required_manifest):
            return False, "manifest_context_incomplete"
        if not self.evidence_items or not self.scenario_results:
            return False, "evidence_or_scenarios_missing"
        names = {item.name for item in self.evidence_items}
        for item in self.evidence_items:
            if any(
                not str(value).strip()
                for value in (
                    item.name,
                    item.source_uri,
                    item.checksum,
                    item.producer,
                    item.commit,
                    item.schema_version,
                )
            ):
                return False, "evidence_item_metadata_incomplete"
        for scenario_id, status in self.scenario_results.items():
            refs = self.scenario_evidence_refs.get(scenario_id, ())
            if status == ScenarioStatus.PASS and (not refs or any(ref not in names for ref in refs)):
                return False, f"scenario_evidence_unbound:{scenario_id}"
        return True, "OK"

    def compute_bundle_hash(self) -> str:
        evidence_items = [
            {
                "name": item.name,
                "source_uri": item.source_uri,
                "checksum": item.checksum,
                "producer": item.producer,
                "commit": item.commit,
                "schema_version": item.schema_version,
            }
            for item in self.evidence_items
        ]
        content = json.dumps(
            {
                "bundle_id": self.bundle_id,
                "manifest": self.manifest,
                "evidence_items": evidence_items,
                "scenario_results": {k: v.value for k, v in self.scenario_results.items()},
                "scenario_evidence_refs": {key: list(value) for key, value in self.scenario_evidence_refs.items()},
                "created_at": self.created_at,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(content.encode()).hexdigest()

    def is_complete(self) -> bool:
        valid, _reason = self.validate()
        return valid


@dataclass
class GateCertificate:
    """Gate 证书 — 绑定完整上下文。"""

    gate_id: str  # G0, G1, ..., G8
    result: GateResult
    bundle_hash: str
    repository: str
    commit: str
    dependency_lock_hash: str = ""
    container_digest: str = ""
    policy_version: str = ""
    dataset_manifest_hash: str = ""
    account_environment: str = ""
    valid_from: str = ""
    valid_until: str = ""
    signature: str = ""
    revoked: bool = False

    def is_valid(self) -> bool:
        if self.revoked:
            return False
        if self.result != GateResult.PASS:
            return False
        required_context = (
            self.gate_id,
            self.bundle_hash,
            self.repository,
            self.commit,
            self.dependency_lock_hash,
            self.container_digest,
            self.policy_version,
            self.dataset_manifest_hash,
            self.account_environment,
            self.valid_from,
            self.signature,
        )
        if any(not str(value).strip() for value in required_context):
            return False
        if self.valid_until:
            try:
                expiry = datetime.fromisoformat(self.valid_until)
                if datetime.now(timezone.utc) > expiry:
                    return False
            except ValueError:
                return False
        return True


class GateRunner:
    """Gate Runner — 从原始日志、事件库、交易所回执自动计算场景结果。

    G0-G8 规则:
    - G0: 编译检查 (lint + typecheck)
    - G1: 单元测试
    - G2: 集成测试 + 架构测试
    - G3: 安全扫描 + 依赖审计
    - G4: Testnet 合约测试
    - G5: Testnet 协议矩阵 (create/cancel/partial/race)
    - G6: Shadow — 真实连续运行（不发送风险订单）
    - G7: Testnet Canary — 最小品种最少交易量
    - G8: 无人值守 30 天认证
    """

    GATES: dict[str, list[str]] = {
        "G0": ["lint", "typecheck"],
        "G1": ["unit_tests"],
        "G2": ["integration_tests", "architecture_tests"],
        "G3": ["security_scan", "dependency_audit"],
        "G4": ["testnet_contract_tests"],
        "G5": ["testnet_protocol_matrix"],
        "G6": ["shadow_live_7d"],
        "G7": ["testnet_canary_14d"],
        "G8": ["unattended_30d"],
    }

    def __init__(self) -> None:
        self._results: dict[str, dict[str, ScenarioStatus]] = {}

    def run_gate(self, gate_id: str, scenario_results: dict[str, ScenarioStatus]) -> GateResult:
        required = self.GATES.get(gate_id, [])
        if not required:
            return GateResult.NOT_VERIFIABLE

        self._results[gate_id] = scenario_results

        for scenario in required:
            status = scenario_results.get(scenario)
            if status is None:
                return GateResult.NOT_VERIFIABLE
            if status == ScenarioStatus.FAIL:
                return GateResult.FAIL
            if status == ScenarioStatus.NOT_VERIFIABLE:
                return GateResult.NOT_VERIFIABLE

        return GateResult.PASS

    def has_p0_blocker(self) -> bool:
        """任一 P0 失败立即阻断后续 Gate。"""
        for _gate_id, results in self._results.items():
            for _scenario, status in results.items():
                if status == ScenarioStatus.FAIL:
                    return True
        return False

    def generate_certificate(
        self, gate_id: str, bundle: EvidenceBundle, commit: str, repo: str = "yhwhhwhy001/beidou"
    ) -> GateCertificate:
        result = self.run_gate(gate_id, bundle.scenario_results)
        complete, _reason = bundle.validate()
        if result == GateResult.PASS and not complete:
            result = GateResult.NOT_VERIFIABLE
        manifest = bundle.manifest if isinstance(bundle.manifest, dict) else {}
        return GateCertificate(
            gate_id=gate_id,
            result=result,
            bundle_hash=bundle.compute_bundle_hash(),
            repository=str(manifest.get("repository") or repo),
            commit=str(manifest.get("commit") or commit),
            dependency_lock_hash=str(manifest.get("dependency_lock_hash", "")),
            container_digest=str(manifest.get("container_digest", "")),
            policy_version=str(manifest.get("policy_version", "")),
            dataset_manifest_hash=str(manifest.get("dataset_manifest_hash", "")),
            account_environment=str(manifest.get("account_environment", "")),
            valid_from=datetime.now(timezone.utc).isoformat(),
        )
