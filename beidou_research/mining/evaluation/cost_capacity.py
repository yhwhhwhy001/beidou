"""BF-06: 成本与容量评估。

评估因子在实际交易约束下的可行性：
- 交易成本：fee + spread + slippage + funding + market impact
- 容量曲线：不同 AUM 下的收益衰减
- 成本压力测试：1x/1.5x/2x
- 成本模型 UNKNOWN 时不得进入 COST_CAPACITY_VERIFIED
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CostModel:
    """交易成本模型。"""

    taker_fee_bps: float = 4.0  # Taker 费率（VIP1: 2bps × 2 = 4bps round trip）
    maker_fee_bps: float = 2.0  # Maker 费率
    avg_spread_bps: float = 1.0  # 平均买卖价差
    slippage_bps: float = 1.0  # 预期滑点
    funding_rate_8h_pct: float = 0.01  # 资金费率（每 8 小时百分比）
    impact_bps_per_10k: float = 0.1  # 每 10K 交易量的市场冲击（bps）
    model_version: str = "bf06-v1"

    @property
    def is_unknown(self) -> bool:
        return self.taker_fee_bps <= 0 and self.avg_spread_bps <= 0

    def round_trip_cost_bps(self, hold_hours: float = 4.0, use_maker: bool = False) -> float:
        """估算往返交易成本（bps）。

        Args:
            hold_hours: 持有时间（小时）
            use_maker: 是否使用 maker 费率
        """
        fee = self.maker_fee_bps if use_maker else self.taker_fee_bps
        funding_bps = self.funding_rate_8h_pct * (hold_hours / 8.0) * 100
        return fee + self.avg_spread_bps + self.slippage_bps + funding_bps

    def impact_bps_for_size(self, notional_usd: float) -> float:
        """根据交易规模估算市场冲击。"""
        if notional_usd <= 0:
            return 0.0
        units_of_10k = notional_usd / 10000.0
        return self.impact_bps_per_10k * units_of_10k

    def total_cost_bps(
        self,
        notional_usd: float,
        hold_hours: float = 4.0,
        use_maker: bool = False,
    ) -> float:
        """总交易成本（bps）= round trip + market impact。"""
        round_trip = self.round_trip_cost_bps(hold_hours, use_maker)
        impact = self.impact_bps_for_size(notional_usd)
        return round_trip + impact


@dataclass
class CapacityResult:
    """容量评估结果。"""

    aum_levels: list[float]  # AUM 级别（USD）
    net_returns: list[float]  # 对应净收益
    gross_returns: list[float]  # 对应毛收益
    capacity_at_zero_return: float  # 零收益容量
    capacity_at_half_return: float  # 收益减半容量
    recommended_max_aum: float  # 推荐最大 AUM


class CapacityEvaluator:
    """容量评估器。

    估算不同管理规模下的策略收益衰减。
    随着 AUM 增长，市场冲击增加，净收益下降。
    """

    def __init__(self, cost_model: CostModel | None = None) -> None:
        self.cost_model = cost_model or CostModel()

    def evaluate_capacity_curve(
        self,
        predictions: list[float],
        returns: list[float],
        *,
        aum_range: list[float] | None = None,
        avg_holding_hours: float = 4.0,
        trades_per_period: float = 1.0,
    ) -> CapacityResult:
        """评估容量曲线。

        在不同 AUM 级别下模拟净收益（考虑市场冲击）。
        """
        if aum_range is None:
            aum_range = [10_000, 50_000, 100_000, 250_000, 500_000, 1_000_000, 5_000_000]

        gross_return = _mean(returns) if returns else 0.0
        _compute_sharpe_annualized(returns)

        net_returns = []
        gross_returns_list = []
        for aum in aum_range:
            impact_bps = self.cost_model.impact_bps_for_size(aum)
            round_trip_bps = self.cost_model.round_trip_cost_bps(avg_holding_hours)
            total_cost_bps = round_trip_bps + impact_bps
            total_cost_decimal = total_cost_bps / 10000.0
            net_return = gross_return - total_cost_decimal * trades_per_period
            net_returns.append(net_return)
            gross_returns_list.append(gross_return)

        # 寻找零收益容量
        capacity_zero = _find_zero_crossing(aum_range, net_returns)

        # 收益减半容量
        half_gross = gross_return / 2.0 if gross_return > 0 else 0.0
        capacity_half = _find_level(aum_range, net_returns, half_gross)

        # 推荐 AUM：收益降到原始的 75%
        recommended = _find_level(aum_range, net_returns, gross_return * 0.75)

        return CapacityResult(
            aum_levels=aum_range,
            net_returns=[round(nr, 8) for nr in net_returns],
            gross_returns=[round(gr, 8) for gr in gross_returns_list],
            capacity_at_zero_return=round(capacity_zero, 0),
            capacity_at_half_return=round(capacity_half, 0),
            recommended_max_aum=round(recommended, 0),
        )

    def is_cost_viable(
        self,
        net_return: float,
        gross_return: float,
    ) -> tuple[bool, str]:
        """检查扣除成本后策略是否仍然可行。

        UNKNOWN 成本模型时返回失败。
        """
        if self.cost_model.is_unknown:
            return False, "cost_model_unknown"

        if net_return <= 0:
            return False, f"negative_net_return: {net_return:.6f}"

        # 收益衰减不超过 50%
        if gross_return > 0 and net_return < gross_return * 0.5:
            return False, f"severe_cost_decay: net={net_return:.6f} gross={gross_return:.6f}"

        return True, "cost_viable"


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _compute_sharpe_annualized(returns: list[float], periods_per_year: int = 365) -> float:
    """年化 Sharpe ratio（假设日度数据，periods_per_year=365）。"""
    if len(returns) < 2:
        return 0.0
    mean_r = _mean(returns)
    var = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
    std = var**0.5
    if std == 0:
        return 0.0
    return (mean_r / std) * (periods_per_year**0.5)


def _find_zero_crossing(x: list[float], y: list[float]) -> float:
    """线性插值寻找 y=0 时的 x 值。"""
    for i in range(len(y) - 1):
        if y[i] >= 0 and y[i + 1] < 0:
            # y=0 between i and i+1
            ratio = y[i] / (y[i] - y[i + 1]) if (y[i] - y[i + 1]) != 0 else 0.5
            return x[i] + ratio * (x[i + 1] - x[i])
    # 未找到零点
    return x[-1] if y[-1] > 0 else 0.0


def _find_level(x: list[float], y: list[float], target: float) -> float:
    """线性插值寻找 y=target 时的 x 值。"""
    for i in range(len(y) - 1):
        if (y[i] - target) * (y[i + 1] - target) <= 0:
            if y[i] == y[i + 1]:
                return x[i]
            ratio = (target - y[i]) / (y[i + 1] - y[i])
            return x[i] + ratio * (x[i + 1] - x[i])
    # 未找到
    return x[-1] if y[-1] > target else x[0]
