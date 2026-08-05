"""BF-01 合同测试 — PredictionKey, LabelRecord, PredictionRecord, DatasetManifest."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from beidou_research.mining.contracts import (
    DatasetManifest,
    HorizonUnit,
    LabelQuality,
    LabelRecord,
    LabelSpec,
    PredictionKey,
    PredictionRecord,
    PriceType,
    ReturnType,
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


@pytest.fixture
def sample_prediction_key() -> PredictionKey:
    return PredictionKey(
        venue=VenueId("BINANCE"),
        symbol=InstrumentId("BTCUSDT"),
        timeframe="1h",
        prediction_time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
        data_available_time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
        horizon=4,
        horizon_unit=HorizonUnit.BAR,
        factor_id=FactorId("test_factor"),
        factor_version=SchemaVersion("1.0.0"),
    )


@pytest.fixture
def sample_label_record(sample_prediction_key) -> LabelRecord:
    return LabelRecord(
        label_id="label-001",
        prediction_key=sample_prediction_key,
        label_start_time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
        label_end_time=datetime(2026, 1, 15, 16, 0, tzinfo=timezone.utc),
        label_available_time=datetime(2026, 1, 15, 16, 0, tzinfo=timezone.utc),
        entry_price_type=PriceType.CLOSE,
        exit_price_type=PriceType.CLOSE,
        cost_model_version="v1",
        label_value=0.02,
        gross_return=0.025,
        expected_cost_bps=5.0,
    )


# ================================================================
# PredictionKey tests
# ================================================================


class TestPredictionKey:
    """PredictionKey 验收测试。"""

    def test_creation_valid(self, sample_prediction_key):
        """有效 PredictionKey 可以正常创建。"""
        assert sample_prediction_key.venue == VenueId("BINANCE")
        assert sample_prediction_key.symbol == InstrumentId("BTCUSDT")
        assert sample_prediction_key.timeframe == "1h"
        assert sample_prediction_key.horizon == 4

    def test_future_data_rejected(self):
        """注入未来数据时创建 PredictionKey 必须失败。"""
        with pytest.raises(ValueError, match="data_available_time"):
            PredictionKey(
                venue=VenueId("BINANCE"),
                symbol=InstrumentId("BTCUSDT"),
                timeframe="1h",
                prediction_time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
                # data_available_time AFTER prediction_time → FUTURE DATA
                data_available_time=datetime(2026, 1, 15, 13, 0, tzinfo=timezone.utc),
                horizon=4,
                horizon_unit=HorizonUnit.BAR,
                factor_id=FactorId("test"),
                factor_version=SchemaVersion("1.0"),
            )

    def test_data_available_equals_prediction_allowed(self):
        """data_available_time == prediction_time 允许（同时刻数据可用）。"""
        pk = PredictionKey(
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("ETHUSDT"),
            timeframe="5m",
            prediction_time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
            data_available_time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
            horizon=12,
            horizon_unit=HorizonUnit.BAR,
            factor_id=FactorId("test"),
            factor_version=SchemaVersion("1.0"),
        )
        assert pk.data_available_time == pk.prediction_time

    def test_timeframe_matches_same(self):
        """相同 timeframe 匹配。"""
        pk1 = PredictionKey(
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            prediction_time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
            data_available_time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
            horizon=4,
            horizon_unit=HorizonUnit.BAR,
            factor_id=FactorId("f1"),
            factor_version=SchemaVersion("1.0"),
        )
        pk2 = PredictionKey(
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            prediction_time=datetime(2026, 1, 15, 14, 0, tzinfo=timezone.utc),
            data_available_time=datetime(2026, 1, 15, 14, 0, tzinfo=timezone.utc),
            horizon=4,
            horizon_unit=HorizonUnit.BAR,
            factor_id=FactorId("f1"),
            factor_version=SchemaVersion("1.0"),
        )
        assert pk1.timeframe_matches(pk2)

    def test_timeframe_mismatch_detected(self):
        """不同 timeframe 不匹配 — 1h vs 5m 不得交叉配对。"""
        pk_1h = PredictionKey(
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            prediction_time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
            data_available_time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
            horizon=1,
            horizon_unit=HorizonUnit.BAR,
            factor_id=FactorId("f1"),
            factor_version=SchemaVersion("1.0"),
        )
        pk_5m = PredictionKey(
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="5m",
            prediction_time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
            data_available_time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
            horizon=12,
            horizon_unit=HorizonUnit.BAR,
            factor_id=FactorId("f1"),
            factor_version=SchemaVersion("1.0"),
        )
        assert not pk_1h.timeframe_matches(pk_5m)
        assert not pk_5m.timeframe_matches(pk_1h)

    def test_symbol_matches_same_venue_symbol(self):
        """相同 venue+symbol 匹配。"""
        pk1 = PredictionKey(
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            prediction_time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
            data_available_time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
            horizon=4,
            horizon_unit=HorizonUnit.BAR,
            factor_id=FactorId("f1"),
            factor_version=SchemaVersion("1.0"),
        )
        pk2 = PredictionKey(
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            prediction_time=datetime(2026, 1, 15, 13, 0, tzinfo=timezone.utc),
            data_available_time=datetime(2026, 1, 15, 13, 0, tzinfo=timezone.utc),
            horizon=4,
            horizon_unit=HorizonUnit.BAR,
            factor_id=FactorId("f1"),
            factor_version=SchemaVersion("1.0"),
        )
        assert pk1.symbol_matches(pk2)

    def test_symbol_mismatch_different_symbol(self):
        """不同 symbol 不匹配。"""
        pk_btc = PredictionKey(
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            prediction_time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
            data_available_time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
            horizon=4,
            horizon_unit=HorizonUnit.BAR,
            factor_id=FactorId("f1"),
            factor_version=SchemaVersion("1.0"),
        )
        pk_eth = PredictionKey(
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("ETHUSDT"),
            timeframe="1h",
            prediction_time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
            data_available_time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
            horizon=4,
            horizon_unit=HorizonUnit.BAR,
            factor_id=FactorId("f1"),
            factor_version=SchemaVersion("1.0"),
        )
        assert not pk_btc.symbol_matches(pk_eth)

    def test_sort_key_ordering(self):
        """to_sort_key 提供一致的排序。"""
        pk1 = PredictionKey(
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("AAVEUSDT"),
            timeframe="1h",
            prediction_time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
            data_available_time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
            horizon=4,
            horizon_unit=HorizonUnit.BAR,
            factor_id=FactorId("f1"),
            factor_version=SchemaVersion("1.0"),
        )
        pk2 = PredictionKey(
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            prediction_time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
            data_available_time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
            horizon=4,
            horizon_unit=HorizonUnit.BAR,
            factor_id=FactorId("f1"),
            factor_version=SchemaVersion("1.0"),
        )
        # AAVEUSDT < BTCUSDT 字母顺序
        assert pk1.to_sort_key() < pk2.to_sort_key()

    def test_frozen_immutable(self, sample_prediction_key):
        """PredictionKey 是不可变的（frozen dataclass）。"""
        with pytest.raises(Exception):
            sample_prediction_key.venue = VenueId("OTHER")  # type: ignore


# ================================================================
# LabelRecord tests
# ================================================================


class TestLabelRecord:
    """LabelRecord 验收测试。"""

    def test_creation_valid(self, sample_label_record):
        """有效 LabelRecord 正常创建。"""
        assert sample_label_record.label_id == "label-001"
        assert sample_label_record.label_value == 0.02
        assert sample_label_record.quality_status == LabelQuality.VALID

    def test_start_after_end_rejected(self, sample_prediction_key):
        """label_start_time >= label_end_time 必须拒绝。"""
        with pytest.raises(ValueError, match="label_start_time"):
            LabelRecord(
                label_id="bad",
                prediction_key=sample_prediction_key,
                label_start_time=datetime(2026, 1, 15, 16, 0, tzinfo=timezone.utc),
                label_end_time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
                label_available_time=datetime(2026, 1, 15, 16, 0, tzinfo=timezone.utc),
                entry_price_type=PriceType.CLOSE,
                exit_price_type=PriceType.CLOSE,
                cost_model_version="v1",
                label_value=0.0,
            )

    def test_available_before_end_rejected(self, sample_prediction_key):
        """label_available_time < label_end_time 必须拒绝。"""
        with pytest.raises(ValueError, match="label_available_time"):
            LabelRecord(
                label_id="bad",
                prediction_key=sample_prediction_key,
                label_start_time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
                label_end_time=datetime(2026, 1, 15, 16, 0, tzinfo=timezone.utc),
                label_available_time=datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc),
                entry_price_type=PriceType.CLOSE,
                exit_price_type=PriceType.CLOSE,
                cost_model_version="v1",
                label_value=0.0,
            )

    def test_overlaps_detection_true(self, sample_prediction_key):
        """重叠标签检测。"""
        t0 = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 15, 14, 0, tzinfo=timezone.utc)
        t4 = datetime(2026, 1, 15, 16, 0, tzinfo=timezone.utc)

        label_a = LabelRecord(
            label_id="a",
            prediction_key=sample_prediction_key,
            label_start_time=t0,
            label_end_time=t4,
            label_available_time=t4,
            entry_price_type=PriceType.CLOSE,
            exit_price_type=PriceType.CLOSE,
            cost_model_version="v1",
            label_value=0.01,
        )
        label_b = LabelRecord(
            label_id="b",
            prediction_key=sample_prediction_key,
            label_start_time=t2,
            label_end_time=datetime(2026, 1, 15, 18, 0, tzinfo=timezone.utc),
            label_available_time=datetime(2026, 1, 15, 18, 0, tzinfo=timezone.utc),
            entry_price_type=PriceType.CLOSE,
            exit_price_type=PriceType.CLOSE,
            cost_model_version="v1",
            label_value=0.02,
        )
        # a: [12, 16), b: [14, 18) → 重叠 [14, 16)
        assert label_a.overlaps_with(label_b)
        assert label_b.overlaps_with(label_a)

    def test_overlaps_detection_false(self, sample_prediction_key):
        """非重叠标签检测。"""
        t0 = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 15, 14, 0, tzinfo=timezone.utc)
        t4 = datetime(2026, 1, 15, 16, 0, tzinfo=timezone.utc)

        label_a = LabelRecord(
            label_id="a",
            prediction_key=sample_prediction_key,
            label_start_time=t0,
            label_end_time=t2,
            label_available_time=t2,
            entry_price_type=PriceType.CLOSE,
            exit_price_type=PriceType.CLOSE,
            cost_model_version="v1",
            label_value=0.01,
        )
        label_b = LabelRecord(
            label_id="b",
            prediction_key=sample_prediction_key,
            label_start_time=t2,
            label_end_time=t4,
            label_available_time=t4,
            entry_price_type=PriceType.CLOSE,
            exit_price_type=PriceType.CLOSE,
            cost_model_version="v1",
            label_value=0.02,
        )
        # a: [12, 14), b: [14, 16) → 无重叠（边界接触不算）
        assert not label_a.overlaps_with(label_b)

    def test_overlaps_different_symbols_never_overlap(self, sample_prediction_key):
        """不同 symbol 的标签永不重叠。"""
        pk_eth = PredictionKey(
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("ETHUSDT"),
            timeframe="1h",
            prediction_time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
            data_available_time=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
            horizon=4,
            horizon_unit=HorizonUnit.BAR,
            factor_id=FactorId("f1"),
            factor_version=SchemaVersion("1.0"),
        )
        t0 = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        t4 = datetime(2026, 1, 15, 16, 0, tzinfo=timezone.utc)

        label_a = LabelRecord(
            label_id="a",
            prediction_key=sample_prediction_key,
            label_start_time=t0,
            label_end_time=t4,
            label_available_time=t4,
            entry_price_type=PriceType.CLOSE,
            exit_price_type=PriceType.CLOSE,
            cost_model_version="v1",
            label_value=0.01,
        )
        label_b = LabelRecord(
            label_id="b",
            prediction_key=pk_eth,
            label_start_time=t0,
            label_end_time=t4,
            label_available_time=t4,
            entry_price_type=PriceType.CLOSE,
            exit_price_type=PriceType.CLOSE,
            cost_model_version="v1",
            label_value=0.02,
        )
        assert not label_a.overlaps_with(label_b)

    def test_is_valid_for_evaluation(self, sample_label_record):
        """VALID 状态的标签可用于评估。"""
        assert sample_label_record.is_valid_for_evaluation()

    def test_future_leak_not_valid(self, sample_label_record):
        """FUTURE_LEAK 的标签不能用于评估。"""
        # 通过修改 quality_status 字段的方式
        leak_label = LabelRecord(
            label_id="leak",
            prediction_key=sample_label_record.prediction_key,
            label_start_time=sample_label_record.label_start_time,
            label_end_time=sample_label_record.label_end_time,
            label_available_time=sample_label_record.label_available_time,
            entry_price_type=PriceType.CLOSE,
            exit_price_type=PriceType.CLOSE,
            cost_model_version="v1",
            label_value=0.01,
            quality_status=LabelQuality.FUTURE_LEAK,
        )
        assert not leak_label.is_valid_for_evaluation()

    def test_cost_adjusted_return(self, sample_label_record):
        """成本调整后的收益计算正确。"""
        # gross_return = 0.025, expected_cost_bps = 5.0 → cost decimal = 0.0005
        # cost_adjusted = 0.025 - 0.0005 = 0.0245
        expected = 0.025 - (5.0 / 10000.0)
        assert abs(sample_label_record.cost_adjusted_return() - expected) < 1e-9

    def test_frozen_immutable(self, sample_label_record):
        """LabelRecord 是不可变的。"""
        with pytest.raises(Exception):
            sample_label_record.label_value = 99.0  # type: ignore


# ================================================================
# PredictionRecord tests
# ================================================================


class TestPredictionRecord:
    """PredictionRecord 验收测试。"""

    @pytest.fixture
    def sample_prediction(self, sample_prediction_key) -> PredictionRecord:
        return PredictionRecord(
            prediction_key=sample_prediction_key,
            prediction_value=0.015,
            feature_manifest_hash="abc123",
            dataset_manifest_hash="def456",
            code_hash="ghi789",
            parameter_hash="jkl012",
            factor_expression_hash="mno345",
        )

    def test_creation(self, sample_prediction):
        """PredictionRecord 正常创建。"""
        assert sample_prediction.prediction_value == 0.015
        assert sample_prediction.revision == 0

    def test_revision_tracking(self, sample_prediction_key):
        """revision 追踪正常。"""
        pred_v0 = PredictionRecord(
            prediction_key=sample_prediction_key,
            prediction_value=0.01,
            revision=0,
        )
        pred_v1 = PredictionRecord(
            prediction_key=sample_prediction_key,
            prediction_value=0.015,  # 修订后的值
            revision=1,
        )
        assert pred_v0.revision == 0
        assert pred_v1.revision == 1
        assert pred_v0.prediction_value != pred_v1.prediction_value

    def test_to_lineage(self, sample_prediction):
        """to_lineage 返回完整溯源信息。"""
        lineage = sample_prediction.to_lineage()
        assert lineage["prediction_value"] == 0.015
        assert lineage["code_hash"] == "ghi789"
        assert lineage["prediction_key"]["venue"] == "BINANCE"
        assert lineage["prediction_key"]["symbol"] == "BTCUSDT"

    def test_frozen_immutable(self, sample_prediction):
        """PredictionRecord 是不可变的。"""
        with pytest.raises(Exception):
            sample_prediction.prediction_value = 99.0  # type: ignore


# ================================================================
# DatasetManifest tests
# ================================================================


class TestDatasetManifest:
    """DatasetManifest 验收测试。"""

    @pytest.fixture
    def sample_manifest(self) -> DatasetManifest:
        return DatasetManifest(
            manifest_id="ds-001",
            manifest_hash="hash123",
            venue=VenueId("BINANCE"),
            symbols=(InstrumentId("BTCUSDT"), InstrumentId("ETHUSDT")),
            timeframe="1h",
            start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end_time=datetime(2026, 6, 30, tzinfo=timezone.utc),
            feature_names=("close", "sma_20", "rsi_14"),
            feature_versions=(SchemaVersion("1.0"), SchemaVersion("1.0"), SchemaVersion("1.0")),
        )

    def test_contains_prediction_in_range(self, sample_manifest):
        """范围内的预测被识别。"""
        pk = PredictionKey(
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            prediction_time=datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
            data_available_time=datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
            horizon=4,
            horizon_unit=HorizonUnit.BAR,
            factor_id=FactorId("f1"),
            factor_version=SchemaVersion("1.0"),
        )
        assert sample_manifest.contains(pk)

    def test_contains_prediction_out_of_range(self, sample_manifest):
        """范围外的预测被识别。"""
        pk = PredictionKey(
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            prediction_time=datetime(2025, 12, 15, 12, 0, tzinfo=timezone.utc),
            data_available_time=datetime(2025, 12, 15, 12, 0, tzinfo=timezone.utc),
            horizon=4,
            horizon_unit=HorizonUnit.BAR,
            factor_id=FactorId("f1"),
            factor_version=SchemaVersion("1.0"),
        )
        assert not sample_manifest.contains(pk)

    def test_contains_different_venue(self, sample_manifest):
        """不同 venue 不在范围内。"""
        pk = PredictionKey(
            venue=VenueId("OKX"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            prediction_time=datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
            data_available_time=datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
            horizon=4,
            horizon_unit=HorizonUnit.BAR,
            factor_id=FactorId("f1"),
            factor_version=SchemaVersion("1.0"),
        )
        assert not sample_manifest.contains(pk)

    def test_contains_different_timeframe(self, sample_manifest):
        """不同 timeframe 不在范围内。"""
        pk = PredictionKey(
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="5m",
            prediction_time=datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
            data_available_time=datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
            horizon=48,
            horizon_unit=HorizonUnit.BAR,
            factor_id=FactorId("f1"),
            factor_version=SchemaVersion("1.0"),
        )
        assert not sample_manifest.contains(pk)


# ================================================================
# LabelSpec tests
# ================================================================


class TestLabelSpec:
    """LabelSpec 验收测试。"""

    def test_to_hash_deterministic(self):
        """相同参数产生相同 hash。"""
        spec1 = LabelSpec(label_id="test", horizon_bars=4, price_type=PriceType.CLOSE)
        spec2 = LabelSpec(label_id="test", horizon_bars=4, price_type=PriceType.CLOSE)
        assert spec1.to_hash() == spec2.to_hash()

    def test_to_hash_different_for_different_params(self):
        """不同参数产生不同 hash。"""
        spec1 = LabelSpec(label_id="test", horizon_bars=4, price_type=PriceType.CLOSE)
        spec2 = LabelSpec(label_id="test", horizon_bars=8, price_type=PriceType.CLOSE)
        assert spec1.to_hash() != spec2.to_hash()

    def test_to_hash_different_return_type(self):
        """不同 return_type 产生不同 hash。"""
        spec1 = LabelSpec(label_id="test", horizon_bars=4, return_type=ReturnType.LOG)
        spec2 = LabelSpec(label_id="test", horizon_bars=4, return_type=ReturnType.SIMPLE)
        assert spec1.to_hash() != spec2.to_hash()
