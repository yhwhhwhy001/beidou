"""因子研究、边际贡献、生命周期、退役与重启。

PKG-14: 因子定义、IC/RankIC/ICIR、分层回测、衰减分析、换手率、
成本后边际贡献、因子生命周期管理（IDEA→ACTIVE→DEGRADED→SUSPENDED→RETIRED）。
退役证据永久保留；重新启用等同新 Challenger，必须重新经过完整 Gate。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from beidou_shared.types import InstrumentId, SchemaVersion, StrategyId, VenueId


class FactorLifecycle(str, Enum):
    """因子生命周期。退役后重新启用等同新 Challenger。

    BF-07 新状态机 (Section 10):
    IDEA → GENERATED → SANITY_PASSED → RESEARCH_VALIDATED
    → OOS_VERIFIED → COST_CAPACITY_VERIFIED → PAPER_TRADING
    → CHALLENGER → ACTIVE → DEGRADED → SUSPENDED → RETIRED
    """
    # 新状态 (BF-07)
    IDEA = "IDEA"
    GENERATED = "GENERATED"                     # 候选已生成
    SANITY_PASSED = "SANITY_PASSED"             # DQ/泄漏/复杂度通过
    RESEARCH_VALIDATED = "RESEARCH_VALIDATED"   # 统计显著性+多重检验通过
    OOS_VERIFIED = "OOS_VERIFIED"              # Purged WFO/CPCV 通过
    COST_CAPACITY_VERIFIED = "COST_CAPACITY_VERIFIED"  # 成本/容量通过
    PAPER_TRADING = "PAPER_TRADING"
    CHALLENGER = "CHALLENGER"
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    SUSPENDED = "SUSPENDED"
    RETIRED = "RETIRED"

    # 兼容旧状态（标记为 LEGACY）
    RESEARCH = "RESEARCH"           # DEPRECATED: 使用 RESEARCH_VALIDATED
    BACKTEST = "BACKTEST"           # DEPRECATED: 使用 OOS_VERIFIED


# 合法生命周期转换 (BF-07 更新)
FACTOR_LIFECYCLE_TRANSITIONS: dict[FactorLifecycle, set[FactorLifecycle]] = {
    # 新路径 (含旧状态兼容)
    FactorLifecycle.IDEA: {FactorLifecycle.GENERATED, FactorLifecycle.RESEARCH, FactorLifecycle.RESEARCH_VALIDATED, FactorLifecycle.RETIRED},
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


class FactorEvaluator:
    """因子评估器 — 计算 IC、RankIC、ICIR、分层回测、衰减、边际贡献。"""

    @staticmethod
    def compute_ic(predictions: list[float], returns: list[float]) -> tuple[float, float]:
        """计算 Pearson IC（信息系数）及其标准误。"""
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
        """ICIR = mean(IC) / std(IC)。"""
        if len(ic_series) < 2:
            return 0.0
        mean_ic = sum(ic_series) / len(ic_series)
        var = sum((ic - mean_ic) ** 2 for ic in ic_series) / (len(ic_series) - 1)
        std_ic = var ** 0.5
        if std_ic == 0:
            return float("inf") if mean_ic > 0 else float("-inf") if mean_ic < 0 else 0.0
        return mean_ic / std_ic

    @staticmethod
    def compute_decile_spread(predictions: list[float], returns: list[float]) -> float:
        """计算分层回测的顶部分位数 vs 底部分位数收益差。"""
        if len(predictions) < 10:
            return 0.0
        paired = sorted(zip(predictions, returns), key=lambda x: x[0])
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

        collinear_with = [
            fid for fid, corr in correlation_matrix.get(factor_id, {}).items()
            if abs(corr) > 0.7
        ]

        marginal_sharpe = factor_performance.icir * 0.1  # 近似边际 Sharpe
        if collinear_with:
            marginal_sharpe *= 0.5  # 共线性折扣
        marginal_ic = factor_performance.ic_mean * 0.5

        diversification = 0.0
        if not collinear_with:
            avg_corr = sum(abs(v) for v in correlation_matrix.get(factor_id, {}).values()) / max(len(correlation_matrix.get(factor_id, {})), 1)
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
        if marginal_contributions and any(m.has_adverse_collinearity() for m in marginal_contributions):
            return False
        return True

    def promote_to_challenger(self, factor_id: str) -> bool:
        record = self._factors.get(factor_id)
        if record is None:
            return False
        if record.lifecycle not in (FactorLifecycle.PAPER_TRADING, FactorLifecycle.SUSPENDED):
            return False
        return record.transition(FactorLifecycle.CHALLENGER)

    def promote_to_active(self, factor_id: str) -> bool:
        record = self._factors.get(factor_id)
        if record is None:
            return False
        if record.lifecycle != FactorLifecycle.CHALLENGER:
            return False
        # 必须经过完整评估
        if not record.performance:
            return False
        last_perf = record.performance[-1]
        if last_perf.icir < 0.3:
            return False
        return record.transition(FactorLifecycle.ACTIVE)

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
                {"icir": p.icir, "ic_mean": p.ic_mean, "period": p.evaluation_period}
                for p in record.performance
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
