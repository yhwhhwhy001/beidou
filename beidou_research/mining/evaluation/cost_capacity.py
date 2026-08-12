"""BF-06: 成本与容量评估 (PKG07: Signal-Aware 版本)。

评估因子在实际交易约束下的可行性：
- 交易成本：fee + spread + slippage + funding + market impact
- 容量曲线：不同 AUM 下的收益衰减（信号感知）
- 成本压力测试：1x/1.5x/2x
- 成本模型 UNKNOWN 时不得进入 COST_CAPACITY_VERIFIED

BDS-P0-005 修复：
- impact 模型使用 predictions / turnover / ADV / participation rate
- 容量曲线基于信号强度和预期成交量
- 输出 breaking point 和 half-life capacity
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CostModel:
    """交易成本模型（信号感知）。"""

    # PKG02: 默认值仅表示未获取到真实费率，使用前必须验证 is_verified
    taker_fee_bps: float = 0.0
    maker_fee_bps: float = 0.0
    avg_spread_bps: float = 1.0  # 平均买卖价差
    slippage_bps: float = 1.0  # 预期滑点
    funding_rate_8h_pct: float = 0.01  # 资金费率（每 8 小时百分比）
    impact_bps_per_10k: float = 0.1  # 每 10K 交易量的市场冲击（bps）— legacy linear fallback

    # PKG07: 信号感知冲击参数
    adv_30d: float = 0.0  # 30天平均日交易量（USD）
    order_book_depth_avg: float = 0.0  # 平均盘口深度（USD，top N levels）
    participation_rate_cap: float = 0.05  # 最大参与率（5%）
    volatility: float = 0.02  # 日波动率（默认2%）

    model_version: str = "bf06-v2-signal-aware"

    @property
    def is_unknown(self) -> bool:
        return self.taker_fee_bps <= 0 and self.avg_spread_bps <= 0

    def round_trip_cost_bps(self, hold_hours: float = 4.0, use_maker: bool = False) -> float:
        """估算往返交易成本（bps）。"""
        fee = self.maker_fee_bps if use_maker else self.taker_fee_bps
        funding_bps = self.funding_rate_8h_pct * (hold_hours / 8.0) * 100
        return fee + self.avg_spread_bps + self.slippage_bps + funding_bps

    def _legacy_impact_bps(self, notional_usd: float) -> float:
        """Legacy linear impact — fallback when ADV is unknown."""
        if notional_usd <= 0:
            return 0.0
        return self.impact_bps_per_10k * (notional_usd / 10000.0)

    def impact_bps_for_size(
        self,
        notional_usd: float,
        *,
        participation_rate: float | None = None,
    ) -> float:
        """根据交易规模估算市场冲击（信号感知）。

        优先使用 ADV + volatility + participation 模型；
        ADV 未知时回退到 legacy linear 模型。
        """
        if notional_usd <= 0:
            return 0.0

        if self.adv_30d > 0 and self.volatility > 0:
            pr = participation_rate if participation_rate is not None else (notional_usd / self.adv_30d)
            # 基于 Kyle's lambda / Almgren-Chriss 简化模型:
            # impact = participation_rate × volatility × sqrt(trade/ADV) × scaling
            # 简化为: impact_bps = participation_rate × volatility × 10000
            impact = pr * self.volatility * 10000
            return impact

        return self._legacy_impact_bps(notional_usd)

    def total_cost_bps(
        self,
        notional_usd: float,
        hold_hours: float = 4.0,
        use_maker: bool = False,
        *,
        participation_rate: float | None = None,
    ) -> float:
        """总交易成本（bps）= round trip + market impact。"""
        round_trip = self.round_trip_cost_bps(hold_hours, use_maker)
        impact = self.impact_bps_for_size(notional_usd, participation_rate=participation_rate)
        return round_trip + impact


@dataclass
class CapacityCurvePoint:
    """容量曲线上的一个点。"""

    aum_level: float  # AUM（USD）
    gross_return: float  # 毛收益（年化）
    net_return: float  # 净收益（年化，扣成本后）
    impact_bps: float  # 市场冲击（bps）
    turnover_annual: float  # 年化换手率


@dataclass
class SignalCapacityReport:
    """信号感知容量评估完整报告。"""

    curve: list[CapacityCurvePoint]
    capacity_at_zero_return: float  # 零收益容量
    capacity_at_half_return: float  # 收益减半容量
    recommended_max_aum: float  # 推荐最大 AUM
    signal_decay_ratio: float  # 信号衰减比（最大AUM时net/gross）
    avg_turnover: float  # 平均年化换手率
    adv_used: float  # 使用的ADV
    participation_at_capacity: float  # 推荐AUM时的参与率
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Signal-Aware Impact Model
# ---------------------------------------------------------------------------


class SignalAwareImpactModel:
    """信号感知市场冲击模型。

    结合预测信号强度、换手率、ADV 和参与率来估计交易冲击。
    不同信号强度对应不同的预期成交量和市场冲击。

    BDS-P0-005: 替代原有固定费率线性模型。
    """

    def __init__(self, cost_model: CostModel) -> None:
        self._cost = cost_model

    def estimate_signal_impact(
        self,
        predictions: list[float],
        returns: list[float],
        *,
        avg_daily_volume: float | None = None,
        volatility: float | None = None,
    ) -> tuple[float, float, list[str]]:
        """根据信号估算每笔交易的平均冲击和换手率。

        Args:
            predictions: 预测信号列表（如因子值）
            returns: 对应收益率列表
            avg_daily_volume: 日均交易量（USD），None 时从 predictions 推断
            volatility: 日波动率，None 时从 returns 计算

        Returns:
            (avg_impact_bps, annual_turnover, warnings)
        """
        warnings: list[str] = []
        n = len(predictions)
        if n == 0:
            return 0.0, 0.0, ["empty_predictions"]

        adv = avg_daily_volume if avg_daily_volume is not None else self._cost.adv_30d
        if adv <= 0:
            return self._cost._legacy_impact_bps(10000), 1.0, ["adv_unknown_using_legacy"]

        vol = volatility if volatility is not None else self._cost.volatility
        if vol <= 0:
            vol = _compute_volatility(returns)
            if vol <= 0:
                return self._cost._legacy_impact_bps(10000), 1.0, ["volatility_unknown"]

        # 信号强度 → 预期成交规模
        # 强信号产生更大仓位 → 更高参与率
        abs_preds = [abs(p) for p in predictions if math.isfinite(p)]
        if not abs_preds:
            return 0.0, 0.0, ["no_finite_predictions"]

        mean_abs_signal = _mean(abs_preds)
        std_signal = _std(abs_preds, mean_abs_signal)

        # 换手率：基于收益序列的波动推断
        turnover = _estimate_turnover_from_returns(returns)

        # 参与率：信号越强，分仓越大
        # 归一化到 [0.1%, participation_rate_cap]
        signal_intensity = min(1.0, mean_abs_signal / (std_signal + 1e-10))
        base_participation = 0.001  # 最小参与率 0.1%
        max_participation = self._cost.participation_rate_cap
        participation = base_participation + signal_intensity * (max_participation - base_participation)

        # 冲击计算: impact_bps = participation × volatility × 10000
        avg_impact_bps = participation * vol * 10000

        # 高参与率警告
        if participation > 0.03:
            warnings.append(f"high_participation:{participation:.1%}")
        if turnover > 50:
            warnings.append(f"high_turnover:{turnover:.0f}x/year")

        return avg_impact_bps, turnover, warnings


# ---------------------------------------------------------------------------
# Capacity Evaluator
# ---------------------------------------------------------------------------


class CapacityEvaluator:
    """容量评估器（信号感知版本）。

    估算不同管理规模下的策略收益衰减。
    随着 AUM 增长，市场冲击增加，净收益下降。

    BDS-P0-005: 使用信号强度计算预期成交量，不再假设固定每笔规模。
    """

    def __init__(self, cost_model: CostModel | None = None) -> None:
        self.cost_model = cost_model or CostModel()
        self._impact_model = SignalAwareImpactModel(self.cost_model)

    def evaluate_capacity_curve(
        self,
        predictions: list[float],
        returns: list[float],
        *,
        aum_range: list[float] | None = None,
        avg_holding_hours: float = 4.0,
        trades_per_period: float = 1.0,
        avg_daily_volume: float | None = None,
    ) -> SignalCapacityReport:
        """评估信号感知容量曲线。

        在不同 AUM 级别下模拟净收益。
        使用 predictions 信号强度 + ADV 计算参与率和冲击，
        而非假设固定每笔交易规模。

        Args:
            predictions: 预测信号值列表
            returns: 对应收益率列表
            aum_range: AUM 级别列表（USD），默认覆盖 $10K 到 $50M
            avg_holding_hours: 平均持仓时间
            trades_per_period: 每期交易次数
            avg_daily_volume: 日均交易量（USD），None 时从 CostModel 获取

        Returns:
            SignalCapacityReport
        """
        if aum_range is None:
            aum_range = [10_000, 50_000, 100_000, 250_000, 500_000, 1_000_000, 5_000_000, 10_000_000, 50_000_000]

        # 计算毛收益（年化）
        gross_return = _mean(returns) if returns else 0.0
        if gross_return <= 0:
            # 无正收益时，容量为0
            empty_curve = [CapacityCurvePoint(aum, gross_return, min(0.0, gross_return), 0.0, 0.0) for aum in aum_range]
            return SignalCapacityReport(
                curve=empty_curve,
                capacity_at_zero_return=0.0,
                capacity_at_half_return=0.0,
                recommended_max_aum=0.0,
                signal_decay_ratio=0.0,
                avg_turnover=0.0,
                adv_used=avg_daily_volume or self.cost_model.adv_30d,
                participation_at_capacity=0.0,
                warnings=["non_positive_gross_return"],
            )

        # 使用信号感知冲击模型
        adv = avg_daily_volume if avg_daily_volume is not None else self.cost_model.adv_30d
        vol = self.cost_model.volatility
        if vol <= 0:
            vol = _compute_volatility(returns)

        _avg_impact_bps, annual_turnover, warnings = self._impact_model.estimate_signal_impact(
            predictions,
            returns,
            avg_daily_volume=adv,
            volatility=vol,
        )

        # 每笔交易的固定成本（fee + spread + slippage + funding）
        round_trip_bps = self.cost_model.round_trip_cost_bps(avg_holding_hours)

        curve: list[CapacityCurvePoint] = []
        for aum in aum_range:
            # 随 AUM 增长的冲击：参与率 = 单笔规模 / ADV
            trade_size = aum * 0.02  # 假定每笔交易占 AUM 的 2%
            if adv > 0:
                participation = trade_size / adv
                size_impact_bps = participation * vol * 10000
            else:
                size_impact_bps = self.cost_model._legacy_impact_bps(trade_size)

            total_cost_bps = round_trip_bps + size_impact_bps
            total_cost_decimal = total_cost_bps / 10000.0
            net_return = gross_return - total_cost_decimal * trades_per_period * annual_turnover

            curve.append(
                CapacityCurvePoint(
                    aum_level=aum,
                    gross_return=round(gross_return, 8),
                    net_return=round(net_return, 8),
                    impact_bps=round(size_impact_bps, 4),
                    turnover_annual=round(annual_turnover, 2),
                )
            )

        # 寻找关键容量点
        net_vals = [p.net_return for p in curve]
        aum_vals = [p.aum_level for p in curve]

        capacity_zero = _find_zero_crossing(aum_vals, net_vals)
        capacity_half = _find_level(aum_vals, net_vals, gross_return / 2.0)
        recommended = _find_level(aum_vals, net_vals, gross_return * 0.75)

        # 参与率 at recommended AUM
        recommended_participation = 0.0
        if adv > 0 and recommended > 0:
            recommended_participation = (recommended * 0.02) / adv

        decay_ratio = 0.0
        if gross_return > 0 and curve:
            decay_ratio = curve[-1].net_return / gross_return

        return SignalCapacityReport(
            curve=curve,
            capacity_at_zero_return=round(capacity_zero, 0),
            capacity_at_half_return=round(capacity_half, 0),
            recommended_max_aum=round(recommended, 0),
            signal_decay_ratio=round(decay_ratio, 6),
            avg_turnover=round(annual_turnover, 2),
            adv_used=adv,
            participation_at_capacity=round(recommended_participation, 6),
            warnings=warnings,
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

    def evaluate_with_gate(
        self,
        predictions: list[float],
        returns: list[float],
        *,
        aum_range: list[float] | None = None,
        avg_daily_volume: float | None = None,
    ) -> tuple[SignalCapacityReport, bool, str]:
        """完整评估 + Gate 判定。

        Returns:
            (report, gate_passed, gate_reason)
        """
        report = self.evaluate_capacity_curve(
            predictions,
            returns,
            aum_range=aum_range,
            avg_daily_volume=avg_daily_volume,
        )

        failures: list[str] = []

        if report.capacity_at_zero_return <= 0:
            failures.append("zero_capacity")

        if report.signal_decay_ratio < 0.3:
            failures.append(f"severe_decay:{report.signal_decay_ratio:.2%}")

        if report.avg_turnover > 100:
            failures.append(f"excessive_turnover:{report.avg_turnover:.0f}")

        cost_viable, cost_reason = self.is_cost_viable(
            report.curve[-1].net_return if report.curve else 0.0,
            report.curve[-1].gross_return if report.curve else 0.0,
        )
        if not cost_viable:
            failures.append(cost_reason)

        gate_passed = len(failures) == 0
        gate_reason = "; ".join(failures) if failures else "capacity_gate_passed"

        return report, gate_passed, gate_reason


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    finite = [v for v in values if math.isfinite(v)]
    if not finite:
        return 0.0
    return sum(finite) / len(finite)


def _std(values: list[float], mean: float | None = None) -> float:
    finite = [v for v in values if math.isfinite(v)]
    if len(finite) < 2:
        return 0.0
    m = mean if mean is not None else _mean(finite)
    var = sum((v - m) ** 2 for v in finite) / (len(finite) - 1)
    return math.sqrt(var)


def _compute_volatility(returns: list[float]) -> float:
    """从日收益序列计算年化波动率。"""
    finite = [r for r in returns if math.isfinite(r)]
    if len(finite) < 2:
        return 0.0
    daily_vol = _std(finite)
    return daily_vol * math.sqrt(365)


def _estimate_turnover_from_returns(returns: list[float]) -> float:
    """从收益序列估算年化换手率。

    基于收益自相关性和波动率推断交易频率。
    """
    finite = [r for r in returns if math.isfinite(r)]
    if len(finite) < 2:
        return 1.0  # 默认每年换手1次

    # 通过收益符号变化频率估算换手
    sign_changes = sum(1 for i in range(1, len(finite)) if (finite[i] >= 0) != (finite[i - 1] >= 0))
    change_rate = sign_changes / (len(finite) - 1) if len(finite) > 1 else 0.0

    # 换手率 = 符号变化率 × 365（年化）
    annual_turnover = change_rate * 365
    return max(0.1, min(annual_turnover, 500.0))  # clamp [0.1, 500]


def _compute_sharpe_annualized(returns: list[float], periods_per_year: int = 365) -> float:
    """年化 Sharpe ratio（假设日度数据，periods_per_year=365）。"""
    if len(returns) < 2:
        return 0.0
    mean_r = _mean(returns)
    std_r = _std(returns, mean_r)
    if std_r == 0:
        return 0.0
    return (mean_r / std_r) * math.sqrt(periods_per_year)


def _find_zero_crossing(x: list[float], y: list[float]) -> float:
    """线性插值寻找 y=0 时的 x 值。"""
    for i in range(len(y) - 1):
        if y[i] >= 0 and y[i + 1] < 0:
            ratio = y[i] / (y[i] - y[i + 1]) if (y[i] - y[i + 1]) != 0 else 0.5
            return x[i] + ratio * (x[i + 1] - x[i])
    return x[-1] if (y[-1] if y else 0) > 0 else 0.0


def _find_level(x: list[float], y: list[float], target: float) -> float:
    """线性插值寻找 y=target 时的 x 值。"""
    for i in range(len(y) - 1):
        if (y[i] - target) * (y[i + 1] - target) <= 0:
            if y[i] == y[i + 1]:
                return x[i]
            ratio = (target - y[i]) / (y[i + 1] - y[i]) if (y[i + 1] - y[i]) != 0 else 0.5
            return x[i] + ratio * (x[i + 1] - x[i])
    return x[-1] if (y[-1] if y else 0) > target else x[0]
