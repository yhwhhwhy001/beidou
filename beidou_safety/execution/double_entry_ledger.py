"""BD-CV44: 复式账本引擎。

每个 transaction postings 借贷守恒。
TripleReconciliation: Exchange / Local / Ledger 三方比较。
SYSTEM_IS_AUTHORITATIVE 禁止。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class LegType(str, Enum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class AccountType(str, Enum):
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    INCOME = "INCOME"
    EXPENSE = "EXPENSE"


@dataclass(frozen=True)
class LedgerPosting:
    account: str
    account_type: AccountType
    leg_type: LegType
    amount: float
    currency: str = "USDT"
    description: str = ""


@dataclass
class LedgerTransaction:
    """BD-CV44: 复式账本交易。"""

    tx_id: str
    postings: list[LedgerPosting] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    tx_hash: str = ""

    def is_balanced(self) -> bool:
        """BD-CV44 AC-44-01: 借贷守恒验证。"""
        if not self.postings:
            return True
        debits = sum(p.amount for p in self.postings if p.leg_type == LegType.DEBIT)
        credits = sum(p.amount for p in self.postings if p.leg_type == LegType.CREDIT)
        return abs(debits - credits) < 0.0001

    def compute_hash(self) -> str:
        data = {
            "tx_id": self.tx_id,
            "postings": [
                {"account": p.account, "type": p.leg_type.value, "amount": p.amount}
                for p in self.postings
            ],
            "timestamp": self.timestamp,
        }
        return hashlib.sha256(str(data).encode()).hexdigest()[:16]

    @classmethod
    def record_trade(
        cls,
        tx_id: str,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        commission: float = 0.0,
    ) -> LedgerTransaction:
        """BD-CV44: 记录一笔交易 — 复式分录。"""
        notional = quantity * price
        is_buy = side.upper() == "BUY"

        postings = [
            # 资产变动
            LedgerPosting(
                account=f"positions:{symbol}",
                account_type=AccountType.ASSET,
                leg_type=LegType.DEBIT if is_buy else LegType.CREDIT,
                amount=quantity,
                description=f"{side} {quantity} @ {price}",
            ),
            # 现金变动
            LedgerPosting(
                account="cash:USDT",
                account_type=AccountType.ASSET,
                leg_type=LegType.CREDIT if is_buy else LegType.DEBIT,
                amount=notional,
                description=f"Cash settlement for {symbol}",
            ),
        ]
        # 手续费
        if commission > 0:
            postings.append(
                LedgerPosting(
                    account="expense:commission",
                    account_type=AccountType.EXPENSE,
                    leg_type=LegType.DEBIT,
                    amount=commission,
                    description=f"Commission for {symbol}",
                )
            )
            postings.append(
                LedgerPosting(
                    account="cash:USDT",
                    account_type=AccountType.ASSET,
                    leg_type=LegType.CREDIT,
                    amount=commission,
                    description=f"Commission payment for {symbol}",
                )
            )

        return cls(tx_id=tx_id, postings=postings)

    @classmethod
    def record_funding(cls, tx_id: str, symbol: str, amount: float) -> LedgerTransaction:
        """BD-CV44: 记录资金费率。"""
        is_positive = amount > 0
        return cls(
            tx_id=tx_id,
            postings=[
                LedgerPosting(
                    account="cash:USDT",
                    account_type=AccountType.ASSET,
                    leg_type=LegType.DEBIT if is_positive else LegType.CREDIT,
                    amount=abs(amount),
                    description=f"Funding payment for {symbol}",
                ),
                LedgerPosting(
                    account="income:funding" if is_positive else "expense:funding",
                    account_type=AccountType.INCOME if is_positive else AccountType.EXPENSE,
                    leg_type=LegType.CREDIT if is_positive else LegType.DEBIT,
                    amount=abs(amount),
                    description=f"Funding {symbol}",
                ),
            ],
        )


@dataclass
class TripleReconciliationResult:
    """BD-CV44: 三方对账结果。"""

    venue_orders: int = 0
    local_orders: int = 0
    ledger_entries: int = 0
    venue_positions: float = 0.0
    local_positions: float = 0.0
    ledger_positions: float = 0.0
    is_matched: bool = False
    mismatches: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    def detect_same_source_fraud(self) -> bool:
        """BD-CV44 AC-44-02: 三方同源伪造必须被检测。"""
        unique = set(self.sources)
        return len(unique) >= 3

    def can_pass(self) -> bool:
        """BD-CV44 AC-44-03: MISMATCHED/UNKNOWN 不能得到 PASS。"""
        if not self.is_matched:
            return False
        if len(self.mismatches) > 0:
            return False
        if not self.detect_same_source_fraud():
            return False
        return True
