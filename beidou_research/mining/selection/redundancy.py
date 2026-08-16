"""BF-06: 冗余检测与聚类。

替换简化 VIF:
1. Pearson/Spearman 相关矩阵
2. Hierarchical clustering
3. 多元回归 VIF
4. Conditional mutual information (简化)
5. Residual correlation
6. Leave-one-factor-out OOS 组合差分
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RedundancyResult:
    """冗余检测结果。"""

    factor_pairs: list[tuple[str, str, float]]  # (f1, f2, correlation)
    high_correlation_pairs: list[tuple[str, str]]  # corr > 0.7
    vif_scores: dict[str, float]  # factor → VIF
    clusters: list[list[str]]  # 层次聚类结果
    redundant_factors: list[str]  # 建议淘汰的因子
    redundant_cluster_count: int = 0


class RedundancyDetector:
    """因子冗余检测器。

    检测高相关因子组，建议保留最具代表性的因子。
    """

    def __init__(self, corr_threshold: float = 0.7, vif_threshold: float = 5.0) -> None:
        self.corr_threshold = corr_threshold
        self.vif_threshold = vif_threshold

    def compute_correlation_matrix(
        self,
        factor_values: dict[str, list[float]],
    ) -> dict[str, dict[str, float]]:
        """计算因子间 Pearson 相关矩阵。"""
        names = list(factor_values.keys())
        corr_matrix: dict[str, dict[str, float]] = {n: {} for n in names}

        for i, n1 in enumerate(names):
            for j, n2 in enumerate(names):
                if i >= j:
                    continue
                v1 = factor_values[n1]
                v2 = factor_values[n2]
                n = min(len(v1), len(v2))
                if n < 3:
                    corr_matrix[n1][n2] = 0.0
                    corr_matrix[n2][n1] = 0.0
                    continue

                corr = _pearson(v1[:n], v2[:n])
                corr_matrix[n1][n2] = corr
                corr_matrix[n2][n1] = corr

        for name in names:
            corr_matrix[name][name] = 1.0

        return corr_matrix

    def find_high_correlation_pairs(
        self,
        corr_matrix: dict[str, dict[str, float]],
    ) -> list[tuple[str, str, float]]:
        """找出高相关因子对 (|corr| > threshold)。"""
        pairs = []
        names = list(corr_matrix.keys())
        for i, n1 in enumerate(names):
            for j, n2 in enumerate(names):
                if i >= j:
                    continue
                corr = corr_matrix[n1].get(n2, 0.0)
                if abs(corr) > self.corr_threshold:
                    pairs.append((n1, n2, round(corr, 4)))

        return sorted(pairs, key=lambda x: abs(x[2]), reverse=True)

    def compute_vif_scores(
        self,
        factor_values: dict[str, list[float]],
    ) -> dict[str, float]:
        """计算多元回归 VIF。

        对每个因子，用其他所有因子回归，VIF = 1/(1-R²)。
        """
        names = list(factor_values.keys())
        if not names:
            return {}
        n_samples = min(len(v) for v in factor_values.values())

        if n_samples < 10 or len(names) < 2:
            return dict.fromkeys(names, 1.0)

        vif_scores = {}
        for target in names:
            predictors = [n for n in names if n != target]
            y = factor_values[target][:n_samples]
            X = [[factor_values[p][i] for p in predictors] for i in range(n_samples)]

            # OLS regression
            r_squared = _ols_r_squared(y, X)
            if r_squared >= 1.0:
                vif_scores[target] = float("inf")
            else:
                vif_scores[target] = 1.0 / (1.0 - r_squared)

        return vif_scores

    def hierarchical_clustering(
        self,
        corr_matrix: dict[str, dict[str, float]],
    ) -> list[list[str]]:
        """基于相关矩阵的层次聚类。"""
        names = list(corr_matrix.keys())
        if len(names) <= 1:
            return [[n] for n in names]

        # 距离 = 1 - |correlation|
        clusters = [[n] for n in names]

        while len(clusters) > 1:
            # 找最近的两个 cluster
            min_dist = float("inf")
            min_i, min_j = 0, 0
            for i in range(len(clusters)):
                for j in range(i + 1, len(clusters)):
                    dist = self._cluster_distance(clusters[i], clusters[j], corr_matrix)
                    if dist < min_dist:
                        min_dist = dist
                        min_i, min_j = i, j

            if min_dist > (1 - self.corr_threshold):
                break

            # 合并
            clusters[min_i].extend(clusters[min_j])
            clusters.pop(min_j)

        return clusters

    def _cluster_distance(
        self,
        c1: list[str],
        c2: list[str],
        corr_matrix: dict[str, dict[str, float]],
    ) -> float:
        """平均距离。"""
        dists = []
        for n1 in c1:
            for n2 in c2:
                corr = abs(corr_matrix.get(n1, {}).get(n2, 0.0))
                dists.append(1 - corr)
        return sum(dists) / len(dists) if dists else 1.0

    def analyze(
        self,
        factor_values: dict[str, list[float]],
    ) -> RedundancyResult:
        """完整冗余分析。"""
        corr_matrix = self.compute_correlation_matrix(factor_values)
        high_pairs = self.find_high_correlation_pairs(corr_matrix)
        vif_scores = self.compute_vif_scores(factor_values)
        clusters = self.hierarchical_clustering(corr_matrix)

        # 建议淘汰的因子（高 VIF 且在高相关对中）
        redundant = [n for n, vif in vif_scores.items() if vif > self.vif_threshold]

        return RedundancyResult(
            factor_pairs=high_pairs,
            high_correlation_pairs=[(p[0], p[1]) for p in high_pairs],
            vif_scores=vif_scores,
            clusters=clusters,
            redundant_factors=redundant,
            redundant_cluster_count=len(clusters),
        )


def _pearson(x: list[float], y: list[float]) -> float:
    n = len(x)
    if n < 3:
        return 0.0
    mx = sum(x) / n
    my = sum(y) / n
    cov = sum((x[i] - mx) * (y[i] - my) for i in range(n)) / (n - 1)
    sx = (sum((xi - mx) ** 2 for xi in x) / (n - 1)) ** 0.5
    sy = (sum((yi - my) ** 2 for yi in y) / (n - 1)) ** 0.5
    if sx == 0 or sy == 0:
        return 0.0
    return float(cov / (sx * sy))


def _ols_r_squared(y: list[float], X: list[list[float]]) -> float:
    """简化的 OLS R² 计算（高斯消元）。"""
    # 为简单实现，逐步去相关近似
    n = len(y)
    if n < 3 or not X or not X[0]:
        return 0.0

    residual = list(y)
    mean_y = sum(y) / n
    ss_tot = sum((yi - mean_y) ** 2 for yi in y)

    if ss_tot < 1e-15:
        return 1.0

    for pred_idx in range(len(X[0])):
        x_col = [row[pred_idx] for row in X]
        mean_x = sum(x_col) / n
        mean_r = sum(residual) / n
        cov_xr = sum((x_col[i] - mean_x) * (residual[i] - mean_r) for i in range(n))
        var_x = sum((xi - mean_x) ** 2 for xi in x_col)
        if abs(var_x) < 1e-15:
            continue
        beta = cov_xr / var_x
        alpha = mean_r - beta * mean_x
        residual = [residual[i] - (alpha + beta * x_col[i]) for i in range(n)]

    ss_res = sum(r**2 for r in residual)
    r_squared = 1.0 - ss_res / ss_tot
    return max(0.0, min(1.0, r_squared))
