"""
剩余 P0 修复测试: BDS-P0-013~021, P0-023

覆盖:
- P0-016: EmergencyReduceOnly 双向 reduce-only
- P0-015: 固定 min_quantity 改为从 venue rules 获取
- P0-017: 终态订单不可被 UNKNOWN 覆盖
- P0-018: PARTIALLY_FILLED 恢复保留交易所状态
- P0-023: 杠杆变更权限管控
"""

from __future__ import annotations

from beidou_safety.execution.order_state import (
    OrderEvent,
    OrderStateTracker,
    OrderStatus,
)
from beidou_safety.execution.algorithms import (
    EmergencyReduceOnlyAlgorithm,
    ExecutionAlgorithmType,
)
from beidou_control.plane import ControlPlane, ControlAction, RiskDirection


class TestEmergencyReduceOnly:
    """P0-016: 应急减仓双向支持。"""

    def test_can_handle_long_position(self) -> None:
        """LONG 仓位可应急退出 (SELL)。"""
        algo = EmergencyReduceOnlyAlgorithm()
        # 用 mock context 测试
        from unittest.mock import Mock
        ctx = Mock()
        ctx.urgency = 0.9
        ctx.side = "BUY"
        ctx.position_side = "LONG"
        ctx.total_quantity.amount = "1.0"
        ctx.predicted_cost_bps = 10.0
        assert algo.can_handle(ctx)

    def test_can_handle_short_position(self) -> None:
        """SHORT 仓位可应急退出 (BUY reduce-only)。"""
        from unittest.mock import Mock
        algo = EmergencyReduceOnlyAlgorithm()
        ctx = Mock()
        ctx.urgency = 0.9
        ctx.side = "SELL"
        ctx.position_side = "SHORT"
        ctx.total_quantity.amount = "1.0"
        ctx.predicted_cost_bps = 10.0
        # 修复后 SHORT 也能应急退出
        assert algo.can_handle(ctx)

    def test_emergency_reduce_only_type(self) -> None:
        """算法类型正确。"""
        algo = EmergencyReduceOnlyAlgorithm()
        assert algo.algorithm_type == ExecutionAlgorithmType.EMERGENCY_REDUCE_ONLY


class TestOrderTerminalProtection:
    """P0-017, P0-018: 订单终态保护 + 恢复保留状态。"""

    def test_filled_cannot_become_unknown(self) -> None:
        """FILLED 终态不可转 UNKNOWN。"""
        tracker = OrderStateTracker(order_id="test-filled")
        tracker.status = OrderStatus.FILLED
        result = tracker.apply(OrderEvent.UNKNOWN)
        assert result is False  # 终态拒绝
        assert tracker.status == OrderStatus.FILLED

    def test_canceled_cannot_become_unknown(self) -> None:
        """CANCELED 终态不可转 UNKNOWN。"""
        tracker = OrderStateTracker(order_id="test-canceled")
        tracker.status = OrderStatus.CANCELED
        result = tracker.apply(OrderEvent.UNKNOWN)
        assert result is False
        assert tracker.status == OrderStatus.CANCELED

    def test_rejected_cannot_become_unknown(self) -> None:
        """REJECTED 终态不可转 UNKNOWN。"""
        tracker = OrderStateTracker(order_id="test-rejected")
        tracker.status = OrderStatus.REJECTED
        result = tracker.apply(OrderEvent.UNKNOWN)
        assert result is False

    def test_normal_can_become_unknown(self) -> None:
        """非终态可以进入 UNKNOWN。"""
        tracker = OrderStateTracker(order_id="test-new")
        tracker.status = OrderStatus.NEW
        result = tracker.apply(OrderEvent.UNKNOWN)
        assert result is True
        assert tracker.status == OrderStatus.UNKNOWN

    def test_unknown_recovery_preserves_state(self) -> None:
        """UNKNOWN 恢复时保留交易所事实（PARTIALLY_FILLED 而非空白 NEW）。"""
        tracker = OrderStateTracker(order_id="test-recover")
        tracker.status = OrderStatus.UNKNOWN
        result = tracker.apply(OrderEvent.RECOVERED)
        assert result is True
        # PKG14: 恢复后为 PARTIALLY_FILLED — 保留交易所真实状态
        # 不再直接跳到 NEW（那样会丢失 filled_qty/avg_price）
        assert tracker.status == OrderStatus.PARTIALLY_FILLED

    def test_terminal_states_detected(self) -> None:
        """终态检测正确。"""
        for status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
            tracker = OrderStateTracker(order_id=f"term-{status.value}")
            tracker.status = status
            assert tracker.is_terminal()


class TestLeverageControl:
    """P0-023: 杠杆变更权限管控。"""

    def test_leverage_rejected_in_locked_state(self) -> None:
        """LOCK 状态拒绝杠杆变更。"""
        plane = ControlPlane()
        plane.execute_action(ControlAction.LOCK)
        result = plane.validate_leverage_change("BTCUSDT", 5.0)
        assert not result.allowed
        assert "LOCK" in result.reason_code

    def test_leverage_rejected_in_no_new_risk(self) -> None:
        """NO_NEW_RISK 状态拒绝杠杆变更。"""
        plane = ControlPlane()
        plane.execute_action(ControlAction.NO_NEW_RISK)
        result = plane.validate_leverage_change("ETHUSDT", 3.0)
        assert not result.allowed

    def test_leverage_allowed_in_resume(self) -> None:
        """RESUME 状态允许杠杆变更。"""
        plane = ControlPlane()
        plane.execute_action(ControlAction.RESUME)
        result = plane.validate_leverage_change("BTCUSDT", 2.0)
        assert result.allowed
