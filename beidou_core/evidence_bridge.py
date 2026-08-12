"""研究证据 → 运行时桥接。

启动时扫描 evidence/factors/**/*.json：重算 artifact_hash 对拍、
验证 EvidenceBundle 完整性、环境来源检查（canary/live 拒绝
historical_replay），随后按 promotion_chain 逐级调用
FactorPromotionGate.validate_evidence 复验并推进生命周期。

任何失败只记录并跳过该文件，不阻断启动（fail-closed 但非阻塞）。
每个文件从解析到组件注册的整段处理均被异常隔离：单个文件异常
（含非 dict payload 等未预期形态）只产生一条 rejected，不终止扫描。

NaN 处理：研究侧链上的 icir/ic 可能为 NaN（factor 序列 NaN 传播）。
复验前将非有限值归一为 0.0 — 门禁的 ICIR 阈值比较将拒绝该级
（fail-closed），绝不把 NaN 传给门禁（NaN < 阈值 恒为 False，
会让阈值检查静默通过）。

威胁模型：promotion_chain_hash 与 bundle artifact_hash 同一模型 —
防意外损坏/格式漂移的确定性对拍，非密钥化签名；有写入链权限的
攻击者可同时改写两处哈希，此处不做防伪承诺。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from beidou_research.factors.factor import FactorDefinition, FactorLifecycle, FactorRecord
from beidou_research.mining.evidence import EvidenceBundle
from beidou_shared.types import SchemaVersion, VenueId

REPLAY_REJECTED_ENV_MODES = frozenset({"canary", "live"})
CORE_FACTOR_IDS = frozenset(
    {
        "meanrev_entry_v1", "trend_entry_v1", "breakout_entry_v1",
        "momentum_filter_v1", "volatility_filter_v1", "volume_filter_v1",
        "trailing_exit_v1", "time_exit_v1",
    }
)


@dataclass
class BridgeReport:
    applied: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)


class EvidenceBridge:
    @staticmethod
    def load_and_apply(
        *,
        registry: Any,
        gate: Any,
        env_mode: str,
        component_registry: dict,
        entry_ids: set[str],
        filter_ids: set[str],
        exit_ids: set[str],
        evidence_dir: str = "evidence/factors",
        alerts: Any = None,
    ) -> BridgeReport:
        report = BridgeReport()
        root = Path(evidence_dir)
        if not root.exists():
            return report
        files = sorted(root.rglob("*.json"))
        handled_factor_ids: set[str] = set()
        for path in files:
            try:
                payload = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                report.rejected.append((str(path), f"unreadable:{type(exc).__name__}"))
                continue
            # F1: 每文件处理体整段异常隔离 — 非 dict payload、未预期形态等
            # 任何异常只记一条 rejected，绝不终止整个扫描。
            try:
                data = payload.get("data")
                if not isinstance(data, dict):
                    report.rejected.append((str(path), "missing_bundle_data"))
                    continue
                chain = payload.get("promotion_chain")
                if not isinstance(chain, list) or not chain:
                    report.rejected.append((str(path), "missing_promotion_chain"))
                    continue
                # F2: 链完整性对拍（与 bundle artifact_hash 同模型）—
                # 逐字节重算 sha256 与写入时绑定哈希比对，链篡改不晋级。
                chain_hash = hashlib.sha256(json.dumps(chain, sort_keys=True, default=str).encode()).hexdigest()
                stored_chain_hash = str(payload.get("promotion_chain_hash", ""))
                if chain_hash != stored_chain_hash:
                    report.rejected.append((str(path), "promotion_chain_hash_mismatch"))
                    continue
                try:
                    bundle = EvidenceBundle(**{k: v for k, v in data.items() if k != "created_at"})
                except TypeError as exc:
                    report.rejected.append((str(path), f"bundle_construct:{exc}"))
                    continue
                recomputed = bundle.compute_bundle_hash()
                stored_hash = str(data.get("artifact_hash", ""))
                if recomputed != stored_hash:
                    report.rejected.append((str(path), "artifact_hash_mismatch"))
                    continue
                promotable, reason = bundle.can_promote()
                if not promotable:
                    report.rejected.append((str(path), f"bundle:{reason}"))
                    continue
                evidence_source = str(payload.get("evidence_source", "")).strip()
                if evidence_source == "historical_replay" and env_mode in REPLAY_REJECTED_ENV_MODES:
                    report.rejected.append((str(path), "replay_evidence_rejected_in_production"))
                    continue
                expression_string = str(payload.get("expression_string", "")).strip()
                role = str(payload.get("role", "entry")).strip().lower() or "entry"
                if not expression_string:
                    report.rejected.append((str(path), "missing_expression_string"))
                    continue

                factor_id = str(bundle.factor_id)
                if factor_id in handled_factor_ids:
                    continue  # 同因子多品种 bundle：确定性取第一个
                handled_factor_ids.add(factor_id)

                record = registry.get(factor_id)
                if record is None:
                    definition = FactorDefinition(
                        factor_id=factor_id,
                        name=f"mined-{factor_id}",
                        version=SchemaVersion(bundle.factor_version or "2.0.0"),
                        description=f"Mined factor {factor_id} (evidence {bundle.artifact_hash[:12]})",
                        author="factor-miner",
                        category="mined",
                        universe=frozenset({VenueId("BINANCE")}),
                        instrument_types=frozenset({"perpetual"}),
                        economic_rationale=payload.get("economic_rationale", "mined factor"),
                        lookback_period="1h",
                        rebalance_interval="1h",
                    )
                    record = registry.register(definition)

                # F4: 组件注册在"已 ACTIVE 幂等跳过"分支之前 —
                # skip 与晋级两条路径都注册 ExpressionComponent，
                # 否则重启后已 ACTIVE 因子不再可交易。
                if record.has_authorized_active_evidence():
                    EvidenceBridge._register_expression_component(
                        factor_id, expression_string, role,
                        component_registry, entry_ids, filter_ids, exit_ids,
                    )
                    report.applied.append(factor_id)  # 已 ACTIVE：幂等跳过
                    continue

                # 逐级复验：不可直接采信文件内 approved 字段
                applied = EvidenceBridge._apply_chain(record, gate, chain, bundle, path, report)
                if applied:
                    EvidenceBridge._register_expression_component(
                        factor_id, expression_string, role,
                        component_registry, entry_ids, filter_ids, exit_ids,
                    )
            except Exception as exc:  # F1 隔离网：任何未预期异常只跳过该文件，不终止扫描
                report.rejected.append((str(path), f"unhandled:{type(exc).__name__}"))
                continue
        return report

    @staticmethod
    def _apply_chain(record: FactorRecord, gate: Any, chain: list[dict], bundle: EvidenceBundle,
                     path: Path, report: BridgeReport) -> bool:
        from beidou_research.factors.factor import FactorPerformance, PromotionDecision

        for step in chain:
            try:
                from_state = FactorLifecycle(str(step["from"]))
                to_state = FactorLifecycle(str(step["to"]))
            except (KeyError, ValueError) as exc:
                report.rejected.append((str(path), f"chain_state:{exc}"))
                return False
            if record.lifecycle == to_state:
                continue
            # 逐级复验必须强制 min_icir/min_sample 阈值（fail-closed）：
            # 从链记录重建 FactorPerformance 传给门禁。
            # NaN/non-finite → 0.0：门禁的 ICIR 阈值比较将拒绝该级。
            step_icir = EvidenceBridge._as_finite_float(step.get("icir", 0.0))
            step_ic = EvidenceBridge._as_finite_float(step.get("ic", 0.0))
            try:
                step_samples = int(step.get("sample_count", 0))
            except (TypeError, ValueError):
                step_samples = 0
            evidence_ids = step.get("evidence_ids", [])
            if not isinstance(evidence_ids, list):
                evidence_ids = []
            performance = FactorPerformance(
                factor_id=record.definition.factor_id,
                evaluation_period="historical",
                sample_count=step_samples,
                ic_mean=step_ic,
                ic_std=0.0,
                icir=step_icir,
                rank_ic_mean=0.0,
                rank_ic_std=0.0,
                rank_icir=step_icir,
            )
            decision = gate.validate_evidence(
                factor_id=record.definition.factor_id,
                current_state=from_state,
                target_state=to_state,
                performance=performance,
                evidence_ids=evidence_ids,
                factor_version=str(step.get("factor_version", "")),
                commit=str(step.get("commit", "")),
                dataset_hash=str(step.get("dataset_hash", "")),
                policy_version=str(step.get("policy_version", "")),
                falsifier=str(step.get("falsifier", "factor-miner")),
                evidence_bundle=bundle if to_state == FactorLifecycle.ACTIVE else None,
            )
            if not decision.approved:
                report.rejected.append((str(path), f"gate_rejected@{to_state.value}:{decision.reason[:120]}"))
                return False
            if record.lifecycle != from_state:
                report.rejected.append((str(path), f"chain_order_mismatch@{from_state.value}"))
                return False
            record.transition(to_state)
            record.promotion_history.append(
                PromotionDecision(
                    decision_id=decision.decision_id,
                    factor_id=decision.factor_id,
                    from_state=from_state,
                    to_state=to_state,
                    approved=True,
                    reason=decision.reason,
                    factor_version=decision.factor_version,
                    commit=decision.commit,
                    dataset_hash=decision.dataset_hash,
                    evidence_ids=decision.evidence_ids,
                    policy_version=decision.policy_version,
                    falsifier=decision.falsifier,
                    evidence_artifact_hash=bundle.artifact_hash,
                )
            )
        report.applied.append(record.definition.factor_id)
        return True

    @staticmethod
    def _as_finite_float(value: Any) -> float:
        """JSON 链值 → float；NaN/±inf/不可转换一律归一为 0.0（fail-closed）。"""
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(parsed):
            return 0.0
        return parsed

    @staticmethod
    def _register_expression_component(
        factor_id: str,
        expression_string: str,
        role: str,
        component_registry: dict,
        entry_ids: set[str],
        filter_ids: set[str],
        exit_ids: set[str],
    ) -> None:
        from beidou_core.expression_component import ExpressionComponent

        component_registry[factor_id] = (
            partial(ExpressionComponent, factor_id=factor_id, expression_string=expression_string, role=role.upper()),
            (),
        )
        if role == "filter":
            filter_ids.add(factor_id)
        elif role == "exit":
            exit_ids.add(factor_id)
        else:
            entry_ids.add(factor_id)
