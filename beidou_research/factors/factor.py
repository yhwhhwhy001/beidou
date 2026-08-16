"""因子研究、边际贡献、生命周期、退役与重启。

PKG-14: 因子定义、IC/RankIC/ICIR、分层回测、衰减分析、换手率、
成本后边际贡献、因子生命周期管理（IDEA→ACTIVE→DEGRADED→SUSPENDED→RETIRED）。
退役证据永久保留；重新启用等同新 Challenger，必须重新经过完整 Gate。

BD-CV22: 集成 FactorEvidence contract — NaN/空evidence/旧evidence不能PROMOTED。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from beidou_research.contracts import FactorEvidence, FactorLifecycleState  # BD-CV22
from beidou_shared.types import SchemaVersion, VenueId


class FactorLifecycle(str, Enum):
    """因子生命周期。退役后重新启用等同新 Challenger。

    BF-07 新状态机 (Section 10):
    IDEA → GENERATED → SANITY_PASSED → RESEARCH_VALIDATED
    → OOS_VERIFIED → COST_CAPACITY_VERIFIED → PAPER_TRADING
    → CHALLENGER → ACTIVE → DEGRADED → SUSPENDED → RETIRED
    """

    # 新状态 (BF-07)
    IDEA = "IDEA"
    GENERATED = "GENERATED"  # 候选已生成
    SANITY_PASSED = "SANITY_PASSED"  # DQ/泄漏/复杂度通过
    RESEARCH_VALIDATED = "RESEARCH_VALIDATED"  # 统计显著性+多重检验通过
    OOS_VERIFIED = "OOS_VERIFIED"  # Purged WFO/CPCV 通过
    COST_CAPACITY_VERIFIED = "COST_CAPACITY_VERIFIED"  # 成本/容量通过
    PAPER_TRADING = "PAPER_TRADING"
    CHALLENGER = "CHALLENGER"
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    SUSPENDED = "SUSPENDED"
    RETIRED = "RETIRED"

    # 兼容旧状态（标记为 LEGACY）
    RESEARCH = "RESEARCH"  # DEPRECATED: 使用 RESEARCH_VALIDATED
    BACKTEST = "BACKTEST"  # DEPRECATED: 使用 OOS_VERIFIED


# 合法生命周期转换 (BF-07 更新)
FACTOR_LIFECYCLE_TRANSITIONS: dict[FactorLifecycle, set[FactorLifecycle]] = {
    # 新路径 (含旧状态兼容)
    FactorLifecycle.IDEA: {
        FactorLifecycle.GENERATED,
        FactorLifecycle.RESEARCH,
        FactorLifecycle.RESEARCH_VALIDATED,
        FactorLifecycle.RETIRED,
    },
    FactorLifecycle.GENERATED: {FactorLifecycle.SANITY_PASSED, FactorLifecycle.RETIRED},
    FactorLifecycle.SANITY_PASSED: {FactorLifecycle.RESEARCH_VALIDATED, FactorLifecycle.RETIRED},
    FactorLifecycle.RESEARCH_VALIDATED: {FactorLifecycle.OOS_VERIFIED, FactorLifecycle.RETIRED},
    FactorLifecycle.OOS_VERIFIED: {FactorLifecycle.COST_CAPACITY_VERIFIED, FactorLifecycle.RETIRED},
    FactorLifecycle.COST_CAPACITY_VERIFIED: {FactorLifecycle.PAPER_TRADING, FactorLifecycle.RETIRED},
    FactorLifecycle.PAPER_TRADING: {FactorLifecycle.CHALLENGER, FactorLifecycle.RETIRED},
    FactorLifecycle.CHALLENGER: {FactorLifecycle.ACTIVE, FactorLifecycle.RETIRED},
    FactorLifecycle.ACTIVE: {FactorLifecycle.DEGRADED, FactorLifecycle.SUSPENDED, FactorLifecycle.RETIRED},
    FactorLifecycle.DEGRADED: {FactorLifecycle.ACTIVE, FactorLifecycle.SUSPENDED, FactorLifecycle.RETIRED},
    FactorLifecycle.SUSPENDED: {FactorLifecycle.CHALLENGER, FactorLifecycle.RETIRED},
    FactorLifecycle.RETIRED: set(),
    # 兼容旧路径 (DEPRECATED)
    FactorLifecycle.RESEARCH: {FactorLifecycle.BACKTEST, FactorLifecycle.RESEARCH_VALIDATED, FactorLifecycle.RETIRED},
    FactorLifecycle.BACKTEST: {FactorLifecycle.PAPER_TRADING, FactorLifecycle.OOS_VERIFIED, FactorLifecycle.RETIRED},
}


@dataclass(frozen=True, slots=True)
class FactorDefinition:
    """因子定义 — 不可变版本化记录。"""

    factor_id: str
    name: str
    version: SchemaVersion
    description: str
    author: str
    category: str  # e.g., momentum, mean_reversion, flow, sentiment, volatility
    universe: frozenset[VenueId]  # 适用交易所
    instrument_types: frozenset[str]  # e.g., perpetual, spot
    economic_rationale: str  # 经济假设
    lookback_period: str  # e.g., "24h", "7d"
    rebalance_interval: str  # e.g., "1h", "1d"
    parameters: dict[str, Any] = field(default_factory=dict)
    tags: frozenset[str] = field(default_factory=frozenset)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class FactorPerformance:
    """因子性能指标。IC/RankIC/ICIR、分层回测、衰减分析。"""

    factor_id: str
    evaluation_period: str  # e.g., "2026-Q1"
    sample_count: int
    ic_mean: float  # Information Coefficient 均值
    ic_std: float
    icir: float  # Information Coefficient / IC std = IC/IC_std
    rank_ic_mean: float  # Rank IC 均值
    rank_ic_std: float
    rank_icir: float
    long_short_spread: float | None = None  # 多空分层收益差
    top_bottom_decile_spread: float | None = None  # 顶部vs底部分位数收益
    turnover_pct: float | None = None  # 日均换手率
    hit_rate: float | None = None  # 方向正确率
    capacity_decay: dict[str, float] = field(default_factory=dict)  # 不同容量下的 IC 衰减
    cost_adjusted_ic: float | None = None  # 扣除交易成本后的 IC
    sharpe_contribution: float | None = None  # 对组合 Sharpe 的边际贡献
    max_drawdown_pct: float | None = None
    venue_breakdown: dict[VenueId, float] = field(default_factory=dict)  # 跨所 IC 分解
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class MarginalContribution:
    """边际贡献分析。评估因子加入现有组合后的增量收益和风险。"""

    factor_id: str
    existing_factor_ids: frozenset[str]
    marginal_sharpe: float  # 边际 Sharpe 增量
    marginal_ic: float  # 边际 IC 增量
    diversification_benefit: float  # 分散化收益（降低组合波动）
    collinearity_vif: float  # 方差膨胀因子（VIF），>5 表示高共线性
    collinearity_with: list[str] = field(default_factory=list)  # 高共线因子
    information_ratio_change: float = 0.0  # IR 变化
    turnover_impact_pct: float = 0.0  # 对组合换手率影响
    cost_impact_bps: float = 0.0  # 对组合成本影响（bps）
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def has_adverse_collinearity(self) -> bool:
        """高共线性且无边际贡献 = 不应激活。"""
        return self.collinearity_vif > 5.0 and self.marginal_sharpe <= 0.01


@dataclass
class FactorRecord:
    """因子运行时记录。绑定定义、性能、生命周期。"""

    definition: FactorDefinition
    lifecycle: FactorLifecycle = FactorLifecycle.IDEA
    performance: list[FactorPerformance] = field(default_factory=list)
    marginal_contributions: list[MarginalContribution] = field(default_factory=list)
    retirement_evidence: dict[str, Any] = field(default_factory=dict)
    retirement_reason: str = ""
    retired_at: datetime | None = None
    restarted_from: str | None = None  # 重新启用的原始 factor_id
    promotion_history: list[PromotionDecision] = field(default_factory=list)  # BD-T06: 晋级证据链

    def transition(self, target: FactorLifecycle) -> bool:
        allowed = FACTOR_LIFECYCLE_TRANSITIONS.get(self.lifecycle, set())
        if target not in allowed:
            return False
        self.lifecycle = target
        return True

    def can_restart(self) -> bool:
        """退役后重新启用必须走完整 Gate — 等同新 Challenger。"""
        return self.lifecycle in (FactorLifecycle.SUSPENDED,)

    def restart_as_challenger(self) -> bool:
        """重新启用 = 新 Challenger，需重新验证。"""
        if not self.can_restart():
            return False
        self.lifecycle = FactorLifecycle.CHALLENGER
        self.retirement_evidence["restarted_at"] = datetime.now(timezone.utc).isoformat()
        return True

    def has_authorized_active_evidence(self) -> bool:
        """Return whether ACTIVE is backed by a sealed promotion artifact.

        ``lifecycle`` is intentionally mutable for compatibility with older
        research tooling.  Execution paths must therefore consult this
        predicate instead of treating a local ACTIVE enum as authorization.
        """

        if self.lifecycle != FactorLifecycle.ACTIVE:
            return False
        return any(
            decision.approved
            and decision.to_state == FactorLifecycle.ACTIVE
            and bool(decision.evidence_artifact_hash.strip())
            and bool(decision.commit.strip())
            and bool(decision.dataset_hash.strip())
            and bool(decision.policy_version.strip())
            for decision in self.promotion_history
        )


# ================================================================
# BD-T06: 证据驱动的因子晋级系统 (Production 模式)
# ================================================================


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    """BD-T06: 不可变晋级决策。每次生命周期转换的唯一证据记录。

    绑定: factor_version, commit, dataset_hash, evidence_ids, policy_version, falsifier。
    """

    decision_id: str
    factor_id: str
    from_state: FactorLifecycle
    to_state: FactorLifecycle
    approved: bool
    reason: str
    # 证据绑定
    factor_version: str = ""
    commit: str = ""
    dataset_hash: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    policy_version: str = ""
    falsifier: str = ""  # 谁执行了此决策
    evidence_artifact_hash: str = ""  # sealed EvidenceBundle.artifact_hash
    # 性能指标
    ic: float = 0.0
    rank_ic: float = 0.0
    icir: float = 0.0
    cost_adjusted_ic: float = 0.0
    sample_count: int = 0
    # 元数据
    decided_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    correlation_id: str = ""


# BD-T06: 每个生命周期阶段的证据要求
PROMOTION_EVIDENCE_REQUIREMENTS: dict[FactorLifecycle, dict] = {
    FactorLifecycle.IDEA: {
        "required_evidence": ["code_compiles", "basic_test_pass", "economic_rationale"],
        "min_icir": None,
        "min_sample_count": 0,
        "description": "因子代码编译通过，基础测试通过，有明确经济假设",
    },
    FactorLifecycle.GENERATED: {
        "required_evidence": ["non_zero_output", "non_constant_output", "no_lookahead_bias"],
        "min_icir": None,
        "min_sample_count": 20,
        "description": "因子产生非零、非常量输出，无前视偏差",
    },
    FactorLifecycle.SANITY_PASSED: {
        "required_evidence": ["ic_significant", "rank_ic_significant", "decile_spread_positive"],
        "min_icir": 0.3,
        "min_sample_count": 100,
        "description": "IC 统计显著，Rank IC 显著，分层回测有效",
    },
    FactorLifecycle.RESEARCH_VALIDATED: {
        "required_evidence": ["multiple_testing_corrected", "no_p_hacking", "purged_wfo_complete"],
        "min_icir": 0.3,
        "min_sample_count": 200,
        "description": "多重检验校正通过，无 p-hacking，Purged WFO 完成",
    },
    FactorLifecycle.OOS_VERIFIED: {
        "required_evidence": ["oos_icir_stable", "cpcv_passed", "no_regime_overfit"],
        "min_icir": 0.2,
        "min_sample_count": 300,
        "description": "样本外 ICIR 稳定，CPCV 通过，无 regime 过拟合",
    },
    FactorLifecycle.COST_CAPACITY_VERIFIED: {
        "required_evidence": ["cost_adjusted_ic_positive", "capacity_decay_acceptable", "turnover_acceptable"],
        "min_icir": 0.2,
        "min_sample_count": 300,
        "description": "扣除成本后 IC 为正，容量衰减可接受，换手率合理",
    },
    FactorLifecycle.PAPER_TRADING: {
        "required_evidence": ["paper_sharpe_positive", "paper_drawdown_acceptable", "signal_consistency_good"],
        "min_icir": 0.15,
        "min_sample_count": 500,
        "description": "Paper 交易 Sharpe 为正，回撤可接受，信号一致性良好",
    },
    FactorLifecycle.CHALLENGER: {
        "required_evidence": ["challenger_period_complete", "live_signal_quality", "latency_within_slo"],
        "min_icir": 0.1,
        "min_sample_count": 500,
        "description": "Challenger 阶段完成，实盘信号质量好，延迟在 SLO 内",
    },
    FactorLifecycle.ACTIVE: {
        # ACTIVE is a production authorization, not a diagnostic label.  It
        # therefore needs an explicit, independently replayable promotion
        # envelope even when the source state is CHALLENGER.
        "required_evidence": [
            "sealed_oos_verified",
            "cost_capacity_verified",
            "paper_shadow_verified",
            "active_approval",
        ],
        "min_icir": 0.1,
        "min_sample_count": 500,
        "description": "活跃交易中，必须绑定 sealed OOS、成本容量、Paper/Shadow 和具名批准",
    },
}


class FactorPromotionGate:
    """BD-T06: 因子晋级门禁 — 验证每个阶段所需证据。

    所有环境都强制证据门禁；Testnet/Paper 不能绕过生产语义。
    """

    def __init__(self, strict: bool = True):
        # ``strict=False`` used to be an environment escape hatch.  Keeping
        # the parameter for API compatibility but refusing the bypass makes a
        # testnet startup unable to manufacture an ACTIVE factor.
        self._strict = True
        self._requested_strict = strict

    def validate_evidence(
        self,
        factor_id: str,
        current_state: FactorLifecycle,
        target_state: FactorLifecycle,
        performance: FactorPerformance | None = None,
        evidence_ids: list[str] | None = None,
        factor_version: str = "",
        commit: str = "",
        dataset_hash: str = "",
        policy_version: str = "",
        falsifier: str = "system",
        evidence_bundle: Any | None = None,
    ) -> PromotionDecision:
        """验证晋级证据是否满足目标阶段要求。

        Returns:
            PromotionDecision: 包含审批结果和原因
        """
        import uuid

        decision_id = f"promo-{factor_id}-{target_state.value}-{uuid.uuid4().hex[:8]}"

        allowed_targets = FACTOR_LIFECYCLE_TRANSITIONS.get(current_state, set())
        if target_state not in allowed_targets:
            return PromotionDecision(
                decision_id=decision_id,
                factor_id=factor_id,
                from_state=current_state,
                to_state=target_state,
                approved=False,
                reason=f"Invalid lifecycle transition: {current_state.value} -> {target_state.value}",
                factor_version=factor_version,
                commit=commit,
                dataset_hash=dataset_hash,
                evidence_ids=evidence_ids or [],
                policy_version=policy_version,
                falsifier=falsifier,
            )

        # 严格模式: 验证证据要求
        requirements = PROMOTION_EVIDENCE_REQUIREMENTS.get(target_state)
        if requirements is None:
            return PromotionDecision(
                decision_id=decision_id,
                factor_id=factor_id,
                from_state=current_state,
                to_state=target_state,
                approved=False,
                reason=f"No evidence requirements defined for {target_state.value}",
            )

        failures: list[str] = []

        # All research-to-runtime transitions need reproducible provenance;
        # an arbitrary list of evidence labels is not a substitute for the
        # sealed artifact that produced them.  ACTIVE is a production
        # authorization and therefore additionally requires a complete,
        # promotable EvidenceBundle bound to this exact factor/data/policy.
        evidence_bound_states = {
            FactorLifecycle.RESEARCH_VALIDATED,
            FactorLifecycle.OOS_VERIFIED,
            FactorLifecycle.COST_CAPACITY_VERIFIED,
            FactorLifecycle.PAPER_TRADING,
            FactorLifecycle.CHALLENGER,
            FactorLifecycle.ACTIVE,
        }
        if target_state in evidence_bound_states:
            bindings = {
                "commit": commit,
                "dataset_hash": dataset_hash,
                "policy_version": policy_version,
                "falsifier": falsifier,
            }
            missing_bindings = [
                name
                for name, value in bindings.items()
                if not isinstance(value, str) or not value.strip() or value.strip().upper() == "UNKNOWN"
            ]
            if missing_bindings:
                failures.append(f"Missing provenance bindings: {missing_bindings}")

        if target_state == FactorLifecycle.ACTIVE:
            if evidence_bundle is None:
                failures.append("ACTIVE requires a sealed EvidenceBundle")
            else:
                try:
                    promotable, bundle_reason = evidence_bundle.can_promote()
                except Exception as exc:
                    promotable = False
                    bundle_reason = f"bundle_contract_error:{type(exc).__name__}"
                if not promotable:
                    failures.append(f"EvidenceBundle not promotable: {bundle_reason}")
                else:
                    bundle_factor_id = str(getattr(evidence_bundle, "factor_id", ""))
                    bundle_factor_version = str(getattr(evidence_bundle, "factor_version", ""))
                    bundle_dataset_hash = str(getattr(evidence_bundle, "dataset_manifest_hash", ""))
                    bundle_policy_version = str(getattr(evidence_bundle, "policy_version", ""))
                    if bundle_factor_id != factor_id:
                        failures.append("EvidenceBundle factor_id mismatch")
                    if factor_version and bundle_factor_version != factor_version:
                        failures.append("EvidenceBundle factor_version mismatch")
                    if dataset_hash and bundle_dataset_hash != dataset_hash:
                        failures.append("EvidenceBundle dataset_manifest_hash mismatch")
                    if policy_version and bundle_policy_version != policy_version:
                        failures.append("EvidenceBundle policy_version mismatch")

        evidence_artifact_hash = str(getattr(evidence_bundle, "artifact_hash", "") or "")

        # 检查必需证据
        required = requirements.get("required_evidence", [])
        provided = set(evidence_ids or [])
        missing = [e for e in required if e not in provided]
        if missing:
            failures.append(f"Missing required evidence: {missing}")

        # 检查 ICIR 阈值
        if performance is not None:
            # M04-F01 (P0): NaN/Inf 不得通过晋级门禁 —— NaN < threshold 在
            # Python 中恒 False（直接通过），Inf 同理。非有限指标必须显式
            # 拒绝并记录,绝不能进入生命周期推进。
            non_finite_metrics = []
            if not math.isfinite(float(performance.icir)):
                non_finite_metrics.append(f"icir={performance.icir!r}")
            if not math.isfinite(float(performance.ic_mean)):
                non_finite_metrics.append(f"ic_mean={performance.ic_mean!r}")
            if isinstance(performance.sample_count, (int, float)) and not math.isfinite(
                float(performance.sample_count)
            ):
                non_finite_metrics.append(f"sample_count={performance.sample_count!r}")
            if non_finite_metrics:
                failures.append("Non-finite metrics rejected: " + ", ".join(non_finite_metrics))

            min_icir = requirements.get("min_icir")
            if min_icir is not None and performance.icir < min_icir:
                failures.append(f"ICIR {performance.icir:.3f} < threshold {min_icir}")

            min_samples = requirements.get("min_sample_count", 0)
            if performance.sample_count < min_samples:
                failures.append(f"Sample count {performance.sample_count} < required {min_samples}")

        if failures:
            return PromotionDecision(
                decision_id=decision_id,
                factor_id=factor_id,
                from_state=current_state,
                to_state=target_state,
                approved=False,
                reason="; ".join(failures),
                factor_version=factor_version,
                commit=commit,
                dataset_hash=dataset_hash,
                evidence_ids=evidence_ids or [],
                policy_version=policy_version,
                falsifier=falsifier,
                evidence_artifact_hash=evidence_artifact_hash,
                ic=performance.ic_mean if performance else 0.0,
                icir=performance.icir if performance else 0.0,
                sample_count=performance.sample_count if performance else 0,
            )

        # 通过
        return PromotionDecision(
            decision_id=decision_id,
            factor_id=factor_id,
            from_state=current_state,
            to_state=target_state,
            approved=True,
            reason=f"All evidence requirements met for {target_state.value}",
            factor_version=factor_version,
            commit=commit,
            dataset_hash=dataset_hash,
            evidence_ids=evidence_ids or [],
            policy_version=policy_version,
            falsifier=falsifier,
            evidence_artifact_hash=evidence_artifact_hash,
            ic=performance.ic_mean if performance else 0.0,
            icir=performance.icir if performance else 0.0,
            sample_count=performance.sample_count if performance else 0,
        )

    def promote(
        self,
        record: FactorRecord,
        target_state: FactorLifecycle,
        performance: FactorPerformance | None = None,
        evidence_ids: list[str] | None = None,
        **kwargs,
    ) -> PromotionDecision:
        """执行因子晋级：验证证据 → 创建 PromotionDecision → 推进生命周期。

        Returns:
            PromotionDecision: 包含审批结果的不可变记录
        """
        decision = self.validate_evidence(
            factor_id=record.definition.factor_id,
            current_state=record.lifecycle,
            target_state=target_state,
            performance=performance,
            evidence_ids=evidence_ids,
            **kwargs,
        )

        if decision.approved:
            record.transition(target_state)
            record.promotion_history.append(decision)

        return decision


class FactorEvaluator:
    """因子评估器 — 计算 IC、RankIC、ICIR、分层回测、衰减、边际贡献。

    M04-F04: **deprecated 评估器** —— 统计推断请用
    ``beidou_research.mining.evaluation.metrics``（Newey-West 标准误、
    block bootstrap、VIF 等生产评估实现）。本类保留给 FactorRegistry
    的旧调用路径,迁移（M14 生命周期联动）完成后删除。
    """

    @staticmethod
    def compute_ic(predictions: list[float], returns: list[float]) -> tuple[float, float]:
        """计算 Pearson IC（信息系数）。

        M04-F03: 第二返回值是**收益标准差**（非标准误）—— 历史标注
        错误,当前无生产消费者（仅测试忽略性消费）。IC 的统计推断请用
        mining/evaluation/metrics.py 的 Newey-West/block bootstrap。
        """
        if len(predictions) < 3 or len(returns) < 3:
            return 0.0, 0.0
        n = min(len(predictions), len(returns))
        preds = predictions[:n]
        rets = returns[:n]

        mean_p = sum(preds) / n
        mean_r = sum(rets) / n

        cov = sum((preds[i] - mean_p) * (rets[i] - mean_r) for i in range(n)) / (n - 1)
        std_p = (sum((p - mean_p) ** 2 for p in preds) / (n - 1)) ** 0.5
        std_r = (sum((r - mean_r) ** 2 for r in rets) / (n - 1)) ** 0.5

        if std_p == 0 or std_r == 0:
            return 0.0, 0.0
        ic = cov / (std_p * std_r)
        return ic, std_r

    @staticmethod
    def compute_rank_ic(predictions: list[float], returns: list[float]) -> float:
        """计算 Spearman Rank IC。"""
        if len(predictions) < 3:
            return 0.0
        n = len(predictions)

        def rankify(data: list[float]) -> list[float]:
            indexed = sorted(enumerate(data), key=lambda x: x[1])
            ranks = [0.0] * len(data)
            i = 0
            while i < len(indexed):
                j = i
                while j < len(indexed) and indexed[j][1] == indexed[i][1]:
                    j += 1
                avg_rank = (i + j - 1) / 2.0 + 1
                for k in range(i, j):
                    ranks[indexed[k][0]] = avg_rank
                i = j
            return ranks

        rank_p = rankify(predictions)
        rank_r = rankify(returns)

        mean_rp = sum(rank_p) / n
        mean_rr = sum(rank_r) / n

        cov = sum((rank_p[i] - mean_rp) * (rank_r[i] - mean_rr) for i in range(n)) / (n - 1)
        std_rp = (sum((rp - mean_rp) ** 2 for rp in rank_p) / (n - 1)) ** 0.5
        std_rr = (sum((rr - mean_rr) ** 2 for rr in rank_r) / (n - 1)) ** 0.5

        if std_rp == 0 or std_rr == 0:
            return 0.0
        return cov / (std_rp * std_rr)

    @staticmethod
    def compute_icir(ic_series: list[float]) -> float:
        """ICIR = mean(IC) / std(IC)。

        M04-F02 (P0): 零方差（全同 IC 序列）返回 0.0 —— 旧实现返回
        ±inf,穿透下游 `icir < threshold` 门禁（Inf 恒通过）。
        无法从全同样本推断信息比,0.0 是保守中性值。
        """
        if len(ic_series) < 2:
            return 0.0
        mean_ic = sum(ic_series) / len(ic_series)
        var = sum((ic - mean_ic) ** 2 for ic in ic_series) / (len(ic_series) - 1)
        std_ic = var**0.5
        # M04-F02: 浮点精度下全同序列的 std 可能为 ~1e-17 而非精确 0 ——
        # 用相对容差判定"不可推断"（保守 0.0）。
        if std_ic < 1e-12 * max(1.0, abs(mean_ic)):
            return 0.0
        return mean_ic / std_ic

    @staticmethod
    def compute_decile_spread(predictions: list[float], returns: list[float]) -> float:
        """计算分层回测的顶部分位数 vs 底部分位数收益差。"""
        if len(predictions) < 10:
            return 0.0
        paired = sorted(zip(predictions, returns, strict=False), key=lambda x: x[0])
        n = len(paired)
        decile_size = max(1, n // 10)
        top_decile = paired[-decile_size:]
        bottom_decile = paired[:decile_size]
        top_avg = sum(r for _, r in top_decile) / len(top_decile)
        bottom_avg = sum(r for _, r in bottom_decile) / len(bottom_decile)
        return top_avg - bottom_avg

    @staticmethod
    def compute_turnover(new_positions: list[float], old_positions: list[float]) -> float:
        """计算换手率。"""
        if len(new_positions) != len(old_positions) or len(new_positions) == 0:
            return 0.0
        changes = sum(abs(new_positions[i] - old_positions[i]) for i in range(len(new_positions)))
        gross = sum(abs(p) for p in old_positions)
        if gross == 0:
            return 0.0
        return changes / gross

    @staticmethod
    def compute_vif(correlation_matrix: dict[str, dict[str, float]], target_factor: str) -> float:
        """计算方差膨胀因子检测共线性。VIF > 5 表示高共线性。"""
        correlations = correlation_matrix.get(target_factor, {})
        if not correlations:
            return 1.0
        max_r2 = max(abs(v) ** 2 for v in correlations.values())
        if max_r2 >= 1.0:
            return float("inf")
        return 1.0 / (1.0 - max_r2)

    @staticmethod
    def compute_marginal_contribution(
        factor_performance: FactorPerformance,
        existing_factors: list[FactorPerformance],
        correlation_matrix: dict[str, dict[str, float]],
    ) -> MarginalContribution:
        """计算因子对现有组合的边际贡献。"""
        factor_id = factor_performance.factor_id
        existing_ids = frozenset(f.id for f in existing_factors)
        vif = FactorEvaluator.compute_vif(correlation_matrix, factor_id)

        collinear_with = [fid for fid, corr in correlation_matrix.get(factor_id, {}).items() if abs(corr) > 0.7]

        marginal_sharpe = factor_performance.icir * 0.1  # 近似边际 Sharpe
        if collinear_with:
            marginal_sharpe *= 0.5  # 共线性折扣
        marginal_ic = factor_performance.ic_mean * 0.5

        diversification = 0.0
        if not collinear_with:
            avg_corr = sum(abs(v) for v in correlation_matrix.get(factor_id, {}).values()) / max(
                len(correlation_matrix.get(factor_id, {})), 1
            )
            diversification = max(0.0, 0.3 - avg_corr)

        return MarginalContribution(
            factor_id=factor_id,
            existing_factor_ids=existing_ids,
            marginal_sharpe=marginal_sharpe,
            marginal_ic=marginal_ic,
            diversification_benefit=diversification,
            collinearity_vif=vif,
            collinearity_with=collinear_with,
        )


class FactorRegistry:
    """因子注册中心。管理因子全生命周期，退役证据永久保留。"""

    def __init__(self) -> None:
        self._factors: dict[str, FactorRecord] = {}
        self._retired: dict[str, FactorRecord] = {}  # 退役证据永久保留
        self._history: dict[str, list[str]] = {}  # name -> all factor_ids

    def register(self, definition: FactorDefinition) -> FactorRecord:
        if definition.factor_id in self._factors:
            raise ValueError(f"Factor {definition.factor_id} already registered")
        record = FactorRecord(definition=definition)
        self._factors[definition.factor_id] = record
        name = definition.name
        if name not in self._history:
            self._history[name] = []
        self._history[name].append(definition.factor_id)
        return record

    def get(self, factor_id: str) -> FactorRecord | None:
        return self._factors.get(factor_id) or self._retired.get(factor_id)

    def get_active(self) -> list[FactorRecord]:
        return [f for f in self._factors.values() if f.lifecycle == FactorLifecycle.ACTIVE]

    def get_challengers(self) -> list[FactorRecord]:
        return [f for f in self._factors.values() if f.lifecycle == FactorLifecycle.CHALLENGER]

    def evaluate(
        self,
        factor_id: str,
        performance: FactorPerformance,
        marginal_contributions: list[MarginalContribution],
    ) -> bool:
        """评估因子是否可以晋升。无边际贡献的因子不能仅凭单独高 Sharpe 激活。"""
        record = self._factors.get(factor_id)
        if record is None:
            return False
        record.performance.append(performance)
        record.marginal_contributions.extend(marginal_contributions)

        # 关键条件：ICIR > 0.3 且有显著边际贡献
        if performance.icir < 0.3:
            return False
        if performance.cost_adjusted_ic is not None and performance.cost_adjusted_ic < 0.01:
            return False
        return not (marginal_contributions and any(m.has_adverse_collinearity() for m in marginal_contributions))

    def promote_to_challenger(self, factor_id: str) -> bool:
        record = self._factors.get(factor_id)
        if record is None:
            return False
        if record.lifecycle not in (FactorLifecycle.PAPER_TRADING, FactorLifecycle.SUSPENDED):
            return False
        # 不能通过这个兼容方法绕过 CHALLENGER 阶段证据；生产路径使用
        # FactorPromotionGate.promote(...) 并持久化 PromotionDecision。
        return False

    def promote_to_active(self, factor_id: str) -> bool:
        record = self._factors.get(factor_id)
        if record is None:
            return False
        if record.lifecycle != FactorLifecycle.CHALLENGER:
            return False
        # ACTIVE 是生产授权，不是“最近 ICIR 足够高”的诊断标签。只有
        # FactorPromotionGate 生成并记录 approved PromotionDecision 后，
        # 这里才允许返回成功；兼容调用没有证据参数，必须 fail closed。
        if not any(
            decision.approved and decision.to_state == FactorLifecycle.ACTIVE for decision in record.promotion_history
        ):
            return False
        return record.lifecycle == FactorLifecycle.ACTIVE

    def degrade(self, factor_id: str, reason: str) -> bool:
        record = self._factors.get(factor_id)
        if record is None:
            return False
        if record.lifecycle != FactorLifecycle.ACTIVE:
            return False
        return record.transition(FactorLifecycle.DEGRADED)

    def suspend(self, factor_id: str, reason: str) -> bool:
        record = self._factors.get(factor_id)
        if record is None:
            return False
        return record.transition(FactorLifecycle.SUSPENDED)

    def retire(self, factor_id: str, reason: str) -> bool:
        """退役因子。证据永久保留在 _retired 中。历史 Replay 可用，生产引用禁止。"""
        record = self._factors.get(factor_id)
        if record is None:
            return False
        record.retirement_reason = reason
        record.retirement_evidence = {
            "reason": reason,
            "retired_at": datetime.now(timezone.utc).isoformat(),
            "final_lifecycle": record.lifecycle.value,
            "performance_history": [
                {"icir": p.icir, "ic_mean": p.ic_mean, "period": p.evaluation_period} for p in record.performance
            ],
        }
        record.retired_at = datetime.now(timezone.utc)
        record.lifecycle = FactorLifecycle.RETIRED
        # 移入退役永久保留
        self._retired[factor_id] = record
        del self._factors[factor_id]
        return True

    def restart_as_new_idea(self, retired_factor_id: str, new_definition: FactorDefinition) -> FactorRecord | None:
        """退役因子重新提交 = 全新 IDEA，需完整重新 Gate。"""
        retired = self._retired.get(retired_factor_id)
        if retired is None:
            return None
        # 必须在 SUSPENDED 状态才能重启
        # RETIRED 的永远不能直接激活
        record = FactorRecord(
            definition=new_definition,
            lifecycle=FactorLifecycle.IDEA,
            restarted_from=retired_factor_id,
        )
        self._factors[new_definition.factor_id] = record
        return record

    def get_retired(self) -> list[FactorRecord]:
        """获取所有退役因子（证据永久保留）。"""
        return list(self._retired.values())

    def cross_venue_stability(
        self,
        factor_id: str,
        venue_ics: dict[VenueId, float],
        min_stability_threshold: float = 0.3,
    ) -> tuple[bool, str]:
        """检查因子跨交易所的稳定性。所有 venue IC 必须同向且高于阈值。"""
        record = self.get(factor_id)
        if record is None:
            return False, "Factor not found"
        if not venue_ics:
            return False, "No venue data"
        signs = set()
        for venue, ic in venue_ics.items():
            if abs(ic) < min_stability_threshold:
                return False, f"Venue {venue} IC {ic:.3f} below threshold {min_stability_threshold}"
            signs.add(1 if ic > 0 else -1)
        if len(signs) > 1:
            return False, "IC direction inconsistent across venues"
        return True, "Cross-venue stable"


# --- BD-CV22: FactorEvidence 合约桥接 ---


def bridge_to_factor_evidence(
    factor_id: str,
    state: str,
    sharpe: float,
    evidence_dag_hash: str = "",
) -> FactorEvidence:
    """BD-CV22: 将现有因子状态桥接到 FactorEvidence contract。

    NaN score/空evidence 不能 PROMOTED。
    """
    _state_map = {
        "ACTIVE": FactorLifecycleState.ACTIVE,
        "DEGRADED": FactorLifecycleState.DEGRADED,
        "RETIRED": FactorLifecycleState.RETIRED,
        "CANDIDATE": FactorLifecycleState.CANDIDATE,
    }
    fs = _state_map.get(state, FactorLifecycleState.CANDIDATE)
    return FactorEvidence(
        factor_id=factor_id,
        state=fs,
        sharpe=sharpe,
        evidence_dag_hash=evidence_dag_hash,
        last_promotion_at=datetime.now(timezone.utc).isoformat(),
    )
