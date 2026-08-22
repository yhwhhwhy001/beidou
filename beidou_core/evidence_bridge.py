"""研究证据 → 运行时桥接。

启动时扫描 evidence/factors/**/*.json：重算 artifact_hash 对拍、
验证 EvidenceBundle 完整性、环境来源检查（canary/live 拒绝
historical_replay），随后按 promotion_chain 逐级调用
FactorPromotionGate.validate_evidence 复验并推进生命周期。

文件格式（与 JSONFileFactorStore.save_factor_version 透传一致）：
顶层 {"factor_id", "version", "data": {...}}；data 内层同时承载
bundle 字段与扩展键（promotion_chain/promotion_chain_hash/
evidence_source/expression_string/role）— 扩展键从 data 读取，
且不参与 EvidenceBundle 构造。

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
# store 透传的 data 内层扩展键：不属于 EvidenceBundle 构造参数，需单独取出
EVIDENCE_EXTENSION_KEYS = frozenset(
    {"promotion_chain", "promotion_chain_hash", "evidence_source", "expression_string", "role"}
)
CORE_FACTOR_IDS = frozenset(
    {
        "meanrev_entry_v1",
        "trend_entry_v1",
        "breakout_entry_v1",
        "momentum_filter_v1",
        "volatility_filter_v1",
        "volume_filter_v1",
        "trailing_exit_v1",
        "time_exit_v1",
    }
)


@dataclass
class BridgeReport:
    applied: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class _ValidatedEvidence:
    """A bundle that passed the file-level integrity checks."""

    path: Path
    factor_id: str
    bundle: EvidenceBundle
    chain: list[dict[str, Any]]
    expression_string: str
    role: str
    economic_rationale: Any


_LIFECYCLE_RANK: dict[FactorLifecycle, int] = {
    state: rank
    for rank, state in enumerate(
        (
            FactorLifecycle.IDEA,
            FactorLifecycle.GENERATED,
            FactorLifecycle.SANITY_PASSED,
            FactorLifecycle.RESEARCH_VALIDATED,
            FactorLifecycle.OOS_VERIFIED,
            FactorLifecycle.COST_CAPACITY_VERIFIED,
            FactorLifecycle.PAPER_TRADING,
            FactorLifecycle.CHALLENGER,
            FactorLifecycle.ACTIVE,
        )
    )
}


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
        candidates_by_factor: dict[str, list[_ValidatedEvidence]] = {}
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
                # GAP-10: 扩展键（chain/hash/source/expression/role）由 store
                # 透传在 data 内层，从 data 读取而非文件顶层。
                chain = data.get("promotion_chain")
                if not isinstance(chain, list) or not chain:
                    report.rejected.append((str(path), "missing_promotion_chain"))
                    continue
                # F2: 链完整性对拍（与 bundle artifact_hash 同模型）—
                # 逐字节重算 sha256 与写入时绑定哈希比对，链篡改不晋级。
                chain_hash = hashlib.sha256(json.dumps(chain, sort_keys=True, default=str).encode()).hexdigest()
                stored_chain_hash = str(data.get("promotion_chain_hash", ""))
                if chain_hash != stored_chain_hash:
                    report.rejected.append((str(path), "promotion_chain_hash_mismatch"))
                    continue
                bundle_fields = {
                    k: v for k, v in data.items() if k not in EVIDENCE_EXTENSION_KEYS and k != "created_at"
                }
                try:
                    bundle = EvidenceBundle(**bundle_fields)
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
                evidence_source = str(data.get("evidence_source", "")).strip()
                if evidence_source == "historical_replay" and env_mode in REPLAY_REJECTED_ENV_MODES:
                    report.rejected.append((str(path), "replay_evidence_rejected_in_production"))
                    continue
                expression_string = str(data.get("expression_string", "")).strip()
                role = str(data.get("role", "entry")).strip().lower() or "entry"
                if not expression_string:
                    report.rejected.append((str(path), "missing_expression_string"))
                    continue

                factor_id = str(bundle.factor_id)
                candidates_by_factor.setdefault(factor_id, []).append(
                    _ValidatedEvidence(
                        path=path,
                        factor_id=factor_id,
                        bundle=bundle,
                        chain=chain,
                        expression_string=expression_string,
                        role=role,
                        economic_rationale=data.get("economic_rationale", "mined factor"),
                    )
                )
            except Exception as exc:  # F1 隔离网：任何未预期异常只跳过该文件，不终止扫描
                report.rejected.append((str(path), f"unhandled:{type(exc).__name__}"))
                continue
        # 同一 factor_id 可能有多份合法文件（不同品种、候选或版本）。
        # 先选最高生命周期证据，再应用；如果该候选的链门禁失败，则回退到
        # 下一份较弱但仍完整的证据。这样既不被文件名决定，也不会绕过
        # 逐级门禁。
        for factor_id in sorted(candidates_by_factor):
            candidates = sorted(
                candidates_by_factor[factor_id],
                key=EvidenceBridge._candidate_rank,
                reverse=True,
            )
            for candidate in candidates:
                path = candidate.path
                try:
                    record = registry.get(factor_id)
                    if record is None:
                        definition = FactorDefinition(
                            factor_id=factor_id,
                            name=f"mined-{factor_id}",
                            version=SchemaVersion(candidate.bundle.factor_version or "2.0.0"),
                            description=(f"Mined factor {factor_id} (evidence {candidate.bundle.artifact_hash[:12]})"),
                            author="factor-miner",
                            category="mined",
                            universe=frozenset({VenueId("BINANCE")}),
                            instrument_types=frozenset({"perpetual"}),
                            economic_rationale=candidate.economic_rationale,
                            lookback_period="1h",
                            rebalance_interval="1h",
                        )
                        record = registry.register(definition)

                    # F4: 组件注册在"已 ACTIVE 幂等跳过"分支之前 —
                    # skip 与晋级两条路径都注册 ExpressionComponent，
                    # 否则重启后已 ACTIVE 因子不再可交易。
                    if record.has_authorized_active_evidence():
                        EvidenceBridge._register_expression_component(
                            factor_id,
                            candidate.expression_string,
                            candidate.role,
                            component_registry,
                            entry_ids,
                            filter_ids,
                            exit_ids,
                        )
                        report.applied.append(factor_id)  # 已 ACTIVE：幂等跳过
                        break

                    # 逐级复验：不可直接采信文件内 approved 字段
                    applied = EvidenceBridge._apply_chain(
                        record,
                        gate,
                        candidate.chain,
                        candidate.bundle,
                        path,
                        report,
                    )
                    if applied:
                        EvidenceBridge._register_expression_component(
                            factor_id,
                            candidate.expression_string,
                            candidate.role,
                            component_registry,
                            entry_ids,
                            filter_ids,
                            exit_ids,
                        )
                        break
                except Exception as exc:  # F1 隔离网：单候选异常不终止扫描
                    report.rejected.append((str(path), f"unhandled:{type(exc).__name__}"))
                    continue
        return report

    @staticmethod
    def _candidate_rank(candidate: _ValidatedEvidence) -> tuple[int, int, str]:
        """Return a deterministic rank that prefers the strongest chain."""
        try:
            target = FactorLifecycle(str(candidate.chain[-1]["to"]))
        except (KeyError, TypeError, ValueError):
            target = FactorLifecycle.IDEA
        return (_LIFECYCLE_RANK.get(target, -1), len(candidate.chain), str(candidate.path))

    @staticmethod
    def _apply_chain(
        record: FactorRecord, gate: Any, chain: list[dict], bundle: EvidenceBundle, path: Path, report: BridgeReport
    ) -> bool:
        from beidou_research.factors.factor import FactorPerformance, PromotionDecision

        original_lifecycle = record.lifecycle
        original_history_length = len(record.promotion_history)

        def rollback() -> None:
            record.lifecycle = original_lifecycle
            del record.promotion_history[original_history_length:]

        try:
            for step in chain:
                try:
                    from_state = FactorLifecycle(str(step["from"]))
                    to_state = FactorLifecycle(str(step["to"]))
                except (KeyError, ValueError) as exc:
                    rollback()
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
                    rollback()
                    report.rejected.append((str(path), f"gate_rejected@{to_state.value}:{decision.reason[:120]}"))
                    return False
                if record.lifecycle != from_state:
                    rollback()
                    report.rejected.append((str(path), f"chain_order_mismatch@{from_state.value}"))
                    return False
                if not record.transition(to_state):
                    rollback()
                    report.rejected.append((str(path), f"transition_rejected@{to_state.value}"))
                    return False
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
        except Exception:
            rollback()
            raise
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
