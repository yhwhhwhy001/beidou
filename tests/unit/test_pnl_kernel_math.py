"""PnL 内核数学性质测试（M08）。

M08-F01/F02/F03: 旧实现的数学错误固化测试 —— 复利总收益、年化量纲
统一、复利回撤。
"""

from __future__ import annotations

import math

from beidou_research.mining.evaluation.pnl_kernel import StrategyPnL, StrategyPnLKernel


def test_total_return_is_compound_log_sum() -> None:
    """M08-F01: total_return = Σlog(1+r),不是 log(1+Σr)。"""
    returns = (0.5, -0.3, 0.2)  # 大幅波动使两种公式显著分歧
    pnl = StrategyPnL(net_returns=returns)
    expected = math.log1p(0.5) + math.log1p(-0.3) + math.log1p(0.2)
    assert abs(pnl.total_return - expected) < 1e-12
    # 旧公式 log(1+Σr) 与之不同(Σr=0.4)
    assert abs(pnl.total_return - math.log(1.4)) > 0.01


def test_total_return_handles_large_drawdown() -> None:
    """Σr < -1 时旧公式域错误;新公式安全(每期 r > -1 即可)。"""
    pnl = StrategyPnL(net_returns=(-0.7, -0.6))
    assert math.isfinite(pnl.total_return)


def test_sharpe_is_annualized_and_consistent_with_volatility() -> None:
    """M08-F02: sharpe = per-bar × √ppy,与 ann_vol/ann_return 同口径。"""
    kernel = StrategyPnLKernel()
    returns = (0.01, -0.005, 0.02, 0.004, -0.01, 0.008, 0.012, -0.006, 0.015, 0.002, -0.008, 0.01)
    pnl = StrategyPnL(net_returns=returns)
    metrics = kernel.evaluate(pnl, periods_per_year=252)
    assert metrics.sharpe is not None
    mean_r = sum(returns) / len(returns)
    std_r = (sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)) ** 0.5
    # metrics 输出 round(4),容差 5e-5
    assert abs(metrics.sharpe - mean_r / std_r * math.sqrt(252)) < 5e-5
    assert abs(metrics.annualized_volatility - std_r * math.sqrt(252)) < 5e-5


def test_drawdown_is_compounding() -> None:
    """M08-F03: 回撤基于复利权益曲线。"""
    kernel = StrategyPnLKernel()
    # +50% 后 -40%:权益 1.5 → 0.9,回撤 = 0.6/1.5 = 0.4
    returns = [0.5, -0.4]
    dd = kernel._compute_max_drawdown(returns)
    assert abs(dd - 0.4) < 1e-9
    # 算术累计旧公式会得 (0.5 - 0.1)/0.5 = 0.8(错误放大)
    assert dd < 0.5


def test_sharpe_from_pnl_annualized() -> None:
    kernel = StrategyPnLKernel()
    pnl = StrategyPnL(net_returns=(0.01, -0.005, 0.02, 0.004, -0.01, 0.008, 0.012, -0.006, 0.015))
    annualized = kernel.compute_sharpe_from_pnl(pnl, periods_per_year=8760)  # 1h
    daily = kernel.compute_sharpe_from_pnl(pnl, periods_per_year=252)
    assert annualized is not None and daily is not None
    assert abs(annualized / daily - math.sqrt(8760 / 252)) < 1e-9
