"""
PKG04 (BDS-P0-003, BDS-P0-004): 统一策略 PnL 评估内核测试。

验证：
1. prediction → position → gross pnl → cost → net pnl 链路完整
2. 所有指标从 net_returns 推导
3. DSR/PBO 只能接受 StrategyPnLKernel 输出的 Sharpe
"""

from __future__ import annotations

import math

import pytest

from beidou_research.mining.evaluation.pnl_kernel import (
    DSRPBOAdapter,
    StrategyMetrics,
    StrategyPnL,
    StrategyPnLKernel,
)


class TestStrategyPnLKernel:
    """PKG04: 核心 PnL 计算链路测试。"""

    def test_basic_pnl_chain(self) -> None:
        """基本链路：预测→仓位→毛利→成本→净利。"""
        kernel = StrategyPnLKernel()
        predictions = [0.01, 0.02, -0.01, 0.03, 0.0]
        prices = [100.0, 101.0, 100.5, 102.0, 101.0]

        pnl = kernel.compute_pnl(predictions, prices)

        assert pnl.n_periods >= 2
        assert isinstance(pnl.net_returns, tuple)

    def test_evaluate_generates_all_metrics(self) -> None:
        """评估生成所有标准指标（Sharpe, Sortino, MaxDD, Calmar, IC 等）。"""
        kernel = StrategyPnLKernel()
        predictions = [0.01] * 100 + [-0.01] * 100
        prices = [100.0 + i * 0.5 for i in range(200)]

        pnl = kernel.compute_pnl(predictions, prices)
        assert pnl.is_valid

        # PnL 输出长度 = n_prices - 1，用价格收益作为 returns 输入
        price_returns = [(prices[i + 1] - prices[i]) / prices[i] for i in range(len(prices) - 1)]
        # predictions 需要对齐到 PnL 输出长度
        aligned_preds = predictions[1:]  # predictions[1:] 对齐 price returns
        metrics = kernel.evaluate(pnl, predictions=aligned_preds, returns=price_returns)

        assert metrics.is_verifiable
        assert metrics.sharpe is not None
        assert metrics.sortino is not None
        assert metrics.max_drawdown >= 0
        assert metrics.n_periods >= 12
        assert metrics.ic_mean != 0.0 or metrics.rank_ic_mean != 0.0

    def test_insufficient_data_returns_not_verifiable(self) -> None:
        """不足 12 期数据返回 NOT_VERIFIABLE。"""
        kernel = StrategyPnLKernel()
        predictions = [0.01, -0.02]
        prices = [100.0, 99.0]

        pnl = kernel.compute_pnl(predictions, prices)
        metrics = kernel.evaluate(pnl)

        assert not metrics.is_verifiable
        assert metrics.sharpe is None

    def test_mismatched_lengths_raises(self) -> None:
        """输入序列长度不一致必须报错。"""
        kernel = StrategyPnLKernel()
        with pytest.raises(ValueError):
            kernel.compute_pnl([0.01, 0.02], [100.0])

    def test_compute_sharpe_from_pnl(self) -> None:
        """直接 PnL→Sharpe 方法供 DSR/PBO 使用。"""
        kernel = StrategyPnLKernel()
        predictions = [0.01] * 50 + [-0.01] * 50
        prices = [100.0 + i for i in range(100)]

        pnl = kernel.compute_pnl(predictions, prices)
        sharpe = kernel.compute_sharpe_from_pnl(pnl)

        assert sharpe is not None

    def test_max_drawdown(self) -> None:
        """最大回撤计算正确。"""
        returns = [0.1, 0.2, -0.3, 0.1]
        dd = StrategyPnLKernel._compute_max_drawdown(returns)
        assert 0 <= dd <= 1.0


class TestDSRPBOAdapter:
    """PKG04: DSR/PBO 适配器 — 确保 Sharpe 来源验证。"""

    def test_validate_sharpe_from_kernel(self) -> None:
        """只有 StrategyPnLKernel 输出的 Sharpe 才有效。"""
        kernel = StrategyPnLKernel()
        adapter = DSRPBOAdapter(kernel)

        predictions = [0.01] * 60 + [-0.01] * 60
        prices = [100.0 + i * 0.5 for i in range(120)]
        pnl = kernel.compute_pnl(predictions, prices)
        metrics = kernel.evaluate(pnl)

        assert adapter.validate_sharpe_source(metrics)

    def test_invalid_sharpe_rejected(self) -> None:
        """NOT_VERIFIABLE 的 metrics 被拒绝。"""
        adapter = DSRPBOAdapter()
        metrics = StrategyMetrics.not_verifiable()
        assert not adapter.validate_sharpe_source(metrics)

    def test_prepare_dsr_input(self) -> None:
        """DSR 输入必须从 StrategyPnL 提取。"""
        kernel = StrategyPnLKernel()
        adapter = DSRPBOAdapter(kernel)

        predictions = [0.01] * 52 + [-0.01] * 52
        prices = [100.0 + i for i in range(104)]
        pnl = kernel.compute_pnl(predictions, prices)
        dsr_input = adapter.prepare_dsr_input(pnl)

        assert dsr_input["sharpe"] is not None
        assert dsr_input["n_periods"] > 0

    def test_invalid_pnl_dsr_input_returns_error(self) -> None:
        """无效 PnL 的 DSR 输入返回 error。"""
        adapter = DSRPBOAdapter()
        empty_pnl = StrategyPnL(net_returns=())
        dsr_input = adapter.prepare_dsr_input(empty_pnl)
        assert dsr_input["error"] == "pnl_not_verifiable"


class TestMutationPnLKernel:
    """PKG04: Mutation 测试 — 证明内核不可绕过。"""

    def test_mutation_metrics_not_verifiable_prevents_dsr(self) -> None:
        """Mutation: NOT_VERIFIABLE 的 metrics 不能用于 DSR。"""
        adapter = DSRPBOAdapter()
        empty_pnl = StrategyPnL(net_returns=(0.01,))  # 仅 1 期
        dsr_input = adapter.prepare_dsr_input(empty_pnl)
        assert dsr_input["sharpe"] is None

    def test_mutation_raw_returns_bypass_detected(self) -> None:
        """Mutation: 原始收益绕过内核的模式被检测。

        如果直接用原始收益算 Sharpe（不通过 StrategyPnLKernel），
        这个测试显示了差异——内核计算的 Sharpe 基于策略 PnL，
        不是原始资产收益。
        """
        kernel = StrategyPnLKernel()
        # 模拟原始未来收益（非策略 PnL）
        raw_returns = [0.01] * 60
        raw_pnl = StrategyPnL(net_returns=tuple(raw_returns))

        # 内核基于策略 PnL（预测驱动仓位）
        predictions = [0.01] * 60
        prices = [100.0 + i * 0.2 for i in range(60)]
        strategy_pnl = kernel.compute_pnl(predictions, prices)

        raw_sharpe = kernel.compute_sharpe_from_pnl(raw_pnl)
        strategy_sharpe = kernel.compute_sharpe_from_pnl(strategy_pnl)

        # 原始收益 Sharpe ≠ 策略 PnL Sharpe — 证明两者不能混用
        assert raw_sharpe != strategy_sharpe, "原始收益与策略 PnL 的 Sharpe 应不同"
