"""BF-06: 因子组合方法。

首期允许:
- equal-risk contribution
- covariance shrinkage weighting
- non-negative constrained linear weights
- ElasticNet (因子选择+组合)
- regime-conditioned weight table
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EnsembleWeights:
    """因子组合权重。"""
    weights: dict[str, float]        # factor_id → weight
    method: str                      # "equal_risk" / "shrinkage" / "elastic_net" / "regime_table"
    total_weight: float = 1.0
    non_negative: bool = True
    regime: str = "all"


class FactorEnsemble:
    """因子组合器。

    将多个因子信号组合为单一 Alpha 信号。
    首期禁止直接将不可解释深度模型输出作为 Live 方向信号。
    """

    def __init__(self) -> None:
        pass

    def equal_risk_contribution(
        self,
        factor_returns: dict[str, list[float]],
    ) -> EnsembleWeights:
        """等风险贡献 (ERC) 权重。

        每个因子分配与其波动率倒数成比例的权重。
        """
        if not factor_returns:
            return EnsembleWeights(weights={}, method="equal_risk")

        vols = {}
        for fid, rets in factor_returns.items():
            n = len(rets)
            if n < 2:
                vols[fid] = 1.0
            else:
                mean_r = sum(rets) / n
                std_r = (sum((r - mean_r) ** 2 for r in rets) / (n - 1)) ** 0.5
                vols[fid] = std_r + 1e-10

        # 权重 ∝ 1 / vol
        inv_vols = {fid: 1.0 / v for fid, v in vols.items()}
        total_inv = sum(inv_vols.values())
        weights = {fid: w / total_inv for fid, w in inv_vols.items()}

        return EnsembleWeights(weights=weights, method="equal_risk")

    def covariance_shrinkage(
        self,
        factor_returns: dict[str, list[float]],
        shrinkage: float = 0.3,
    ) -> EnsembleWeights:
        """协方差收缩加权。

        将样本协方差向对角矩阵收缩，提高稳健性。
        """
        if not factor_returns:
            return EnsembleWeights(weights={}, method="shrinkage")

        names = list(factor_returns.keys())
        n = min(len(v) for v in factor_returns.values())
        if n < 10 or len(names) < 2:
            # 回退到等权
            weights = {n: 1.0 / len(names) for n in names}
            return EnsembleWeights(weights=weights, method="shrinkage")

        # 样本协方差
        mean_rets = {fid: sum(rets) / n for fid, rets in factor_returns.items()}
        cov = {}
        for i, f1 in enumerate(names):
            for j, f2 in enumerate(names):
                if i > j:
                    continue
                r1 = factor_returns[f1][:n]
                r2 = factor_returns[f2][:n]
                c = sum((r1[k] - mean_rets[f1]) * (r2[k] - mean_rets[f2]) for k in range(n)) / (n - 1)
                cov[(f1, f2)] = c

        # 收缩目标: 对角矩阵
        avg_var = sum(cov.get((f, f), 0.0) for f in names) / len(names)

        # 收缩协方差
        shrunk_diag = {}
        for f in names:
            sample_var = cov.get((f, f), avg_var)
            shrunk_diag[f] = shrinkage * avg_var + (1 - shrinkage) * sample_var

        # 基于收缩后风险的权重: w ∝ 1 / sqrt(var)
        inv_risks = {f: 1.0 / max(v, 1e-10) ** 0.5 for f, v in shrunk_diag.items()}
        total = sum(inv_risks.values())
        weights = {f: w / total for f, w in inv_risks.items()}

        return EnsembleWeights(weights=weights, method="shrinkage")

    def non_negative_linear(
        self,
        factor_returns: dict[str, list[float]],
    ) -> EnsembleWeights:
        """非负约束线性权重。

        所有权重 ≥ 0 (不允许做空因子)。
        """
        erc = self.equal_risk_contribution(factor_returns)
        # ERC 权重天然非负，直接使用
        weights = {k: max(0.0, v) for k, v in erc.weights.items()}
        total = sum(weights.values()) or 1.0
        weights = {k: v / total for k, v in weights.items()}
        return EnsembleWeights(weights=weights, method="non_negative", non_negative=True)

    def combine_signals(
        self,
        factor_signals: dict[str, float],
        weights: EnsembleWeights,
    ) -> float:
        """组合因子信号为单一 Alpha 信号。"""
        combined = 0.0
        for fid, signal in factor_signals.items():
            w = weights.weights.get(fid, 0.0)
            combined += w * signal
        return combined
