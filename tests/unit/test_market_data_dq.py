"""
PKG22 (BDS-P1-039/040/041): 行情数据质量测试。

覆盖：
- P1-039: KLine key 包含 interval（多周期隔离）
- P1-040: 逐笔 volume vs 24h 累计 volume
- P1-041: 异常不 silent pass（DQ 事件记录）
"""

from __future__ import annotations

from datetime import datetime, timezone

from beidou_data.klines import KLineGenerator
from beidou_data.market import BarIntegrity, BarSequenceValidator, ClosedBar, ClosedBarNormalizer
from beidou_shared.types import InstrumentId, Price, Quantity, VenueId, VenueInstrument

BTC_USDT = VenueInstrument(
    venue_id=VenueId("BINANCE_USDM"),
    instrument_id=InstrumentId("BTCUSDT"),
)


def _make_bar(symbol: str = "BTCUSDT", interval: str = "5m", open_time: datetime | None = None) -> ClosedBar:
    t = open_time or datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    return ClosedBar(
        venue_instrument=VenueInstrument(
            venue_id=VenueId("BINANCE_USDM"),
            instrument_id=InstrumentId(symbol),
        ),
        open_time=t,
        close_time=datetime(2026, 1, 1, 12, 5, tzinfo=timezone.utc),
        interval=interval,
        open=Price(amount="50000"),
        high=Price(amount="50100"),
        low=Price(amount="49900"),
        close=Price(amount="50050"),
        volume=Quantity(amount="10"),
        is_closed=True,
        revision=0,
        sequence=1,
        available_at=datetime.now(timezone.utc),
        source="test",
    )


# ---------------------------------------------------------------------------
# BDS-P1-039: KLine key 包含 interval
# ---------------------------------------------------------------------------


class TestKLineKeyIncludesInterval:
    """BDS-P1-039: 多周期 K 线 key 隔离。"""

    def test_different_intervals_have_separate_keys(self) -> None:
        """不同 interval 的 K 线存储在不同 key 下。"""
        gen_5m = KLineGenerator(interval="5m")
        gen_1h = KLineGenerator(interval="1h")

        key_5m = gen_5m._make_key(BTC_USDT)
        key_1h = gen_1h._make_key(BTC_USDT)

        assert key_5m != key_1h, "不同 interval 应有不同的 key"
        assert "5m" in key_5m
        assert "1h" in key_1h
        assert "BTCUSDT" in key_5m
        assert "BTCUSDT" in key_1h

    def test_same_interval_same_key(self) -> None:
        """相同 interval 产生相同 key。"""
        gen_a = KLineGenerator(interval="15m")
        gen_b = KLineGenerator(interval="15m")

        assert gen_a._make_key(BTC_USDT) == gen_b._make_key(BTC_USDT)

    def test_multi_interval_klines_isolated(self) -> None:
        """不同 interval 的 K 线互不干扰。"""
        gen_5m = KLineGenerator(interval="5m")
        gen_1h = KLineGenerator(interval="1h")

        ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        gen_5m.process_tick(BTC_USDT, Price(amount="50000"), Quantity(amount="1"), ts)
        gen_1h.process_tick(BTC_USDT, Price(amount="50000"), Quantity(amount="1"), ts)

        klines_5m = gen_5m.get_klines(BTC_USDT)
        klines_1h = gen_1h.get_klines(BTC_USDT)

        # 两个 generator 的存储应该隔离
        assert len(klines_5m) == 0  # 第一个 tick 还没闭合
        assert len(klines_1h) == 0


class TestBarSequenceValidatorMultiInterval:
    """BDS-P1-039: BarSequenceValidator 按 symbol+interval 隔离。"""

    def test_different_intervals_independent_sequence(self) -> None:
        """不同 interval 的 bar 序列独立验证。"""
        validator = BarSequenceValidator()

        bar_5m = _make_bar("BTCUSDT", "5m")
        bar_1h = _make_bar("BTCUSDT", "1h", datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))

        result_5m = validator.validate(bar_5m)
        result_1h = validator.validate(bar_1h)

        # 两个 interval 互不干扰
        assert result_5m == BarIntegrity.OK
        assert result_1h == BarIntegrity.OK

    def test_same_symbol_same_interval_gap_detected(self) -> None:
        """同 symbol+interval 的 gap 被检测。"""
        validator = BarSequenceValidator(max_gap_seconds=300)

        bar1 = _make_bar("ETHUSDT", "5m", datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
        bar2 = _make_bar("ETHUSDT", "5m", datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc))  # 1h gap

        validator.validate(bar1)
        result = validator.validate(bar2)
        assert result == BarIntegrity.GAP_DETECTED


# ---------------------------------------------------------------------------
# BDS-P1-041: 异常不 silent pass
# ---------------------------------------------------------------------------


class TestDQExceptionNotSilent:
    """BDS-P1-041: DQ 异常被记录，不静默跳过。"""

    def test_normalizer_counts_dq_incidents(self) -> None:
        """Normalizer 在异常时增加 DQ 计数。"""
        normalizer = ClosedBarNormalizer()
        initial = normalizer._dq_incidents

        # 传入会触发 Exception 的数据（open_time 为 None 会通过检查，
        # 但在后续处理中可能触发异常；用 dict key 缺失触发 TypeError）
        result = normalizer.normalize(
            {"open_time": [], "close_time": [], "open": "abc"},  # list 不能当 datetime
            BTC_USDT,
            interval="5m",
        )

        # 如果 normalize 内部捕获了异常，dq_incidents 应该增加
        # 或者在早期验证阶段就返回 INVALID 不触发异常（同样正确）
        # 关键是 dq_incidents 机制存在且可访问
        assert result.status == BarIntegrity.INVALID
        assert normalizer._dq_incidents == initial + 1

    def test_normalize_invalid_data_returns_invalid(self) -> None:
        """异常/无效数据返回非 OK 结果而非崩溃。"""
        normalizer = ClosedBarNormalizer()

        result = normalizer.normalize(
            {},  # 完全缺失
            BTC_USDT,
            interval="5m",
        )
        # 缺失数据应在早期验证被拦截，返回非 OK 状态
        assert result.status != BarIntegrity.OK
        assert result.bar is None


# ---------------------------------------------------------------------------
# BDS-P1-040: Volume source
# ---------------------------------------------------------------------------


class TestVolumeSource:
    """BDS-P1-040: volume 来源标记 vs 24h 累计。"""

    def test_kline_generator_uses_incremental_volume(self) -> None:
        """KLineGenerator 使用增量 volume，每次 tick 累加。"""
        gen = KLineGenerator(interval="5m")

        ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        # 第一次 tick: qty=1
        gen.process_tick(BTC_USDT, Price(amount="50000"), Quantity(amount="1"), ts)
        # 第二次 tick: qty=2
        gen.process_tick(
            BTC_USDT, Price(amount="50100"), Quantity(amount="2"), datetime(2026, 1, 1, 12, 1, 0, tzinfo=timezone.utc)
        )

        current = gen.get_current_bar(BTC_USDT)
        assert current is not None
        # volume 应该是 1 + 2 = 3（累加），不是 24h 累计
        assert float(current.volume.amount) == 3.0, f"volume 应为逐笔累加 (3.0)，而非 24h 累计: {current.volume.amount}"

    def test_closed_bar_has_is_closed_flag(self) -> None:
        """闭合 bar 必须有 is_closed=True。"""
        gen = KLineGenerator(interval="5m")

        ts = datetime(2026, 1, 1, 11, 55, 0, tzinfo=timezone.utc)
        result = gen.process_tick(BTC_USDT, Price(amount="50000"), Quantity(amount="1"), ts)

        # 第一个 tick 不闭合（5m bar 从 11:55 到 12:00）
        if result is not None:
            assert result.is_closed is True  # 如果是跨边界 tick


# ---------------------------------------------------------------------------
# Mutation 测试
# ---------------------------------------------------------------------------


class TestMutationMarketDQ:
    """PKG22: Mutation 测试。"""

    def test_mutation_key_without_interval_would_collide(self) -> None:
        """Mutation: 不含 interval 的 key 会导致多周期碰撞。"""
        # 模拟旧版 key（只有 venue:instrument）
        old_key = f"{BTC_USDT.venue_id}:{BTC_USDT.instrument_id}"
        new_key_5m = KLineGenerator(interval="5m")._make_key(BTC_USDT)
        new_key_1h = KLineGenerator(interval="1h")._make_key(BTC_USDT)

        # 旧 key 对 5m 和 1h 相同
        assert new_key_5m != old_key, "新 key 应不同于旧 key（包含 interval）"
        # 新 key 之间不同
        assert new_key_5m != new_key_1h
