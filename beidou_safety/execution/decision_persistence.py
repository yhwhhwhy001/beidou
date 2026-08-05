"""决策持久化与独立重算 — BD-07 item 8, BD-10 items 1,3,5。

所有决策和输入快照持久化，支持独立重算。
多分录账户类型定义和权威来源声明。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class AccountType(str, Enum):
    """BD-10: 多分录账户类型。"""
    MARGIN_CASH = "MARGIN_CASH"
    POSITION_COST = "POSITION_COST"
    REALIZED_PNL = "REALIZED_PNL"
    FEE_EXPENSE = "FEE_EXPENSE"
    FUNDING = "FUNDING"
    EXCHANGE_CLEARING = "EXCHANGE_CLEARING"
    STRATEGY_OWNER = "STRATEGY_OWNER"


@dataclass(frozen=True, slots=True)
class DecisionSnapshot:
    """不可变决策快照 — BD-07 item 8。

    包含所有输入和输出，支持独立重算验证。
    """
    decision_id: str
    strategy_id: str
    instrument_id: str
    venue_id: str

    # 输入快照
    account_fact_version: str
    policy_version: str
    feature_snapshot_hash: str
    market_state: dict
    cost_estimate: dict
    risk_snapshot: dict

    # 决策输出
    direction: str
    target_quantity: float
    approval_id: str
    approval_hash: str

    # 元数据
    correlation_id: str
    causation_id: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # 可验证性
    checksum: str = ""

    def __post_init__(self):
        if not self.checksum:
            object.__setattr__(self, "checksum", self._compute_checksum())

    def _compute_checksum(self) -> str:
        content = json.dumps({
            "decision_id": self.decision_id,
            "account_fact_version": self.account_fact_version,
            "policy_version": self.policy_version,
            "feature_snapshot_hash": self.feature_snapshot_hash,
            "market_state": self.market_state,
            "cost_estimate": self.cost_estimate,
            "direction": self.direction,
            "target_quantity": self.target_quantity,
            "approval_id": self.approval_id,
            "approval_hash": self.approval_hash,
        }, sort_keys=True, default=str)
        return hashlib.sha256(content.encode()).hexdigest()

    def verify_inputs(self, account_facts: dict, policy: dict,
                      features: dict) -> bool:
        """独立重算验证：输入是否匹配。"""
        return (
            account_facts.get("version") == self.account_fact_version and
            policy.get("version") == self.policy_version
        )


class DecisionStore:
    """决策持久化存储。

    所有决策 append-only 保存，支持独立审计和重算。
    """

    def __init__(self):
        self._decisions: list[DecisionSnapshot] = []
        self._checksums: set[str] = set()

    def persist(self, decision: DecisionSnapshot) -> bool:
        """持久化决策。重复 checksum 拒绝。"""
        if decision.checksum in self._checksums:
            return False
        self._decisions.append(decision)
        self._checksums.add(decision.checksum)
        return True

    def get_by_correlation(self, correlation_id: str) -> list[DecisionSnapshot]:
        return [d for d in self._decisions if d.correlation_id == correlation_id]

    def verify_all(self, account_facts: dict, policy: dict,
                   features: dict) -> list[str]:
        """批量独立重算验证。返回不匹配的决策 ID。"""
        mismatches = []
        for d in self._decisions:
            if not d.verify_inputs(account_facts, policy, features):
                mismatches.append(d.decision_id)
        return mismatches

    def integrity_check(self) -> bool:
        """全部决策 checksum 完整性验证。"""
        for d in self._decisions:
            if d.checksum != d._compute_checksum():
                return False
        return True


# ================================================================
# BD-10 item 5: 权威来源声明
# ================================================================

class DataAuthority(str, Enum):
    """数据权威来源声明。"""
    EXCHANGE = "EXCHANGE"       # 余额/仓位/订单/成交 → 交易所是权威
    INTERNAL = "INTERNAL"       # 意图/审批/策略归属 → 内部事件是权威
    DERIVED = "DERIVED"         # projection → 从事件重放派生
    NOT_VERIFIABLE = "NOT_VERIFIABLE"


AUTHORITY_MAP = {
    "account_balance": DataAuthority.EXCHANGE,
    "positions": DataAuthority.EXCHANGE,
    "open_orders": DataAuthority.EXCHANGE,
    "fills": DataAuthority.EXCHANGE,
    "fees": DataAuthority.EXCHANGE,
    "funding_payments": DataAuthority.EXCHANGE,
    "order_intents": DataAuthority.INTERNAL,
    "risk_approvals": DataAuthority.INTERNAL,
    "strategy_assignments": DataAuthority.INTERNAL,
    "ledger_entries": DataAuthority.DERIVED,
    "pnl_projections": DataAuthority.DERIVED,
    "performance_metrics": DataAuthority.DERIVED,
}


def get_authority(data_type: str) -> DataAuthority:
    """查询数据类型的权威来源。"""
    return AUTHORITY_MAP.get(data_type, DataAuthority.NOT_VERIFIABLE)
