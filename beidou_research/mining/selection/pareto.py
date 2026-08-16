"""BF-06: Pareto 前沿选择。

不把所有指标压成一个不可解释分数。
先构造 Pareto Front，再根据策略目标选择。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ParetoCandidate:
    """Pareto 候选。"""

    candidate_id: str
    metrics: dict[str, float]  # 所有评估指标
    is_pareto_optimal: bool = False
    pareto_rank: int = 999  # 0 = 前沿, 1 = 次前沿, ...


@dataclass
class ParetoResult:
    """Pareto 分析结果。"""

    candidates: list[ParetoCandidate]
    front_size: int
    fronts: list[list[ParetoCandidate]]
    dominated_count: int


class ParetoSelector:
    """Pareto 前沿选择器。

    使用多目标优化而非单目标标量化。
    目标:
      maximize: OOS ICIR, Sharpe, fold consistency, regime stability, capacity
      minimize: turnover, drawdown, correlation, complexity, parameter sensitivity
    """

    def compute_pareto_fronts(
        self,
        candidates: list[dict[str, Any]],
        maximize_metrics: list[str] | None = None,
        minimize_metrics: list[str] | None = None,
    ) -> ParetoResult:
        """计算 Pareto 前沿。

        Args:
            candidates: 候选列表 [{candidate_id, icir, sharpe, turnover, complexity, ...}]
            maximize_metrics: 需要最大化的指标
            minimize_metrics: 需要最小化的指标

        Returns:
            ParetoResult
        """
        if maximize_metrics is None:
            maximize_metrics = ["icir", "sharpe", "fold_consistency"]
        if minimize_metrics is None:
            minimize_metrics = ["turnover", "complexity", "correlation"]

        # 转换为 ParetoCandidate
        pc_list = []
        for c in candidates:
            metrics = {}
            for k, v in c.items():
                if isinstance(v, (int, float)):
                    metrics[k] = float(v)
            pc_list.append(
                ParetoCandidate(
                    candidate_id=c.get("candidate_id", c.get("hash", "unknown")),
                    metrics=metrics,
                )
            )

        # 标准化目标向量
        # maximize 方向: value
        # minimize 方向: -value
        objectives = []
        for pc in pc_list:
            obj = []
            for m in maximize_metrics:
                obj.append(pc.metrics.get(m, 0.0))
            for m in minimize_metrics:
                obj.append(-pc.metrics.get(m, 0.0))
            objectives.append(obj)

        # Fast Non-dominated Sort (NDS)
        n = len(pc_list)
        dominated_by = [0] * n
        dominates: list[list[int]] = [[] for _ in range(n)]

        for i in range(n):
            for j in range(n):
                if i == j:
                    continue
                if self._dominates(objectives[i], objectives[j]):
                    dominates[i].append(j)
                    dominated_by[j] += 1

        # 分配前沿
        fronts = []
        current_front = [i for i in range(n) if dominated_by[i] == 0]
        rank = 0

        while current_front:
            for i in current_front:
                pc_list[i].is_pareto_optimal = rank == 0
                pc_list[i].pareto_rank = rank

            fronts.append([pc_list[i] for i in current_front])
            next_front = []
            for i in current_front:
                for j in dominates[i]:
                    dominated_by[j] -= 1
                    if dominated_by[j] == 0:
                        next_front.append(j)
            current_front = next_front
            rank += 1

        return ParetoResult(
            candidates=pc_list,
            front_size=len(fronts[0]) if fronts else 0,
            fronts=fronts,
            dominated_count=n - (len(fronts[0]) if fronts else 0),
        )

    def _dominates(self, a: list[float], b: list[float]) -> bool:
        """检查 a 是否 Pareto-dominates b。

        a dominates b if:
          ∀i: a[i] >= b[i]  AND  ∃j: a[j] > b[j]
        """
        m = len(a)
        at_least_equal = all(a[i] >= b[i] for i in range(m))
        strictly_better = any(a[i] > b[i] for i in range(m))
        return at_least_equal and strictly_better

    def select_diverse(
        self,
        front: list[ParetoCandidate],
        max_count: int = 10,
    ) -> list[ParetoCandidate]:
        """从前沿中选择多样性最大的子集。"""
        if len(front) <= max_count:
            return front

        # Greedy diversity selection: 选择目标空间中最分散的点
        selected = [front[0]]
        remaining = list(front[1:])

        while len(selected) < max_count and remaining:
            # 找离已选点最远的候选
            best_idx = 0
            best_dist = -1.0
            for i, c in enumerate(remaining):
                min_dist = min(self._metric_distance(c, s) for s in selected)
                if min_dist > best_dist:
                    best_dist = min_dist
                    best_idx = i
            selected.append(remaining.pop(best_idx))

        return selected

    def _metric_distance(self, a: ParetoCandidate, b: ParetoCandidate) -> float:
        """两个候选的欧几里得距离。"""
        all_keys = set(a.metrics.keys()) | set(b.metrics.keys())
        dist = 0.0
        for k in sorted(all_keys):
            va = a.metrics.get(k, 0.0)
            vb = b.metrics.get(k, 0.0)
            dist += (va - vb) ** 2
        return float(dist**0.5)
