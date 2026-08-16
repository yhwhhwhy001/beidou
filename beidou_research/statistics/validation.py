"""BD-CV20 统计验证模块 — **已退役为 fail-closed 桩**（M05-F02）。

历史实现为占位（PurgedWFCV 全零分、scores_fn 从不调用、CPCV 全零分、
PBO 简化近似）—— 任何下游若接线会静默放行假证据。真实实现位于:
- beidou_research.mining.evaluation.purged_walk_forward（Purged WF）
- beidou_research.mining.evaluation.cpcv（CPCV）
- beidou_research.mining.evaluation.multiple_testing（PBO/DSR/Holm/BH）

本模块全部入口 raise NotImplementedError 并指明迁移目标,防止误接。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, NoReturn


def _retired() -> NoReturn:
    raise NotImplementedError(
        "statistics/validation.py is retired (M05-F02): use "
        "beidou_research.mining.evaluation.{purged_walk_forward,cpcv,multiple_testing} "
        "for the real statistical validation kernels"
    )

# ============================================================================
# Purged Walk-Forward Cross Validation
# ============================================================================


@dataclass(frozen=True)
class PurgedWFCVResult:
    """Purged WF CV 结果。按真实标签区间重叠 purge，并实现 embargo。"""

    n_splits: int
    train_sizes: list[int]
    test_sizes: list[int]
    embargo_sizes: list[int]
    oos_scores: list[float]
    mean_score: float
    std_score: float
    is_significant: bool

    @classmethod
    def run(
        cls,
        n_samples: int,
        n_splits: int = 5,
        test_size: float = 0.2,
        embargo_pct: float = 0.01,
        purge_pct: float = 0.01,
        scores_fn: Any = None,
        seed: int = 42,
    ) -> PurgedWFCVResult:
        """执行 Purged WF CV。

        Purge: 移除训练集中与测试集标签区间重叠的样本（防泄漏）。
        Embargo: 在训练集末尾和测试集开头之间留出 gap。
        """
        if n_samples < 10 or n_splits < 1:
            return cls(
                n_splits=0,
                train_sizes=[],
                test_sizes=[],
                embargo_sizes=[],
                oos_scores=[],
                mean_score=float("nan"),
                std_score=float("nan"),
                is_significant=False,
            )

        if n_samples < 10 or n_splits < 1:
            return cls(
                n_splits=0,
                train_sizes=[],
                test_sizes=[],
                embargo_sizes=[],
                oos_scores=[],
                mean_score=float("nan"),
                std_score=float("nan"),
                is_significant=False,
            )
        _retired()


# ============================================================================
# Combinatorially Purged Cross Validation (CPCV)
# ============================================================================


@dataclass(frozen=True)
class CPCVResult:
    """CPCV 结果。生成 N 个 train/test 组合并 purge。"""

    n_groups: int
    n_combinations: int
    scores: list[float]
    mean_score: float
    std_score: float

    @classmethod
    def run(cls, n_samples: int, n_groups: int = 6, test_groups: int = 2, seed: int = 42) -> CPCVResult:
        """运行 CPCV。

        n_combos = C(n_groups, test_groups)
        """
        if n_groups < 2 or test_groups >= n_groups or n_samples < n_groups:
            return cls(n_groups=0, n_combinations=0, scores=[], mean_score=float("nan"), std_score=float("nan"))
        _retired()


# ============================================================================
# Probability of Backtest Overfitting (PBO)
# ============================================================================


@dataclass(frozen=True)
class PBOResult:
    """PBO 结果。生成组合分区，计算 overfitting 概率。"""

    n_combos: int
    pbo: float  # probability of overfitting
    performance_degradation: float  # OOS vs IS degradation
    is_overfit: bool
    logits: list[float] = field(default_factory=list)

    @classmethod
    def compute(cls, is_scores: list[float], oos_scores: list[float], n_combos: int = 0, seed: int = 42) -> PBOResult:
        """BD-CV20: 计算 PBO。

        n_combos 改变时组合分区集合真实变化。
        """
        n = min(len(is_scores), len(oos_scores))
        if n < 2:
            return cls(n_combos=0, pbo=1.0, performance_degradation=0.0, is_overfit=True)
        _retired()


# ============================================================================
# Deflated Sharpe Ratio (DSR)
# ============================================================================


@dataclass(frozen=True)
class DSRResult:
    """DSR 结果。考虑多次试验的 deflated Sharpe ratio。"""

    observed_sharpe: float
    expected_max_sharpe: float  # E[max(SR)] under null
    deflated_sharpe: float
    p_value: float
    is_significant: bool

    @classmethod
    def compute(
        cls,
        observed_sharpe: float,
        n_trials: int,
        sample_size: int,
        skewness: float | None = None,
        kurtosis: float | None = None,
        seed: int = 42,
    ) -> DSRResult:
        """BD-CV20: 计算 DSR。

        明确采用或拒绝 skewness/kurtosis 输入。
        公式: DSR = (SR_obs - E[max(SR)]) / std[max(SR)]

        E[max(SR)] ≈ sqrt(2 * log(n_trials)) * (1 - gamma * skewness / 6 + ...)
        简化: 使用 Bailey & Lopez de Prado (2014) 近似。
        """
        if sample_size < 2 or n_trials <= 1:
            # 单次试验无需 deflation —— 保守返回不显著（退役桩语义）
            return cls(
                observed_sharpe=observed_sharpe,
                expected_max_sharpe=0.0,
                deflated_sharpe=0.0,
                p_value=1.0,
                is_significant=False,
            )
        _retired()


# ============================================================================
# Holm step-down adjusted p-values
# ============================================================================


@dataclass(frozen=True)
class HolmResult:
    """Holm-Bonferroni 校正结果。"""

    raw_p_values: list[float]
    adjusted_p_values: list[float]
    significant_indices: list[int]
    family_id: str

    @classmethod
    def compute(cls, p_values: list[float], alpha: float = 0.05, family_id: str = "default") -> HolmResult:
        """BD-CV20: Holm step-down adjusted p-value。

        排序、累积单调约束、原索引恢复。
        adjusted p-values 单调且不小于对应原始排序约束。
        """
        n = len(p_values)
        if n == 0:
            return cls(raw_p_values=[], adjusted_p_values=[], significant_indices=[], family_id=family_id)

        # NaN policy: NaN → 1.0
        clean = [1.0 if math.isnan(p) or not math.isfinite(p) else max(0.0, min(1.0, p)) for p in p_values]

        # Sort by p-value ascending, keep original indices
        indexed = sorted(enumerate(clean), key=lambda x: x[1])
        sorted_indices = [idx for idx, _ in indexed]
        # Holm step-down: adj_p[i] = min(1, max(prev_adj, p[i] * (n - i)))
        adjusted = [0.0] * n
        for rank, (orig_idx, p) in enumerate(indexed):
            multiplier = n - rank
            adj = min(1.0, p * multiplier)
            if rank > 0:
                adj = max(adj, adjusted[sorted_indices[rank - 1]])
            adjusted[orig_idx] = adj

        significant = [i for i, adj in enumerate(adjusted) if adj < alpha]

        return cls(raw_p_values=clean, adjusted_p_values=adjusted, significant_indices=significant, family_id=family_id)


# ============================================================================
# Normal CDF helper
# ============================================================================


def _normal_cdf(x: float) -> float:
    """Standard normal CDF approximation (Abramowitz & Stegun 7.1.26)."""
    if x < -8.0:
        return 0.0
    if x > 8.0:
        return 1.0
    # Marsaglia polar method approximation
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
