"""BF-07: 因子证据包。

EvidenceBundle 是因子晋级的唯一凭证。包含：
- candidate_hash, factor_code_hash, dataset_manifest_hash
- feature_manifest_hash, label_spec_hash
- cost_model_version, policy_version
- random_seed, fold_definitions
- raw_metrics, adjusted_metrics
- failure_reasons, artifact_hash

无 EvidenceBundle 不得迁移生命周期状态。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class EvidenceBundle:
    """因子晋级的完整证据包。

    所有字段参与哈希计算，确保不可篡改。
    """

    # 身份
    bundle_id: str
    candidate_id: str
    factor_id: str
    factor_version: str

    # 代码与数据溯源
    candidate_hash: str = ""
    factor_code_hash: str = ""
    factor_expression_hash: str = ""
    dataset_manifest_hash: str = ""
    feature_manifest_hash: str = ""
    label_spec_hash: str = ""

    # 配置
    cost_model_version: str = ""
    policy_version: str = ""
    random_seed: int = 0

    # 评估证据
    fold_definitions: list[dict[str, Any]] = field(default_factory=list)
    raw_metrics: dict[str, Any] = field(default_factory=dict)
    adjusted_metrics: dict[str, Any] = field(default_factory=dict)

    # 多重检验
    multiple_testing_results: dict[str, Any] = field(default_factory=dict)

    # 稳健性
    stability_results: list[dict[str, Any]] = field(default_factory=list)

    # 成本与容量
    cost_capacity_results: dict[str, Any] = field(default_factory=dict)

    # 结果
    gate_decision: str = ""  # PASS / FAIL / CONDITIONAL_PASS
    failure_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # 元数据
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    evaluator_version: str = "bf07-v1"
    artifact_hash: str = ""

    def compute_bundle_hash(self) -> str:
        """计算证据包的确定性哈希（排除 artifact_hash 自身）。"""
        content = json.dumps(
            {
                "bundle_id": self.bundle_id,
                "candidate_id": self.candidate_id,
                "factor_id": self.factor_id,
                "factor_version": self.factor_version,
                "candidate_hash": self.candidate_hash,
                "factor_code_hash": self.factor_code_hash,
                "factor_expression_hash": self.factor_expression_hash,
                "dataset_manifest_hash": self.dataset_manifest_hash,
                "feature_manifest_hash": self.feature_manifest_hash,
                "label_spec_hash": self.label_spec_hash,
                "cost_model_version": self.cost_model_version,
                "policy_version": self.policy_version,
                "random_seed": self.random_seed,
                "fold_definitions": self.fold_definitions,
                "raw_metrics": self.raw_metrics,
                "adjusted_metrics": self.adjusted_metrics,
                "multiple_testing_results": self.multiple_testing_results,
                "gate_decision": self.gate_decision,
                "failure_reasons": self.failure_reasons,
                "evaluator_version": self.evaluator_version,
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(content.encode()).hexdigest()

    def seal(self) -> str:
        """封存证据包，计算最终哈希。"""
        self.artifact_hash = self.compute_bundle_hash()
        return self.artifact_hash

    def to_dict(self) -> dict[str, Any]:
        """导出为可序列化字典。"""
        return {
            "bundle_id": self.bundle_id,
            "candidate_id": self.candidate_id,
            "factor_id": self.factor_id,
            "factor_version": self.factor_version,
            "candidate_hash": self.candidate_hash,
            "factor_code_hash": self.factor_code_hash,
            "factor_expression_hash": self.factor_expression_hash,
            "dataset_manifest_hash": self.dataset_manifest_hash,
            "feature_manifest_hash": self.feature_manifest_hash,
            "label_spec_hash": self.label_spec_hash,
            "cost_model_version": self.cost_model_version,
            "policy_version": self.policy_version,
            "random_seed": self.random_seed,
            "fold_definitions": self.fold_definitions,
            "raw_metrics": self.raw_metrics,
            "adjusted_metrics": self.adjusted_metrics,
            "multiple_testing_results": self.multiple_testing_results,
            "stability_results": self.stability_results,
            "cost_capacity_results": self.cost_capacity_results,
            "gate_decision": self.gate_decision,
            "failure_reasons": self.failure_reasons,
            "warnings": self.warnings,
            "created_at": self.created_at.isoformat(),
            "evaluator_version": self.evaluator_version,
            "artifact_hash": self.artifact_hash,
        }

    def is_complete(self) -> bool:
        """检查证据包是否包含所有必要字段。"""
        required = [
            self.candidate_hash,
            self.factor_code_hash or self.factor_expression_hash,
            self.dataset_manifest_hash,
            self.gate_decision,
        ]
        return all(v != "" for v in required if isinstance(v, str))

    def can_promote(self) -> tuple[bool, str]:
        """检查是否可以晋级。"""
        if not self.is_complete():
            return False, "evidence_incomplete"
        if self.gate_decision != "PASS":
            return False, f"gate_not_passed: {self.gate_decision}"
        if self.failure_reasons:
            return False, f"has_failure_reasons: {self.failure_reasons[0]}"
        if not self.artifact_hash:
            return False, "not_sealed"
        return True, "ready_for_promotion"
