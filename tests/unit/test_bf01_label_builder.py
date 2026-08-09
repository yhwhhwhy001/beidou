"""BF-01 标签构造器测试 — LabelBuilder, LabelSpec, CostEstimate."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from beidou_research.mining.contracts import (
    LabelQuality,
    LabelSpec,
    PriceType,
    ReturnType,
)
from beidou_research.mining.evaluation.cost_capacity import CostModel
from beidou_research.mining.label_builder import (
    CostEstimate,
    LabelBuilder,
    PricePoint,
)
from beidou_shared.types import (
    FactorId,
    InstrumentId,
    SchemaVersion,
    VenueId,
)

# ================================================================
# Fixtures
# ================================================================


def _make_price_series(
    n: int = 100,
    start_price: float = 100.0,
    drift: float = 0.0,
    volatility: float = 0.02,
    seed: int = 42,
) -> list[PricePoint]:
    """生成合成价格序列。"""
    import random

    rng = random.Random(seed)
    prices = [start_price]
    for _ in range(n - 1):
        ret = drift + rng.gauss(0, volatility)
        prices.append(prices[-1] * (1 + ret))

    points = []
    base_time = datetime(2026, 1, 15, 0, 0, tzinfo=timezone.utc)
    for i, p in enumerate(prices):
        import datetime as dt

        points.append(
            PricePoint(
                venue=VenueId("BINANCE"),
                symbol=InstrumentId("BTCUSDT"),
                timeframe="1h",
                timestamp=base_time + dt.timedelta(hours=i),
                close=p,
                mark=p,
                mid=p,
                vwap=p,
                is_closed=True,
            )
        )
    return points


@pytest.fixture
def sample_prices():
    return _make_price_series(100, seed=42)


@pytest.fixture
def label_builder():
    return LabelBuilder()


# ================================================================
# CostEstimate tests
# ================================================================


class TestCostEstimate:
    """成本估算测试。"""

    def test_default_perpetual(self):
        """默认永续合约成本模型。"""
        cost = CostEstimate.default_perpetual(hold_hours=4.0)
        assert cost.fee_bps == 4.0
        assert cost.spread_bps == 1.0
        assert cost.slippage_bps == 1.0
        assert cost.total_bps > 0
        assert cost.total_bps == cost.fee_bps + cost.spread_bps + cost.slippage_bps + cost.funding_bps

    def test_longer_hold_higher_funding(self):
        """更长持有期导致更高 funding 成本。"""
        cost_short = CostEstimate.default_perpetual(hold_hours=1.0)
        cost_long = CostEstimate.default_perpetual(hold_hours=24.0)
        # 更长的持有期 → 更高的 funding
        assert cost_long.funding_bps > cost_short.funding_bps


# ================================================================
# LabelBuilder tests
# ================================================================


class TestLabelBuilder:
    """LabelBuilder 验收测试。"""

    def test_configured_cost_model_is_used_for_labels(self, sample_prices):
        configured = CostEstimate(fee_bps=20.0, spread_bps=3.0, slippage_bps=2.0, funding_bps=1.0, total_bps=26.0)
        builder = LabelBuilder(cost_model=configured)
        spec = LabelSpec(label_id="custom-cost", horizon_bars=4, cost_adjusted=True)

        labels = builder.build_labels(
            sample_prices,
            spec,
            VenueId("BINANCE"),
            InstrumentId("BTCUSDT"),
            "1h",
            FactorId("test_factor"),
            SchemaVersion("1.0.0"),
        )

        assert labels
        assert all(label.expected_cost_bps == 26.0 for label in labels)

    def test_pipeline_cost_model_contract_is_adapted(self, sample_prices):
        configured = CostModel(
            taker_fee_bps=20.0,
            avg_spread_bps=3.0,
            slippage_bps=2.0,
            funding_rate_8h_pct=0.01,
        )
        builder = LabelBuilder(cost_model=configured)
        labels = builder.build_labels(
            sample_prices,
            LabelSpec(label_id="pipeline-cost", horizon_bars=4, cost_adjusted=True),
            VenueId("BINANCE"),
            InstrumentId("BTCUSDT"),
            "1h",
            FactorId("test_factor"),
            SchemaVersion("1.0.0"),
        )

        assert labels
        assert labels[0].expected_cost_bps == pytest.approx(25.5)
        assert labels[0].cost_model_version == configured.model_version

    def test_missing_requested_price_type_is_not_filled_from_close(self, sample_prices):
        point = sample_prices[0]
        assert point.get_price(PriceType.MARK) == point.mark
        assert point.get_price(PriceType.MID) == point.mid
        assert point.get_price(PriceType.VWAP) == point.vwap

        point.mark = None
        assert point.get_price(PriceType.MARK) is None

    def test_build_labels_basic(self, label_builder, sample_prices):
        """基本标签构造。"""
        label_spec = LabelSpec(
            label_id="test",
            horizon_bars=4,
            price_type=PriceType.CLOSE,
            return_type=ReturnType.LOG,
            cost_adjusted=True,
        )
        labels = label_builder.build_labels(
            price_series=sample_prices,
            label_spec=label_spec,
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            factor_id=FactorId("test_factor"),
            factor_version=SchemaVersion("1.0.0"),
        )

        assert len(labels) > 0
        # 所有标签应该是 VALID 状态
        assert all(l.quality_status == LabelQuality.VALID for l in labels)

    def test_build_labels_count(self, label_builder, sample_prices):
        """标签数量正确。"""
        label_spec = LabelSpec(
            label_id="test",
            horizon_bars=4,
            price_type=PriceType.CLOSE,
            embargo_bars=0,
        )
        labels = label_builder.build_labels(
            price_series=sample_prices,
            label_spec=label_spec,
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            factor_id=FactorId("test_factor"),
            factor_version=SchemaVersion("1.0.0"),
        )
        # n=100 prices, horizon=4, embargo=0 → 96 labels
        assert len(labels) == len(sample_prices) - 4

    def test_build_labels_with_embargo(self, label_builder, sample_prices):
        """embargo 减少标签数量。"""
        label_spec = LabelSpec(
            label_id="test",
            horizon_bars=4,
            embargo_bars=2,
        )
        labels = label_builder.build_labels(
            price_series=sample_prices,
            label_spec=label_spec,
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            factor_id=FactorId("test_factor"),
            factor_version=SchemaVersion("1.0.0"),
        )
        # n=100, horizon=4, embargo=2 → 94 labels
        assert len(labels) == len(sample_prices) - 4 - 2

    def test_log_return_calculation(self, label_builder):
        """对数收益计算正确。"""
        # 确定性价格序列：100 → 110 → 100
        from datetime import timedelta

        base = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        prices = [
            PricePoint(
                VenueId("BINANCE"),
                InstrumentId("BTCUSDT"),
                "1h",
                base + timedelta(hours=i),
                close=p,
                mark=p,
                mid=p,
                vwap=p,
                is_closed=True,
            )
            for i, p in enumerate([100.0, 110.0, 100.0, 110.0, 100.0])
        ]

        label_spec = LabelSpec(
            label_id="test",
            horizon_bars=2,
            price_type=PriceType.CLOSE,
            return_type=ReturnType.LOG,
            cost_adjusted=False,  # 不计成本，方便验证
        )
        labels = label_builder.build_labels(
            price_series=prices,
            label_spec=label_spec,
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            factor_id=FactorId("test"),
            factor_version=SchemaVersion("1.0.0"),
        )
        # t=0: entry=100, exit(t+2)=100, log_return = ln(100/100) = 0
        assert len(labels) >= 1
        assert abs(labels[0].gross_return - 0.0) < 1e-9
        # t=1: entry=110, exit(t+2)=110, log_return = ln(110/110) = 0
        assert abs(labels[1].gross_return - 0.0) < 1e-9
        # t=2: entry=100, exit(t+2)=100, log_return = 0
        assert abs(labels[2].gross_return - 0.0) < 1e-9

    def test_simple_return_calculation(self, label_builder):
        """简单收益计算正确。"""
        from datetime import timedelta

        base = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        prices = [
            PricePoint(
                VenueId("BINANCE"),
                InstrumentId("BTCUSDT"),
                "1h",
                base + timedelta(hours=i),
                close=p,
                mark=p,
                mid=p,
                vwap=p,
                is_closed=True,
            )
            for i, p in enumerate([100.0, 101.0, 102.0])
        ]

        label_spec = LabelSpec(
            label_id="test",
            horizon_bars=1,
            price_type=PriceType.CLOSE,
            return_type=ReturnType.SIMPLE,
            cost_adjusted=False,
        )
        labels = label_builder.build_labels(
            price_series=prices,
            label_spec=label_spec,
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            factor_id=FactorId("test"),
            factor_version=SchemaVersion("1.0.0"),
        )
        # t=0: entry=100, exit(t+1)=101 → (101-100)/100 = 0.01
        assert len(labels) >= 1
        assert abs(labels[0].gross_return - 0.01) < 1e-9
        # t=1: entry=101, exit(t+2)=102 → (102-101)/101 ≈ 0.0099
        assert abs(labels[1].gross_return - (1.0 / 101.0)) < 1e-9

    def test_cost_adjusted_label(self, label_builder, sample_prices):
        """成本调整标签的值小于原始收益。"""
        label_spec = LabelSpec(
            label_id="test",
            horizon_bars=4,
            price_type=PriceType.CLOSE,
            return_type=ReturnType.SIMPLE,
            cost_adjusted=True,
        )
        labels = label_builder.build_labels(
            price_series=sample_prices,
            label_spec=label_spec,
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            factor_id=FactorId("test_factor"),
            factor_version=SchemaVersion("1.0.0"),
        )
        for label in labels:
            # 扣成本后的值应 <= 原始值
            assert label.label_value <= label.gross_return + 1e-9

    def test_label_metadata_complete(self, label_builder, sample_prices):
        """标签包含完整元数据。"""
        label_spec = LabelSpec(label_id="test", horizon_bars=4)
        labels = label_builder.build_labels(
            price_series=sample_prices,
            label_spec=label_spec,
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            factor_id=FactorId("test_factor"),
            factor_version=SchemaVersion("1.0.0"),
        )
        assert len(labels) > 0
        label = labels[0]
        # 验证 prediction_key 绑定
        assert label.prediction_key.venue == VenueId("BINANCE")
        assert label.prediction_key.symbol == InstrumentId("BTCUSDT")
        assert label.prediction_key.timeframe == "1h"
        assert label.prediction_key.factor_id == FactorId("test_factor")
        # 验证元数据
        assert "entry_price" in label.metadata
        assert "exit_price" in label.metadata
        assert "fee_bps" in label.metadata
        assert label.expected_cost_bps > 0
        # 验证 label_id 不为空
        assert label.label_id != ""

    def test_label_id_deterministic(self, label_builder):
        """相同参数的标签 ID 确定性。"""
        from datetime import timedelta

        base = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)

        def make_prices():
            return [
                PricePoint(
                    VenueId("BINANCE"),
                    InstrumentId("BTCUSDT"),
                    "1h",
                    base + timedelta(hours=i),
                    close=100.0 + i,
                    mark=100.0 + i,
                    mid=100.0 + i,
                    vwap=100.0 + i,
                    is_closed=True,
                )
                for i in range(10)
            ]

        label_spec = LabelSpec(label_id="test", horizon_bars=2)

        labels1 = label_builder.build_labels(
            price_series=make_prices(),
            label_spec=label_spec,
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            factor_id=FactorId("test"),
            factor_version=SchemaVersion("1.0.0"),
        )
        labels2 = label_builder.build_labels(
            price_series=make_prices(),
            label_spec=label_spec,
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            factor_id=FactorId("test"),
            factor_version=SchemaVersion("1.0.0"),
        )
        # 相同参数应产生相同 label_id
        assert labels1[0].label_id == labels2[0].label_id

    def test_open_bar_skipped(self, label_builder):
        """未闭合 K 线被跳过。"""
        from datetime import timedelta

        base = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        prices = [
            PricePoint(
                VenueId("BINANCE"),
                InstrumentId("BTCUSDT"),
                "1h",
                base + timedelta(hours=i),
                close=100.0 + i,
                mark=100.0 + i,
                mid=100.0 + i,
                vwap=100.0 + i,
                is_closed=(i < 9),
            )  # 最后一个是未闭合 K 线
            for i in range(10)
        ]

        label_spec = LabelSpec(label_id="test", horizon_bars=2)
        labels = label_builder.build_labels(
            price_series=prices,
            label_spec=label_spec,
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            factor_id=FactorId("test"),
            factor_version=SchemaVersion("1.0.0"),
        )
        # 未闭合的 entry 和 exit 应被跳过
        for label in labels:
            assert label.quality_status == LabelQuality.VALID

    def test_triple_barrier_basic(self, label_builder):
        """Triple-barrier 标签基本功能。"""
        from datetime import timedelta

        base = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        # 构造足够长的价格序列
        n = 60  # > max_hold_bars (48)
        prices_list = [100.0]
        for i in range(1, n):
            if i == 1:
                prices_list.append(103.0)  # +3% 突破 upper barrier (2%)
            else:
                prices_list.append(prices_list[-1] * (1 + 0.001))

        price_series = [
            PricePoint(
                VenueId("BINANCE"),
                InstrumentId("BTCUSDT"),
                "1h",
                base + timedelta(hours=i),
                close=p,
                mark=p,
                mid=p,
                vwap=p,
                is_closed=True,
            )
            for i, p in enumerate(prices_list)
        ]

        label_spec = LabelSpec(label_id="tb_test", horizon_bars=4, cost_adjusted=False)
        labels = label_builder.build_triple_barrier_labels(
            price_series=price_series,
            label_spec=label_spec,
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            factor_id=FactorId("test"),
            factor_version=SchemaVersion("1.0.0"),
            upper_barrier=2.0,
            lower_barrier=-1.0,
            max_hold_bars=48,
        )
        assert len(labels) > 0
        # t=0: entry=100, t=1 price=103 (+3% > 2%) → +1
        assert labels[0].label_value == 1.0

    def test_triple_barrier_lower_hit(self, label_builder):
        """Triple-barrier 突破下限。"""
        from datetime import timedelta

        base = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        n = 60
        prices_list = [100.0]
        for i in range(1, n):
            if i == 1:
                prices_list.append(98.5)  # -1.5% < -1% lower barrier
            else:
                prices_list.append(prices_list[-1] * (1 + 0.001))

        price_series = [
            PricePoint(
                VenueId("BINANCE"),
                InstrumentId("BTCUSDT"),
                "1h",
                base + timedelta(hours=i),
                close=p,
                mark=p,
                mid=p,
                vwap=p,
                is_closed=True,
            )
            for i, p in enumerate(prices_list)
        ]

        label_spec = LabelSpec(label_id="tb_test", horizon_bars=4, cost_adjusted=False)
        labels = label_builder.build_triple_barrier_labels(
            price_series=price_series,
            label_spec=label_spec,
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            factor_id=FactorId("test"),
            factor_version=SchemaVersion("1.0.0"),
            upper_barrier=2.0,
            lower_barrier=-1.0,
            max_hold_bars=48,
        )
        assert len(labels) > 0
        assert labels[0].label_value == -1.0

    def test_triple_barrier_horizontal(self, label_builder):
        """Triple-barrier 水平触及（无突破）。"""
        from datetime import timedelta

        base = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        # 价格固定在 100±0.2%，不会触及 ±5% barriers
        n = 60
        prices_list = [100.0]
        for i in range(1, n):
            prices_list.append(100.0 + 0.2 * (i % 5 - 2))

        price_series = [
            PricePoint(
                VenueId("BINANCE"),
                InstrumentId("BTCUSDT"),
                "1h",
                base + timedelta(hours=i),
                close=p,
                mark=p,
                mid=p,
                vwap=p,
                is_closed=True,
            )
            for i, p in enumerate(prices_list)
        ]

        label_spec = LabelSpec(label_id="tb_test", horizon_bars=4, cost_adjusted=False)
        labels = label_builder.build_triple_barrier_labels(
            price_series=price_series,
            label_spec=label_spec,
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            factor_id=FactorId("test"),
            factor_version=SchemaVersion("1.0.0"),
            upper_barrier=5.0,
            lower_barrier=-5.0,
            max_hold_bars=48,
        )
        assert len(labels) > 0
        assert all(l.label_value == 0.0 for l in labels)

    def test_label_available_time_after_end(self, label_builder, sample_prices):
        """标签可用时间 ≥ label_end_time。"""
        label_spec = LabelSpec(label_id="test", horizon_bars=4)
        labels = label_builder.build_labels(
            price_series=sample_prices,
            label_spec=label_spec,
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            factor_id=FactorId("test_factor"),
            factor_version=SchemaVersion("1.0.0"),
        )
        for label in labels:
            assert label.label_available_time >= label.label_end_time

    def test_label_prediction_key_traceable(self, label_builder, sample_prices):
        """任意标签可以追溯到原始事件范围。"""
        label_spec = LabelSpec(label_id="test", horizon_bars=4)
        labels = label_builder.build_labels(
            price_series=sample_prices,
            label_spec=label_spec,
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            factor_id=FactorId("test_factor"),
            factor_version=SchemaVersion("1.0.0"),
        )
        label = labels[0]
        pk = label.prediction_key
        # 验证所有关键字段都可追溯
        assert pk.venue == VenueId("BINANCE")
        assert pk.symbol == InstrumentId("BTCUSDT")
        assert pk.timeframe == "1h"
        assert pk.horizon == 4
        assert pk.factor_id == FactorId("test_factor")
        assert pk.factor_version == SchemaVersion("1.0.0")
        assert label.label_start_time == pk.prediction_time
