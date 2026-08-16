"""KLineGenerator 迟到 tick 防护测试（M01-F02，P0-05）。

背景: 迟到 tick（timestamp 早于当前 bar open_time，或早于最后一根已闭合
bar）会被静默并入当前 bar，改写 high/low/close/volume —— 已闭合事实被
污染。修复: 迟到 tick 拒绝（返回 None + 拒绝计数与原因审计），绝不
污染当前或已闭合 bar。
"""

from __future__ import annotations

from datetime import datetime, timezone

from beidou_data.klines import KLineGenerator
from beidou_shared.types import InstrumentId, Price, Quantity, VenueId, VenueInstrument

_VI = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))


def _tick(gen: KLineGenerator, price: str, hour: int, minute: int = 0):
    return gen.process_tick(
        _VI,
        Price(amount=price),
        Quantity(amount="1"),
        datetime(2026, 1, 1, hour, minute, tzinfo=timezone.utc),
    )


def test_late_tick_within_open_bar_is_rejected() -> None:
    gen = KLineGenerator(interval="1h")
    _tick(gen, "100", 10, 0)
    _tick(gen, "101", 10, 30)
    result = _tick(gen, "50", 9, 59)  # 迟到:早于当前 bar open_time(10:00)

    assert result is None
    assert gen.rejected_ticks == 1
    assert "late" in (gen.last_rejection or "")
    # 当前 bar 未被污染
    current = gen.get_current_bar(_VI)
    assert current is not None
    assert float(current.high.amount) == 101.0
    assert float(current.close.amount) == 101.0
    assert float(current.low.amount) == 100.0


def test_late_tick_after_bar_close_is_rejected() -> None:
    gen = KLineGenerator(interval="1h")
    _tick(gen, "100", 10, 0)
    completed = _tick(gen, "101", 11, 5)  # 关闭 10:00 bar,开启 11:00 bar
    assert completed is not None and completed.is_closed

    result = _tick(gen, "50", 10, 45)  # 迟到:属于已闭合 bar 的窗口
    assert result is None
    assert gen.rejected_ticks == 1
    # 已闭合 bar 不被污染（闭合 bar 的 close/high 保持窗口内最后 tick 的 100.0）
    closed = gen.get_klines(_VI)[0]
    assert float(closed.close.amount) == 100.0
    assert float(closed.high.amount) == 100.0
    assert float(closed.low.amount) == 100.0


def test_in_order_ticks_still_work() -> None:
    gen = KLineGenerator(interval="1h")
    _tick(gen, "100", 10, 0)
    _tick(gen, "105", 10, 30)
    completed = _tick(gen, "102", 11, 1)
    assert completed is not None
    assert float(completed.open.amount) == 100.0
    assert float(completed.high.amount) == 105.0
    assert float(completed.close.amount) == 105.0  # 窗口内最后 tick
    assert completed.is_closed is True
    assert gen.rejected_ticks == 0


def test_boundary_tick_at_close_time_opens_new_bar() -> None:
    gen = KLineGenerator(interval="1h")
    _tick(gen, "100", 10, 0)
    completed = _tick(gen, "101", 11, 0)  # 恰好在 close_time 边界
    assert completed is not None and completed.is_closed
    current = gen.get_current_bar(_VI)
    assert current is not None
    assert float(current.open.amount) == 101.0


def test_minute_interval_buckets_floor_to_boundary() -> None:
    """M01-F03-R2: 分钟桶起点 floor 到 Binance 小时对齐边界（对抗审查反例 3）。

    Binance 分钟 K 线边界对齐整点: 5m → :00/:05/:10…; 15m → :00/:15/:30/:45;
    30m → :00/:30; 1m → 每分。10:04:30 的 tick 归属对应边界桶。
    """
    for interval, expected_minute in (("5m", 0), ("15m", 0), ("30m", 0), ("1m", 4)):
        gen = KLineGenerator(interval=interval)
        gen.process_tick(
            _VI,
            Price(amount="100"),
            Quantity(amount="1"),
            datetime(2026, 1, 1, 10, 4, 30, tzinfo=timezone.utc),
        )
        current = gen.get_current_bar(_VI)
        assert current is not None
        assert current.open_time == datetime(2026, 1, 1, 10, expected_minute, tzinfo=timezone.utc)

    # 非零分钟起点（15m: 10:17 → 10:15 桶; 30m: 10:37 → 10:30 桶）
    gen15 = KLineGenerator(interval="15m")
    gen15.process_tick(
        _VI, Price(amount="100"), Quantity(amount="1"), datetime(2026, 1, 1, 10, 17, tzinfo=timezone.utc)
    )
    assert gen15.get_current_bar(_VI).open_time == datetime(2026, 1, 1, 10, 15, tzinfo=timezone.utc)
    gen30 = KLineGenerator(interval="30m")
    gen30.process_tick(
        _VI, Price(amount="100"), Quantity(amount="1"), datetime(2026, 1, 1, 10, 37, tzinfo=timezone.utc)
    )
    assert gen30.get_current_bar(_VI).open_time == datetime(2026, 1, 1, 10, 30, tzinfo=timezone.utc)


def test_update_writes_to_same_key_as_feed_reads() -> None:
    """M01-F03-R2: update() 默认 venue 与 feed 读取 key 一致（5m 生成器可达）。"""
    gen = KLineGenerator(interval="5m")
    gen.update(
        price=100.0,
        volume=1.0,
        timestamp=datetime(2026, 1, 1, 10, 4, 30, tzinfo=timezone.utc),
        symbol="BTCUSDT",
    )
    vi = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))
    current = gen.get_current_bar(vi)
    assert current is not None
    assert float(current.close.amount) == 100.0
