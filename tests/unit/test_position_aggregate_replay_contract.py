"""PositionAggregate.replay reduce-only 跨零防护的契约测试（M00-F04）。

背景: 契约版 replay 对违反 reduce-only 方向的成交仅 ``pass`` 空操作，
成交仍被应用 → 潜伏跨零反向开仓；且缺失 BUY 侧对称防护。修复语义
（与既有 test_contract_bridges 的开仓用例一致）：从空仓建仓不受限，
仅拦截会使净仓位穿越零点的成交（多头 SELL 超量 / 空头 BUY 超量）。
"""

from __future__ import annotations

from beidou_safety.execution.contracts import Fill, PositionAggregate


def _fill(side: str, qty: float, price: float = 100.0) -> Fill:
    return Fill(fill_id=f"{side}-{qty}", symbol="BTCUSDT", side=side, quantity=qty, price=price)


def test_open_from_flat_is_allowed() -> None:
    """从空仓建仓是 replay 的合法主用例（与 test_contract_bridges 一致）。"""
    assert PositionAggregate(symbol="BTCUSDT").replay([_fill("BUY", 1.0)]).net_position == 1.0
    assert PositionAggregate(symbol="BTCUSDT").replay([_fill("SELL", 1.0)]).net_position == -1.0


def test_sell_beyond_long_is_skipped_no_cross_zero() -> None:
    pa = PositionAggregate(symbol="BTCUSDT", net_position=2.0, avg_entry_price=90.0).replay(
        [_fill("SELL", 1.0), _fill("SELL", 5.0)]  # SELL 1 合规,SELL 5 会跨零 → 跳过
    )
    assert pa.net_position == 1.0
    assert pa.is_reduce_only_compliant is False


def test_buy_beyond_short_is_skipped_no_cross_zero() -> None:
    pa = PositionAggregate(symbol="BTCUSDT", net_position=-2.0, avg_entry_price=110.0).replay(
        [_fill("BUY", 1.0), _fill("BUY", 5.0)]  # BUY 1 合规,BUY 5 会跨零 → 跳过
    )
    assert pa.net_position == -1.0
    assert pa.is_reduce_only_compliant is False


def test_sell_exactly_to_zero_is_allowed() -> None:
    """恰好减到零不跨零，合规。"""
    pa = PositionAggregate(symbol="BTCUSDT", net_position=2.0).replay([_fill("SELL", 2.0)])
    assert pa.net_position == 0.0
    assert pa.is_reduce_only_compliant is True


def test_buy_exactly_to_zero_is_allowed() -> None:
    pa = PositionAggregate(symbol="BTCUSDT", net_position=-2.0).replay([_fill("BUY", 2.0)])
    assert pa.net_position == 0.0
    assert pa.is_reduce_only_compliant is True


def test_same_direction_adds_are_allowed() -> None:
    pa = PositionAggregate(symbol="BTCUSDT", net_position=2.0).replay([_fill("BUY", 1.0)])
    assert pa.net_position == 3.0
    assert pa.is_reduce_only_compliant is True


def test_replay_is_deterministic() -> None:
    fills = [_fill("BUY", 2.0), _fill("SELL", 1.0), _fill("SELL", 5.0), _fill("BUY", 0.5)]
    first = PositionAggregate(symbol="BTCUSDT").replay(fills)
    second = PositionAggregate(symbol="BTCUSDT").replay(fills)
    assert first == second
    assert first.net_position == 1.5  # SELL 5 跨零被跳过


def test_previously_non_compliant_aggregate_applies_without_guard() -> None:
    """已标记非合规的聚合（历史事实）保持原语义：不再拦截后续成交。"""
    pa = PositionAggregate(symbol="BTCUSDT", net_position=2.0, is_reduce_only_compliant=False).replay(
        [_fill("SELL", 5.0)]
    )
    assert pa.net_position == -3.0
    assert pa.is_reduce_only_compliant is False


def test_fills_history_preserves_raw_sequence() -> None:
    pa = PositionAggregate(symbol="BTCUSDT").replay([_fill("SELL", 1.0), _fill("BUY", 1.0)])
    assert len(pa.fills) == 2  # 历史保留原始序列（含被跳过的成交）
    assert pa.net_position == 0.0
    assert pa.is_reduce_only_compliant is True
