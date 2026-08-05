
"""P0 控制面 API。账户事实、风险状态、紧急操作。"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from beidou_shared.types import (
    AccountId, CorrelationId, InstrumentId, MonetaryValue, Quantity,
    ResultStatus, VenueId,
)

class ControlAction(str, Enum):
    NO_NEW_RISK = "NO_NEW_RISK"
    EXIT_ONLY = "EXIT_ONLY"
    EMERGENCY_FLATTEN = "EMERGENCY_FLATTEN"
    LOCK = "LOCK"
    RESUME = "RESUME"

@dataclass
class AccountFactOverview:
    account_id: AccountId; venue_id: VenueId
    total_equity: MonetaryValue; available_balance: MonetaryValue
    margin_used: MonetaryValue; margin_ratio: float
    unrealized_pnl: MonetaryValue
    positions: dict[InstrumentId, Quantity] = field(default_factory=dict)
    open_orders: int = 0; unknown_fields: list[str] = field(default_factory=list)
    differences: list[str] = field(default_factory=list)

@dataclass
class RiskDashboard:
    total_exposure: float; leverage: float
    concentration_pct: float; active_strategies: int
    pending_approvals: int; risk_events_24h: int
    system_status: str = "ACTIVE"

class ControlPlane:
    """P0 控制面。可视化风险状态、账户事实概览、紧急操作。"""
    def __init__(self):
        self._action = ControlAction.NO_NEW_RISK
        self._account_overview: dict[str, AccountFactOverview] = {}
        self._risk_dashboard = RiskDashboard(total_exposure=0, leverage=0, concentration_pct=0, active_strategies=0, pending_approvals=0, risk_events_24h=0)

    def execute_action(self, action: ControlAction) -> ResultStatus:
        self._action = action; return ResultStatus.SUCCESS

    def get_status(self) -> ControlAction:
        return self._action

    def update_account_overview(self, overview: AccountFactOverview) -> None:
        self._account_overview[f"{overview.account_id}:{overview.venue_id}"] = overview

    def get_unknown_or_differences(self) -> list[str]:
        issues: list[str] = []
        for k, o in self._account_overview.items():
            issues.extend(o.unknown_fields)
            issues.extend(o.differences)
        return issues

    def emergency_lock(self, reason: str) -> ResultStatus:
        self._action = ControlAction.LOCK
        return ResultStatus.SUCCESS
