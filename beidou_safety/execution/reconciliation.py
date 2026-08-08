"""持续对账、前置账户事实缓存与差异修复。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from beidou_shared.types import AccountId, CorrelationId, MonetaryValue, Quantity, VenueId


class ReconciliationStatus(str, Enum):
    """BD-P0-10: 对账结果状态。"""

    MATCHED = "MATCHED"
    MISMATCHED = "MISMATCHED"
    ONE_SIDE_MISSING = "ONE_SIDE_MISSING"
    BOTH_SIDES_MISSING = "BOTH_SIDES_MISSING"  # → UNKNOWN, blocks new risk
    STALE = "STALE"  # BD-T13: 数据过期，阻断新风险
    ERROR = "ERROR"

    @property
    def is_safe(self) -> bool:
        """是否可以安全继续交易。仅 MATCHED 可安全。"""
        return self in (ReconciliationStatus.MATCHED,)


@dataclass
class ReconciliationResult:
    """BD-P0-10: 对账结果。包含类型化状态和差异列表。"""

    matched: bool
    status: ReconciliationStatus = ReconciliationStatus.MATCHED
    differences: list[str] = field(default_factory=list)
    system_facts: AccountFactSnapshot | None = None
    exchange_facts: AccountFactSnapshot | None = None

    @property
    def is_unknown(self) -> bool:
        return self.status == ReconciliationStatus.BOTH_SIDES_MISSING

    @property
    def should_block_new_risk(self) -> bool:
        """BD-P0-10 AC-10-01: 所有非 MATCHED 状态（MISMATCHED/ONE_SIDE_MISSING/BOTH_SIDES_MISSING/ERROR）均阻止新风险。"""
        return self.status is not ReconciliationStatus.MATCHED


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
        """BD-P0-10: 对账 — 双方缺失 → UNKNOWN，从不匹配。

        AC-10-01: both-sides-missing → UNKNOWN, blocks new risk.
        AC-10-02: differences never silently ignored.
        """
        key = f"{account_id}:{venue_id}"
        sys_facts = self._system_facts.get(key)
        ex_facts = self._exchange_facts.get(key)
        if sys_facts is None and ex_facts is None:
            # BD-P0-10: 双方缺失 → BOTH_SIDES_MISSING, blocks new risk
            return ReconciliationResult(
                matched=False,
                status=ReconciliationStatus.BOTH_SIDES_MISSING,
                differences=["BOTH_SIDES_MISSING: system and exchange facts unavailable — UNKNOWN"],
            )
        if sys_facts is None or ex_facts is None:
            return ReconciliationResult(
                matched=False,
                status=ReconciliationStatus.ONE_SIDE_MISSING,
                differences=["One side missing"],
                system_facts=sys_facts,
                exchange_facts=ex_facts,
            )
        diffs: list[str] = []

        # 余额比较
        bal_diff = abs(float(sys_facts.balance.amount) - float(ex_facts.balance.amount))
        if bal_diff > 0.5:  # 容忍 0.5 以内浮点误差
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

        status = ReconciliationStatus.MATCHED if len(diffs) == 0 else ReconciliationStatus.MISMATCHED
        return ReconciliationResult(
            matched=len(diffs) == 0,
            status=status,
            differences=diffs,
            system_facts=sys_facts,
            exchange_facts=ex_facts,
        )

    def repair_strategy(self, result: ReconciliationResult) -> str:
        if result.matched:
            return "NO_ACTION"
        # BD-P0-10 / BD-T01: 任何差异（含余额不匹配）都不允许系统单方面以自身
        # 事实覆盖交易所事实 — SYSTEM_IS_AUTHORITATIVE 已移除。所有不匹配场景
        # 一律要求人工介入修复。
        return "MANUAL_REPAIR_REQUIRED"
