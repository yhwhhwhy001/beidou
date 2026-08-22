"""Behavior-backed coverage for the remaining IntentOutbox restart edges."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from beidou_safety.execution import OrderIntent
from beidou_safety.execution.command_aggregate import ChildCommandState, ExecutionChildCommand
from beidou_safety.execution.intent import IntentOutbox
from beidou_shared.types import AccountId, AccountRef, InstrumentId, OrderSide, OrderType, Quantity, VenueId


def _intent(intent_id: str = "intent-remaining", *, idempotency_key: str = "") -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
        instrument_id=InstrumentId("BTCUSDT"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(amount="1"),
        client_order_id=f"client-{intent_id}",
        idempotency_key=idempotency_key,
    )


def _child(intent_id: str, *, quantity: str = "1") -> ExecutionChildCommand:
    return ExecutionChildCommand.create(
        parent_intent_id=intent_id,
        sequence=0,
        symbol="BTCUSDT",
        side="BUY",
        quantity=quantity,
        order_type="MARKET",
        time_in_force="GTC",
        client_order_id=f"client-child-{intent_id}",
        rule_snapshot_hash="rules-v1",
    )


def _claimed_plan(outbox: IntentOutbox, intent_id: str) -> None:
    intent = _intent(intent_id, idempotency_key=f"idem-{intent_id}")
    outbox.commit(intent)
    assert outbox.claim("coverage-worker") is not None
    outbox.persist_execution_plan(intent_id, [_child(intent_id)])


def test_memory_and_sqlite_outbox_accessors_and_restart_edges(tmp_path: Path) -> None:
    memory = IntentOutbox()
    memory._recover_after_restart()
    assert memory._outbox == []
    with pytest.raises(RuntimeError, match="memory outbox"):
        memory._connect()
    assert memory._db_intents(("PENDING",)) == []

    db = IntentOutbox(str(tmp_path / "restart.db"))
    assert db._outbox == []
    assert db.restore_execution_plan("missing") is None
    with pytest.raises(ValueError, match="exactly three"):
        db._db_intents(("PENDING",))


def test_sqlite_schema_migration_adds_missing_approval_hash(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE risk_approvals ("
            "approval_id TEXT PRIMARY KEY NOT NULL, intent_id TEXT UNIQUE NOT NULL, "
            "decision TEXT NOT NULL, signature TEXT NOT NULL, proposal_hash TEXT NOT NULL, "
            "account_snapshot_hash TEXT NOT NULL, risk_snapshot_hash TEXT NOT NULL, "
            "policy_version TEXT NOT NULL, nonce TEXT NOT NULL, expires_at REAL NOT NULL, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.commit()
    IntentOutbox(str(db_path))
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(risk_approvals)")}
    assert "intent_hash" in columns


def test_deserialize_naive_timestamp_is_normalized() -> None:
    intent = _intent("naive-time", idempotency_key="idem-naive")
    payload = IntentOutbox._serialize_intent(intent, "idem-naive")
    data = json.loads(payload)
    data["created_at"] = data["created_at"].replace("+00:00", "")
    restored = IntentOutbox._deserialize_intent(json.dumps(data))
    assert restored.created_at.tzinfo is not None
    assert restored.created_at.utcoffset() is not None


def test_memory_plan_requires_claim_and_rejects_conflicting_replay() -> None:
    outbox = IntentOutbox()
    intent = _intent("memory-plan", idempotency_key="idem-memory-plan")
    outbox.commit(intent)
    with pytest.raises(ValueError, match="PARENT_NOT_CLAIMED"):
        outbox.persist_execution_plan(intent.intent_id, [_child(intent.intent_id)])
    assert outbox.claim("coverage-worker") is intent
    first = outbox.persist_execution_plan(intent.intent_id, [_child(intent.intent_id)])
    assert outbox.persist_execution_plan(intent.intent_id, [_child(intent.intent_id)]) == first
    conflicting = _child(intent.intent_id, quantity="2")
    with pytest.raises(ValueError, match="PLAN_CONFLICT"):
        outbox.persist_execution_plan(intent.intent_id, [conflicting])


def test_memory_transition_and_sqlite_concurrent_change_fail_closed(tmp_path: Path) -> None:
    memory = IntentOutbox()
    with pytest.raises(ValueError, match="PLAN_NOT_FOUND"):
        memory.transition_execution_child("missing", 0, ChildCommandState.SENDING, event_id="missing")

    db_path = tmp_path / "concurrent.db"
    db = IntentOutbox(str(db_path))
    _claimed_plan(db, "concurrent")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE execution_commands SET command_hash=? WHERE parent_intent_id=? AND sequence=?",
            ("tampered-command-hash", "concurrent", 0),
        )
        conn.commit()
    with pytest.raises(ValueError, match="CONCURRENT_CHANGE"):
        db.transition_execution_child("concurrent", 0, ChildCommandState.SENDING, event_id="send-concurrent")


def test_memory_projector_handles_missing_client_and_stream_replay() -> None:
    outbox = IntentOutbox()
    with pytest.raises(ValueError, match="CLIENT_ID_MISSING"):
        outbox.project_user_order_update(SimpleNamespace(client_order_id=""))

    _claimed_plan(outbox, "project-memory")
    outbox.transition_execution_child("project-memory", 0, ChildCommandState.SENDING, event_id="send")
    outbox.transition_execution_child(
        "project-memory", 0, ChildCommandState.ACKED, event_id="ack", exchange_order_id="venue-1"
    )
    outbox.transition_execution_child(
        "project-memory", 0, ChildCommandState.FILLED, event_id="fill", cumulative_filled_quantity="1"
    )
    late_new = SimpleNamespace(
        client_order_id="client-child-project-memory",
        order_status=SimpleNamespace(value="NEW"),
        cumulative_quantity=Quantity(amount="0"),
        event=SimpleNamespace(event_id="late-new"),
        order_id="venue-1",
    )
    restored = outbox.project_user_order_update(late_new)
    assert restored is not None and restored.children[0].state is ChildCommandState.FILLED


def test_duplicate_identity_and_dead_letter_are_fail_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "identity.db"
    outbox = IntentOutbox(str(db_path))
    intent = _intent("identity", idempotency_key="idem-identity")
    outbox.commit(intent)
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE intent_outbox SET payload=? WHERE intent_id=?", ("not-json", intent.intent_id))
        conn.commit()
    assert outbox.duplicate_order_count_24h() is None

    valid = IntentOutbox(str(tmp_path / "identity-list.db"))
    valid_intent = _intent("identity-list", idempotency_key="idem-identity-list")
    valid.commit(valid_intent)
    with sqlite3.connect(tmp_path / "identity-list.db") as conn:
        conn.execute("UPDATE intent_outbox SET payload=? WHERE intent_id=?", ("[]", valid_intent.intent_id))
        conn.commit()
    assert valid.duplicate_order_count_24h() is None

    dead = IntentOutbox(str(tmp_path / "dead.db"))
    dead_intent = _intent("dead", idempotency_key="idem-dead")
    dead.commit(dead_intent)
    dead.dead_letter(dead_intent.intent_id, "manual quarantine")
    assert dead.dead_letter_count == 1
    assert dead.dead_letter_ids == [dead_intent.intent_id]
    assert dead.stats["state_counts"]["DEAD_LETTER"] == 1


def test_memory_retention_grooms_ack_and_pending_state() -> None:
    outbox = IntentOutbox()
    outbox._MAX_PROCESSED_RETENTION = 0
    first = _intent("ack-no-key")
    second = _intent("pending-retention")
    outbox.commit(first)
    outbox.commit(second)
    outbox.ack(first.intent_id)
    assert first.intent_id in outbox._processed
    assert outbox._states
    outbox._processed.update({"stale-a", "stale-b"})
    outbox._groom()
    assert "stale-a" not in outbox._processed and "stale-b" not in outbox._processed


def test_persistent_ack_without_key_and_pending_approval_restore(tmp_path: Path) -> None:
    db_path = tmp_path / "ack.db"
    outbox = IntentOutbox(str(db_path))
    intent = _intent("ack-db", idempotency_key="idem-ack-db")
    outbox.commit(intent)
    outbox.ack(intent.intent_id)
    assert outbox.stats["state_counts"]["ACKED"] == 1
    assert outbox.restore_pending_approvals() == []
