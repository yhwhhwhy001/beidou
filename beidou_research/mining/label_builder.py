"""BF-01: 标签构造器。

从价格序列构造因子评估标签：
1. 基础标签：forward log return - expected cost
2. 可选：triple-barrier, residual return, cross-sectional rank return
3. 成本调整：fee + spread + slippage + funding
4. 标签质量标记：MISSING_PRICE, OVERLAPPING, FUTURE_LEAK, STALE
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from beidou_shared.types import (
    DataQualityTier,
    FactorId,
    InstrumentId,
    SchemaVersion,
    VenueId,
)

from .contracts import (
    HorizonUnit,
    LabelQuality,
    LabelRecord,
    LabelSpec,
    PredictionKey,
    PriceType,
    ReturnType,
)

# ================================================================
# 价格提供者接口
# ================================================================


@dataclass
class PricePoint:
    """单个价格点。"""

    venue: VenueId
    symbol: InstrumentId
    timeframe: str
    timestamp: datetime
    close: float
    mark: float | None = None
    mid: float | None = None
    vwap: float | None = None
    # Feature columns must be supplied by the dataset; the mining runner must
    # not manufacture OHLCV values from close/zero when they are absent.
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    # Missing close-state metadata must not silently become usable market data.
    # Callers that cannot prove a bar is closed are rejected by LabelBuilder.
    is_closed: bool = False

    def get_price(self, price_type: PriceType) -> float | None:
        """根据价格类型获取价格。"""
        mapping = {
            PriceType.CLOSE: self.close,
            PriceType.MARK: self.mark,
            PriceType.MID: self.mid,
            PriceType.VWAP: self.vwap,
        }
        val = mapping.get(price_type)
        # A requested venue price type is an independent fact.  Falling back
        # to close would silently turn a missing mark/mid/VWAP into a valid
        # label and can introduce an unobservable execution assumption.
        return val


# ================================================================
# 成本模型
# ================================================================


@dataclass(frozen=True)
class CostEstimate:
    """往返交易成本估算（bps）。"""

    fee_bps: float = 0.0  # 交易手续费
    spread_bps: float = 0.0  # 买卖价差
    slippage_bps: float = 0.0  # 滑点
    funding_bps: float = 0.0  # 资金费率（按持有期估算）
    total_bps: float = 0.0  # 总成本

    @classmethod
    def default_perpetual(cls, hold_hours: float = 4.0) -> CostEstimate:
        """默认永续合约成本模型（VIP1 费率）。

        Args:
            hold_hours: 持有时间（小时），用于估算 funding
        """
        # PKG02: 费用从 CostEstimate 类属性获取，默认 0 表示未验证
        fee_bps = float(getattr(cls, "_configured_taker_fee_bps", 0.0) or 0.0)
        spread_bps = 1.0  # 平均 spread
        slippage_bps = 1.0  # 预期滑点
        # funding: ~0.01% per 8h, estimated for hold period
        funding_bps = 0.01 * (hold_hours / 8.0) * 100
        total_bps = fee_bps + spread_bps + slippage_bps + funding_bps
        return cls(
            fee_bps=fee_bps,
            spread_bps=spread_bps,
            slippage_bps=slippage_bps,
            funding_bps=funding_bps,
            total_bps=total_bps,
        )


# ================================================================
# 标签构造器
# ================================================================


class LabelBuilder:
    """标签构造器 — 从价格序列构造评估标签。

    核心标签公式：
        y(t, h) = log(price(t+h) / price(t)) - expected_round_trip_cost(t, h)

    使用示例:
        builder = LabelBuilder()
        labels = builder.build_labels(
            price_series=price_series,
            label_spec=LabelSpec(horizon_bars=4, price_type=PriceType.CLOSE),
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            factor_id=FactorId("test_factor"),
            factor_version=SchemaVersion("1.0.0"),
        )
    """

    def __init__(self, cost_model: CostEstimate | Any | None = None) -> None:
        self._custom_cost_model = cost_model is not None
        self._cost_model = cost_model or CostEstimate.default_perpetual()

    def _cost_for_hold(self, hold_hours: float) -> CostEstimate:
        """Return the configured cost model without silently replacing it."""

        if self._custom_cost_model:
            if isinstance(self._cost_model, CostEstimate):
                return self._cost_model
            # PipelineConfig uses evaluation.cost_capacity.CostModel.  Adapt
            # its explicit fee/spread/slippage/funding inputs into the label
            # contract instead of passing an incompatible object through.
            try:
                fee_bps = float(self._cost_model.taker_fee_bps)
                spread_bps = float(self._cost_model.avg_spread_bps)
                slippage_bps = float(self._cost_model.slippage_bps)
                funding_bps = float(self._cost_model.funding_rate_8h_pct) * (hold_hours / 8.0) * 100.0
                total_bps = fee_bps + spread_bps + slippage_bps + funding_bps
            except (AttributeError, TypeError, ValueError, OverflowError) as exc:
                raise TypeError("unsupported_cost_model_contract") from exc
            if not all(math.isfinite(v) for v in (fee_bps, spread_bps, slippage_bps, funding_bps, total_bps)):
                raise ValueError("non_finite_cost_model")
            return CostEstimate(
                fee_bps=fee_bps,
                spread_bps=spread_bps,
                slippage_bps=slippage_bps,
                funding_bps=funding_bps,
                total_bps=total_bps,
            )
        return CostEstimate.default_perpetual(hold_hours)

    @property
    def cost_model_version(self) -> str:
        """Return the configured cost model version for evidence binding."""

        if not self._custom_cost_model:
            return "bf01-default-perpetual"
        return str(getattr(self._cost_model, "model_version", "custom-unversioned"))

    def build_labels(
        self,
        price_series: list[PricePoint],
        label_spec: LabelSpec,
        venue: VenueId,
        symbol: InstrumentId,
        timeframe: str,
        factor_id: FactorId,
        factor_version: SchemaVersion,
        *,
        embargo_bars: int | None = None,
        min_data_quality: DataQualityTier = DataQualityTier.PASS,
    ) -> list[LabelRecord]:
        """从价格序列构造标签。

        Args:
            price_series: 按时间排序的价格序列
            label_spec: 标签规格
            venue: 交易所
            symbol: 品种
            timeframe: K 线粒度
            factor_id: 因子 ID
            factor_version: 因子版本
            embargo_bars: 覆盖 label_spec 的禁运期
            min_data_quality: 覆盖 label_spec 的最低数据质量

        Returns:
            标签记录列表
        """
        if embargo_bars is None:
            embargo_bars = label_spec.embargo_bars

        labels = []
        horizon = label_spec.horizon_bars
        spec_hash = label_spec.to_hash()

        for t in range(len(price_series) - horizon - embargo_bars):
            entry_point = price_series[t]
            exit_point = price_series[t + horizon]

            # 验证 closed-bar
            if not entry_point.is_closed:
                continue
            if not exit_point.is_closed:
                continue

            entry_price = entry_point.get_price(label_spec.price_type)
            exit_price = exit_point.get_price(label_spec.price_type)

            if (
                entry_price is None
                or exit_price is None
                or not _is_valid_price(entry_price)
                or not _is_valid_price(exit_price)
            ):
                # 创建质量标记为 MISSING_PRICE 的标签
                pk = PredictionKey(
                    venue=venue,
                    symbol=symbol,
                    timeframe=timeframe,
                    prediction_time=entry_point.timestamp,
                    data_available_time=entry_point.timestamp,
                    horizon=horizon,
                    horizon_unit=HorizonUnit.BAR,
                    factor_id=factor_id,
                    factor_version=factor_version,
                )
                labels.append(
                    LabelRecord(
                        label_id=_generate_label_id(pk, spec_hash),
                        prediction_key=pk,
                        label_start_time=entry_point.timestamp,
                        label_end_time=exit_point.timestamp,
                        label_available_time=exit_point.timestamp,
                        entry_price_type=label_spec.price_type,
                        exit_price_type=label_spec.price_type,
                        cost_model_version=self.cost_model_version,
                        label_value=0.0,
                        gross_return=0.0,
                        quality_status=LabelQuality.MISSING_PRICE,
                        label_spec_hash=spec_hash,
                    )
                )
                continue

            # 计算原始收益
            gross_return = _compute_return(entry_price, exit_price, label_spec.return_type)
            if not math.isfinite(gross_return):
                continue

            # 估算持有期成本
            hold_hours = horizon * _timeframe_to_hours(timeframe)
            cost_est = self._cost_for_hold(hold_hours)
            expected_cost = cost_est.total_bps / 10000.0  # bps → decimal

            # 标签值 = 原始收益 - 预期成本
            label_value = gross_return - expected_cost if label_spec.cost_adjusted else gross_return

            # 确定标签质量
            quality = LabelQuality.VALID
            available_time = exit_point.timestamp

            # 构造 PredictionKey
            pk = PredictionKey(
                venue=venue,
                symbol=symbol,
                timeframe=timeframe,
                prediction_time=entry_point.timestamp,
                data_available_time=entry_point.timestamp,
                horizon=horizon,
                horizon_unit=HorizonUnit.BAR,
                factor_id=factor_id,
                factor_version=factor_version,
            )

            label = LabelRecord(
                label_id=_generate_label_id(pk, spec_hash),
                prediction_key=pk,
                label_start_time=entry_point.timestamp,
                label_end_time=exit_point.timestamp,
                label_available_time=available_time,
                entry_price_type=label_spec.price_type,
                exit_price_type=label_spec.price_type,
                cost_model_version=self.cost_model_version,
                label_value=round(label_value, 10),
                gross_return=round(gross_return, 10),
                expected_cost_bps=cost_est.total_bps,
                quality_status=quality,
                label_spec_hash=spec_hash,
                metadata={
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "hold_hours": hold_hours,
                    "fee_bps": cost_est.fee_bps,
                    "spread_bps": cost_est.spread_bps,
                    "slippage_bps": cost_est.slippage_bps,
                    "funding_bps": cost_est.funding_bps,
                },
            )
            labels.append(label)

        return labels

    def build_triple_barrier_labels(
        self,
        price_series: list[PricePoint],
        label_spec: LabelSpec,
        venue: VenueId,
        symbol: InstrumentId,
        timeframe: str,
        factor_id: FactorId,
        factor_version: SchemaVersion,
        *,
        upper_barrier: float = 2.0,  # 止盈阈值（收益率 %）
        lower_barrier: float = -1.0,  # 止损阈值（收益率 %）
        max_hold_bars: int = 48,
    ) -> list[LabelRecord]:
        """构造 triple-barrier 标签。

        标签值:
            +1 if 触及 upper_barrier 先于 lower_barrier
            -1 if 触及 lower_barrier 先于 upper_barrier
             0 if max_hold_bars 到期未触及任何 barrier（或同时触及）

        同时返回对应的实际收益（用于回归评估）。
        """
        spec_hash = label_spec.to_hash()
        labels = []
        horizon = label_spec.horizon_bars
        upper_dec = upper_barrier / 100.0
        lower_dec = lower_barrier / 100.0

        for t in range(len(price_series) - max_hold_bars):
            entry_point = price_series[t]
            if not entry_point.is_closed:
                continue

            entry_price = entry_point.get_price(label_spec.price_type)
            if entry_price is None or entry_price <= 0:
                continue

            # 扫描未来 K 线，检测 barrier 触及
            label_value = 0.0
            actual_return = 0.0
            exit_time = entry_point.timestamp
            exit_idx = t

            for h in range(1, min(max_hold_bars + 1, len(price_series) - t)):
                curr_point = price_series[t + h]
                curr_price = curr_point.get_price(label_spec.price_type)
                if curr_price is None or curr_price <= 0:
                    continue

                ret = (curr_price - entry_price) / entry_price

                if ret >= upper_dec:
                    label_value = 1.0
                    actual_return = ret
                    exit_time = curr_point.timestamp
                    exit_idx = t + h
                    break
                elif ret <= lower_dec:
                    label_value = -1.0
                    actual_return = ret
                    exit_time = curr_point.timestamp
                    exit_idx = t + h
                    break
            else:
                # 水平触及：使用最后价格
                final_point = price_series[min(t + max_hold_bars, len(price_series) - 1)]
                final_price = final_point.get_price(label_spec.price_type)
                if final_price and final_price > 0:
                    actual_return = (final_price - entry_price) / entry_price
                    exit_time = final_point.timestamp
                    exit_idx = min(t + max_hold_bars, len(price_series) - 1)

            hold_hours = (exit_idx - t) * _timeframe_to_hours(timeframe)
            cost_est = self._cost_for_hold(hold_hours)
            expected_cost = cost_est.total_bps / 10000.0

            net_return = actual_return - expected_cost if label_spec.cost_adjusted else actual_return

            # 成本调整后的 label_value：如果成本后无法盈利，标记为 0
            final_label = label_value if net_return * label_value >= 0 else 0.0

            pk = PredictionKey(
                venue=venue,
                symbol=symbol,
                timeframe=timeframe,
                prediction_time=entry_point.timestamp,
                data_available_time=entry_point.timestamp,
                horizon=horizon,
                horizon_unit=HorizonUnit.BAR,
                factor_id=factor_id,
                factor_version=factor_version,
            )

            labels.append(
                LabelRecord(
                    label_id=_generate_label_id(pk, spec_hash),
                    prediction_key=pk,
                    label_start_time=entry_point.timestamp,
                    label_end_time=exit_time,
                    label_available_time=exit_time,
                    entry_price_type=label_spec.price_type,
                    exit_price_type=label_spec.price_type,
                    cost_model_version=self.cost_model_version,
                    label_value=round(final_label, 10),
                    gross_return=round(actual_return, 10),
                    expected_cost_bps=cost_est.total_bps,
                    quality_status=LabelQuality.VALID,
                    label_spec_hash=spec_hash,
                    metadata={
                        "entry_price": entry_price,
                        "exit_idx": exit_idx,
                        "upper_barrier": upper_barrier,
                        "lower_barrier": lower_barrier,
                        "label_value_raw": label_value,
                        "hold_bars_actual": exit_idx - t,
                    },
                )
            )

        return labels


# ================================================================
# 辅助函数
# ================================================================


def _compute_return(
    entry_price: float,
    exit_price: float,
    return_type: ReturnType,
) -> float:
    """计算指定类型的收益。"""
    if return_type == ReturnType.LOG:
        return math.log(exit_price / entry_price)
    elif return_type == ReturnType.SIMPLE:
        return (exit_price - entry_price) / entry_price
    elif return_type == ReturnType.RESIDUAL:
        # 基础实现：residual = simple return（需要控制变量回归来完整实现）
        return (exit_price - entry_price) / entry_price
    else:
        return (exit_price - entry_price) / entry_price


def _is_valid_price(value: float | None) -> bool:
    """Return true only for finite, strictly positive venue prices."""

    try:
        return value is not None and math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError, OverflowError):
        return False


def _timeframe_to_hours(tf: str) -> float:
    """将 timeframe 字符串转换为小时数。"""
    mapping = {
        "1m": 1 / 60,
        "5m": 5 / 60,
        "15m": 15 / 60,
        "30m": 30 / 60,
        "1h": 1,
        "2h": 2,
        "4h": 4,
        "6h": 6,
        "8h": 8,
        "12h": 12,
        "1d": 24,
        "3d": 72,
        "1w": 168,
    }
    return mapping.get(tf, 1.0)


def _generate_label_id(pk: PredictionKey, spec_hash: str) -> str:
    """生成确定性标签 ID。"""
    content = (
        f"{pk.venue}:{pk.symbol}:{pk.timeframe}:"
        f"{pk.prediction_time.isoformat()}:{pk.horizon}:"
        f"{pk.factor_id}:{pk.factor_version}:{spec_hash}"
    )
    return hashlib.sha256(content.encode()).hexdigest()[:32]
