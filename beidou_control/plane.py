"""P0 控制面 API — 订单强制门禁、风险方向分类、状态版本化与拒绝审计。

每个订单 Intent 在提交 Outbox 前和 Executor 发送前各校验一次控制状态。
控制状态带单调递增版本号，旧实例不得使用过期状态。
每次拒绝记录 intent hash、状态版本、reason code。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class RiskDirection(str, Enum):
    """Intent 的风险方向分类。

    - INCREASE: 增加风险暴露（开仓、加仓）
    - REDUCE: 减少风险暴露（减仓、止盈、止损）
    - FLATTEN: 紧急平仓全部头寸
    - CANCEL: 撤销已有订单
    - QUERY: 只读查询操作
    """

    INCREASE = "INCREASE"
    REDUCE = "REDUCE"
    FLATTEN = "FLATTEN"
    CANCEL = "CANCEL"
    QUERY = "QUERY"


class ControlAction(str, Enum):
    NO_NEW_RISK = "NO_NEW_RISK"
    EXIT_ONLY = "EXIT_ONLY"
    EMERGENCY_FLATTEN = "EMERGENCY_FLATTEN"
    LOCK = "LOCK"
    RESUME = "RESUME"


# 控制状态 → 允许的风险方向矩阵
CONTROL_ALLOW_MATRIX: dict[ControlAction, set[RiskDirection]] = {
    ControlAction.NO_NEW_RISK: {
        RiskDirection.REDUCE,
        RiskDirection.FLATTEN,
        RiskDirection.CANCEL,
        RiskDirection.QUERY,
    },
    ControlAction.EXIT_ONLY: {
        RiskDirection.REDUCE,
        RiskDirection.FLATTEN,
        RiskDirection.CANCEL,
        RiskDirection.QUERY,
    },
    ControlAction.EMERGENCY_FLATTEN: {
        RiskDirection.FLATTEN,
        RiskDirection.CANCEL,
        RiskDirection.QUERY,
    },
    ControlAction.LOCK: {
        RiskDirection.QUERY,
    },
    ControlAction.RESUME: {
        RiskDirection.INCREASE,
        RiskDirection.REDUCE,
        RiskDirection.FLATTEN,
        RiskDirection.CANCEL,
        RiskDirection.QUERY,
    },
}


@dataclass
class RejectionRecord:
    """不可变拒绝记录。"""

    timestamp: str
    intent_hash: str
    intent_id: str
    control_state: str
    state_version: int
    risk_direction: str
    reason_code: str
    reason_detail: str

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "intent_hash": self.intent_hash,
            "intent_id": self.intent_id,
            "control_state": self.control_state,
            "state_version": self.state_version,
            "risk_direction": self.risk_direction,
            "reason_code": self.reason_code,
            "reason_detail": self.reason_detail,
        }


@dataclass
class ValidationResult:
    """Intent 验证结果。"""

    allowed: bool
    reason_code: str
    state_version: int
    control_state: str
    risk_direction: RiskDirection


# --- 状态持久化分隔符 ---
_STATE_FILE = "evidence/BD-01/control_state.json"
_REJECTION_LOG = "evidence/BD-01/rejections.jsonl"


class ControlPlane:
    """P0 控制面。风险状态、Intent 门禁、版本化持久化。"""

    def __init__(self):
        self._action = ControlAction.NO_NEW_RISK
        self._version: int = 0
        self._rejections: list[RejectionRecord] = []
        self._state_change_log: list[dict] = []
        self._account_overview: dict[str, AccountFactOverview] = {}
        self._risk_dashboard = RiskDashboard(
            total_exposure=0,
            leverage=0,
            concentration_pct=0,
            active_strategies=0,
            pending_approvals=0,
            risk_events_24h=0,
        )

    # --- State management ---

    def execute_action(self, action: ControlAction) -> ControlAction:
        """执行状态变更，递增版本号，持久化。"""
        old_action = self._action
        self._action = action
        self._version += 1
        self._log_state_change(old_action.value, action.value)
        self._persist_state()
        return action

    def get_status(self) -> ControlAction:
        return self._action

    @property
    def version(self) -> int:
        return self._version

    # --- Intent classification ---

    @staticmethod
    def classify_risk_direction(
        side: str,
        reduce_only: bool = False,
        close_position: bool = False,
        order_type: str = "MARKET",
    ) -> RiskDirection:
        """根据订单参数分类风险方向。

        分类规则（按优先级）:
        1. CANCEL orders → CANCEL
        2. closePosition=True 或 FLATTEN 语义 → FLATTEN
        3. reduceOnly=True → REDUCE
        4. BUY 开仓 / SELL 开仓 → INCREASE
        5. 只读/查询类 → QUERY
        """
        side_upper = side.upper() if side else "UNKNOWN"

        # 查询/只读类操作
        if order_type.upper() in ("QUERY", "STATUS", "INFO"):
            return RiskDirection.QUERY

        # 取消类
        if order_type.upper() in ("CANCEL", "CANCEL_ORDER"):
            return RiskDirection.CANCEL

        # 紧急平仓
        if close_position:
            return RiskDirection.FLATTEN

        # 减仓/保护
        if reduce_only:
            return RiskDirection.REDUCE

        # 开仓方向判断
        # 注意：此分类需要配合仓位上下文才能精确判断
        # 无仓位上下文的默认分类：新订单视为 INCREASE
        if side_upper in ("BUY", "SELL"):
            return RiskDirection.INCREASE

        # UNKNOWN → fail-closed: 任何未知方向视为 INCREASE（最保守）
        return RiskDirection.INCREASE

    @staticmethod
    def classify_intent(intent: Any) -> RiskDirection:
        """从 OrderIntent 对象分类风险方向。"""
        side = str(getattr(intent, "side", "UNKNOWN"))
        order_type = str(getattr(intent, "order_type", "MARKET"))

        # 检查是否有 reduce_only 或 close_position 标记
        reduce_only = getattr(intent, "reduce_only", False)
        close_position = getattr(intent, "close_position", False)

        # 也检查 quantity 符号（负数量表示减仓）
        qty = getattr(intent, "quantity", None)
        if qty is not None:
            qty_val = float(getattr(qty, "amount", 0))
            if qty_val < 0:
                reduce_only = True

        return ControlPlane.classify_risk_direction(
            side=side,
            reduce_only=reduce_only,
            close_position=close_position,
            order_type=order_type,
        )

    # --- Intent validation ---

    def validate_intent(self, intent: Any) -> ValidationResult:
        """验证 Intent 是否在当前控制状态下被允许。

        返回 ValidationResult:
        - allowed=True: 可以通过
        - allowed=False: 被拒绝，含 reason_code
        """
        direction = self.classify_intent(intent)
        allowed_directions = CONTROL_ALLOW_MATRIX.get(self._action, {RiskDirection.QUERY})

        # LOCK 状态特殊处理：FLATTEN/CANCEL 需要 Emergency Policy 签名
        # BD-FIX (S1): LOCK 状态下拒绝所有操作（移除可被布尔字段绕过的紧急旁路）。
        # 紧急平仓必须先将控制面切换到 EMERGENCY_FLATTEN。
        if self._action == ControlAction.LOCK:
            return ValidationResult(
                allowed=False,
                reason_code="LOCK_REJECTS_ALL",
                state_version=self._version,
                control_state=self._action.value,
                risk_direction=direction,
            )

        allowed = direction in allowed_directions

        if not allowed:
            reason_code = f"{self._action.value}_REJECTS_{direction.value}"
            return ValidationResult(
                allowed=False,
                reason_code=reason_code,
                state_version=self._version,
                control_state=self._action.value,
                risk_direction=direction,
            )

        return ValidationResult(
            allowed=True,
            reason_code="OK",
            state_version=self._version,
            control_state=self._action.value,
            risk_direction=direction,
        )

    def reject_intent(self, intent: Any, result: ValidationResult) -> RejectionRecord:
        """记录一次拒绝，写入不可变审计日志。"""
        intent_id = str(getattr(intent, "intent_id", "UNKNOWN"))
        intent_hash = self._compute_intent_hash(intent)
        record = RejectionRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            intent_hash=intent_hash,
            intent_id=intent_id,
            control_state=result.control_state,
            state_version=result.state_version,
            risk_direction=result.risk_direction.value,
            reason_code=result.reason_code,
            reason_detail=f"State {result.control_state} v{result.state_version} rejects {result.risk_direction.value}",
        )
        self._rejections.append(record)
        self._persist_rejection(record)
        return record

    def should_accept(self, intent: Any) -> bool:
        """便捷方法：Intent 是否应被接受。拒绝时自动记录审计事件。"""
        result = self.validate_intent(intent)
        if not result.allowed:
            self.reject_intent(intent, result)
        return result.allowed

    # --- Rejection audit ---

    def rejection_count(self) -> int:
        return len(self._rejections)

    def recent_rejections(self, n: int = 20) -> list[dict]:
        return [r.to_dict() for r in self._rejections[-n:]]

    # --- State persistence ---

    def _persist_state(self) -> None:
        """将控制状态持久化到磁盘。"""
        os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
        state_data = {
            "control_action": self._action.value,
            "version": self._version,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "change_log": self._state_change_log[-50:],  # 保留最近 50 条
        }
        with open(_STATE_FILE, "w") as f:
            json.dump(state_data, f, indent=2)

    def _persist_rejection(self, record: RejectionRecord) -> None:
        """将拒绝记录追加到审计日志。"""
        os.makedirs(os.path.dirname(_REJECTION_LOG), exist_ok=True)
        with open(_REJECTION_LOG, "a") as f:
            f.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")

    def _log_state_change(self, old_state: str, new_state: str) -> None:
        self._state_change_log.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "old_state": old_state,
                "new_state": new_state,
                "version": self._version,
            }
        )

    def restore_state(self) -> bool:
        """从磁盘恢复控制状态。如果磁盘版本 > 内存版本，更新内存状态。"""
        if not os.path.exists(_STATE_FILE):
            return False
        try:
            with open(_STATE_FILE) as f:
                data = json.load(f)
            disk_version = data.get("version", 0)
            if disk_version > self._version:
                self._action = ControlAction(data.get("control_action", "NO_NEW_RISK"))
                self._version = disk_version
                return True
            return False  # 内存版本 >= 磁盘版本
        except (json.JSONDecodeError, KeyError):
            return False

    @staticmethod
    def _compute_intent_hash(intent: Any) -> str:
        """计算 Intent 的稳定哈希。"""
        parts = [
            str(getattr(intent, "intent_id", "")),
            str(getattr(intent, "side", "")),
            str(getattr(intent, "order_type", "")),
            str(getattr(intent, "quantity", "")),
            str(getattr(intent, "instrument_id", "")),
        ]
        return hashlib.sha256(":".join(parts).encode()).hexdigest()[:16]

    # --- Legacy compatibility ---

    def emergency_lock(self, reason: str) -> ControlAction:
        self._log_state_change(self._action.value, ControlAction.LOCK.value)
        return self.execute_action(ControlAction.LOCK)

    def update_account_overview(self, overview: AccountFactOverview) -> None:
        self._account_overview[f"{overview.account_id}:{overview.venue_id}"] = overview

    def get_unknown_or_differences(self) -> list[str]:
        issues: list[str] = []
        for _k, o in self._account_overview.items():
            issues.extend(o.unknown_fields)
            issues.extend(o.differences)
        return issues


# --- 向后兼容类型（未被 BD-P0-01 修改） ---

from beidou_shared.types import (
    AccountId,
    InstrumentId,
    MonetaryValue,
    Quantity,
    VenueId,
)


@dataclass
class AccountFactOverview:
    """账户事实概览 — 与 ReconciliationEngine 配合使用。"""

    account_id: AccountId
    venue_id: VenueId
    total_equity: MonetaryValue
    available_balance: MonetaryValue
    margin_used: MonetaryValue
    margin_ratio: float
    unrealized_pnl: MonetaryValue
    positions: dict[InstrumentId, Quantity] = field(default_factory=dict)
    open_orders: int = 0
    unknown_fields: list[str] = field(default_factory=list)
    differences: list[str] = field(default_factory=list)


@dataclass
class RiskDashboard:
    """风险仪表盘概览。"""

    total_exposure: float
    leverage: float
    concentration_pct: float
    active_strategies: int
    pending_approvals: int
    risk_events_24h: int
    system_status: str = "ACTIVE"
