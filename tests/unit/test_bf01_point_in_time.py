"""BF-01 点时连接测试 — PointInTimeJoin, ClosedBarEnforcer, FutureDataGuard, IsolationValidator."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from beidou_research.mining.contracts import (
    HorizonUnit,
    LabelQuality,
    LabelRecord,
    PredictionKey,
    PredictionRecord,
    PriceType,
)
from beidou_research.mining.point_in_time import (
    BarInfo,
    ClosedBarEnforcer,
    FutureDataGuard,
    IsolationValidator,
    PointInTimeJoin,
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


def _t(hour: int, day: int = 15) -> datetime:
    return datetime(2026, 1, day, hour, 0, tzinfo=timezone.utc)


def _make_pk(
    venue: str = "BINANCE",
    symbol: str = "BTCUSDT",
    timeframe: str = "1h",
    hour: int = 12,
    horizon: int = 4,
    factor_id: str = "test_factor",
) -> PredictionKey:
    """创建有效的 PredictionKey（data_available == prediction_time）。"""
    return PredictionKey(
        venue=VenueId(venue),
        symbol=InstrumentId(symbol),
        timeframe=timeframe,
        prediction_time=_t(hour),
        data_available_time=_t(hour),  # 必须 ≤ prediction_time
        horizon=horizon,
        horizon_unit=HorizonUnit.BAR,
        factor_id=FactorId(factor_id),
        factor_version=SchemaVersion("1.0.0"),
    )


def _make_pred(pk: PredictionKey, value: float = 0.01, revision: int = 0) -> PredictionRecord:
    return PredictionRecord(
        prediction_key=pk,
        prediction_value=value,
        revision=revision,
    )


def _make_label(
    pk: PredictionKey,
    label_id: str = "L001",
    label_value: float = 0.02,
    quality: LabelQuality = LabelQuality.VALID,
) -> LabelRecord:
    return LabelRecord(
        label_id=label_id,
        prediction_key=pk,
        label_start_time=pk.prediction_time,
        label_end_time=pk.prediction_time + timedelta(hours=pk.horizon),
        label_available_time=pk.prediction_time + timedelta(hours=pk.horizon),
        entry_price_type=PriceType.CLOSE,
        exit_price_type=PriceType.CLOSE,
        cost_model_version="v1",
        label_value=label_value,
        quality_status=quality,
    )


# ================================================================
# PredictionKey 点时约束
# ================================================================


class TestPredictionKeyPITConstraint:
    """PredictionKey 强制执行 data_available_time ≤ prediction_time。"""

    def test_future_data_rejected_at_construction(self):
        """注入未来数据时 PredictionKey 构造直接失败。"""
        with pytest.raises(ValueError, match="data_available_time"):
            PredictionKey(
                venue=VenueId("BINANCE"),
                symbol=InstrumentId("BTCUSDT"),
                timeframe="1h",
                prediction_time=_t(12),
                data_available_time=_t(14),  # AFTER prediction → 拒绝!
                horizon=4,
                horizon_unit=HorizonUnit.BAR,
                factor_id=FactorId("test"),
                factor_version=SchemaVersion("1.0"),
            )

    def test_valid_key_allowed(self):
        """有效键正常创建。"""
        pk = _make_pk()
        assert pk.data_available_time <= pk.prediction_time


# ================================================================
# PointInTimeJoin tests
# ================================================================


class TestPointInTimeJoin:
    """点时连接引擎验收测试。"""

    def test_store_prediction_basic(self):
        join = PointInTimeJoin()
        pk = _make_pk()
        pred = _make_pred(pk)
        result = join.store_prediction(pred)
        assert result.prediction_value == 0.01

    def test_store_prediction_idempotent_same_revision(self):
        """相同 revision 的重复写入返回已有记录（幂等）。"""
        join = PointInTimeJoin()
        pk = _make_pk()
        pred1 = _make_pred(pk, value=0.01, revision=1)
        pred2 = _make_pred(pk, value=0.02, revision=1)

        r1 = join.store_prediction(pred1)
        r2 = join.store_prediction(pred2)
        assert r1.prediction_value == 0.01
        assert r2.prediction_value == 0.01  # 幂等

    def test_store_prediction_higher_revision_wins(self):
        """更高 revision 覆盖旧记录。"""
        join = PointInTimeJoin()
        pk = _make_pk()
        join.store_prediction(_make_pred(pk, value=0.01, revision=0))
        join.store_prediction(_make_pred(pk, value=0.02, revision=1))

        key = pk.to_sort_key()
        stored = join._predictions[key]
        assert stored[0].prediction_value == 0.02
        assert stored[0].revision == 1

    def test_store_prediction_lower_revision_ignored(self):
        """更低 revision 的写入被忽略，返回最高 revision 记录。"""
        join = PointInTimeJoin()
        pk = _make_pk()
        join.store_prediction(_make_pred(pk, value=0.02, revision=1))
        result = join.store_prediction(_make_pred(pk, value=0.01, revision=0))

        # 低 revision 被忽略，返回已有的高 revision 记录
        assert result.prediction_value == 0.02
        assert result.revision == 1

    def test_store_label(self):
        join = PointInTimeJoin()
        pk = _make_pk()
        label = _make_label(pk)
        result = join.store_label(label)
        assert result.label_value == 0.02

    def test_join_valid(self):
        """有效预测和标签成功连接。"""
        join = PointInTimeJoin()
        pk = _make_pk()
        pred = _make_pred(pk)
        label = _make_label(pk)

        join.store_prediction(pred)
        join.store_label(label)

        result = join.join(pred)
        assert result.is_valid
        assert result.prediction.prediction_value == 0.01
        assert result.label.label_value == 0.02

    def test_join_no_matching_label(self):
        """无匹配标签时返回无效结果。"""
        join = PointInTimeJoin()
        pk = _make_pk()
        pred = _make_pred(pk)

        join.store_prediction(pred)
        result = join.join(pred)
        assert not result.is_valid
        assert "no_matching_label" in result.failure_reason

    def test_join_label_quality_filtered(self):
        """非 VALID 标签被过滤。"""
        join = PointInTimeJoin()
        pk = _make_pk()
        pred = _make_pred(pk)
        label = _make_label(pk, quality=LabelQuality.FUTURE_LEAK)

        join.store_prediction(pred)
        join.store_label(label)

        result = join.join(pred)
        assert not result.is_valid
        assert "label_quality" in result.failure_reason

    def test_join_bar_not_available_rejected(self):
        """使用 ClosedBarEnforcer 验证 K 线可用性 — 未闭合 K 线被拒绝。"""
        enforcer = ClosedBarEnforcer()
        pk = _make_pk(hour=12)
        pred = _make_pred(pk)

        # 注册未闭合的 K 线
        bar = BarInfo(
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            open_time=_t(11),
            close_time=_t(12),
            is_closed=False,  # 未闭合!
        )
        enforcer.register_bar(bar)

        # 验证 enforcer 拒绝
        assert not enforcer.enforce(pred)

        # 而已闭合 K 线可用
        bar2 = BarInfo(
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            open_time=_t(11),
            close_time=_t(12),
            is_closed=True,
        )
        enforcer.register_bar(bar2)
        # enforce 检查 data_available_time 之前的已闭合 K 线
        assert enforcer.enforce(pred)

    def test_overlap_detection(self):
        """重叠标签检测。"""
        join = PointInTimeJoin()

        pk1 = _make_pk(hour=12, horizon=4)
        pk2 = _make_pk(hour=14, horizon=4)

        join.store_label(_make_label(pk1, label_id="L1"))
        join.store_label(_make_label(pk2, label_id="L2"))

        overlaps = join.detect_overlaps(VenueId("BINANCE"), InstrumentId("BTCUSDT"), "1h")
        assert len(overlaps) == 1

    def test_no_overlap_for_disjoint_labels(self):
        join = PointInTimeJoin()
        pk1 = _make_pk(hour=12, horizon=2)
        pk2 = _make_pk(hour=14, horizon=2)

        join.store_label(_make_label(pk1, label_id="L1"))
        join.store_label(_make_label(pk2, label_id="L2"))

        overlaps = join.detect_overlaps(VenueId("BINANCE"), InstrumentId("BTCUSDT"), "1h")
        assert len(overlaps) == 0

    def test_join_all_with_report(self):
        """批量连接生成 JoinReport。"""
        join = PointInTimeJoin()

        pk1 = _make_pk(hour=12, factor_id="f1")
        pred1 = _make_pred(pk1)
        label1 = _make_label(pk1, label_id="L1")

        pk2 = _make_pk(hour=13, factor_id="f2")
        pred2 = _make_pred(pk2)
        # 故意不存 label2 → unmatched

        join.store_prediction(pred1)
        join.store_prediction(pred2)
        join.store_label(label1)

        _results, report = join.join_all(predictions=[pred1, pred2])

        assert report.total_predictions == 2
        assert report.matched == 1
        assert report.unmatched == 1

    def test_cross_timeframe_join_rejected(self):
        """不同 timeframe 的预测与标签不能配对。"""
        join = PointInTimeJoin()
        pk_1h = _make_pk(timeframe="1h")
        pk_5m = _make_pk(timeframe="5m")

        pred = _make_pred(pk_1h)
        label = _make_label(pk_5m, label_id="wrong_tf")

        join.store_prediction(pred)
        join.store_label(label)

        result = join.join(pred)
        # 标签的 timeframe 是 5m，预测是 1h → 不匹配
        assert not result.is_valid
        assert "no_matching_label" in result.failure_reason

    def test_join_label_available_time_check(self):
        """标签在 prediction_time 之后才可用 → 连接时验证通过。"""
        join = PointInTimeJoin()
        pk = _make_pk(hour=12, horizon=4)
        pred = _make_pred(pk)
        # label_available_time >= prediction_time + horizon → OK
        label = _make_label(pk, label_id="L_good")

        join.store_prediction(pred)
        join.store_label(label)

        result = join.join(pred)
        assert result.is_valid


# ================================================================
# ClosedBarEnforcer tests
# ================================================================


class TestClosedBarEnforcer:
    """Closed-bar 强制器验收测试。"""

    def test_closed_bar_available(self):
        enforcer = ClosedBarEnforcer()
        bar = BarInfo(
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            open_time=_t(12),
            close_time=_t(13),
            is_closed=True,
        )
        enforcer.register_bar(bar)

        assert enforcer.is_bar_available(
            VenueId("BINANCE"),
            InstrumentId("BTCUSDT"),
            "1h",
            _t(12),
            _t(14),  # query at 14:00, bar closed at 13:00
        )

    def test_open_bar_not_available(self):
        enforcer = ClosedBarEnforcer()
        bar = BarInfo(
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            open_time=_t(12),
            close_time=_t(13),
            is_closed=False,
        )
        enforcer.register_bar(bar)

        assert not enforcer.is_bar_available(
            VenueId("BINANCE"),
            InstrumentId("BTCUSDT"),
            "1h",
            _t(12),
            _t(14),
        )

    def test_closed_but_too_early_not_available(self):
        enforcer = ClosedBarEnforcer()
        bar = BarInfo(
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            open_time=_t(12),
            close_time=_t(13),
            is_closed=True,
        )
        enforcer.register_bar(bar)

        # 查询时间在 close_time 之前
        query_time = datetime(2026, 1, 15, 12, 30, tzinfo=timezone.utc)
        assert not enforcer.is_bar_available(
            VenueId("BINANCE"),
            InstrumentId("BTCUSDT"),
            "1h",
            _t(12),
            query_time,  # query at 12:30, bar closes at 13:00
        )

    def test_future_data_injection_fails(self):
        """使用 bar 级别的未来数据检查：bar 未闭合时不能用于预测。"""
        enforcer = ClosedBarEnforcer()

        bar = BarInfo(
            venue=VenueId("BINANCE"),
            symbol=InstrumentId("BTCUSDT"),
            timeframe="1h",
            open_time=_t(12),
            close_time=_t(13),
            is_closed=False,
        )
        enforcer.register_bar(bar)

        # 在 bar 闭合前查询 → 不可用
        query_time = datetime(2026, 1, 15, 12, 30, tzinfo=timezone.utc)
        assert not enforcer.is_bar_available(
            VenueId("BINANCE"),
            InstrumentId("BTCUSDT"),
            "1h",
            _t(12),
            query_time,
        )

    def test_enforce_prediction_with_bar(self):
        """通过 ClosedBarEnforcer 验证预测使用的 bar 数据。"""
        enforcer = ClosedBarEnforcer()
        pk = _make_pk(hour=12)
        pred = _make_pred(pk)

        # 注册一个已闭合 bar（在 data_available_time 之前）
        bar = BarInfo(
            venue=pk.venue,
            symbol=pk.symbol,
            timeframe=pk.timeframe,
            open_time=_t(11),
            close_time=_t(12),
            is_closed=True,
        )
        enforcer.register_bar(bar)
        assert enforcer.enforce(pred)

    def test_enforce_no_closed_bar_fails(self):
        enforcer = ClosedBarEnforcer()
        pk = _make_pk(hour=12)
        pred = _make_pred(pk)

        bar = BarInfo(
            venue=pk.venue,
            symbol=pk.symbol,
            timeframe=pk.timeframe,
            open_time=_t(11),
            close_time=_t(12),
            is_closed=False,
        )
        enforcer.register_bar(bar)
        assert not enforcer.enforce(pred)


# ================================================================
# FutureDataGuard tests
# ================================================================


class TestFutureDataGuard:
    """未来数据注入检测测试。"""

    def test_data_available_ok(self):
        """数据可用时间 check 正确。"""
        assert FutureDataGuard.check_data_availability(
            query_time=_t(14),
            data_timestamp=_t(12),
        )

    def test_future_data_rejected(self):
        """未来数据被检测。"""
        assert not FutureDataGuard.check_data_availability(
            query_time=_t(12),
            data_timestamp=_t(14),  # 数据来自未来！
        )

    def test_assert_no_future_data_clean(self):
        """无未来数据时无违规。"""
        guard = FutureDataGuard()
        pk = _make_pk(hour=12)
        pred = _make_pred(pk)
        violations = guard.assert_no_future_data([pred])
        assert len(violations) == 0

    def test_prediction_key_rejects_future_at_construction(self):
        """PredictionKey 在构造时就拒绝未来数据 — 这是设计正确的。"""
        # PredictionKey 本身的约束确保 data_available ≤ prediction
        # 这是第一道防线
        with pytest.raises(ValueError):
            PredictionKey(
                venue=VenueId("BINANCE"),
                symbol=InstrumentId("BTCUSDT"),
                timeframe="1h",
                prediction_time=_t(12),
                data_available_time=_t(14),  # 未来数据!
                horizon=4,
                horizon_unit=HorizonUnit.BAR,
                factor_id=FactorId("f"),
                factor_version=SchemaVersion("1.0"),
            )


# ================================================================
# IsolationValidator tests
# ================================================================


class TestIsolationValidator:
    """多品种多周期隔离验证测试。"""

    def test_group_by_scope_same_scope(self):
        preds = [_make_pred(_make_pk(hour=h)) for h in [12, 13, 14]]
        groups = IsolationValidator.group_by_scope(preds)
        assert len(groups) == 1

    def test_group_by_scope_different_timeframes(self):
        preds = [
            _make_pred(_make_pk(timeframe="1h")),
            _make_pred(_make_pk(timeframe="5m")),
        ]
        groups = IsolationValidator.group_by_scope(preds)
        assert len(groups) == 2

    def test_group_by_scope_different_symbols(self):
        preds = [
            _make_pred(_make_pk(symbol="BTCUSDT")),
            _make_pred(_make_pk(symbol="ETHUSDT")),
        ]
        groups = IsolationValidator.group_by_scope(preds)
        assert len(groups) == 2

    def test_validate_batch_no_violations(self):
        preds = [_make_pred(_make_pk(hour=12)), _make_pred(_make_pk(hour=13))]
        violations = IsolationValidator.validate_batch(preds)
        assert len(violations) == 0

    def test_validate_batch_symbol_mismatch(self):
        preds = [
            _make_pred(_make_pk(symbol="BTCUSDT")),
            _make_pred(_make_pk(symbol="ETHUSDT")),
        ]
        violations = IsolationValidator.validate_batch(preds)
        assert len(violations) >= 1

    def test_validate_batch_timeframe_mismatch(self):
        preds = [
            _make_pred(_make_pk(timeframe="1h")),
            _make_pred(_make_pk(timeframe="5m")),
        ]
        violations = IsolationValidator.validate_batch(preds)
        assert len(violations) >= 1
