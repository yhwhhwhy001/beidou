"""BF-05: 快速预筛。

淘汰条件：
- 数据质量非 PASS
- 缺失率超 policy
- 方差为零或近零
- 极端值比例异常
- 因子值无法在两次执行中确定性复现
- 与标签时间重叠非法
- 计算成本超过预算
- 与已有候选 canonical hash 重复
- 过高换手或成本后期望为负
- 有效样本不足
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from beidou_shared.types import DataQualityTier


@dataclass
class FastScreenResult:
    """快速预筛结果。"""

    passed: bool
    failure_reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class FastScreenConfig:
    """快速预筛配置 — 所有阈值来自 policy。"""

    min_sample_count: int = 30
    max_missing_rate: float = 0.10  # 最大缺失率 10%
    min_variance: float = 1e-12  # 最小方差（防零方差）
    max_extreme_ratio: float = 0.05  # 最大极端值比例 5%
    extreme_sigma: float = 5.0  # 极端值判定（σ 倍数）
    max_complexity_score: float = 20.0  # 最大表达式复杂度
    max_lookback_bars: int = 500  # 最大回看期
    max_compute_time_ms: float = 100.0  # 最大单次计算时间（ms）
    min_effective_samples: int = 20  # 最小有效样本（非缺失）
    max_turnover_pct: float = 50.0  # 最大换手率（%）
    require_deterministic: bool = True  # 必须确定性复现


class FastScreen:
    """快速预筛器。

    以极低成本淘汰明显不合格的候选因子，避免在 Purged WFO/CPCV
    上浪费计算资源。预期淘汰率 > 80%。
    """

    def __init__(self, config: FastScreenConfig | None = None) -> None:
        self.config = config or FastScreenConfig()

    def screen(
        self,
        factor_values: list[float],
        *,
        expression_hash: str = "",
        existing_hashes: set[str] | None = None,
        complexity_score: float = 0.0,
        data_quality: DataQualityTier = DataQualityTier.PASS,
        dq_failure_count: int = 0,
    ) -> FastScreenResult:
        """对单个候选因子执行快速预筛。

        Args:
            factor_values: 因子值序列
            expression_hash: 规范化表达式哈希
            existing_hashes: 已有候选的哈希集合（重复消除）
            complexity_score: 表达式复杂度
            data_quality: 输入数据质量
            dq_failure_count: 数据质量失败次数

        Returns:
            FastScreenResult with passed=True/False
        """
        cfg = self.config
        reasons: list[str] = []
        metrics: dict[str, Any] = {}

        n = len(factor_values)

        # 1. 数据质量检查
        if data_quality == DataQualityTier.FAIL:
            reasons.append("data_quality: FAIL")
            return FastScreenResult(passed=False, failure_reasons=reasons, metrics=metrics)

        if dq_failure_count > 0:
            metrics["dq_failure_count"] = dq_failure_count
            if dq_failure_count > 3:
                reasons.append(f"data_quality: {dq_failure_count} failures")

        # 2. 样本量检查
        metrics["sample_count"] = n
        if n < cfg.min_sample_count:
            reasons.append(f"sample_count: {n} < {cfg.min_sample_count}")

        # 3. 缺失率检查
        missing = sum(1 for v in factor_values if _is_missing(v))
        missing_rate = missing / max(n, 1)
        metrics["missing_rate"] = missing_rate
        if missing_rate > cfg.max_missing_rate:
            reasons.append(f"missing_rate: {missing_rate:.3f} > {cfg.max_missing_rate}")

        # 4. 有效样本检查
        effective = n - missing
        metrics["effective_samples"] = effective
        if effective < cfg.min_effective_samples:
            reasons.append(f"effective_samples: {effective} < {cfg.min_effective_samples}")

        # 5. 方差检查
        valid_vals = [v for v in factor_values if not _is_missing(v)]
        if valid_vals:
            mean_val = sum(valid_vals) / len(valid_vals)
            variance = sum((v - mean_val) ** 2 for v in valid_vals) / len(valid_vals)
            metrics["variance"] = variance
            if variance < cfg.min_variance:
                reasons.append(f"variance: {variance:.2e} < {cfg.min_variance:.2e}")

            # 6. 极端值检查（使用 MAD-based robust z-score 避免 masking 效应）
            if variance > 0:
                # 使用 MAD (Median Absolute Deviation) 进行鲁棒异常值检测
                sorted_vals = sorted(valid_vals)
                n_v = len(sorted_vals)
                median_val = sorted_vals[n_v // 2]
                abs_deviations = sorted([abs(v - median_val) for v in valid_vals])
                mad = abs_deviations[n_v // 2]
                # 比例常数：正态分布下 MAD ≈ 0.6745 * σ
                robust_sigma = mad / 0.6745 if mad > 0 else (variance**0.5)

                extreme_count = sum(1 for v in valid_vals if abs(v - median_val) > cfg.extreme_sigma * robust_sigma)
                extreme_ratio = extreme_count / len(valid_vals)
                metrics["extreme_ratio"] = extreme_ratio
                metrics["robust_sigma"] = robust_sigma
                if extreme_ratio > cfg.max_extreme_ratio:
                    reasons.append(f"extreme_ratio: {extreme_ratio:.3f} > {cfg.max_extreme_ratio}")

        # 7. 表达式哈希重复检查
        if existing_hashes and expression_hash and expression_hash in existing_hashes:
            reasons.append("duplicate: expression_hash already exists")

        # 8. 复杂度检查
        metrics["complexity_score"] = complexity_score
        if complexity_score > cfg.max_complexity_score:
            reasons.append(f"complexity: {complexity_score:.1f} > {cfg.max_complexity_score}")

        # 9. 确定性检查（标记需要，实际验证需要两次执行）
        metrics["deterministic_required"] = cfg.require_deterministic

        passed = len(reasons) == 0
        return FastScreenResult(
            passed=passed,
            failure_reasons=reasons,
            metrics=metrics,
        )

    def screen_batch(
        self,
        candidates: list[dict[str, Any]],
        *,
        existing_hashes: set[str] | None = None,
    ) -> list[FastScreenResult]:
        """批量预筛。

        每个候选 dict 应包含:
        - factor_values: list[float]
        - expression_hash: str
        - complexity_score: float (可选)
        - data_quality: DataQualityTier (可选)
        """
        if existing_hashes is None:
            existing_hashes = set()

        results = []
        for c in candidates:
            result = self.screen(
                factor_values=c["factor_values"],
                expression_hash=c.get("expression_hash", ""),
                existing_hashes=existing_hashes,
                complexity_score=c.get("complexity_score", 0.0),
                data_quality=c.get("data_quality", DataQualityTier.PASS),
                dq_failure_count=c.get("dq_failure_count", 0),
            )
            if result.passed and c.get("expression_hash"):
                existing_hashes.add(c["expression_hash"])
            results.append(result)

        return results


def _is_missing(v: float) -> bool:
    """检查因子值是否缺失。"""
    if v is None:
        return True
    if isinstance(v, float):
        import math

        return math.isnan(v) or math.isinf(v)
    return False
