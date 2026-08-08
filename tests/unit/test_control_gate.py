"""BD-P0-01 测试 — 控制面强制门禁、风险方向分类、状态持久化、竞态安全。"""

from __future__ import annotations

import json
import os
import tempfile

from beidou_control.plane import (
    CONTROL_ALLOW_MATRIX,
    ControlAction,
    ControlPlane,
    RiskDirection,
)
from beidou_safety.execution import OrderIntent
from beidou_shared.types import (
    AccountId,
    AccountRef,
    CorrelationId,
    InstrumentId,
    OrderSide,
    OrderType,
    Price,
    Quantity,
    TimeInForce,
    VenueId,
)


def _make_intent(
    intent_id: str = "test-001",
    side: OrderSide = OrderSide.BUY,
    order_type: OrderType = OrderType.MARKET,
    qty: float = 1.0,
    reduce_only: bool = False,
    close_position: bool = False,
    emergency_signed: bool = False,
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("default")),
        instrument_id=InstrumentId("BTCUSDT"),
        side=side,
        order_type=order_type,
        quantity=Quantity(amount=str(qty)),
        price=Price(amount="50000"),
        time_in_force=TimeInForce.GTC,
        correlation_id=CorrelationId(f"corr-{intent_id}"),
        reduce_only=reduce_only,
        close_position=close_position,
        emergency_policy_signed=emergency_signed,
    )


# ================================================================
# RiskDirection 分类测试
# ================================================================


class TestRiskDirectionClassification:
    """AC-01-01: 风险方向分类正确性。"""

    def test_entry_buy_is_increase(self):
        intent = _make_intent(side=OrderSide.BUY)
        assert ControlPlane.classify_intent(intent) == RiskDirection.INCREASE

    def test_entry_sell_is_increase(self):
        intent = _make_intent(side=OrderSide.SELL)
        assert ControlPlane.classify_intent(intent) == RiskDirection.INCREASE

    def test_reduce_only_is_reduce(self):
        intent = _make_intent(side=OrderSide.SELL, reduce_only=True)
        assert ControlPlane.classify_intent(intent) == RiskDirection.REDUCE

    def test_close_position_is_flatten(self):
        intent = _make_intent(side=OrderSide.SELL, close_position=True)
        assert ControlPlane.classify_intent(intent) == RiskDirection.FLATTEN

    def test_negative_quantity_is_reduce(self):
        intent = _make_intent(side=OrderSide.SELL, qty=-1.0)
        assert ControlPlane.classify_intent(intent) == RiskDirection.REDUCE

    def test_cancel_order_is_cancel(self):
        """CANCEL 类型订单应分类为 CANCEL 方向。"""
        assert ControlPlane.classify_risk_direction(side="BUY", order_type="CANCEL") == RiskDirection.CANCEL

    def test_query_is_query(self):
        assert ControlPlane.classify_risk_direction(side="UNKNOWN", order_type="QUERY") == RiskDirection.QUERY


# ================================================================
# 控制状态允许矩阵测试
# ================================================================


class TestControlAllowMatrix:
    """控制状态 × 风险方向完整矩阵验证。"""

    ALL_STATES = list(ControlAction)
    ALL_DIRECTIONS = list(RiskDirection)

    def test_no_new_risk_rejects_increase(self):
        """NO_NEW_RISK 必须拒绝 INCREASE。"""
        assert RiskDirection.INCREASE not in CONTROL_ALLOW_MATRIX[ControlAction.NO_NEW_RISK]

    def test_no_new_risk_allows_reduce(self):
        """NO_NEW_RISK 必须允许 REDUCE。"""
        assert RiskDirection.REDUCE in CONTROL_ALLOW_MATRIX[ControlAction.NO_NEW_RISK]

    def test_no_new_risk_allows_flatten(self):
        assert RiskDirection.FLATTEN in CONTROL_ALLOW_MATRIX[ControlAction.NO_NEW_RISK]

    def test_exit_only_rejects_increase(self):
        """EXIT_ONLY 必须拒绝 INCREASE。"""
        assert RiskDirection.INCREASE not in CONTROL_ALLOW_MATRIX[ControlAction.EXIT_ONLY]

    def test_lock_rejects_increase(self):
        """LOCK 必须拒绝 INCREASE。"""
        assert RiskDirection.INCREASE not in CONTROL_ALLOW_MATRIX[ControlAction.LOCK]

    def test_lock_rejects_reduce(self):
        """LOCK 必须拒绝 REDUCE（无 Emergency 签名）。"""
        assert RiskDirection.REDUCE not in CONTROL_ALLOW_MATRIX[ControlAction.LOCK]

    def test_lock_allows_query(self):
        """LOCK 必须允许 QUERY。"""
        assert RiskDirection.QUERY in CONTROL_ALLOW_MATRIX[ControlAction.LOCK]

    def test_resume_allows_all(self):
        """RESUME 必须允许所有方向。"""
        for direction in RiskDirection:
            assert direction in CONTROL_ALLOW_MATRIX[ControlAction.RESUME], f"RESUME should allow {direction}"

    def test_every_state_defines_matrix(self):
        """每个控制状态必须有定义好的允许矩阵。"""
        for state in ControlAction:
            assert state in CONTROL_ALLOW_MATRIX, f"{state} missing from allow matrix"
            assert len(CONTROL_ALLOW_MATRIX[state]) > 0, f"{state} has empty allow list"


# ================================================================
# Intent 验证测试
# ================================================================


class TestIntentValidation:
    """Intent 在控制面下的验证行为。"""

    def test_no_new_risk_accepts_reduce_order(self):
        cp = ControlPlane()
        cp.execute_action(ControlAction.NO_NEW_RISK)
        intent = _make_intent(side=OrderSide.SELL, reduce_only=True)
        assert cp.should_accept(intent)

    def test_no_new_risk_rejects_entry_order(self):
        cp = ControlPlane()
        cp.execute_action(ControlAction.NO_NEW_RISK)
        intent = _make_intent(side=OrderSide.BUY)
        assert not cp.should_accept(intent)
        assert cp.rejection_count() == 1

    def test_lock_rejects_entry_order(self):
        cp = ControlPlane()
        cp.execute_action(ControlAction.LOCK)
        intent = _make_intent(side=OrderSide.BUY)
        assert not cp.should_accept(intent)

    def test_lock_rejects_reduce_without_emergency(self):
        cp = ControlPlane()
        cp.execute_action(ControlAction.LOCK)
        intent = _make_intent(side=OrderSide.SELL, reduce_only=True)
        assert not cp.should_accept(intent)

    def test_lock_rejects_flatten_without_emergency(self):
        """LOCK 状态下，无 Emergency Policy 签名的 FLATTEN 应被拒绝。"""
        cp = ControlPlane()
        cp.execute_action(ControlAction.LOCK)
        intent = _make_intent(side=OrderSide.SELL, close_position=True, emergency_signed=False)
        result = cp.validate_intent(intent)
        assert not result.allowed
        assert result.reason_code == "LOCK_NO_EMERGENCY_SIGNATURE"

    def test_lock_accepts_emergency_flatten(self):
        """LOCK 状态下，有 Emergency Policy 签名的 FLATTEN 应被允许。"""
        cp = ControlPlane()
        cp.execute_action(ControlAction.LOCK)
        intent = _make_intent(side=OrderSide.SELL, close_position=True, emergency_signed=True)
        result = cp.validate_intent(intent)
        assert result.allowed, f"Emergency-signed FLATTEN should be allowed in LOCK, got {result.reason_code}"
        assert result.reason_code == "LOCK_EMERGENCY_OVERRIDE"

    def test_resume_accepts_all_intents(self):
        cp = ControlPlane()
        cp.execute_action(ControlAction.RESUME)
        for side in (OrderSide.BUY, OrderSide.SELL):
            for ro in (True, False):
                intent = _make_intent(side=side, reduce_only=ro)
                assert cp.should_accept(intent), f"RESUME should accept side={side} reduce_only={ro}"

    def test_rejection_yields_incrementing_count(self):
        cp = ControlPlane()
        cp.execute_action(ControlAction.NO_NEW_RISK)
        assert cp.rejection_count() == 0
        cp.should_accept(_make_intent(side=OrderSide.BUY, intent_id="r1"))
        assert cp.rejection_count() == 1
        cp.should_accept(_make_intent(side=OrderSide.BUY, intent_id="r2"))
        assert cp.rejection_count() == 2


# ================================================================
# 竞态安全测试
# ================================================================


class TestRaceConditionSafety:
    """AC-01-01: 状态变更时队列中未发送订单的正确拒绝。"""

    def test_state_change_rejects_queued_intents(self):
        """从 RESUME 切至 LOCK 时，之前验证通过的 Intent 在发送前被拒绝。"""
        cp = ControlPlane()

        # 在 RESUME 状态下验证通过
        cp.execute_action(ControlAction.RESUME)
        intent = _make_intent(side=OrderSide.BUY)
        assert cp.validate_intent(intent).allowed  # 通过第一次检查

        # 切换到 LOCK — 模拟竞态
        cp.execute_action(ControlAction.LOCK)

        # 第二次检查应失败（状态版本已变更）
        assert not cp.should_accept(intent)

    def test_version_increments_on_action_change(self):
        cp = ControlPlane()
        v0 = cp.version
        cp.execute_action(ControlAction.EXIT_ONLY)
        assert cp.version == v0 + 1
        cp.execute_action(ControlAction.LOCK)
        assert cp.version == v0 + 2

    def test_version_tracks_all_changes(self):
        cp = ControlPlane()
        versions = []
        for action in ControlAction:
            cp.execute_action(action)
            versions.append((action.value, cp.version))
        # 每个操作都递增
        assert len({v for _, v in versions}) == len(versions)


# ================================================================
# 拒绝审计测试
# ================================================================


class TestRejectionAudit:
    """AC-01-03: 所有拒绝可追溯到状态版本和 reason code。"""

    def test_rejection_contains_required_fields(self):
        cp = ControlPlane()
        cp.execute_action(ControlAction.NO_NEW_RISK)
        intent = _make_intent(side=OrderSide.BUY, intent_id="audit-test")
        result = cp.validate_intent(intent)
        record = cp.reject_intent(intent, result)

        assert record.intent_id == "audit-test"
        assert record.control_state == "NO_NEW_RISK"
        assert record.state_version > 0
        assert record.reason_code == "NO_NEW_RISK_REJECTS_INCREASE"
        assert "NO_NEW_RISK" in record.reason_detail
        assert len(record.intent_hash) == 16

    def test_rejection_record_to_dict(self):
        cp = ControlPlane()
        cp.execute_action(ControlAction.LOCK)
        intent = _make_intent(side=OrderSide.BUY, intent_id="dict-test")
        result = cp.validate_intent(intent)
        record = cp.reject_intent(intent, result)

        d = record.to_dict()
        assert d["intent_id"] == "dict-test"
        assert d["control_state"] == "LOCK"
        assert d["reason_code"] == "LOCK_REJECTS_INCREASE"
        assert "state_version" in d

    def test_rejection_reason_code_format(self):
        """reason code 格式: {STATE}_REJECTS_{DIRECTION}。"""
        cp = ControlPlane()
        cp.execute_action(ControlAction.EXIT_ONLY)
        intent = _make_intent(side=OrderSide.BUY)
        result = cp.validate_intent(intent)
        assert result.reason_code == "EXIT_ONLY_REJECTS_INCREASE"


# ================================================================
# 状态持久化测试
# ================================================================


class TestStatePersistence:
    """AC-01-02: 控制状态在重启后保持一致。"""

    def test_state_persisted_to_disk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            import beidou_control.plane as plane_mod

            old_file = plane_mod._STATE_FILE
            old_log = plane_mod._REJECTION_LOG
            try:
                evidence_dir = os.path.join(tmpdir, "evidence", "BD-01")
                os.makedirs(evidence_dir, exist_ok=True)
                plane_mod._STATE_FILE = os.path.join(evidence_dir, "control_state.json")
                plane_mod._REJECTION_LOG = os.path.join(evidence_dir, "rejections.jsonl")

                cp = ControlPlane()
                cp.execute_action(ControlAction.EXIT_ONLY)

                assert os.path.exists(plane_mod._STATE_FILE)
                with open(plane_mod._STATE_FILE) as f:
                    data = json.load(f)
                assert data["control_action"] == "EXIT_ONLY"
                assert data["version"] == 1
            finally:
                plane_mod._STATE_FILE = old_file
                plane_mod._REJECTION_LOG = old_log

    def test_restore_respects_higher_disk_version(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            import beidou_control.plane as plane_mod

            old_file = plane_mod._STATE_FILE
            old_log = plane_mod._REJECTION_LOG
            try:
                evidence_dir = os.path.join(tmpdir, "evidence", "BD-01")
                os.makedirs(evidence_dir, exist_ok=True)
                plane_mod._STATE_FILE = os.path.join(evidence_dir, "control_state.json")
                plane_mod._REJECTION_LOG = os.path.join(evidence_dir, "rejections.jsonl")

                # 写入高版本状态（模拟另一个实例的持久化）
                with open(plane_mod._STATE_FILE, "w") as f:
                    json.dump({"control_action": "LOCK", "version": 10}, f)

                cp = ControlPlane()
                assert cp.version == 0
                restored = cp.restore_state()
                assert restored
                assert cp.version == 10
                assert cp.get_status() == ControlAction.LOCK
            finally:
                plane_mod._STATE_FILE = old_file
                plane_mod._REJECTION_LOG = old_log

    def test_memory_version_higher_ignores_disk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            import beidou_control.plane as plane_mod

            old_file = plane_mod._STATE_FILE
            old_log = plane_mod._REJECTION_LOG
            try:
                evidence_dir = os.path.join(tmpdir, "evidence", "BD-01")
                os.makedirs(evidence_dir, exist_ok=True)
                plane_mod._STATE_FILE = os.path.join(evidence_dir, "control_state.json")
                plane_mod._REJECTION_LOG = os.path.join(evidence_dir, "rejections.jsonl")

                cp = ControlPlane()
                cp.execute_action(ControlAction.RESUME)
                cp.execute_action(ControlAction.RESUME)  # v=2
                # 磁盘上是 v=1 的旧数据
                assert cp.version == 2
                restored = cp.restore_state()
                assert not restored  # 内存版本更新，不覆盖
                assert cp.version == 2
            finally:
                plane_mod._STATE_FILE = old_file
                plane_mod._REJECTION_LOG = old_log


# ================================================================
# UNKNOWN fail-closed 测试
# ================================================================


class TestUnknownFailClosed:
    """UNKNOWN 控制状态必须 FAIL-CLOSED。"""

    def test_unknown_direction_treated_as_increase(self):
        """未识别的订单方向 → INCREASE（最保守分类）。"""
        assert ControlPlane.classify_risk_direction(side="UNKNOWN_DIRECTION") == RiskDirection.INCREASE

    def test_default_matrix_does_not_allow_unknown(self):
        """默认矩阵不应包含未定义的状态。"""
        # 每个已定义的状态都有明确的 allowed set
        for _state, allowed in CONTROL_ALLOW_MATRIX.items():
            assert isinstance(allowed, set)
            for d in allowed:
                assert isinstance(d, RiskDirection)
