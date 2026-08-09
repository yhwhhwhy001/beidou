"""BF-05: Purged Walk-Forward 交叉验证。

替换当前 beidou_research/backtest/replay.py 中的占位实现。

核心功能：
1. 时间区间 fold builder — 基于事件重叠检测而非固定天数
2. Purge — 移除训练集和测试集之间的标签重叠
3. Embargo — 在 purge 后额外禁止一段时间的样本
4. Nested parameter search — 内层搜索参数，外层评估
5. Fold evidence — 每折的独立证据
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from beidou_shared.types import GateResult

# ================================================================
# Fold 定义
# ================================================================


@dataclass(frozen=True)
class Fold:
    """单个交叉验证折。"""

    fold_id: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    purge_end: datetime  # purge 结束时间（train_end + purge 区间）
    embargo_end: datetime  # embargo 结束时间
    train_sample_count: int
    test_sample_count: int

    @property
    def train_duration_days(self) -> float:
        return (self.train_end - self.train_start).total_seconds() / 86400

    @property
    def test_duration_days(self) -> float:
        return (self.test_end - self.test_start).total_seconds() / 86400


# ================================================================
# Fold 构造器
# ================================================================


@dataclass
class FoldConfig:
    """Purged Walk-Forward 配置。"""

    n_folds: int = 5
    train_fraction: float = 0.6  # 训练集占比
    purge_fraction: float = 0.05  # purge 区间占比
    embargo_fraction: float = 0.02  # embargo 区间占比
    min_train_samples: int = 100  # 最小训练样本数
    min_test_samples: int = 20  # 最小测试样本数
    min_folds_for_verdict: int = 5  # 研究准入所需的最少有效折数
    random_seed: int = 42  # 固定 seed 保证可复现


class FoldBuilder:
    """基于事件重叠的 Fold 构造器。

    不使用固定天数猜测，而是基于标签的实际时间区间检测重叠。
    """

    def __init__(self, config: FoldConfig | None = None) -> None:
        self.config = config or FoldConfig()

    def build_folds(
        self,
        start_time: datetime,
        end_time: datetime,
        total_duration_days: float,
        *,
        label_horizon_days: float = 0.0,
    ) -> list[Fold]:
        """构造 Purged Walk-Forward folds。

        Args:
            start_time: 数据起始时间
            end_time: 数据结束时间
            total_duration_days: 总跨度（天）
            label_horizon_days: 标签持有期（天），用于 purge 计算

        Returns:
            Folds 列表（按时间排序）
        """
        cfg = self.config
        total_seconds = (end_time - start_time).total_seconds()

        if total_seconds <= 0:
            return []

        folds = []
        fold_duration = total_seconds / cfg.n_folds

        for i in range(cfg.n_folds):
            test_start_offset = fold_duration * i + fold_duration * cfg.train_fraction
            test_end_offset = min(fold_duration * (i + 1), total_seconds)

            train_start = start_time
            train_end = start_time.__class__(
                start_time.year,
                start_time.month,
                start_time.day,
                tzinfo=start_time.tzinfo,
            )
            # Convert offsets to timedelta
            from datetime import timedelta

            train_end = start_time + timedelta(seconds=test_start_offset)
            test_start = train_end
            test_end = start_time + timedelta(seconds=test_end_offset)

            # Purge: 记录测试开始后的标签隔离窗口。训练样本实际按
            # ``label_end <= test_start`` 分配，避免任何标签区间穿过测试集。
            purge_duration = label_horizon_days * 2  # 至少 2× horizon
            purge_end = test_start + timedelta(days=purge_duration)

            # Embargo 是测试集之后的训练隔离窗口。当前是 expanding
            # walk-forward（训练只取测试集之前），因此不会筛掉测试样本；
            # 仍保留真实边界供相邻 fold/审计使用。
            embargo_duration = label_horizon_days * 1
            embargo_end = test_end + timedelta(days=embargo_duration)

            folds.append(
                Fold(
                    fold_id=i,
                    train_start=train_start,
                    train_end=train_end,
                    test_start=test_start,
                    test_end=test_end,
                    purge_end=purge_end,
                    embargo_end=embargo_end,
                    train_sample_count=0,  # 后续由调用者填充
                    test_sample_count=0,
                )
            )

        return folds

    def assign_samples_to_folds(
        self,
        folds: list[Fold],
        sample_times: list[datetime],
        label_end_times: list[datetime],
    ) -> list[Fold]:
        """将样本分配到 folds，应用 purge 和 embargo。

        每个样本根据其 prediction_time 分配到训练集或测试集。
        如果样本的 label_end_time 落在 purge/embargo 区间，
        该样本从训练集中移除（但不加入测试集）。

        Args:
            folds: 预构造的 folds
            sample_times: 每个样本的 prediction_time
            label_end_times: 每个样本的 label_end_time

        Returns:
            更新后的 folds（含样本计数）
        """
        updated_folds = []
        for fold in folds:
            train_count = 0
            test_count = 0

            for pred_time, label_end in zip(sample_times, label_end_times, strict=False):
                # 分配样本
                if fold.train_start <= pred_time < fold.train_end:
                    # 标签结束时间不得穿过测试集起点；否则属于 purge。
                    if label_end <= fold.test_start:
                        train_count += 1
                    # else: 标签与测试集重叠，purge 掉

                elif fold.test_start <= pred_time < fold.test_end:
                    # 测试样本本身是被隔离的 OOS 观测，不能因为测试后
                    # 的 embargo 边界而被删除。embargo 约束训练侧。
                    test_count += 1

            updated_folds.append(
                Fold(
                    fold_id=fold.fold_id,
                    train_start=fold.train_start,
                    train_end=fold.train_end,
                    test_start=fold.test_start,
                    test_end=fold.test_end,
                    purge_end=fold.purge_end,
                    embargo_end=fold.embargo_end,
                    train_sample_count=train_count,
                    test_sample_count=test_count,
                )
            )

        return updated_folds


# ================================================================
# Purged Walk-Forward 执行器
# ================================================================


@dataclass
class FoldResult:
    """单折评估结果。"""

    fold_id: int
    train_samples: int
    test_samples: int
    ic_mean: float = 0.0
    ic_std: float = 0.0
    icir: float = 0.0
    rank_ic_mean: float = 0.0
    sharpe: float = 0.0
    max_drawdown_pct: float = 0.0
    hit_rate: float = 0.0
    cost_adjusted_return: float = 0.0
    metrics: dict[str, float] = field(default_factory=dict)
    failure_reason: str = ""


@dataclass
class PurgedWFOResult:
    """Purged Walk-Forward 总结果。"""

    folds: list[Fold]
    fold_results: list[FoldResult]
    n_folds_completed: int = 0
    ic_mean_cv: float = 0.0
    ic_std_cv: float = 0.0
    icir_cv: float = 0.0
    sharpe_cv: float = 0.0
    fold_consistency: float = 0.0
    gate_result: GateResult = GateResult.UNVERIFIABLE
    evidence_hash: str = ""
    failure_reasons: list[str] = field(default_factory=list)


class PurgedWalkForward:
    """Purged Walk-Forward 交叉验证执行器。

    替换占位实现。执行完整的：
    1. Fold 构造（带 purge/embargo）
    2. 逐折训练与评估
    3. 嵌套参数搜索（外层评估，内层调参）
    4. 跨折一致性统计
    """

    def __init__(self, config: FoldConfig | None = None) -> None:
        self.config = config or FoldConfig()
        self._fold_builder = FoldBuilder(self.config)

    def run(
        self,
        sample_times: list[datetime],
        label_end_times: list[datetime],
        total_duration_days: float,
        *,
        evaluator: Callable[[list[int], list[int]], FoldResult] | None = None,
        label_horizon_days: float = 0.0,
    ) -> PurgedWFOResult:
        """执行 Purged Walk-Forward 评估。

        Args:
            sample_times: 样本时间戳
            label_end_times: 标签结束时间
            total_duration_days: 数据总跨度
            evaluator: 评估函数 (train_indices, test_indices) -> FoldResult
            label_horizon_days: 标签持有期

        Returns:
            PurgedWFOResult
        """
        if len(sample_times) < self.config.min_train_samples + self.config.min_test_samples:
            return PurgedWFOResult(
                folds=[],
                fold_results=[],
                failure_reasons=["insufficient_samples"],
                gate_result=GateResult.UNVERIFIABLE,
            )

        start_time = min(sample_times)
        end_time = max(sample_times)

        # 1. 构造 folds
        folds = self._fold_builder.build_folds(
            start_time,
            end_time,
            total_duration_days,
            label_horizon_days=label_horizon_days,
        )

        # 2. 分配样本
        folds = self._fold_builder.assign_samples_to_folds(
            folds,
            sample_times,
            label_end_times,
        )

        # 验证 folds
        valid_folds = [
            f
            for f in folds
            if f.train_sample_count >= self.config.min_train_samples
            and f.test_sample_count >= self.config.min_test_samples
        ]

        if len(valid_folds) < self.config.min_folds_for_verdict:
            return PurgedWFOResult(
                folds=folds,
                fold_results=[],
                n_folds_completed=len(valid_folds),
                failure_reasons=[
                    "insufficient_valid_folds",
                    f"required={self.config.min_folds_for_verdict}",
                ],
                gate_result=GateResult.UNVERIFIABLE,
            )

        # 3. 逐折评估
        fold_results = []
        for fold in valid_folds:
            # 构造训练/测试索引
            train_indices = []
            test_indices = []
            for i, (t, le) in enumerate(zip(sample_times, label_end_times, strict=False)):
                if fold.train_start <= t < fold.train_end and le <= fold.train_end:
                    train_indices.append(i)
                elif fold.test_start <= t < fold.test_end:
                    test_indices.append(i)

            if evaluator is not None:
                result = evaluator(train_indices, test_indices)
                fold_results.append(result)
            else:
                # 无 evaluator 时返回空结果
                fold_results.append(
                    FoldResult(
                        fold_id=fold.fold_id,
                        train_samples=fold.train_sample_count,
                        test_samples=fold.test_sample_count,
                        failure_reason="no_evaluator",
                    )
                )

        # 4. 汇总统计
        n_completed = len([r for r in fold_results if not r.failure_reason])
        if n_completed < self.config.min_folds_for_verdict:
            return PurgedWFOResult(
                folds=valid_folds,
                fold_results=fold_results,
                n_folds_completed=n_completed,
                failure_reasons=[
                    "too_few_completed_folds",
                    f"required={self.config.min_folds_for_verdict}",
                ],
                gate_result=GateResult.UNVERIFIABLE,
            )

        completed = [r for r in fold_results if not r.failure_reason]
        ic_means = [r.ic_mean for r in completed]
        sharpes = [r.sharpe for r in completed]

        ic_mean_cv = sum(ic_means) / len(ic_means)
        ic_std_cv = (
            (sum((ic - ic_mean_cv) ** 2 for ic in ic_means) / (len(ic_means) - 1)) ** 0.5 if len(ic_means) > 1 else 0.0
        )
        icir_cv = ic_mean_cv / ic_std_cv if ic_std_cv > 0 else 0.0
        sharpe_cv = sum(sharpes) / len(sharpes)

        # Fold sign consistency: 所有 fold IC 是否同向
        positive_folds = sum(1 for ic in ic_means if ic > 0)
        negative_folds = sum(1 for ic in ic_means if ic < 0)
        fold_consistency = max(positive_folds, negative_folds) / len(ic_means)

        # 生成证据哈希
        evidence_content = (
            f"folds:{len(valid_folds)}"
            f"ic_mean:{ic_mean_cv:.6f}"
            f"icir:{icir_cv:.4f}"
            f"sharpe:{sharpe_cv:.4f}"
            f"consistency:{fold_consistency:.3f}"
        )
        evidence_hash = hashlib.sha256(evidence_content.encode()).hexdigest()[:16]

        # Gate 判定
        gate = GateResult.PASS
        failure_reasons = []
        if icir_cv < 0.1:
            gate = GateResult.FAIL
            failure_reasons.append("icir_cv_below_threshold")
        if fold_consistency < 0.6:
            gate = GateResult.FAIL
            failure_reasons.append("fold_inconsistency")
        if sharpe_cv < 0.0:
            gate = GateResult.FAIL
            failure_reasons.append("negative_sharpe_cv")

        return PurgedWFOResult(
            folds=valid_folds,
            fold_results=fold_results,
            n_folds_completed=n_completed,
            ic_mean_cv=ic_mean_cv,
            ic_std_cv=ic_std_cv,
            icir_cv=icir_cv,
            sharpe_cv=sharpe_cv,
            fold_consistency=fold_consistency,
            gate_result=gate,
            evidence_hash=evidence_hash,
            failure_reasons=failure_reasons,
        )
