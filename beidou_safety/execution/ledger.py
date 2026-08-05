"""双重记账、不可变经济事件与完整归因。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from beidou_shared.types import (
    AccountId,
    CorrelationId,
    InstrumentId,
    MonetaryValue,
    VenueId,
)


@dataclass(frozen=True, slots=True)
class JournalEntry:
    entry_id: str
    account_id: AccountId
    venue_id: VenueId
    instrument_id: InstrumentId | None
    debit: MonetaryValue
    credit: MonetaryValue
    description: str
    correlation_id: CorrelationId | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    is_reversible: bool = True

    def is_balanced(self) -> bool:
        return float(self.debit.amount) == float(self.credit.amount)


class ImmutableLedger:
    """不可变账本。所有经济事件追加写入，不可修改。"""

    def __init__(self) -> None:
        self._entries: list[JournalEntry] = []
        self._account_balances: dict[str, MonetaryValue] = {}

    def post(self, entry: JournalEntry) -> str:
        self._entries.append(entry)
        key = f"{entry.account_id}:{entry.venue_id}"
        current = self._account_balances.get(key, MonetaryValue(amount="0", currency=entry.debit.currency))
        new_balance = float(current.amount) + float(entry.debit.amount) - float(entry.credit.amount)
        self._account_balances[key] = MonetaryValue(amount=str(new_balance), currency=current.currency)
        return entry.entry_id

    def get_balance(self, account_id: AccountId, venue_id: VenueId) -> MonetaryValue:
        return self._account_balances.get(f"{account_id}:{venue_id}", MonetaryValue(amount="0"))

    def is_balanced(self) -> bool:
        return sum(float(e.debit.amount) for e in self._entries) == sum(float(e.credit.amount) for e in self._entries)

    def get_entries(self, correlation_id: CorrelationId) -> list[JournalEntry]:
        return [e for e in self._entries if e.correlation_id == correlation_id]

    def verify_attribution(self, entry: JournalEntry) -> bool:
        """完整归因：每条分录可追溯到原始事件。"""
        return entry.correlation_id is not None and any(e.correlation_id == entry.correlation_id for e in self._entries)
