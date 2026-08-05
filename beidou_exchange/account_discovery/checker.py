"""账户能力发现与权限预检实现。查询失败≠空仓。"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from beidou_shared.types import AccountId, CorrelationId, MonetaryValue, Quantity, ResultStatus, VenueId

class AccountQueryStatus(str, Enum):
    SUCCESS = "SUCCESS"
    EMPTY = "EMPTY"
    KNOWN_NONEMPTY = "KNOWN_NONEMPTY"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"
    UNAUTHORIZED = "UNAUTHORIZED"
    RATE_LIMITED = "RATE_LIMITED"

@dataclass(frozen=True, slots=True)
class AccountQueryResult:
    status: AccountQueryStatus
    data: dict[str, Any] | None = None
    correlation_id: CorrelationId | None = None
    error_message: str | None = None
    latency_ms: float | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def is_definitive(self) -> bool:
        return self.status in (AccountQueryStatus.SUCCESS, AccountQueryStatus.EMPTY, AccountQueryStatus.KNOWN_NONEMPTY)

    def should_fail_closed(self) -> bool:
        return self.status in (AccountQueryStatus.UNKNOWN, AccountQueryStatus.ERROR, AccountQueryStatus.UNAUTHORIZED)

@dataclass(frozen=True, slots=True)
class AccountCapabilityReport:
    venue_id: VenueId
    account_id: AccountId
    can_read_balances: AccountQueryResult
    can_read_positions: AccountQueryResult
    can_read_open_orders: AccountQueryResult
    can_trade: AccountQueryResult
    can_withdraw: AccountQueryResult
    ip_whitelisted: bool
    clock_skew_ms: float | None = None
    rate_limit_baseline: dict[str, int] = field(default_factory=dict)
    correlation_id: CorrelationId | None = None
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def is_safe_for_trading(self) -> bool:
        p0_blocks: list[str] = []
        if self.can_withdraw.status not in (AccountQueryStatus.EMPTY,):
            p0_blocks.append("WITHDRAW_PERMISSION_DETECTED")
        if self.can_read_balances.should_fail_closed():
            p0_blocks.append("BALANCE_QUERY_FAILED")
        if self.can_read_positions.should_fail_closed():
            p0_blocks.append("POSITION_QUERY_FAILED")
        if self.can_trade.should_fail_closed():
            p0_blocks.append("TRADE_PERMISSION_UNKNOWN")
        return len(p0_blocks) == 0

    def trading_blockers(self) -> list[str]:
        blockers: list[str] = []
        if self.can_withdraw.status not in (AccountQueryStatus.EMPTY,):
            blockers.append("WITHDRAW_PERMISSION_DETECTED")
        if self.can_read_balances.should_fail_closed():
            blockers.append("BALANCE_QUERY_FAILED")
        if self.can_read_positions.should_fail_closed():
            blockers.append("POSITION_QUERY_FAILED")
        if self.can_trade.should_fail_closed():
            blockers.append("TRADE_PERMISSION_UNKNOWN")
        return blockers


class AccountCapabilityChecker:
    """账户能力检查器 — 使用只读凭据验证账户语义。

    查询失败绝不转为空仓/零余额/无订单。
    """

    def classify_result(self, status: ResultStatus, data: Any, error: str | None = None) -> AccountQueryStatus:
        if status == ResultStatus.SUCCESS:
            if data is None or data == {} or data == []:
                return AccountQueryStatus.EMPTY
            return AccountQueryStatus.KNOWN_NONEMPTY
        if status == ResultStatus.EMPTY:
            return AccountQueryStatus.EMPTY
        if status == ResultStatus.ERROR:
            if error and "unauthorized" in error.lower():
                return AccountQueryStatus.UNAUTHORIZED
            if error and ("rate" in error.lower() or "limit" in error.lower()):
                return AccountQueryStatus.RATE_LIMITED
            return AccountQueryStatus.ERROR
        if status == ResultStatus.UNSUPPORTED:
            return AccountQueryStatus.ERROR
        return AccountQueryStatus.UNKNOWN

    def validate_withdraw_permission(self, can_withdraw: bool) -> AccountQueryResult:
        if can_withdraw:
            return AccountQueryResult(
                status=AccountQueryStatus.KNOWN_NONEMPTY,
                error_message="WITHDRAW_PERMISSION_DETECTED: trading must not have withdraw capability",
            )
        return AccountQueryResult(status=AccountQueryStatus.EMPTY)

    def generate_report(
        self,
        venue_id: VenueId,
        account_id: AccountId,
        balance_result: AccountQueryResult,
        position_result: AccountQueryResult,
        order_result: AccountQueryResult,
        trade_permission_result: AccountQueryResult,
        withdraw_result: AccountQueryResult,
        ip_whitelisted: bool = True,
        clock_skew_ms: float | None = None,
    ) -> AccountCapabilityReport:
        return AccountCapabilityReport(
            venue_id=venue_id,
            account_id=account_id,
            can_read_balances=balance_result,
            can_read_positions=position_result,
            can_read_open_orders=order_result,
            can_trade=trade_permission_result,
            can_withdraw=withdraw_result,
            ip_whitelisted=ip_whitelisted,
            clock_skew_ms=clock_skew_ms,
        )

    def owner_disconnected_check(self, report: AccountCapabilityReport) -> ResultStatus:
        """Owner 失联时不自动解锁 UNKNOWN/LOCKED。"""
        if not report.is_safe_for_trading():
            return ResultStatus.UNKNOWN
        for field in [report.can_read_balances, report.can_read_positions, report.can_trade]:
            if field.should_fail_closed():
                return ResultStatus.UNKNOWN
        return ResultStatus.SUCCESS
