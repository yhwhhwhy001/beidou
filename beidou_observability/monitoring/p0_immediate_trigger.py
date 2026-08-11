"""BD-CV50: P0 事件即时触发器。

P0 事件不等待 10 分钟周期。
订单 UNKNOWN/user stream gap/missing SL/reconciliation mismatch/monitor stall → 即时触发。
P1 FAIL 聚合为 RED。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class TriggerSeverity(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


class TriggerStatus(str, Enum):
    TRIGGERED = "TRIGGERED"
    CLEAR = "CLEAR"


P0_IMMEDIATE_CONDITIONS = [
    "order.UNKNOWN",
    "user_stream.gap",
    "protection.missing_sl",
    "reconciliation.mismatch",
    "monitor.stall",
    "order.duplicate",
    "venue.disconnect",
]


@dataclass
class ImmediateTrigger:
    """BD-CV50: P0 即时触发器。

    订单 UNKNOWN/user stream gap/missing SL/reconciliation mismatch/monitor stall
    等 P0 事件必须即时触发，不等待 10 分钟。
    """

    condition_id: str
    severity: TriggerSeverity = TriggerSeverity.P0
    status: TriggerStatus = TriggerStatus.CLEAR
    triggered_at: float = 0.0
    message: str = ""


@dataclass
class P0ImmediateTriggerSystem:
    """BD-CV50: P0 即时触发系统。"""

    triggers: dict[str, ImmediateTrigger] = field(default_factory=dict)
    _last_audit_cycle: float = 0.0
    _audit_interval: float = 600.0  # 10 min — 仅低频全量审计

    def check_condition(self, condition_id: str, is_active: bool, message: str = "") -> TriggerStatus:
        """检查条件并决定是否即时触发。"""
        trigger = self.triggers.get(condition_id)
        if trigger is None:
            trigger = ImmediateTrigger(condition_id=condition_id)
            self.triggers[condition_id] = trigger

        if condition_id in P0_IMMEDIATE_CONDITIONS and is_active:
            # P0: 即时触发
            trigger.status = TriggerStatus.TRIGGERED
            trigger.triggered_at = time.time()
            trigger.message = message
            return TriggerStatus.TRIGGERED

        trigger.status = TriggerStatus.CLEAR
        return TriggerStatus.CLEAR

    def any_p0_triggered(self) -> bool:
        """BD-CV50 AC-50-02: P0 事件不等待。"""
        return any(
            t.severity == TriggerSeverity.P0 and t.status == TriggerStatus.TRIGGERED
            for t in self.triggers.values()
        )

    def aggregate_state(self) -> str:
        """BD-CV50 AC-50-01: P1 FAIL + P0 FAIL → RED。"""
        p0_triggered = any(
            t.severity == TriggerSeverity.P0 and t.status == TriggerStatus.TRIGGERED
            for t in self.triggers.values()
        )
        p1_triggered = any(
            t.severity == TriggerSeverity.P1 and t.status == TriggerStatus.TRIGGERED
            for t in self.triggers.values()
        )
        if p0_triggered:
            return "RED"
        if p1_triggered:
            return "RED"  # P1 FAIL → RED, never GREEN
        return "GREEN"

    def should_audit(self) -> bool:
        """10-min audit cycle for full system review."""
        now = time.monotonic()
        if now - self._last_audit_cycle >= self._audit_interval:
            self._last_audit_cycle = now
            return True
        return False


@dataclass
class MonitorHeartbeat:
    """BD-CV50: 监控心跳追踪。

    duration 用 monotonic、evidence observed_at 用 wall clock。
    从未 heartbeat → UNKNOWN/FAIL。
    """

    started_at_monotonic: float = 0.0
    last_heartbeat_monotonic: float = 0.0
    last_heartbeat_wallclock: str = ""
    heartbeat_count: int = 0

    def record(self) -> None:
        now_mono = time.monotonic()
        if self.started_at_monotonic == 0.0:
            self.started_at_monotonic = now_mono
        self.last_heartbeat_monotonic = now_mono
        self.heartbeat_count += 1

    def is_alive(self, timeout_seconds: float = 30.0) -> bool:
        """BD-CV50 AC-50-03: 从未 heartbeat → UNKNOWN/FAIL。"""
        if self.heartbeat_count == 0:
            return False
        return (time.monotonic() - self.last_heartbeat_monotonic) < timeout_seconds
