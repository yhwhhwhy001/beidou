"""持续对账、前置账户事实缓存与差异修复。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from beidou_shared.types import AccountId, CorrelationId, MonetaryValue, Quantity, VenueId


@dataclass
class AccountFactSnapshot:
    account_id: AccountId
    venue_id: VenueId
    balance: MonetaryValue
    positions: dict[str, Quantity]
    open_orders: list[str]
    margin_used: MonetaryValue | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    correlation_id: CorrelationId | None = None


@dataclass
class ReconciliationResult:
    matched: bool
    differences: list[str] = field(default_factory=list)
    system_facts: AccountFactSnapshot | None = None
    exchange_facts: AccountFactSnapshot | None = None


class ReconciliationEngine:
    """对账引擎。比较系统事实与交易所事实，差异需修复。"""

    def __init__(self) -> None:
        self._system_facts: dict[str, AccountFactSnapshot] = {}
        self._exchange_facts: dict[str, AccountFactSnapshot] = {}

    def update_system_facts(self, facts: AccountFactSnapshot) -> None:
        self._system_facts[f"{facts.account_id}:{facts.venue_id}"] = facts

    def update_exchange_facts(self, facts: AccountFactSnapshot) -> None:
        self._exchange_facts[f"{facts.account_id}:{facts.venue_id}"] = facts

    def reconcile(self, account_id: AccountId, venue_id: VenueId) -> ReconciliationResult:
        key = f"{account_id}:{venue_id}"
        sys_facts = self._system_facts.get(key)
        ex_facts = self._exchange_facts.get(key)
        if sys_facts is None and ex_facts is None:
            return ReconciliationResult(matched=True)
        if sys_facts is None or ex_facts is None:
            return ReconciliationResult(
                matched=False, differences=["One side missing"], system_facts=sys_facts, exchange_facts=ex_facts
            )
        diffs: list[str] = []

        # 余额比较
        if float(sys_facts.balance.amount) != float(ex_facts.balance.amount):
            diffs.append(f"Balance mismatch: system={sys_facts.balance.amount} exchange={ex_facts.balance.amount}")

        # 活跃订单比较（集合比较，忽略顺序和时序差异）
        sys_orders = set(sys_facts.open_orders)
        ex_orders = set(ex_facts.open_orders)
        if sys_orders != ex_orders:
            missing_on_exchange = sys_orders - ex_orders
            extra_on_exchange = ex_orders - sys_orders
            parts = []
            if missing_on_exchange:
                parts.append(f"Orders in system but not on exchange: {sorted(missing_on_exchange)}")
            if extra_on_exchange:
                parts.append(f"Orders on exchange but not in system: {sorted(extra_on_exchange)}")
            if parts:
                diffs.append("Open orders mismatch: " + "; ".join(parts))

        # 持仓比较
        sys_pos = {str(k): str(v.amount) for k, v in sys_facts.positions.items()}
        ex_pos = {str(k): str(v.amount) for k, v in ex_facts.positions.items()}
        if sys_pos != ex_pos:
            diffs.append(f"Position mismatch: system={sys_pos} exchange={ex_pos}")

        return ReconciliationResult(
            matched=len(diffs) == 0, differences=diffs, system_facts=sys_facts, exchange_facts=ex_facts
        )

    def repair_strategy(self, result: ReconciliationResult) -> str:
        if result.matched:
            return "NO_ACTION"
        if any("Balance mismatch" in d for d in result.differences):
            return "SYSTEM_IS_AUTHORITATIVE"
        return "INVESTIGATE"
