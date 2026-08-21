"""Semantic coverage for durable intent and transactional outbox state machines."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from beidou_infra.outbox import OutboxMessage, OutboxStatus, OutboxWorker, TransactionalOutbox
from beidou_safety.execution import OrderIntent
from beidou_safety.execution.intent import IntentOutbox, OutboxState
from beidou_shared.types import AccountId, AccountRef, InstrumentId, OrderSide, OrderType, Quantity, VenueId


def _intent(intent_id: str = "intent-complete", *, client_order_id: str | None = "client-complete") -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
        instrument_id=InstrumentId("BTCUSDT"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(amount="0.1"),
        client_order_id=client_order_id,
        idempotency_key=f"idem-{intent_id}",
    )


def test_in_memory_transactional_outbox_covers_idempotent_retry_and_quarantine_contracts() -> None:
    outbox = TransactionalOutbox(max_retries=3, dead_letter_threshold=3)
    first = OutboxMessage("order", "1", "CREATED", payload={"qty": "1"})
    duplicate = OutboxMessage("order", "1", "CREATED", payload={"qty": "1"})
    assert first.idempotency_key == duplicate.idempotency_key
    assert outbox.enqueue(first) is True
    assert outbox.enqueue(duplicate) is False
    assert outbox.poll_pending(10) == [first]
    assert outbox.size() == 1

    outbox.mark_sent(first)
    outbox.mark_acked(first)
    assert outbox.poll_pending() == []
    assert outbox.size() == 0

    retry = OutboxMessage("order", "2", "SUBMIT", idempotency_key="retry-key")
    assert outbox.enqueue(retry) is True
    assert outbox.mark_failed(retry) is OutboxStatus.PENDING
    retry.next_attempt_at = 0.0
    assert outbox.poll_pending() == [retry]
    assert outbox.mark_failed(retry) is OutboxStatus.PENDING
    retry.next_attempt_at = 0.0
    assert outbox.mark_failed(retry) is OutboxStatus.DEAD_LETTER
    assert outbox.get_dead_letters() == [retry]

    quarantine = OutboxMessage("order", "3", "CANCEL", idempotency_key="quarantine-key")
    outbox.enqueue(quarantine)
    outbox.quarantine(quarantine, "manual review")
    assert quarantine.status is OutboxStatus.QUARANTINED
    assert outbox.get_quarantined() == [quarantine]


class _Cursor:
    def __init__(self, *, rows: list[list[object]] | None = None, one: list[object] | None = None) -> None:
        self.rows = list(rows or [])
        self.one = list(one or [])
        self.statements: list[tuple[str, tuple[object, ...]]] = []
        self.rowcount = 1
        self.closed = False

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        self.statements.append((sql, params))

    def fetchall(self) -> list[object]:
        return self.rows.pop(0) if self.rows else []

    def fetchone(self) -> object:
        return self.one.pop(0) if self.one else None

    def close(self) -> None:
        self.closed = True


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self.cursor_state = cursor
        self.committed = 0
        self.rolled_back = 0
        self.closed = False

    def cursor(self) -> _Cursor:
        return self.cursor_state

    def transaction(self) -> _Connection:
        return self

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, exc_type: object, *_args: object) -> bool:
        if exc_type is None:
            self.committed += 1
        else:
            self.rolled_back += 1
        return False

    def close(self) -> None:
        self.closed = True


def test_outbox_worker_rejects_unavailable_authority_and_sends_fail_closed() -> None:
    async def scenario() -> None:
        worker = OutboxWorker(fencing_token=1)
        assert await worker.claim(0) == []
        assert await worker.claim(1) == []
        assert await worker.stats() == {"UNKNOWN": 1}
        assert await worker.send(SimpleNamespace()) is False
        assert await worker.process_batch(1) == 0

        with pytest.raises(ValueError, match="both"):
            OutboxWorker(db_conn=object(), connection_factory=lambda: object())
        with pytest.raises(ValueError, match="positive"):
            OutboxWorker(lease_seconds=0)
        with pytest.raises(RuntimeError, match="FENCING"):
            await OutboxWorker(db_conn=_Connection(_Cursor()), fencing_token=0).claim(1)

    asyncio.run(scenario())


def test_outbox_worker_claim_transition_and_stats_use_fencing_evidence() -> None:
    row = {"message_id": "msg-1", "intent_id": "intent-1", "client_order_id": "client-1", "payload": {"x": 1}}
    cursor = _Cursor(rows=[[row]], one=[row])
    conn = _Connection(cursor)
    worker = OutboxWorker(db_conn=conn, lease_owner="worker-1", fencing_token=3)

    async def scenario() -> None:
        claims = await worker.claim(1)
        assert len(claims) == 1
        assert claims[0].message_id == "msg-1"

        transition_cursor = _Cursor(one=[{"intent_id": "intent-1", "status": "SENDING"}, {"intent_id": "intent-1"}])
        worker._conn = _Connection(transition_cursor)
        assert await worker.mark_sent("msg-1") is True

        stats_cursor = _Cursor(rows=[[{"status": "SENT", "count": 1}]])
        worker._conn = _Connection(stats_cursor)
        assert await worker.stats() == {"SENT": 1}

    asyncio.run(scenario())
    sql = "\n".join(statement for statement, _params in cursor.statements)
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "fencing_token" in sql


def test_intent_outbox_memory_state_machine_preserves_unknown_and_idempotency() -> None:
    outbox = IntentOutbox()
    intent = _intent()
    key = outbox.commit(intent)
    outbox.send_to_inbox(intent)
    assert outbox.unacked() == [intent, intent]
    assert outbox.claim("worker") is intent
    assert outbox.claim("worker") is None
    outbox.mark_unknown(intent.intent_id, "ambiguous")
    assert outbox.get_unknown_intents()[0]["client_order_id"] == "client-complete"
    assert outbox.unacked() == []
    outbox.resolve_unknown(intent.intent_id, exchange_order_found=False)
    assert outbox.claim("worker") is intent
    outbox.mark_unknown(intent.intent_id, "ambiguous-again")
    outbox.resolve_unknown(intent.intent_id, exchange_order_found=True)
    assert outbox.stats["state_counts"] == {OutboxState.ACKED.value: 1}
    assert key in outbox._processed

    with pytest.raises(ValueError, match="Duplicate"):
        outbox.commit(intent)
    rejected = _intent("intent-rejected")
    outbox.commit(rejected)
    outbox.reject(rejected.intent_id, "risk veto")
    assert outbox.stats["state_counts"][OutboxState.FAILED.value] == 1
    outbox.resolve_unknown("missing", exchange_order_found=False)


def test_intent_outbox_persistent_approval_validation_and_recovery(tmp_path: Path) -> None:
    db_path = str(tmp_path / "intent-complete.db")
    outbox = IntentOutbox(db_path)
    incomplete = replace(_intent("intent-incomplete"), risk_approval_id="approval", risk_approval_signature=None)
    with pytest.raises(ValueError, match="ENVELOPE_INCOMPLETE"):
        outbox.commit(incomplete)

    missing_expiry = replace(
        _intent("intent-expiry"),
        risk_approval_id="approval",
        risk_approval_signature="signature",
        risk_proposal_hash="proposal",
        risk_intent_hash="intent",
        risk_account_snapshot_hash="account",
        risk_snapshot_hash="risk",
        risk_policy_version="policy",
        risk_nonce="nonce",
    )
    with pytest.raises(ValueError, match="expiry"):
        outbox.commit(missing_expiry)

    missing_fields = replace(
        missing_expiry,
        intent_id="intent-fields",
        idempotency_key="idem-intent-fields",
        risk_expires_at=4102444800.0,
        risk_nonce="",
    )
    with pytest.raises(ValueError, match="FIELDS_REQUIRED"):
        outbox.commit(missing_fields)

    approved = replace(
        _intent("intent-approved"),
        risk_approval_id="approval-approved",
        risk_approval_signature="signature-approved",
        risk_proposal_hash="proposal",
        risk_intent_hash="intent",
        risk_account_snapshot_hash="account",
        risk_snapshot_hash="risk",
        risk_policy_version="policy",
        risk_nonce="nonce",
        risk_expires_at=4102444800.0,
    )
    outbox.commit(approved)
    assert outbox.restore_pending_approvals()[0]["approval_id"] == "approval-approved"
    claimed = outbox.claim("worker")
    assert claimed is not None
    outbox.mark_unknown(approved.intent_id, "query required")
    assert outbox.get_unknown_intents()[0]["intent_id"] == approved.intent_id
    outbox.resolve_unknown(approved.intent_id, exchange_order_found=False)
    outbox.reject(approved.intent_id, "rejected after recovery")
    assert outbox.restore_pending_approvals() == []


def test_intent_groom_records_dropped_ids_without_silent_loss(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(IntentOutbox, "_MAX_OUTBOX_SIZE", 2)
    outbox = IntentOutbox()
    for index in range(3):
        outbox.commit(_intent(f"intent-groom-{index}"))
    assert outbox._dead_letter_count == 2
    assert outbox._dead_letter_ids == ["intent-groom-0", "intent-groom-1"]
    evidence = tmp_path / ".beidou" / "outbox_events.jsonl"
    assert evidence.is_file()
    assert json.loads(evidence.read_text().splitlines()[0])["event"] == "outbox_groom_drop"
