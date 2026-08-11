"""BD-T12: 真正的复式账本 — LedgerTransaction + Posting 不可变追加。

核心规则：
- 每笔 LedgerTransaction 包含 ≥2 个 Posting
- 同一币种的所有 Posting 借贷平衡 (sum(debit) == sum(credit))
- Append-only；更正走 reversal + replacement（从不原地编辑）
- source_event_id 确保幂等（非空强制）
- Decimal 精度 — 禁止 float money（PKG20 BDS-P1-025）
- 余额按 account+venue+currency 隔离（PKG20 BDS-P1-028）
- 可从 postings 重建全部 projection
- 日终试算表/余额/交易归因可导出
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType
from typing import Any

from beidou_shared.types import (
    AccountId,
    CorrelationId,
    InstrumentId,
    MonetaryValue,
    VenueId,
)


class PostingSide(str, Enum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class AccountType(str, Enum):
    CASH = "CASH"
    MARGIN = "MARGIN"
    POSITION_COST = "POSITION_COST"
    REALIZED_PNL = "REALIZED_PNL"
    UNREALIZED_PNL = "UNREALIZED_PNL"
    FEES = "FEES"
    FUNDING = "FUNDING"
    RECEIVABLES = "RECEIVABLES"
    PAYABLES = "PAYABLES"
    ADJUSTMENTS = "ADJUSTMENTS"


class LedgerTransactionType(str, Enum):
    FILL = "FILL"
    FEE = "FEE"
    FUNDING = "FUNDING"
    TRANSFER = "TRANSFER"
    ADJUSTMENT = "ADJUSTMENT"
    REVERSAL = "REVERSAL"


@dataclass(frozen=True, slots=True)
class Posting:
    """单个分录 — 绑定账户、金额、方向。"""

    posting_id: str
    account_id: AccountId
    account_type: AccountType
    venue_id: VenueId
    instrument_id: InstrumentId | None
    amount: MonetaryValue
    side: PostingSide
    description: str = ""


@dataclass(frozen=True, slots=True)
class LedgerTransaction:
    """一笔完整的复式记账交易 — ≥2 个 Posting，借贷平衡。

    PKG20 修复:
    - BDS-P1-025: 禁止 float money — 所有金额使用 Decimal
    - BDS-P1-026: metadata 冻结为 MappingProxyType（不可变）
    - BDS-P1-027: source_event_id 非空强制
    """

    transaction_id: str
    transaction_type: LedgerTransactionType
    postings: tuple[Posting, ...]  # 不可变序列，≥2 个
    source_event_id: str  # PKG20: 非空强制 — 幂等键 (fill_id, fee_id, funding_id, etc.)
    correlation_id: CorrelationId | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    is_correction: bool = False
    reverses_transaction_id: str = ""  # 更正：指向被反转的原始交易
    metadata: MappingProxyType[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if len(self.postings) < 2:
            raise ValueError(f"LedgerTransaction requires ≥2 postings, got {len(self.postings)}")
        # PKG20 (BDS-P1-027): source_event_id 非空强制
        if not self.source_event_id or not self.source_event_id.strip():
            raise ValueError("LedgerTransaction.source_event_id is required for idempotency")

    def is_balanced(self) -> bool:
        """PKG20 (BDS-P1-025): 使用 Decimal 精确计算，禁止 float money。

        同一币种内 DEBIT == CREDIT（容差 1e-12 为 Decimal 比较精度）。
        """
        balances: dict[str, Decimal] = {}
        for p in self.postings:
            currency = p.amount.currency or "USDT"
            bal = balances.get(currency, Decimal("0"))
            try:
                amt = Decimal(str(p.amount.amount))
            except (InvalidOperation, ValueError, TypeError):
                raise ValueError(f"Invalid monetary amount in posting {p.posting_id}: {p.amount.amount}")
            if p.side == PostingSide.DEBIT:
                bal += amt
            else:
                bal -= amt
            balances[currency] = bal
        return all(abs(b) < Decimal("1e-12") for b in balances.values())

    def total_debit(self, currency: str | None = None) -> Decimal:
        """PKG20: 返回 Decimal，禁止 float。"""
        total = Decimal("0")
        for p in self.postings:
            if p.side == PostingSide.DEBIT and (currency is None or p.amount.currency == currency):
                try:
                    total += Decimal(str(p.amount.amount))
                except (InvalidOperation, ValueError, TypeError):
                    raise ValueError(f"Invalid amount in posting {p.posting_id}")
        return total

    def total_credit(self, currency: str | None = None) -> Decimal:
        """PKG20: 返回 Decimal，禁止 float。"""
        total = Decimal("0")
        for p in self.postings:
            if p.side == PostingSide.CREDIT and (currency is None or p.amount.currency == currency):
                try:
                    total += Decimal(str(p.amount.amount))
                except (InvalidOperation, ValueError, TypeError):
                    raise ValueError(f"Invalid amount in posting {p.posting_id}")
        return total


class ImmutableLedger:
    """BD-T12: 不可变复式账本。

    规则:
    - append-only: post() 只能追加
    - 借贷平衡: 每笔 transaction 所有 posting 按币种 sum(debit)==sum(credit)
    - 幂等: 相同 source_event_id 不可重复提交
    - 更正: reversal + replacement（从不可原地编辑）
    - 可从 posting 序列重建所有投影
    """

    def __init__(self) -> None:
        self._transactions: list[LedgerTransaction] = []
        self._frozen: bool = False
        self._seen_event_ids: set[str] = set()
        self._seen_transaction_ids: set[str] = set()

    @property
    def is_frozen(self) -> bool:
        return self._frozen

    @property
    def transaction_count(self) -> int:
        return len(self._transactions)

    def freeze(self) -> None:
        self._frozen = True

    def validate(self, tx: LedgerTransaction) -> None:
        """Validate a transaction without mutating the in-memory journal.

        The execution engine persists the durable journal before appending to
        its process-local projection.  Keeping validation side-effect free
        lets that ordering fail closed: a malformed or duplicate transaction
        is rejected before SQLite can contain a new fact.
        """

        if self._frozen:
            raise RuntimeError("ImmutableLedger is frozen")
        if tx.source_event_id and tx.source_event_id in self._seen_event_ids:
            raise RuntimeError(f"ImmutableLedger: duplicate source_event_id={tx.source_event_id}")
        if tx.transaction_id in self._seen_transaction_ids:
            raise RuntimeError(f"ImmutableLedger: duplicate transaction_id={tx.transaction_id}")
        if not tx.is_balanced():
            raise RuntimeError(f"ImmutableLedger: unbalanced transaction {tx.transaction_id} — rejected")

    def post(self, tx: LedgerTransaction) -> str:
        """追加交易。返回 transaction_id。"""
        self.validate(tx)

        self._transactions.append(tx)
        if tx.source_event_id:
            self._seen_event_ids.add(tx.source_event_id)
        self._seen_transaction_ids.add(tx.transaction_id)
        return tx.transaction_id

    def reverse_transaction(self, original_tx_id: str, reversal_tx_id: str, reason: str) -> LedgerTransaction:
        """创建反转交易 — 对原交易每笔 posting 做反向分录。"""
        original = self._get_transaction(original_tx_id)
        if original is None:
            raise ValueError(f"Original transaction {original_tx_id} not found")

        reversed_postings: list[Posting] = []
        for i, p in enumerate(original.postings):
            reversed_side = PostingSide.CREDIT if p.side == PostingSide.DEBIT else PostingSide.DEBIT
            reversed_postings.append(
                Posting(
                    posting_id=f"{reversal_tx_id}-{i}",
                    account_id=p.account_id,
                    account_type=p.account_type,
                    venue_id=p.venue_id,
                    instrument_id=p.instrument_id,
                    amount=p.amount,
                    side=reversed_side,
                    description=f"REVERSAL of {original_tx_id}: {reason}",
                )
            )

        return LedgerTransaction(
            transaction_id=reversal_tx_id,
            transaction_type=LedgerTransactionType.REVERSAL,
            postings=tuple(reversed_postings),
            source_event_id=f"reversal-{original_tx_id}",  # PKG20: 反转交易需要 source_event_id
            correlation_id=original.correlation_id,
            is_correction=True,
            reverses_transaction_id=original_tx_id,
            metadata=MappingProxyType({"reason": reason, "original_type": original.transaction_type.value}),
        )

    def get_balance(self, account_id: AccountId, venue_id: VenueId, currency: str = "USDT") -> MonetaryValue:
        """PKG20 (BDS-P1-025, BDS-P1-028): 计算指定账户余额 — 按币种隔离，Decimal 精确。"""
        net = Decimal("0")
        for tx in self._transactions:
            for p in tx.postings:
                if p.account_id == account_id and p.venue_id == venue_id and p.amount.currency == currency:
                    try:
                        amt = Decimal(str(p.amount.amount))
                    except (InvalidOperation, ValueError, TypeError):
                        continue
                    if p.side == PostingSide.DEBIT:
                        net += amt
                    else:
                        net -= amt
        return MonetaryValue(amount=str(net))

    def get_trial_balance(self) -> dict[str, Decimal]:
        """PKG20 (BDS-P1-025): 试算表 — 所有账户余额汇总，Decimal 精确。"""
        balances: dict[str, Decimal] = {}
        for tx in self._transactions:
            for p in tx.postings:
                key = f"{p.account_type.value}:{p.account_id}:{p.venue_id}:{p.amount.currency or 'USDT'}"
                bal = balances.get(key, Decimal("0"))
                try:
                    amt = Decimal(str(p.amount.amount))
                except (InvalidOperation, ValueError, TypeError):
                    continue
                if p.side == PostingSide.DEBIT:
                    bal += amt
                else:
                    bal -= amt
                balances[key] = bal
        return balances

    def is_balanced(self) -> bool:
        """PKG20 (BDS-P1-025): 全局借贷平衡检查 — Decimal 精确比较。"""
        currencies: set[str] = set()
        for tx in self._transactions:
            for p in tx.postings:
                currencies.add(p.amount.currency or "USDT")
        for currency in currencies:
            total_debit = sum((tx.total_debit(currency) for tx in self._transactions), Decimal("0"))
            total_credit = sum((tx.total_credit(currency) for tx in self._transactions), Decimal("0"))
            if abs(total_debit - total_credit) >= Decimal("1e-12"):
                return False
        return True

    def rebuild_projection(self) -> dict[str, Any]:
        """从 posting 序列重建完整投影。"""
        return {
            "transaction_count": len(self._transactions),
            "trial_balance": self.get_trial_balance(),
            "is_balanced": self.is_balanced(),
            "frozen": self._frozen,
        }

    def get_transactions_by_type(self, tx_type: LedgerTransactionType) -> list[LedgerTransaction]:
        return [tx for tx in self._transactions if tx.transaction_type == tx_type]

    def get_transactions_by_event(self, source_event_id: str) -> list[LedgerTransaction]:
        return [tx for tx in self._transactions if tx.source_event_id == source_event_id]

    def get_transactions_by_correlation(self, cid: CorrelationId) -> list[LedgerTransaction]:
        return [tx for tx in self._transactions if tx.correlation_id == cid]

    def _get_transaction(self, tx_id: str) -> LedgerTransaction | None:
        for tx in self._transactions:
            if tx.transaction_id == tx_id:
                return tx
        return None
