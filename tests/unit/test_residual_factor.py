"""残差因子计算修复验证。

根因:runner 曾按模块级函数导入 compute_residual_values,但该名字是
ResidualGenerator 的方法,导致每次挖掘的残差因子块都被 ImportError
吞掉("residual factor evaluation failed; candidate omitted")。
修复后通过 MiningRunner._compute_residual_values 懒加载生成器实例。
"""

from __future__ import annotations

from beidou_research.mining.runner import MiningRunner, PipelineConfig


def _runner() -> MiningRunner:
    return MiningRunner(PipelineConfig())


def test_residual_values_removes_collinear_control():
    """完美共线的控制变量 → 残差全部为 0(OLS 精确解)。"""
    base = [float(i) for i in range(50)]
    control = [2.0 * i for i in range(50)]  # ctrl = 2 * base
    out = _runner()._compute_residual_values(base, {"ctrl": control})

    assert len(out) == 50
    assert all(abs(v) < 1e-9 for v in out)


def test_residual_values_skips_zero_variance_control():
    """零方差控制变量(回归无意义)→ 跳过,返回原始因子值。"""
    base = [float(i % 7) for i in range(50)]
    out = _runner()._compute_residual_values(base, {"const": [3.0] * 50})

    assert out == base


def test_residual_values_returns_original_when_insufficient_samples():
    """样本数低于 min_effective_samples → 返回原始因子值。"""
    base = [1.0, 2.0, 3.0]
    out = _runner()._compute_residual_values(base, {"ctrl": [4.0, 5.0, 6.0]})

    assert out == base


def test_residual_values_partial_correlation_ols_math():
    """部分相关控制变量 → 残差与 OLS 逐步回归公式一致。"""
    base = [1.0, 3.0, 2.0, 5.0, 4.0, 6.0, 8.0, 7.0, 10.0, 9.0] * 4  # 40 样本
    control = [float(i) for i in range(40)]

    out = _runner()._compute_residual_values(base, {"ctrl": control})

    n = len(base)
    mean_r = sum(base) / n
    mean_c = sum(control) / n
    cov = sum((base[i] - mean_r) * (control[i] - mean_c) for i in range(n))
    var_c = sum((c - mean_c) ** 2 for c in control)
    beta = cov / var_c
    alpha = mean_r - beta * mean_c
    expected = [base[i] - (alpha + beta * control[i]) for i in range(n)]

    assert len(out) == n
    assert all(abs(a - b) < 1e-12 for a, b in zip(out, expected))
