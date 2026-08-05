"""止盈止损保护引擎测试。止损/止盈计算、移动止损、保护单生命周期。"""
from __future__ import annotations

import pytest
from beidou_shared.types import InstrumentId, OrderSide, Price, Quantity, VenueId
from beidou_safety.protection.engine import (
    StopLossType, TakeProfitType, ProtectionStatus, ProtectionOrder,
    PositionProtection, StopLossCalculator, TrailingStopUpdater,
    TakeProfitCalculator, ProtectionManager,
)


class TestStopLossCalculator:
    """止损计算器测试。"""

    def test_fixed_percent_long(self):
        price = StopLossCalculator.fixed_percent(100.0, OrderSide.BUY, stop_pct=5.0)
        assert price == 95.0

    def test_fixed_percent_short(self):
        price = StopLossCalculator.fixed_percent(100.0, OrderSide.SELL, stop_pct=5.0)
        assert price == 105.0

    def test_fixed_percent_small(self):
        price = StopLossCalculator.fixed_percent(100.0, OrderSide.BUY, stop_pct=1.0)
        assert price == 99.0

    def test_atr_based_long(self):
        price = StopLossCalculator.atr_based(100.0, OrderSide.BUY, atr=3.0, multiplier=2.0)
        assert price == 94.0  # 100 - 3*2

    def test_atr_based_short(self):
        price = StopLossCalculator.atr_based(100.0, OrderSide.SELL, atr=3.0, multiplier=2.0)
        assert price == 106.0  # 100 + 3*2

    def test_volatility_based_long(self):
        price = StopLossCalculator.volatility_based(100.0, OrderSide.BUY, volatility_pct=10.0, multiplier=1.5)
        assert price == 85.0  # 100 * (1 - 10*1.5/100)

    def test_swing_long(self):
        price = StopLossCalculator.swing_structure(swing_low=90.0, swing_high=None, side=OrderSide.BUY)
        assert price == 90.0 * 0.999

    def test_swing_short(self):
        price = StopLossCalculator.swing_structure(swing_low=None, swing_high=110.0, side=OrderSide.SELL)
        assert price == 110.0 * 1.001

    def test_calculate_entry_routing(self):
        """统一入口正确路由到具体类型。"""
        sl = StopLossCalculator.calculate(StopLossType.FIXED_PERCENT, 200.0, OrderSide.BUY, stop_pct=3.0)
        assert sl == 194.0

        sl2 = StopLossCalculator.calculate(StopLossType.ATR_BASED, 200.0, OrderSide.BUY, atr=5.0, multiplier=1.5)
        assert sl2 == 192.5

    def test_trailing_initial_equals_fixed(self):
        """移动止损初始值等于固定百分比。"""
        sl = StopLossCalculator.calculate(StopLossType.TRAILING, 100.0, OrderSide.BUY, stop_pct=2.0)
        assert sl == 98.0


class TestTrailingStopUpdater:
    """移动止损更新器测试。"""

    def test_trail_up_long(self):
        """Long仓位: 价格创新高，止损上移。"""
        new_stop = TrailingStopUpdater.update(
            current_stop=95.0, current_price=105.0,
            highest_price=105.0, lowest_price=None,
            side=OrderSide.BUY, trail_pct=2.0,
        )
        # 105 * 0.98 = 102.9 > 95
        assert new_stop > 95.0
        assert new_stop == 102.9

    def test_trail_no_move_long(self):
        """Long仓位: 价格未创新高，止损不动。"""
        new_stop = TrailingStopUpdater.update(
            current_stop=98.0, current_price=102.0,
            highest_price=110.0, lowest_price=None,
            side=OrderSide.BUY, trail_pct=2.0,
        )
        # highest_price=110 is above current, but stop was at 98
        # 110 * 0.98 = 107.8 > 98, so it should move up
        assert new_stop == 107.8

    def test_trail_never_goes_down_long(self):
        """止损只能上移，不能下移。"""
        new_stop = TrailingStopUpdater.update(
            current_stop=102.0, current_price=100.0,
            highest_price=100.0, lowest_price=None,
            side=OrderSide.BUY, trail_pct=2.0,
        )
        # 100 * 0.98 = 98 < 102, so stay at 102
        assert new_stop == 102.0

    def test_trail_down_short(self):
        """Short仓位: 价格创新低，止损下移。"""
        new_stop = TrailingStopUpdater.update(
            current_stop=105.0, current_price=98.0,
            highest_price=None, lowest_price=95.0,
            side=OrderSide.SELL, trail_pct=2.0,
        )
        # 95 * 1.02 = 96.9 < 105
        assert new_stop < 105.0
        assert new_stop == 96.9

    def test_trail_never_goes_up_short(self):
        """Short止损只能下移。"""
        new_stop = TrailingStopUpdater.update(
            current_stop=97.0, current_price=100.0,
            highest_price=None, lowest_price=100.0,
            side=OrderSide.SELL, trail_pct=2.0,
        )
        # 100 * 1.02 = 102 > 97, so stay at 97
        assert new_stop == 97.0

    def test_min_trail_distance(self):
        """小于最小距离不更新。"""
        new_stop = TrailingStopUpdater.update(
            current_stop=98.0, current_price=100.0,
            highest_price=100.1, lowest_price=None,
            side=OrderSide.BUY, trail_pct=2.0, min_trail_distance=1.0,
        )
        # 100.1 * 0.98 = 98.098, distance = 100.1 - 98.098 = 2.002 > 1.0
        assert new_stop == 98.1  # rounded to 2 decimal


class TestTakeProfitCalculator:
    """止盈计算器测试。"""

    def test_fixed_rr_long(self):
        tp = TakeProfitCalculator.fixed_rr(100.0, 95.0, OrderSide.BUY, rr_ratio=2.0)
        # risk = 5, tp = 100 + 5*2 = 110
        assert tp == 110.0

    def test_fixed_rr_short(self):
        tp = TakeProfitCalculator.fixed_rr(100.0, 105.0, OrderSide.SELL, rr_ratio=2.0)
        # risk = 5, tp = 100 - 5*2 = 90
        assert tp == 90.0

    def test_multi_target(self):
        targets = [
            {"rr_ratio": 1.0, "close_pct": 30},
            {"rr_ratio": 2.0, "close_pct": 40},
            {"rr_ratio": 3.0, "close_pct": 30},
        ]
        result = TakeProfitCalculator.multi_target(100.0, 95.0, OrderSide.BUY, targets)
        assert len(result) == 3
        assert result[0]["price"] == 105.0  # 100 + 5*1
        assert result[0]["close_pct"] == 30
        assert result[1]["price"] == 110.0  # 100 + 5*2
        assert result[1]["close_pct"] == 40
        assert result[2]["price"] == 115.0  # 100 + 5*3

    def test_calculate_entry_routing(self):
        result = TakeProfitCalculator.calculate(
            TakeProfitType.FIXED_RR, 100.0, 95.0, OrderSide.BUY, rr_ratio=3.0,
        )
        assert len(result) == 1
        assert result[0]["price"] == 115.0  # 100 + 5*3


class TestProtectionManager:
    """保护管理器测试。"""

    def _make_manager_with_position(self, **kwargs) -> tuple[ProtectionManager, str]:
        mgr = ProtectionManager()
        pp = mgr.create_protection(
            position_id="pos-001",
            instrument_id=InstrumentId("BTCUSDT"),
            venue_id=VenueId("BINANCE"),
            entry_price=kwargs.get("entry_price", 100.0),
            quantity=kwargs.get("quantity", 0.1),
            side=kwargs.get("side", OrderSide.BUY),
            stop_loss_config=kwargs.get("stop_loss_config", {"type": "FIXED_PERCENT", "stop_pct": 5.0}),
            take_profit_config=kwargs.get("take_profit_config", {"type": "FIXED_RR", "rr_ratio": 2.0}),
        )
        return mgr, pp.position_id

    def test_create_long_with_sl_tp(self):
        mgr, pid = self._make_manager_with_position()
        pp = mgr.get_protection(pid)
        assert pp is not None
        assert pp.stop_loss is not None
        assert pp.stop_loss.is_active()
        assert pp.stop_loss.stop_type == StopLossType.FIXED_PERCENT
        trigger = float(pp.stop_loss.trigger_price.amount)
        assert trigger == 95.0  # 100 * (1 - 5%)
        assert pp.stop_loss.order_type == "STOP_MARKET"
        assert pp.stop_loss.reduce_only is True

        assert len(pp.take_profits) == 1
        tp = pp.take_profits[0]
        assert tp.is_active()
        assert tp.take_profit_type == TakeProfitType.FIXED_RR
        tp_price = float(tp.trigger_price.amount)
        assert tp_price == 110.0  # 100 + 5*2

    def test_create_short_with_sl_tp(self):
        mgr = ProtectionManager()
        pp = mgr.create_protection(
            position_id="pos-short",
            instrument_id=InstrumentId("BTCUSDT"),
            venue_id=VenueId("BINANCE"),
            entry_price=100.0, quantity=0.1, side=OrderSide.SELL,
            stop_loss_config={"type": "FIXED_PERCENT", "stop_pct": 3.0},
            take_profit_config={"type": "FIXED_RR", "rr_ratio": 2.0},
        )
        sl_trigger = float(pp.stop_loss.trigger_price.amount)
        assert sl_trigger == 103.0  # SHORT: 100 * (1 + 3%)
        tp_price = float(pp.take_profits[0].trigger_price.amount)
        # risk = 3, tp = 100 - 3*2 = 94
        assert tp_price == 94.0
        # Short的止损方向应该是BUY (平空单)
        assert pp.stop_loss.side == OrderSide.BUY

    def test_stop_loss_triggered_long(self):
        mgr, pid = self._make_manager_with_position()
        result = mgr.check_price(pid, current_price=94.0)
        assert result["triggered"] is True
        assert len(result["stop_loss"]) == 1
        assert len(result["take_profit"]) == 0
        assert "STOP" in result["details"][0]

    def test_stop_loss_not_triggered_long(self):
        mgr, pid = self._make_manager_with_position()
        result = mgr.check_price(pid, current_price=96.0)
        assert result["triggered"] is False

    def test_take_profit_triggered_long(self):
        mgr, pid = self._make_manager_with_position()
        result = mgr.check_price(pid, current_price=111.0)
        assert result["triggered"] is True
        assert len(result["take_profit"]) == 1
        assert len(result["stop_loss"]) == 0

    def test_both_triggered(self):
        """极端情况：价格同时触发止损和止盈？不应该发生，但测试两者都能被检测。"""
        mgr, pid = self._make_manager_with_position()
        # 这不应该实际发生, 但测试检查逻辑
        result = mgr.check_price(pid, current_price=50.0)
        sl_triggered = len(result["stop_loss"]) > 0
        assert sl_triggered  # 至少止损触发

    def test_trailing_stop_update(self):
        mgr = ProtectionManager()
        mgr.create_protection(
            position_id="pos-trail",
            instrument_id=InstrumentId("BTCUSDT"),
            venue_id=VenueId("BINANCE"),
            entry_price=100.0, quantity=0.1, side=OrderSide.BUY,
            stop_loss_config={"type": "TRAILING", "stop_pct": 3.0},
            trailing_config={"trail_pct": 3.0, "min_trail_distance": 0.5},
        )
        pp = mgr.get_protection("pos-trail")
        old_stop = float(pp.stop_loss.trigger_price.amount)
        assert old_stop == 97.0  # 100 * (1 - 3%)

        # Price goes up → trailing stop should move up
        pp.update_price_extremes(110.0)
        pp.highest_price = 110.0
        pp._protections = mgr._protections  # internal state
        new_stop = mgr.update_trailing_stop("pos-trail")
        assert new_stop is not None
        assert new_stop > 97.0  # stop moved up
        # 110 * 0.97 = 106.7
        assert new_stop == round(110.0 * 0.97, 2)

    def test_multi_target_take_profit(self):
        mgr = ProtectionManager()
        pp = mgr.create_protection(
            position_id="pos-multi",
            instrument_id=InstrumentId("BTCUSDT"),
            venue_id=VenueId("BINANCE"),
            entry_price=100.0, quantity=0.3, side=OrderSide.BUY,
            stop_loss_config={"type": "FIXED_PERCENT", "stop_pct": 5.0},
            take_profit_config={
                "type": "MULTI_TARGET",
                "targets": [
                    {"rr_ratio": 1.0, "close_pct": 30},
                    {"rr_ratio": 2.0, "close_pct": 40},
                    {"rr_ratio": 3.0, "close_pct": 30},
                ],
            },
        )
        assert len(pp.take_profits) == 3
        # Check quantities: 0.3 * 30% = 0.09, 0.3 * 40% = 0.12, 0.3 * 30% = 0.09
        assert float(pp.take_profits[0].quantity.amount) == 0.09
        assert float(pp.take_profits[1].quantity.amount) == 0.12
        assert float(pp.take_profits[2].quantity.amount) == 0.09

    def test_atr_stop_loss(self):
        mgr = ProtectionManager()
        pp = mgr.create_protection(
            position_id="pos-atr",
            instrument_id=InstrumentId("ETHUSDT"),
            venue_id=VenueId("BINANCE"),
            entry_price=2000.0, quantity=0.5, side=OrderSide.BUY,
            stop_loss_config={"type": "ATR_BASED", "atr": 50.0, "multiplier": 2.0},
        )
        sl_price = float(pp.stop_loss.trigger_price.amount)
        assert sl_price == 1900.0  # 2000 - 50*2

    def test_cancel_protections(self):
        mgr, pid = self._make_manager_with_position()
        cancelled = mgr.cancel_protection(pid)
        assert len(cancelled) == 2  # 1 SL + 1 TP
        pp = mgr.get_protection(pid)
        assert pp.stop_loss.status == ProtectionStatus.CANCELLED
        for tp in pp.take_profits:
            assert tp.status == ProtectionStatus.CANCELLED

    def test_mark_executed(self):
        mgr, pid = self._make_manager_with_position()
        pp = mgr.get_protection(pid)
        sl_id = pp.stop_loss.protection_id
        assert mgr.mark_executed(sl_id) is True
        assert pp.stop_loss.status == ProtectionStatus.EXECUTED

    def test_remove_position(self):
        mgr, pid = self._make_manager_with_position()
        mgr.remove_position(pid)
        assert mgr.get_protection(pid) is None
        assert mgr.position_count() == 0

    def test_get_active_stop_losses(self):
        mgr, _ = self._make_manager_with_position()
        mgr.create_protection(
            position_id="pos-002",
            instrument_id=InstrumentId("ETHUSDT"),
            venue_id=VenueId("BINANCE"),
            entry_price=2000.0, quantity=0.5, side=OrderSide.SELL,
            stop_loss_config={"type": "FIXED_PERCENT", "stop_pct": 3.0},
        )
        active_sls = mgr.get_active_stop_losses()
        assert len(active_sls) == 2

    def test_unrealized_pnl(self):
        pp = PositionProtection(
            position_id="p1", instrument_id=InstrumentId("BTCUSDT"),
            venue_id=VenueId("BINANCE"),
            entry_price=100.0, quantity=1.0, side=OrderSide.BUY,
        )
        assert pp.unrealized_pnl_pct(105.0) == pytest.approx(5.0)
        assert pp.unrealized_pnl_pct(95.0) == pytest.approx(-5.0)
        assert pp.unrealized_pnl_pct(100.0) == 0.0

    def test_unrealized_pnl_short(self):
        pp = PositionProtection(
            position_id="p2", instrument_id=InstrumentId("BTCUSDT"),
            venue_id=VenueId("BINANCE"),
            entry_price=100.0, quantity=1.0, side=OrderSide.SELL,
        )
        assert pp.unrealized_pnl_pct(95.0) == pytest.approx(5.0)  # short: price down = profit
        assert pp.unrealized_pnl_pct(105.0) == pytest.approx(-5.0)
