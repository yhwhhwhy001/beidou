"""BD-CV02: TruthSnapshot 与 TradingEligibility — 唯一系统授权判定。

事实快照 → 风险/完整性判定 → 交易资格 → 控制状态。
禁止 supervisor/engine/monitor 各自决定是否可写。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from beidou_control.plane import ControlAction


class TradingEligibility(str, Enum):
    """交易资格 — 系统中只有一个 authority 可从 TruthSnapshot 推导。

    - ELIGIBLE: 所有事实新鲜且验证通过，允许 RESUME
    - NO_NEW_RISK: 事实存在但不足以授权新风险
    - EXIT_ONLY: 仅允许减仓/平仓
    - LOCK: 全部锁定，仅允许 QUERY
    - NOT_VERIFIABLE: 关键事实无法验证
    """

    ELIGIBLE = "ELIGIBLE"
    NO_NEW_RISK = "NO_NEW_RISK"
    EXIT_ONLY = "EXIT_ONLY"
    LOCK = "LOCK"
    NOT_VERIFIABLE = "NOT_VERIFIABLE"


# TradingEligibility → ControlAction 映射
ELIGIBILITY_TO_CONTROL: dict[TradingEligibility, ControlAction] = {
    TradingEligibility.ELIGIBLE: ControlAction.RESUME,
    TradingEligibility.NO_NEW_RISK: ControlAction.NO_NEW_RISK,
    TradingEligibility.EXIT_ONLY: ControlAction.EXIT_ONLY,
    TradingEligibility.LOCK: ControlAction.LOCK,
    TradingEligibility.NOT_VERIFIABLE: ControlAction.NO_NEW_RISK,
}


@dataclass(frozen=True)
class TruthSnapshot:
    """不可变系统事实快照。

    绑定所有关键子系统的最新 hash 与 freshness。
    只有通过完整验证的快照才能用于推导 TradingEligibility。

    BD-CV02 AC-02-02: 空快照(freshness=0, 所有 hash=empty)不能得到 ELIGIBLE。
    """

    # Version metadata
    snapshot_id: str = ""
    created_at: str = ""

    # Component hashes (每个组件提供其当前状态的 SHA-256)
    market_hash: str = ""
    account_hash: str = ""
    order_hash: str = ""
    position_hash: str = ""
    ledger_hash: str = ""
    reconciliation_hash: str = ""
    protection_hash: str = ""
    risk_hash: str = ""
    config_hash: str = ""
    policy_hash: str = ""

    # Freshness (Unix timestamp of each component's last update)
    market_freshness: float = 0.0
    account_freshness: float = 0.0
    order_freshness: float = 0.0
    position_freshness: float = 0.0
    ledger_freshness: float = 0.0
    reconciliation_freshness: float = 0.0
    protection_freshness: float = 0.0
    risk_freshness: float = 0.0
    config_freshness: float = 0.0
    policy_freshness: float = 0.0

    # Component status values
    reconciliation_status: str = "UNKNOWN"  # MATCHED / MISMATCHED / UNKNOWN
    protection_status: str = "UNKNOWN"  # ACTIVE / GAP / UNKNOWN
    risk_status: str = "UNKNOWN"  # NORMAL / WARNING / CRITICAL / UNKNOWN

    # Additional context
    env_mode: str = ""
    control_action: str = "NO_NEW_RISK"

    def is_empty(self) -> bool:
        """检查是否为空快照（所有 hash 为空，freshness=0）。"""
        hash_fields = [
            self.market_hash, self.account_hash, self.order_hash,
            self.position_hash, self.ledger_hash, self.reconciliation_hash,
            self.protection_hash, self.risk_hash, self.config_hash, self.policy_hash,
        ]
        return all(h == "" for h in hash_fields)

    def is_stale(self, max_age_seconds: float = 300.0) -> bool:
        """检查是否有任何关键组件超过最大年龄。"""
        now = datetime.now(timezone.utc).timestamp()
        critical_freshness = [
            ("market", self.market_freshness),
            ("account", self.account_freshness),
            ("order", self.order_freshness),
            ("position", self.position_freshness),
            ("reconciliation", self.reconciliation_freshness),
            ("protection", self.protection_freshness),
            ("risk", self.risk_freshness),
        ]
        for name, freshness in critical_freshness:
            if freshness <= 0:
                return True
            if now - freshness > max_age_seconds:
                return True
        return False

    def has_unknown_components(self) -> list[str]:
        """返回所有 UNKNOWN 状态的组件列表。"""
        unknown = []
        if self.reconciliation_status == "UNKNOWN":
            unknown.append("reconciliation")
        if self.protection_status == "UNKNOWN":
            unknown.append("protection")
        if self.risk_status == "UNKNOWN":
            unknown.append("risk")
        return unknown

    def compute_hash(self) -> str:
        """计算整个快照的 SHA-256。"""
        data = json.dumps({
            "market_hash": self.market_hash,
            "account_hash": self.account_hash,
            "order_hash": self.order_hash,
            "position_hash": self.position_hash,
            "ledger_hash": self.ledger_hash,
            "reconciliation_hash": self.reconciliation_hash,
            "protection_hash": self.protection_hash,
            "risk_hash": self.risk_hash,
            "config_hash": self.config_hash,
            "policy_hash": self.policy_hash,
            "market_freshness": self.market_freshness,
            "account_freshness": self.account_freshness,
            "order_freshness": self.order_freshness,
            "position_freshness": self.position_freshness,
            "ledger_freshness": self.ledger_freshness,
            "reconciliation_freshness": self.reconciliation_freshness,
            "protection_freshness": self.protection_freshness,
            "risk_freshness": self.risk_freshness,
            "config_freshness": self.config_freshness,
            "policy_freshness": self.policy_freshness,
            "reconciliation_status": self.reconciliation_status,
            "protection_status": self.protection_status,
            "risk_status": self.risk_status,
        }, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(data.encode()).hexdigest()


def derive_eligibility(
    snapshot: TruthSnapshot,
    max_age_seconds: float = 300.0,
) -> TradingEligibility:
    """BD-CV02: 从 TruthSnapshot 推导 TradingEligibility。

    这是系统中唯一可以从事实快照推导交易资格的纯函数。
    任何模块不得绕过此函数自行判断是否可写。

    AC-02-02: 非法状态跳转、空快照、陈旧快照均不能得到 ELIGIBLE。
    """

    # 1. 空快照 → NOT_VERIFIABLE
    if snapshot.is_empty():
        return TradingEligibility.NOT_VERIFIABLE

    # 2. 陈旧快照 → NOT_VERIFIABLE
    if snapshot.is_stale(max_age_seconds):
        return TradingEligibility.NOT_VERIFIABLE

    # 3. UNKNOWN 组件 → NO_NEW_RISK
    unknown = snapshot.has_unknown_components()
    if unknown:
        return TradingEligibility.NO_NEW_RISK

    # 4. 对账不匹配 → NO_NEW_RISK
    if snapshot.reconciliation_status == "MISMATCHED":
        return TradingEligibility.NO_NEW_RISK

    # 5. 保护 GAP → NO_NEW_RISK
    if snapshot.protection_status == "GAP":
        return TradingEligibility.NO_NEW_RISK

    # 6. 风险 CRITICAL → LOCK
    if snapshot.risk_status == "CRITICAL":
        return TradingEligibility.LOCK

    # 7. 风险 WARNING → NO_NEW_RISK
    if snapshot.risk_status == "WARNING":
        return TradingEligibility.NO_NEW_RISK

    # 8. 所有条件满足 → ELIGIBLE
    #    需要: reconciliation=MATCHED, protection=ACTIVE, risk=NORMAL
    if (snapshot.reconciliation_status == "MATCHED"
            and snapshot.protection_status == "ACTIVE"
            and snapshot.risk_status == "NORMAL"):
        return TradingEligibility.ELIGIBLE

    # fallback: safe
    return TradingEligibility.NO_NEW_RISK


def eligibility_to_control_action(eligibility: TradingEligibility) -> ControlAction:
    """将 TradingEligibility 映射为 ControlAction。"""
    return ELIGIBILITY_TO_CONTROL.get(eligibility, ControlAction.NO_NEW_RISK)


# 就绪门禁所需的证据要求
RESUME_REQUIRED_EVIDENCE = [
    "TruthSnapshot (新鲜，所有组件非 UNKNOWN)",
    "reconciliation_status = MATCHED",
    "protection_status = ACTIVE",
    "risk_status = NORMAL",
]
