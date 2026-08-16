"""BF-05: 多重检验修正。

必须记录所有被尝试候选，并执行：
- Benjamini-Hochberg FDR
- Holm 校正（少量最终假设）
- Deflated Sharpe Ratio (DSR)
- Probability of Backtest Overfitting (PBO)
- Block bootstrap 置信区间

单独高 Sharpe、单折高 IC 或单一交易对盈利均不得晋级。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# ================================================================
# Benjamini-Hochberg FDR
# ================================================================


@dataclass
class BHResult:
    """Benjamini-Hochberg FDR 结果。"""

    original_pvalues: list[float]
    adjusted_pvalues: list[float]
    significant_at_05: list[bool]  # α = 0.05
    significant_at_01: list[bool]  # α = 0.01
    n_tests: int
    n_significant_05: int
    n_significant_01: int


def benjamini_hochberg(
    pvalues: list[float],
    alpha: float = 0.05,
) -> BHResult:
    """Benjamini-Hochberg 错误发现率控制。

    输入所有测试的 p-value，返回经过多重检验校正后的显著性判断。

    Args:
        pvalues: 所有尝试候选的 p-value 列表
        alpha: FDR 水平（默认 0.05）

    Returns:
        BHResult 包含校正后的 p-value 和显著性判断
    """
    n = len(pvalues)
    if n == 0:
        return BHResult(
            original_pvalues=[],
            adjusted_pvalues=[],
            significant_at_05=[],
            significant_at_01=[],
            n_tests=0,
            n_significant_05=0,
            n_significant_01=0,
        )

    # 按 p-value 排序并记录原始索引
    indexed = sorted(enumerate(pvalues), key=lambda x: x[1])
    sorted_indices = [i for i, _ in indexed]
    [p for _, p in indexed]

    # BH 校正：adjusted_pvalue = min(1, p * n / rank)
    adjusted = [0.0] * n
    for rank, (orig_idx, pval) in enumerate(indexed):
        bh_val = min(1.0, pval * n / (rank + 1))
        adjusted[orig_idx] = bh_val

    # 确保单调性（从后往前）
    for i in range(n - 2, -1, -1):
        orig_idx = sorted_indices[i]
        next_idx = sorted_indices[i + 1]
        adjusted[orig_idx] = min(adjusted[orig_idx], adjusted[next_idx])

    significant_05 = [adjusted[i] < 0.05 for i in range(n)]
    significant_01 = [adjusted[i] < 0.01 for i in range(n)]

    return BHResult(
        original_pvalues=pvalues,
        adjusted_pvalues=adjusted,
        significant_at_05=significant_05,
        significant_at_01=significant_01,
        n_tests=n,
        n_significant_05=sum(significant_05),
        n_significant_01=sum(significant_01),
    )


# ================================================================
# Holm 校正
# ================================================================


@dataclass
class HolmResult:
    """Holm-Bonferroni 校正结果。"""

    original_pvalues: list[float]
    adjusted_pvalues: list[float]
    rejected: list[bool]
    n_tests: int
    n_rejected: int


def holm_correction(pvalues: list[float], alpha: float = 0.05) -> HolmResult:
    """Holm-Bonferroni 逐步校正。

    比 Bonferroni 更 powerful，用于少量最终假设的严格控制。
    """
    n = len(pvalues)
    if n == 0:
        return HolmResult([], [], [], 0, 0)

    indexed = sorted(enumerate(pvalues), key=lambda x: x[1])
    adjusted = [0.0] * n
    rejected = [False] * n

    for k, (orig_idx, pval) in enumerate(indexed):
        adjusted[orig_idx] = min(1.0, pval * (n - k))

    for orig_idx, _ in indexed:
        rejected[orig_idx] = adjusted[orig_idx] < alpha

    return HolmResult(
        original_pvalues=pvalues,
        adjusted_pvalues=adjusted,
        rejected=rejected,
        n_tests=n,
        n_rejected=sum(rejected),
    )


# ================================================================
# Deflated Sharpe Ratio (DSR)
# ================================================================


def deflated_sharpe_ratio(
    observed_sharpe: float,
    n_trials: int,
    sharpe_std: float = 0.15,
    sample_length: int = 252,
    skewness: float = 0.0,
    kurtosis: float = 3.0,
) -> dict[str, float]:
    """Deflated Sharpe Ratio (Harvey & Liu 2015)。

    考虑多重试验后的真实 Sharpe 显著性。

    Args:
        observed_sharpe: 观测到的 Sharpe ratio
        n_trials: 尝试的独立策略数量（多重检验分母）
        sharpe_std: Sharpe ratio 标准差估计
        sample_length: 样本长度
        skewness: 收益偏度
        kurtosis: 收益峰度

    Returns:
        {dsr, p_value, expected_max_sharpe, haircut}
    """
    if n_trials <= 0 or sample_length <= 0:
        return {"dsr": 0.0, "p_value": 1.0, "expected_max_sharpe": 0.0, "haircut": 0.0}

    # 期望最大 Sharpe（在 n_trials 次独立试验后）
    # E[max(SR)] ≈ SR_std * sqrt(2 * log(n_trials))
    import math as _math

    euler_gamma = 0.5772156649
    expected_max = sharpe_std * (
        (1 - euler_gamma) * _math.sqrt(2 * _math.log(n_trials)) + euler_gamma * _math.sqrt(2 * _math.log(n_trials))
    )

    # 简化：使用极值理论近似
    if n_trials == 1:
        expected_max = 0.0
    else:
        # 公式：E[max] ≈ std * [sqrt(2*log(N)) + gamma/sqrt(2*log(N))]
        term = _math.sqrt(2 * _math.log(n_trials))
        expected_max = sharpe_std * term

    # Deflated Sharpe = observed - expected_max(null)
    dsr = observed_sharpe - expected_max

    # p-value 近似（正态）
    z_score = dsr / (sharpe_std / _math.sqrt(sample_length)) if sharpe_std > 0 else 0.0

    # Standard normal CDF approximation
    p_value = 1.0 - _normal_cdf(z_score)
    p_value = max(0.0, min(1.0, p_value))

    haircut = (observed_sharpe - dsr) / observed_sharpe if observed_sharpe != 0 else 0.0

    return {
        "dsr": round(dsr, 6),
        "p_value": round(p_value, 6),
        "expected_max_sharpe": round(expected_max, 6),
        "haircut": round(haircut, 4),
        "n_trials": n_trials,
        "sample_length": sample_length,
    }


# ================================================================
# Probability of Backtest Overfitting (PBO)
# ================================================================


@dataclass
class PBOResult:
    """Probability of Backtest Overfitting 结果。"""

    pbo: float
    performance_degradation: float
    rank_correlation: float
    n_combinations: int
    interpretation: str


def compute_pbo(
    in_sample_performances: list[float],
    out_of_sample_performances: list[float],
    n_splits: int = 16,
    seed: int = 42,
) -> PBOResult:
    """Bailey et al. (2017) PBO 计算。

    通过组合子集比较 IS 和 OOS 性能排序的差异。

    Args:
        in_sample_performances: 各参数组合的 IS 性能
        out_of_sample_performances: 各参数组合的 OOS 性能
        n_splits: 子集划分数量

    Returns:
        PBOResult
    """
    n = len(in_sample_performances)
    if n != len(out_of_sample_performances) or n < 4:
        return PBOResult(
            pbo=1.0,
            performance_degradation=0.0,
            rank_correlation=0.0,
            n_combinations=0,
            interpretation="insufficient_data",
        )

    # IS 和 OOS 性能的排名
    is_ranked = _rank(in_sample_performances)
    oos_ranked = _rank(out_of_sample_performances)

    # 组合子集比较（Bailey et al. 2017 PBO 语义）
    # M05-F03 (P1): 每轮随机半数子集（固定种子,确定性）内比较 —— 旧实现
    # 循环体零随机性,n_combos 次循环是同一次全量比较的重复,PBO 退化为
    # 0/1 单次判定。正确语义: 子集内按 IS 选最优,看其 OOS 排名是否
    # 低于该子集的 OOS 中位数（IS 选优在 OOS 上失效 = 过拟合计数）。
    import random

    n_combos = min(n_splits, n // 2)
    pbo_count = 0
    total_comparisons = 0
    rng = random.Random(seed)
    half = n // 2

    for _ in range(n_combos):
        subset = rng.sample(range(n), half)
        is_best_in_subset = max(subset, key=lambda i: in_sample_performances[i])
        subset_oos_ranks = sorted(oos_ranked[i] for i in subset)
        median_rank_in_subset = subset_oos_ranks[len(subset_oos_ranks) // 2]
        if oos_ranked[is_best_in_subset] < median_rank_in_subset:
            pbo_count += 1
        total_comparisons += 1

    pbo = pbo_count / max(total_comparisons, 1)

    # 性能退化
    best_is_idx = max(range(n), key=lambda i: in_sample_performances[i])
    best_is_oos_perf = out_of_sample_performances[best_is_idx]
    best_oos_perf = max(out_of_sample_performances)
    degradation = (best_oos_perf - best_is_oos_perf) / max(abs(best_oos_perf), 1e-10)

    # 排名相关性 (Spearman)
    rank_corr = _spearman_rank_corr(is_ranked, oos_ranked)

    # 解释
    if pbo < 0.1:
        interpretation = "low_overfitting_risk"
    elif pbo < 0.3:
        interpretation = "moderate_overfitting_risk"
    elif pbo < 0.5:
        interpretation = "high_overfitting_risk"
    else:
        interpretation = "severe_overfitting_risk"

    return PBOResult(
        pbo=round(pbo, 4),
        performance_degradation=round(degradation, 4),
        rank_correlation=round(rank_corr, 4),
        n_combinations=total_comparisons,
        interpretation=interpretation,
    )


# ================================================================
# 辅助函数
# ================================================================


def _normal_cdf(x: float) -> float:
    """标准正态分布累积分布函数（Abramowitz and Stegun 近似）。"""
    if x < -8:
        return 0.0
    if x > 8:
        return 1.0
    # 使用 math.erf
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _rank(values: list[float]) -> list[float]:
    """计算排名（从 1 开始，平均排名处理结）。"""
    n = len(values)
    indexed = sorted(enumerate(values), key=lambda x: x[1])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j < n and indexed[j][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + j + 1) / 2.0  # 平均排名
        for k in range(i, j):
            ranks[indexed[k][0]] = avg_rank
        i = j
    return ranks


def _spearman_rank_corr(x: list[float], y: list[float]) -> float:
    """Spearman 等级相关系数。"""
    n = len(x)
    if n < 2:
        return 0.0
    rank_x = _rank(x)
    rank_y = _rank(y)
    mean_rx = sum(rank_x) / n
    mean_ry = sum(rank_y) / n
    cov = sum((rank_x[i] - mean_rx) * (rank_y[i] - mean_ry) for i in range(n)) / (n - 1)
    std_rx = (sum((rx - mean_rx) ** 2 for rx in rank_x) / (n - 1)) ** 0.5
    std_ry = (sum((ry - mean_ry) ** 2 for ry in rank_y) / (n - 1)) ** 0.5
    if std_rx == 0 or std_ry == 0:
        return 0.0
    return cov / (std_rx * std_ry)


# ================================================================
# 综合多重检验报告
# ================================================================


@dataclass
class MultipleTestingReport:
    """多重检验综合报告。"""

    n_total_trials: int  # 所有被尝试的候选数量
    n_evaluated: int  # 被评估的候选数量
    bh_result: BHResult | None = None
    holm_result: HolmResult | None = None
    dsr: dict[str, float] = field(default_factory=dict)
    pbo: PBOResult | None = None
    verdict: str = ""  # "PASS" / "FAIL" / "NOT_VERIFIABLE"
    failure_reasons: list[str] = field(default_factory=list)
    candidate_index: int | None = None


def evaluate_multiple_testing(
    pvalues: list[float],
    observed_sharpe: float,
    n_trials: int,
    in_sample_sharpes: list[float] | None = None,
    out_of_sample_sharpes: list[float] | None = None,
    sample_length: int = 252,
    candidate_index: int | None = None,
    min_pbo_comparisons: int = 8,
) -> MultipleTestingReport:
    """执行所有多重检验并生成报告。

    所有尝试过的候选必须计入分母（n_total_trials）。
    """
    report = MultipleTestingReport(
        n_total_trials=n_trials,
        n_evaluated=len(pvalues),
        candidate_index=candidate_index,
    )

    # BH-FDR
    report.bh_result = benjamini_hochberg(pvalues)

    # Holm
    report.holm_result = holm_correction(pvalues)

    # DSR
    report.dsr = deflated_sharpe_ratio(
        observed_sharpe=observed_sharpe,
        n_trials=n_trials,
        sample_length=sample_length,
    )

    # PBO
    if in_sample_sharpes and out_of_sample_sharpes and len(in_sample_sharpes) >= 4:
        report.pbo = compute_pbo(in_sample_sharpes, out_of_sample_sharpes)

    # Verdict.  Missing denominator coverage or missing PBO evidence is an
    # evidence gap, never a pass.  Statistical failures remain FAIL.
    failures: list[str] = []
    evidence_gaps: list[str] = []
    if n_trials <= 0:
        evidence_gaps.append("zero_trials_recorded")
    if len(pvalues) != n_trials:
        evidence_gaps.append("trials_not_fully_evaluated")
    if candidate_index is not None and not 0 <= candidate_index < len(pvalues):
        evidence_gaps.append("candidate_index_out_of_range")

    if report.dsr.get("p_value", 1.0) > 0.05:
        failures.append("DSR not significant")
    if report.pbo is None:
        evidence_gaps.append("pbo_missing")
    elif report.pbo.n_combinations < min_pbo_comparisons:
        evidence_gaps.append(f"pbo_comparisons_insufficient:{report.pbo.n_combinations}<{min_pbo_comparisons}")
    elif report.pbo.pbo > 0.20:
        failures.append(f"PBO={report.pbo.pbo:.2f} > 0.20")

    if report.bh_result is None or not report.bh_result.significant_at_05:
        failures.append("BH-FDR not significant")
    elif candidate_index is not None and not report.bh_result.significant_at_05[candidate_index]:
        failures.append("candidate_not_BH_significant")

    report.failure_reasons = evidence_gaps + failures
    if evidence_gaps:
        report.verdict = "NOT_VERIFIABLE"
    elif not failures:
        report.verdict = "PASS"
    else:
        report.verdict = "FAIL"

    return report
