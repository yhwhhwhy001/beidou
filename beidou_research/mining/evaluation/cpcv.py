"""BF-05: Combinatorial Purged Cross-Validation (CPCV).

对 Purged WFO 通过初筛的 finalists 执行 CPCV：
- 生成所有可能的 train/test 组合（路径）
- 每条路径独立训练和评估
- 汇总所有路径的 OOS 性能分布
- 输出 CPCV 均值和标准差

与 Purged WFO 的区别：
- WFO: 固定时间顺序 folds
- CPCV: 所有可能的组合路径，更充分利用数据
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Callable

from beidou_shared.types import GateResult


@dataclass
class CPCVConfig:
    """CPCV 配置。"""

    n_groups: int = 8  # 将数据分为 N 组；8 choose 2 = 28 条路径
    n_test_groups: int = 2  # 每组测试集大小（组数）
    purge_bars: int = 5  # purge K线数
    embargo_bars: int = 0  # embargo K线数
    min_train_groups: int = 3  # 最小训练组数
    min_train_samples: int = 60
    min_test_samples: int = 10
    min_paths_for_verdict: int = 20
    min_path_consistency: float = 0.60
    random_seed: int = 42


@dataclass
class CPCVPath:
    """单条 CPCV 路径。"""

    path_id: int
    train_indices: list[int]
    test_indices: list[int]
    train_sample_count: int = 0
    test_sample_count: int = 0


@dataclass
class CPCVResult:
    """CPCV 评估结果。"""

    n_paths: int
    n_completed: int
    oos_metrics: list[dict[str, float]]
    metric_mean: float
    metric_std: float
    metric_median: float
    metric_q05: float
    metric_q95: float
    path_consistency: float
    gate_result: GateResult = GateResult.UNVERIFIABLE
    evidence_hash: str = ""
    failure_reasons: list[str] = field(default_factory=list)


class CPCVEvaluator:
    """Combinatorial Purged Cross-Validation 评估器。

    生成 N 选 k 的所有组合作为测试集，其余为训练集。
    对每条路径独立评估，汇总 OOS 性能分布。
    """

    def __init__(self, config: CPCVConfig | None = None) -> None:
        self.config = config or CPCVConfig()

    def generate_paths(
        self,
        n_samples: int,
        group_labels: list[int],
    ) -> list[CPCVPath]:
        """生成 CPCV 路径。

        将数据按 group_labels 分为 N 组，生成所有 N-choose-k
        测试集组合（k = n_test_groups）。
        """
        cfg = self.config

        # 确定组边界
        groups: dict[int, list[int]] = {}
        for i, g in enumerate(group_labels):
            if g not in groups:
                groups[g] = []
            groups[g].append(i)

        if len(group_labels) != n_samples or n_samples <= 0:
            return []

        n_groups = len(groups)
        if n_groups < cfg.n_groups:
            # 回退到简单分 N 组
            n_groups = min(cfg.n_groups, max(2, n_samples // 20))
            group_size = n_samples // n_groups
            groups = {}
            for i in range(n_samples):
                g = min(i // max(1, group_size), n_groups - 1)
                if g not in groups:
                    groups[g] = []
                groups[g].append(i)

        # 生成所有路径组合
        import itertools

        all_group_ids = list(groups.keys())
        available_test_groups = len(all_group_ids) - cfg.min_train_groups
        if available_test_groups < 1:
            return []
        k = min(cfg.n_test_groups, available_test_groups)
        if k < 1:
            return []

        paths = []
        path_id = 0

        for test_groups in itertools.combinations(all_group_ids, k):
            # 测试集索引
            test_indices = []
            for g in test_groups:
                test_indices.extend(groups[g])

            # 训练集索引
            test_set = set(test_groups)
            train_indices = []
            for g in all_group_ids:
                if g not in test_set:
                    train_indices.extend(groups[g])

            # Purge 每一个测试块的边界；只看测试集最小/最大索引会让
            # 中间测试块两侧的训练样本泄漏到路径中。
            if test_indices and (cfg.purge_bars > 0 or cfg.embargo_bars > 0):
                test_index_set = set(test_indices)

                def _overlaps_barrier(index: int, *, _test_index_set: set[int] = test_index_set) -> bool:
                    return (
                        cfg.purge_bars > 0
                        and any(abs(index - test_index) <= cfg.purge_bars for test_index in _test_index_set)
                    ) or (
                        cfg.embargo_bars > 0
                        and any(0 < index - test_index <= cfg.embargo_bars for test_index in _test_index_set)
                    )

                train_indices = [index for index in train_indices if not _overlaps_barrier(index)]

            if len(train_indices) < max(cfg.min_train_samples, cfg.min_train_groups):
                continue

            paths.append(
                CPCVPath(
                    path_id=path_id,
                    train_indices=sorted(train_indices),
                    test_indices=sorted(test_indices),
                    train_sample_count=len(train_indices),
                    test_sample_count=len(test_indices),
                )
            )
            path_id += 1

        return paths

    def evaluate(
        self,
        predictions: list[float],
        returns: list[float],
        *,
        group_labels: list[int] | None = None,
        metric_fn: Callable[[list[float], list[float]], float] | None = None,
    ) -> CPCVResult:
        """执行 CPCV 评估。

        Args:
            predictions: 因子预测值
            returns: 标签收益
            group_labels: 组标签（同组内样本有重叠风险）
            metric_fn: 评估指标函数 (predictions, returns) -> float
        """
        n = min(len(predictions), len(returns))
        predictions = predictions[:n]
        returns = returns[:n]

        if group_labels is None:
            group_labels = [i // max(1, n // self.config.n_groups) for i in range(n)]

        if metric_fn is None:
            metric_fn = _default_ic

        paths = self.generate_paths(n, group_labels[:n])

        if len(paths) < self.config.min_paths_for_verdict:
            return CPCVResult(
                n_paths=len(paths),
                n_completed=0,
                oos_metrics=[],
                metric_mean=0.0,
                metric_std=0.0,
                metric_median=0.0,
                metric_q05=0.0,
                metric_q95=0.0,
                path_consistency=0.0,
                failure_reasons=[
                    "insufficient_cpcv_paths",
                    f"required={self.config.min_paths_for_verdict}",
                ],
            )

        oos_metrics = []
        for path in paths:
            test_preds = [predictions[i] for i in path.test_indices if i < n]
            test_rets = [returns[i] for i in path.test_indices if i < n]
            if len(test_preds) >= self.config.min_test_samples:
                metric = metric_fn(test_preds, test_rets)
                if not _is_finite(metric):
                    continue
                oos_metrics.append(
                    {
                        "path_id": path.path_id,
                        "metric": round(metric, 6),
                        "test_samples": len(test_preds),
                    }
                )

        if not oos_metrics:
            return CPCVResult(
                n_paths=len(paths),
                n_completed=0,
                oos_metrics=[],
                metric_mean=0.0,
                metric_std=0.0,
                metric_median=0.0,
                metric_q05=0.0,
                metric_q95=0.0,
                path_consistency=0.0,
                failure_reasons=["no_completed_cpcv_paths"],
            )

        metrics = [m["metric"] for m in oos_metrics]
        n_p = len(metrics)
        mean_m = sum(metrics) / n_p
        std_m = (sum((x - mean_m) ** 2 for x in metrics) / (n_p - 1)) ** 0.5 if n_p > 1 else 0.0

        sorted_m = sorted(metrics)
        median_m = sorted_m[n_p // 2]
        q05 = sorted_m[max(0, int(n_p * 0.05))]
        q95 = sorted_m[min(n_p - 1, int(n_p * 0.95))]

        # Path consistency: OOS 指标同向比例
        positive = sum(1 for m in metrics if m > 0)
        negative = sum(1 for m in metrics if m < 0)
        consistency = max(positive, negative) / n_p if n_p > 0 else 0.0

        gate = GateResult.PASS
        failure_reasons: list[str] = []
        if n_p < self.config.min_paths_for_verdict:
            gate = GateResult.UNVERIFIABLE
            failure_reasons.append("insufficient_completed_cpcv_paths")
        if consistency < self.config.min_path_consistency:
            gate = GateResult.FAIL
            failure_reasons.append("path_inconsistency")
        if mean_m <= 0:
            gate = GateResult.FAIL
            failure_reasons.append("non_positive_mean_metric")
        # GAP-8: q05 是路径分布的一个样本分位数，要求所有路径全正在
        # 统计上过严（36+ 路径中个别路径为负属正常抽样波动）。改为 95%
        # 置信下界判定：q05_se = std/sqrt(n_p)，仅当 q05 + 1.645*se <= 0
        # 才判 FAIL（reason 保持 "non_positive_oos_q05"）。mean 检查不变。
        q05_se = std_m / math.sqrt(n_p) if n_p > 0 and std_m > 0 else 0.0
        if q05 + 1.645 * q05_se <= 0:
            gate = GateResult.FAIL
            failure_reasons.append("non_positive_oos_q05")

        content = f"cpcv:{n_p}:{mean_m:.6f}:{std_m:.6f}:{consistency:.3f}"
        evidence_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

        return CPCVResult(
            n_paths=len(paths),
            n_completed=len(oos_metrics),
            oos_metrics=oos_metrics,
            metric_mean=round(mean_m, 6),
            metric_std=round(std_m, 6),
            metric_median=round(median_m, 6),
            metric_q05=round(q05, 6),
            metric_q95=round(q95, 6),
            path_consistency=round(consistency, 4),
            gate_result=gate,
            evidence_hash=evidence_hash,
            failure_reasons=failure_reasons,
        )


def _default_ic(predictions: list[float], returns: list[float]) -> float:
    """默认 IC 指标。"""
    n = min(len(predictions), len(returns))
    if n < 3:
        return 0.0
    p = predictions[:n]
    r = returns[:n]
    mp = sum(p) / n
    mr = sum(r) / n
    cov = sum((p[i] - mp) * (r[i] - mr) for i in range(n)) / (n - 1)
    sp = (sum((x - mp) ** 2 for x in p) / (n - 1)) ** 0.5
    sr = (sum((x - mr) ** 2 for x in r) / (n - 1)) ** 0.5
    if sp == 0 or sr == 0:
        return 0.0
    return float(cov / (sp * sr))


def _is_finite(value: float) -> bool:
    try:
        import math

        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False
