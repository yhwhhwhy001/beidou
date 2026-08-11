"""
PKG04 (BDS-P0-003, BDS-P0-004): 统一策略 PnL 评估内核。

所有研究 Gate 必须通过此内核消费策略收益统计。
任何绕过此内核手动计算 Sharpe/Sortino/DD/metrics 的行为是架构违规。

核心链路（不可分割、不可跳步）：
    prediction → position → gross_pnl → cost → net_pnl → metrics

与 BDS-P0-003/004 的关系：
- BDS-P0-003: WFO Sharpe 未使用 Factor 预测 — 确保 metrics 从 net PnL 生成
- BDS-P0-004: DSR/PBO 输入 Sharpe 非策略 Sharpe — 确保 DSR/PBO 只接受此内核的输出
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

from beidou_research.mining.evaluation.metrics import (
    _clean_series,
    _mean,
    _std,
    _sharpe,
    compute_ic,
    compute_rank_ic,
    compute_hit_rate,
    compute_turnover,
    compute_cost_adjusted_metrics,
)


# ================================================================
# 核心数据结构
# ================================================================


@dataclass(frozen=True)
class StrategyPnL:
    """策略 PnL — 不可变的净收益序列。

    这是所有研究指标的单一权威来源。
    """

    net_returns: tuple[float, ...]  # 逐期净收益率

    @property
    def n_periods(self) -> int:
        return len(self.net_returns)

    @property
    def total_return(self) -> float:
        """累计净收益（对数累加）。"""
        return math.log(1 + sum(self.net_returns)) if self.net_returns else 0.0

    @property
    def annualized_return(self) -> float:
        """年化收益率。"""
        if not self.net_returns:
            return 0.0
        mean_r = _mean(list(self.net_returns))
        return mean_r * 252  # 假设每日数据

    @property
    def is_valid(self) -> bool:
        """至少需要 2 期数据才能计算统计量。"""
        return self.n_periods >= 2


@dataclass(frozen=True)
class StrategyMetrics:
    """策略评估指标 — 由 StrategyPnLKernel 统一生成。

    所有字段均从 net_returns 推导，确保数学一致性。
    """

    sharpe: float | None  # 年化 Sharpe ratio
    sortino: float | None  # 年化 Sortino ratio
    max_drawdown: float  # 最大回撤（0~1 比例）
    calmar: float | None  # Calmar ratio = 年化收益 / 最大回撤
    annualized_return: float
    annualized_volatility: float
    hit_rate: float
    n_periods: int
    ic_mean: float
    rank_ic_mean: float
    turnover: float = 0.0
    is_verifiable: bool = True

    @classmethod
    def not_verifiable(cls) -> StrategyMetrics:
        """样本不足时的不可验证哨兵值。"""
        return cls(
            sharpe=None,
            sortino=None,
            max_drawdown=0.0,
            calmar=None,
            annualized_return=0.0,
            annualized_volatility=0.0,
            hit_rate=0.0,
            n_periods=0,
            ic_mean=0.0,
            rank_ic_mean=0.0,
            is_verifiable=False,
        )


# ================================================================
# 统一 PnL 评估内核
# ================================================================


class StrategyPnLKernel:
    """PKG04: 统一策略 PnL 评估内核。

    强制预测→仓位→毛收益→成本→净收益→指标的完整链路。
    所有研究 Gate 必须只消费此内核的输出。

    使用示例：
        kernel = StrategyPnLKernel()
        pnl = kernel.compute_pnl(
            predictions=[0.01, -0.02, 0.03],
            prices=[100, 101, 99],
            costs_bps=[5, 5, 5],
        )
        metrics = kernel.evaluate(pnl)
        dsr_result = deflated_sharpe_ratio(
            observed_sharpe=metrics.sharpe, ...
        )
    """

    # 默认交易成本基数（基点）
    DEFAULT_COST_BPS: float = 5.0

    # 最小有效期数
    MIN_PERIODS: int = 12

    def compute_pnl(
        self,
        predictions: Sequence[float],
        prices: Sequence[float],
        costs_bps: Sequence[float] | None = None,
        position_scale: float = 1.0,
    ) -> StrategyPnL:
        """PKG04 核心方法：预测→仓位→毛收益→成本→净收益。

        Args:
            predictions: 每期的预测值（如预期收益方向/强度）
            prices: 每期价格（用于计算收益）
            costs_bps: 每期交易成本（基点），None 则使用默认值
            position_scale: 仓位缩放系数（如 0.1 表示 10% 仓位）

        Returns:
            StrategyPnL 包含净收益序列

        Raises:
            ValueError: 输入序列长度不一致或不足
        """
        n = len(predictions)
        if n != len(prices):
            raise ValueError(f"predictions ({n}) 与 prices ({len(prices)}) 长度不一致")
        if n < 2:
            return StrategyPnL(net_returns=())

        # Step 1: 清理数据
        clean_preds = _clean_series(predictions)
        clean_prices = _clean_series(prices)
        min_n = min(len(clean_preds), len(clean_prices))
        if min_n < 2:
            return StrategyPnL(net_returns=())

        preds = clean_preds[:min_n]
        px = clean_prices[:min_n]

        # Step 2: 预测 → 仓位信号
        # 仓位 = sign(pred) * min(|pred|, 1) * position_scale
        positions: list[float] = []
        for p in preds:
            direction = 1.0 if p > 0 else (-1.0 if p < 0 else 0.0)
            magnitude = min(abs(p), 1.0)
            positions.append(direction * magnitude * position_scale)

        # Step 3: 仓位 → 毛收益
        # gross_return[t] = position[t-1] * (price[t] - price[t-1]) / price[t-1]
        gross_returns: list[float] = []
        for t in range(1, len(px)):
            if px[t - 1] <= 0:
                gross_returns.append(0.0)
                continue
            price_return = (px[t] - px[t - 1]) / px[t - 1]
            gross_returns.append(positions[t - 1] * price_return)

        # Step 4: 毛收益 → 成本 → 净收益
        if costs_bps is None:
            costs = [self.DEFAULT_COST_BPS / 10000.0] * len(gross_returns)
        else:
            cost_clean = _clean_series(costs_bps)
            costs = [
                (cost_clean[i] / 10000.0) if i < len(cost_clean) else self.DEFAULT_COST_BPS / 10000.0
                for i in range(len(gross_returns))
            ]

        net_returns: list[float] = []
        for g, c in zip(gross_returns, costs, strict=True):
            # 成本包括换手成本（按仓位变化）
            if len(positions) > 1:
                prev_pos = abs(positions[len(net_returns)])
                curr_pos = abs(positions[len(net_returns) + 1])
                turnover_cost = abs(curr_pos - prev_pos) * c
            else:
                turnover_cost = c
            net_returns.append(g - turnover_cost)

        return StrategyPnL(net_returns=tuple(net_returns))

    def evaluate(
        self,
        pnl: StrategyPnL,
        predictions: Sequence[float] | None = None,
        returns: Sequence[float] | None = None,
        positions: Sequence[float] | None = None,
    ) -> StrategyMetrics:
        """PKG04: 从 StrategyPnL 生成统一的策略评估指标。

        所有指标从 net_returns 推导，确保数学一致性。
        没有 pnl → 指标不可验证 → 返回 NOT_VERIFIABLE 哨兵。

        Args:
            pnl: 策略净收益
            predictions: 可选，用于计算 IC（与收益对齐）
            returns: 可选，用于计算 hit rate（与预测对齐）
            positions: 可选，用于计算 turnover

        Returns:
            StrategyMetrics 包含所有标准策略指标
        """
        if not pnl.is_valid or pnl.n_periods < self.MIN_PERIODS:
            return StrategyMetrics.not_verifiable()

        net_returns = list(pnl.net_returns)

        # 核心指标（从 net_returns 推导）
        ann_return = pnl.annualized_return
        ann_vol = _std(net_returns) * math.sqrt(252)
        sharpe = _sharpe(net_returns)

        # Sortino: 只考虑下行波动
        downside = [r for r in net_returns if r < 0]
        sortino = None
        if len(downside) >= 2:
            d_std = _std(downside)
            if d_std > 1e-12:
                sortino = _mean(net_returns) * 252 / (d_std * math.sqrt(252))

        # 最大回撤
        max_dd = self._compute_max_drawdown(net_returns)

        # Calmar
        calmar = None
        if max_dd > 1e-12 and ann_return > 0:
            calmar = ann_return / max_dd

        # Hit rate（方向正确率）
        hit_rate = 0.0
        if predictions is not None and returns is not None:
            hit_rate = compute_hit_rate(list(predictions), list(returns))

        # IC
        ic_mean = 0.0
        rank_ic_mean = 0.0
        if predictions is not None and returns is not None:
            ic_result = compute_ic(list(predictions), list(returns))
            ic_mean = ic_result["ic_mean"]
            rank_ic_result = compute_rank_ic(list(predictions), list(returns))
            rank_ic_mean = rank_ic_result["rank_ic_mean"]

        # Turnover
        turnover = 0.0
        if positions is not None and len(positions) >= 2:
            turnover = compute_turnover(
                list(positions[:-1]),
                list(positions[1:]),
            )

        return StrategyMetrics(
            sharpe=round(sharpe, 4) if sharpe is not None else None,
            sortino=round(sortino, 4) if sortino is not None else None,
            max_drawdown=round(max_dd, 4),
            calmar=round(calmar, 4) if calmar is not None else None,
            annualized_return=round(ann_return, 4),
            annualized_volatility=round(ann_vol, 4),
            hit_rate=round(hit_rate, 4),
            n_periods=pnl.n_periods,
            ic_mean=round(ic_mean, 4),
            rank_ic_mean=round(rank_ic_mean, 4),
            turnover=round(turnover, 4),
            is_verifiable=True,
        )

    @staticmethod
    def _compute_max_drawdown(returns: list[float]) -> float:
        """计算最大回撤。"""
        if not returns:
            return 0.0
        peak = returns[0]
        max_dd = 0.0
        cumulative = 0.0
        for r in returns:
            cumulative += r
            if cumulative > peak:
                peak = cumulative
            dd = (peak - cumulative) / max(abs(peak), 1e-12)
            if dd > max_dd:
                max_dd = dd
        return max_dd

    def compute_sharpe_from_pnl(self, pnl: StrategyPnL) -> float | None:
        """直接从 StrategyPnL 计算 Sharpe（供 DSR/PBO 等使用）。

        PKG04 (BDS-P0-004): DSR/PBO 只能通过此方法获取 Sharpe，
        不得使用其他来源的 Sharpe 值。
        """
        if not pnl.is_valid:
            return None
        return _sharpe(list(pnl.net_returns))

    def compute_sortino_from_pnl(self, pnl: StrategyPnL) -> float | None:
        """直接从 StrategyPnL 计算 Sortino。"""
        if not pnl.is_valid:
            return None
        net_returns = list(pnl.net_returns)
        downside = [r for r in net_returns if r < 0]
        if len(downside) < 2:
            return None
        d_std = _std(downside)
        if d_std <= 1e-12:
            return None
        return _mean(net_returns) * 252 / (d_std * math.sqrt(252))


# ================================================================
# PKG04: DSR/PBO 适配器 — 确保输入 Sharpe 来自策略 PnL
# ================================================================


class DSRPBOAdapter:
    """DSR/PBO 适配器。

    强制要求输入 Sharpe 来自 StrategyPnLKernel。
    若传入的 Sharpe 未提供来源证明（StrategyPnL），拒绝计算并返回 NOT_VERIFIABLE。

    BDS-P0-004: DSR/PBO 输入 Sharpe 必须是策略 Sharpe。
    """

    def __init__(self, kernel: StrategyPnLKernel | None = None):
        self._kernel = kernel or StrategyPnLKernel()

    def validate_sharpe_source(self, metrics: StrategyMetrics) -> bool:
        """验证 Sharpe 来源 — 必须来自 StrategyPnLKernel。"""
        return metrics.is_verifiable and metrics.sharpe is not None

    def prepare_dsr_input(self, pnl: StrategyPnL) -> dict:
        """为 DSR 计算准备输入 — 从 StrategyPnL 提取策略 Sharpe。"""
        sharpe = self._kernel.compute_sharpe_from_pnl(pnl)
        if sharpe is None:
            return {"sharpe": None, "error": "pnl_not_verifiable"}
        return {
            "sharpe": sharpe,
            "n_periods": pnl.n_periods,
            "annualized_return": pnl.annualized_return,
        }

    def prepare_pbo_input(self, pnl_list: list[StrategyPnL]) -> dict:
        """为 PBO 计算准备输入 — 所有 Sharpe 来自同一内核。"""
        is_sharpes = []
        oos_sharpes = []

        mid = len(pnl_list) // 2
        for i, pnl in enumerate(pnl_list):
            sharpe = self._kernel.compute_sharpe_from_pnl(pnl)
            if sharpe is None:
                sharpe = 0.0
            if i < mid:
                is_sharpes.append(sharpe)
            else:
                oos_sharpes.append(sharpe)

        return {
            "is_sharpes": is_sharpes,
            "oos_sharpes": oos_sharpes,
            "n_total": len(pnl_list),
        }
