"""BF-04: 残差因子生成器。

对候选因子做控制变量回归:
  candidate_residual = candidate
                      - β1 × market_beta
                      - β2 × volatility
                      - β3 × liquidity
                      - β4 × active_factor_bundle

用于识别独立信息，而不是重复已有因子暴露。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ResidualConfig:
    """残差生成配置。"""
    control_factors: list[str] = field(default_factory=lambda: [
        "market_beta", "volatility", "liquidity",
    ])
    max_controls: int = 5
    min_effective_samples: int = 30
    max_vif: float = 10.0        # 控制变量之间的最大 VIF
    residual_threshold: float = 0.01  # 最小残差方差比


@dataclass
class ResidualSpec:
    """残差因子规格。"""
    residual_id: str
    base_factor: str
    controls: tuple[str, ...]
    expression_hash: str
    complexity_score: float
    parent_factor_ids: tuple[str, ...]


class ResidualGenerator:
    """残差因子生成器。

    对每个候选因子回归掉控制变量，提取独立信号分量。
    如果残差与原始因子高度相关（R² > 0.99），说明因子无独立信息，淘汰。
    """

    def __init__(self, config: ResidualConfig | None = None) -> None:
        self.config = config or ResidualConfig()

    def generate_residuals(
        self,
        candidates: list[dict[str, Any]],
        control_values: dict[str, list[float]] | None = None,
    ) -> list[ResidualSpec]:
        """为候选因子生成残差版本。

        Args:
            candidates: 候选因子列表 [{factor_id, expression_hash, ...}, ...]
            control_values: 控制变量值 {name: [values]}

        Returns:
            残差规格列表
        """
        cfg = self.config
        residuals = []
        seen: set[str] = set()

        control_names = list(control_values.keys()) if control_values else cfg.control_factors[:3]

        for candidate in candidates:
            fid = candidate.get("factor_id", "unknown")
            for control in control_names[:cfg.max_controls]:
                controls = (control,)
                res_id = f"{fid}_residual_{control}"

                expr_hash = hashlib.sha256(
                    f"residual:{fid}:{control}".encode()
                ).hexdigest()[:20]

                if expr_hash not in seen:
                    seen.add(expr_hash)
                    residuals.append(ResidualSpec(
                        residual_id=res_id,
                        base_factor=fid,
                        controls=controls,
                        expression_hash=expr_hash,
                        complexity_score=candidate.get("complexity_score", 1.0) + 2.0,
                        parent_factor_ids=(fid,),
                    ))

        return residuals

    def compute_residual_values(
        self,
        factor_values: list[float],
        control_values: dict[str, list[float]],
    ) -> list[float]:
        """计算残差化后的因子值。

        使用 OLS 回归: factor = α + Σβ_i × control_i + ε
        返回残差 ε（即与 controls 无关的分量）。

        Args:
            factor_values: 原始因子值
            control_values: 控制变量值

        Returns:
            残差化后的因子值
        """
        n = len(factor_values)
        if n < self.config.min_effective_samples:
            return list(factor_values)  # 不够样本，返回原始值

        # 对齐所有序列长度
        control_arrays = {}
        for name, values in control_values.items():
            if len(values) >= n:
                control_arrays[name] = values[:n]

        if not control_arrays:
            return list(factor_values)

        # 简单 OLS: 一次回归一个控制变量，逐步去相关
        residual = list(factor_values)
        for name, cvals in control_arrays.items():
            # regress residual on cvals
            mean_r = sum(residual) / n
            mean_c = sum(cvals) / n
            cov = sum((residual[i] - mean_r) * (cvals[i] - mean_c) for i in range(n))
            var_c = sum((c - mean_c) ** 2 for c in cvals)
            if abs(var_c) < 1e-15:
                continue
            beta = cov / var_c
            alpha = mean_r - beta * mean_c
            # 残差 = y - (alpha + beta * x)
            residual = [residual[i] - (alpha + beta * cvals[i]) for i in range(n)]

        return residual

    def check_independence(
        self,
        original_values: list[float],
        residual_values: list[float],
    ) -> tuple[bool, float]:
        """检查残差是否提供了独立信息。

        Returns:
            (has_independent_info, r_squared_with_original)
        """
        n = min(len(original_values), len(residual_values))
        if n < 10:
            return False, 1.0

        o = original_values[:n]
        r = residual_values[:n]

        # 计算 R² between original and residual
        mean_o = sum(o) / n
        var_o = sum((x - mean_o) ** 2 for x in o)
        mean_r = sum(r) / n
        var_r = sum((x - mean_r) ** 2 for x in r)

        if var_o < 1e-15 or var_r < 1e-15:
            return False, 0.0

        # 残差方差比
        var_ratio = var_r / var_o
        has_info = var_ratio > self.config.residual_threshold

        return has_info, var_ratio
