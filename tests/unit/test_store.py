"""PersistentStore contracts for durable protection facts."""

from __future__ import annotations

from beidou_core.store import PersistentStore
from beidou_safety.execution.ledger import (
    AccountType,
    LedgerTransaction,
    LedgerTransactionType,
    Posting,
    PostingSide,
)
from beidou_shared.types import AccountId, InstrumentId, MonetaryValue, VenueId


def test_protection_pending_is_not_restored_as_active(tmp_path):
    store = PersistentStore(str(tmp_path / "state.db"))

    store.save_protection(
        protection_id="sl-pos-1",
        position_id="pos-1",
        symbol="BTCUSDT",
        side="SELL",
        trigger_price="95000.00",
        order_price=None,
        quantity="0.01",
        order_type="STOP_MARKET",
        status="PENDING",
        stop_type="FIXED_PERCENT",
    )
    assert store.restore_protections() == []

    store.save_protection(
        protection_id="sl-pos-1",
        position_id="pos-1",
        symbol="BTCUSDT",
        side="SELL",
        trigger_price="95000.00",
        order_price=None,
        quantity="0.01",
        order_type="STOP_MARKET",
        status="ACTIVE",
        stop_type="FIXED_PERCENT",
    )
    restored = store.restore_protections()
    assert len(restored) == 1
    assert restored[0]["protection_id"] == "sl-pos-1"
    assert restored[0]["status"] == "ACTIVE"
    assert restored[0]["quantity"] == "0.01"


def test_ledger_transaction_round_trip_is_append_only(tmp_path):
    store = PersistentStore(str(tmp_path / "ledger.db"))
    tx = LedgerTransaction(
        transaction_id="tx-1",
        transaction_type=LedgerTransactionType.FILL,
        source_event_id="fill-1",
        postings=(
            Posting(
                posting_id="tx-1-debit",
                account_id=AccountId("default"),
                account_type=AccountType.POSITION_COST,
                venue_id=VenueId("BINANCE"),
                instrument_id=InstrumentId("BTCUSDT"),
                amount=MonetaryValue(amount="10"),
                side=PostingSide.DEBIT,
            ),
            Posting(
                posting_id="tx-1-credit",
                account_id=AccountId("default"),
                account_type=AccountType.CASH,
                venue_id=VenueId("BINANCE"),
                instrument_id=InstrumentId("BTCUSDT"),
                amount=MonetaryValue(amount="10"),
                side=PostingSide.CREDIT,
            ),
        ),
    )

    store.save_ledger_transaction(tx)
    store.save_ledger_transaction(tx)  # idempotent replay
    rows = store.restore_ledger_transactions()
    assert len(rows) == 1
    assert len(rows[0]["postings"]) == 2
    assert rows[0]["source_event_id"] == "fill-1"


def test_ledger_source_event_conflict_is_not_silently_ignored(tmp_path):
    store = PersistentStore(str(tmp_path / "ledger-conflict.db"))
    tx = LedgerTransaction(
        transaction_id="tx-1",
        transaction_type=LedgerTransactionType.FILL,
        source_event_id="fill-1",
        postings=(
            Posting(
                "p1", AccountId("default"), AccountType.CASH, VenueId("BINANCE"),
                None, MonetaryValue(amount="1"), PostingSide.DEBIT,
            ),
            Posting(
                "p2", AccountId("default"), AccountType.POSITION_COST, VenueId("BINANCE"),
                None, MonetaryValue(amount="1"), PostingSide.CREDIT,
            ),
        ),
    )
    store.save_ledger_transaction(tx)
    conflicting = LedgerTransaction(
        transaction_id="tx-2",
        transaction_type=LedgerTransactionType.FILL,
        source_event_id="fill-1",
        postings=tx.postings,
    )
    import pytest

    with pytest.raises(RuntimeError, match="source_event_id conflict"):
        store.save_ledger_transaction(conflicting)


def test_fill_event_is_pending_until_authoritative_facts_commit(tmp_path):
    store = PersistentStore(str(tmp_path / "fill-state.db"))

    assert store.save_fill_event(
        "fill-1",
        "order-1",
        "BTCUSDT",
        "BUY",
        "0.10",
        "0.10",
        "100",
        "PARTIALLY_FILLED",
    )
    assert store.get_fill_event("fill-1")["processing_state"] == "PENDING"
    store.mark_fill_event_committed("fill-1")
    assert store.get_fill_event("fill-1")["processing_state"] == "COMMITTED"


def test_opening_projection_requires_provenance_and_round_trips(tmp_path):
    store = PersistentStore(str(tmp_path / "opening.db"))

    import pytest

    with pytest.raises(ValueError, match="provenance"):
        store.save_account_opening_projection(
            "opening-1",
            "default",
            "BINANCE",
            "1000",
            "USDT",
            8,
            {},
            [],
            "2026-08-09T00:00:00+00:00",
            "",
            "",
            "",
            "",
        )

    store.save_account_opening_projection(
        "opening-1",
        "default",
        "BINANCE",
        "1000",
        "USDT",
        8,
        {"BTCUSDT": "0.25"},
        ["order-1"],
        "2026-08-09T00:00:00+00:00",
        "AUTHORIZED_READ_SNAPSHOT",
        "account-v1",
        "sha256:evidence",
        "approval-1",
    )
    restored = store.restore_account_opening_projection("default", "BINANCE")
    assert restored["balance_amount"] == "1000"
    assert restored["positions"] == {"BTCUSDT": "0.25"}
    assert restored["open_orders"] == ["order-1"]
