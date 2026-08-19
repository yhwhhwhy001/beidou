"""Failure-first PostgreSQL execution-outbox contract tests.

These tests exercise the SQL/transaction boundary with a DB-API recording
connection.  They do not pretend to certify a live PostgreSQL instance; a
real migration, crash and double-worker integration run remains a release
gate.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from beidou_infra.outbox import OutboxWorker, PostgresIntentOutbox
from beidou_safety.execution import OrderIntent
from beidou_safety.execution.command_aggregate import ChildCommandState, ExecutionChildCommand, ParentExecutionState
from beidou_shared.types import (
    AccountId,
    AccountRef,
    InstrumentId,
    OrderSide,
    OrderType,
    Quantity,
    TimeInForce,
    VenueId,
)


class _RecordingCursor:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchone_values: list[Any] = []
        self.fetchall_values: list[list[Any]] = []
        self.rowcount = 1
        self.fail_on: str | None = None

    def __enter__(self) -> _RecordingCursor:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.statements.append((sql, params))
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("injected database failure")

    def fetchone(self) -> Any:
        return self.fetchone_values.pop(0) if self.fetchone_values else None

    def fetchall(self) -> list[Any]:
        return self.fetchall_values.pop(0) if self.fetchall_values else []

    def close(self) -> None:
        return None


class _RecordingTransaction:
    def __init__(self, conn: _RecordingConnection) -> None:
        self._conn = conn

    def __enter__(self) -> _RecordingTransaction:
        self._conn.transaction_count += 1
        return self

    def __exit__(self, exc_type: object, *_args: object) -> bool:
        if exc_type is None:
            self._conn.committed += 1
        else:
            self._conn.rolled_back += 1
        return False


class _RecordingConnection:
    def __init__(self) -> None:
        self.cursor_state = _RecordingCursor()
        self.transaction_count = 0
        self.committed = 0
        self.rolled_back = 0

    def cursor(self) -> _RecordingCursor:
        return self.cursor_state

    def transaction(self) -> _RecordingTransaction:
        return _RecordingTransaction(self)

    def close(self) -> None:
        return None


def _intent(*, approved: bool = True, intent_id: str = "intent-pg-1") -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
        instrument_id=InstrumentId("BTCUSDT"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(amount="0.1"),
        time_in_force=TimeInForce.GTC,
        client_order_id="cid-pg-1",
        idempotency_key=f"idem-{intent_id}",
        risk_approval_id="approval-pg-1" if approved else None,
        risk_approval_signature="sig-pg-1" if approved else None,
        risk_proposal_hash="proposal-hash" if approved else "",
        risk_intent_hash="intent-hash" if approved else "",
        risk_account_snapshot_hash="account-hash" if approved else "",
        risk_snapshot_hash="risk-hash" if approved else "",
        risk_policy_version="policy-v1" if approved else "",
        risk_nonce="nonce-pg-1" if approved else "",
        risk_expires_at=4102444800.0 if approved else None,
    )


def _children() -> list[ExecutionChildCommand]:
    return [
        ExecutionChildCommand.create(
            parent_intent_id="intent-pg-1",
            sequence=sequence,
            symbol="BTCUSDT",
            side="BUY",
            quantity="0.05",
            order_type="LIMIT",
            time_in_force="GTC",
            client_order_id=f"cid-pg-1-{sequence}",
            limit_price="50000",
            rule_snapshot_hash="rule-1",
        )
        for sequence in range(2)
    ]


def test_postgres_execution_plan_persists_every_child_and_event_atomically() -> None:
    conn = _RecordingConnection()
    conn.cursor_state.fetchone_values.append(("intent-pg-1",))
    conn.cursor_state.fetchall_values.append([])
    store = PostgresIntentOutbox(
        connection_factory=lambda: conn,
        lease_owner="worker-1",
        fencing_token=7,
    )

    aggregate = store.persist_execution_plan("intent-pg-1", _children())

    assert aggregate.state is ParentExecutionState.PLANNED
    assert conn.committed == 1
    sql = "\n".join(statement for statement, _params in conn.cursor_state.statements)
    assert sql.count("INSERT INTO v3_execution_commands") == 2
    assert sql.count("INSERT INTO v3_execution_command_events") == 2
    assert "o.lease_owner=%s" in sql
    assert "o.fencing_token=%s" in sql
    assert "o.lease_until>=CURRENT_TIMESTAMP" in sql
    assert "fencing_token" in sql


def test_postgres_execution_plan_requires_current_parent_lease_and_fence() -> None:
    conn = _RecordingConnection()
    conn.cursor_state.fetchone_values.append(None)
    store = PostgresIntentOutbox(
        connection_factory=lambda: conn,
        lease_owner="worker-stale",
        fencing_token=6,
    )

    with pytest.raises(ValueError, match="EXECUTION_PLAN_PARENT_NOT_CLAIMED"):
        store.persist_execution_plan("intent-pg-1", _children())

    sql = "\n".join(statement for statement, _params in conn.cursor_state.statements)
    assert "o.lease_owner=%s" in sql
    assert "o.fencing_token=%s" in sql
    assert "INSERT INTO v3_execution_commands" not in sql


def test_postgres_execution_child_transition_is_fenced_and_append_audited() -> None:
    conn = _RecordingConnection()
    payloads = [(json.dumps(child.to_payload()),) for child in _children()]
    conn.cursor_state.fetchone_values.append(None)
    conn.cursor_state.fetchall_values.append(payloads)
    store = PostgresIntentOutbox(
        connection_factory=lambda: conn,
        lease_owner="worker-1",
        fencing_token=7,
    )

    aggregate = store.transition_execution_child(
        "intent-pg-1",
        0,
        ChildCommandState.SENDING,
        event_id="send-0",
    )

    assert aggregate.state is ParentExecutionState.IN_FLIGHT
    sql = "\n".join(statement for statement, _params in conn.cursor_state.statements)
    assert "UPDATE v3_execution_commands" in sql
    assert "lease_owner=%s" in sql
    assert "fencing_token=%s" in sql
    assert "SELECT 1 FROM v3_transactional_outbox AS o" in sql
    assert "o.lease_until>=CURRENT_TIMESTAMP" in sql
    assert "INSERT INTO v3_execution_command_events" in sql


def test_postgres_user_update_for_non_aggregate_order_is_not_projected() -> None:
    conn = _RecordingConnection()
    conn.cursor_state.fetchone_values.append(None)
    store = PostgresIntentOutbox(
        connection_factory=lambda: conn,
        lease_owner="worker-1",
        fencing_token=7,
    )

    result = store.project_user_order_update(SimpleNamespace(client_order_id="external-protection-order"))

    assert result is None
    sql = "\n".join(statement for statement, _params in conn.cursor_state.statements)
    assert "SELECT parent_intent_id,sequence FROM v3_execution_commands" in sql
    assert "UPDATE v3_execution_commands" not in sql


def test_postgres_intent_outbox_commits_approval_intent_and_event_atomically() -> None:
    conn = _RecordingConnection()
    store = PostgresIntentOutbox(connection_factory=lambda: conn)

    key = store.commit(_intent())

    assert key == "idem-intent-pg-1"
    assert conn.transaction_count == 1
    assert conn.committed == 1
    sql = "\n".join(statement for statement, _params in conn.cursor_state.statements)
    assert "INSERT INTO v3_order_intents" in sql
    assert "INSERT INTO v3_risk_approvals" in sql
    assert "INSERT INTO v3_transactional_outbox" in sql
    assert "INSERT INTO v3_outbox_events" in sql


def test_postgres_intent_outbox_rolls_back_when_outbox_insert_fails() -> None:
    conn = _RecordingConnection()
    conn.cursor_state.fail_on = "INSERT INTO v3_transactional_outbox"
    store = PostgresIntentOutbox(connection_factory=lambda: conn)

    with pytest.raises(RuntimeError, match="injected database failure"):
        store.commit(_intent())

    assert conn.committed == 0
    assert conn.rolled_back == 1


def test_postgres_intent_outbox_rejects_unapproved_risk_increase() -> None:
    called = False

    def factory() -> _RecordingConnection:
        nonlocal called
        called = True
        return _RecordingConnection()

    with pytest.raises(ValueError, match="RISK_APPROVAL_REQUIRED"):
        PostgresIntentOutbox(connection_factory=factory).commit(_intent(approved=False))
    assert called is False


def test_postgres_intent_outbox_rejects_expired_approval_before_database_access() -> None:
    called = False

    def factory() -> _RecordingConnection:
        nonlocal called
        called = True
        return _RecordingConnection()

    expired = replace(_intent(), risk_expires_at=1.0)
    with pytest.raises(ValueError, match="RISK_APPROVAL_EXPIRED"):
        PostgresIntentOutbox(connection_factory=factory).commit(expired)
    assert called is False


def test_postgres_intent_outbox_rejects_incomplete_approval_envelope() -> None:
    incomplete = replace(_intent(), risk_snapshot_hash="")
    with pytest.raises(ValueError, match="RISK_APPROVAL_FIELDS_REQUIRED"):
        PostgresIntentOutbox(connection_factory=_RecordingConnection).commit(incomplete)


def test_postgres_intent_outbox_rejects_missing_exact_order_binding() -> None:
    incomplete = replace(_intent(), risk_intent_hash="")
    with pytest.raises(ValueError, match="RISK_APPROVAL_FIELDS_REQUIRED"):
        PostgresIntentOutbox(connection_factory=_RecordingConnection).commit(incomplete)


def test_postgres_duplicate_identity_probe_requires_fenced_durable_facts() -> None:
    no_fence = PostgresIntentOutbox(connection_factory=_RecordingConnection)
    assert no_fence.duplicate_order_count_24h() is None

    conn = _RecordingConnection()
    conn.cursor_state.fetchone_values.append((2, 2, 1, 2))
    store = PostgresIntentOutbox(
        connection_factory=lambda: conn,
        fencing_token=7,
        lease_owner="worker-1",
    )
    assert store.duplicate_order_count_24h() == 1
    sql = "\n".join(statement for statement, _params in conn.cursor_state.statements)
    assert "COUNT(DISTINCT client_order_id)" in sql
    assert "CURRENT_TIMESTAMP - INTERVAL '24 hours'" in sql


def test_postgres_duplicate_identity_probe_keeps_missing_identity_unknown() -> None:
    conn = _RecordingConnection()
    conn.cursor_state.fetchone_values.append((2, 1, 1, 2))
    store = PostgresIntentOutbox(connection_factory=lambda: conn, fencing_token=7)

    assert store.duplicate_order_count_24h() is None


def test_postgres_worker_claims_with_skip_locked_and_fencing() -> None:
    conn = _RecordingConnection()
    conn.cursor_state.fetchall_values.append([("msg-1", "intent-1", "cid-1", json.dumps({"intent_id": "intent-1"}))])
    conn.cursor_state.fetchone_values.append(("msg-1", "intent-1", "cid-1", {"intent_id": "intent-1"}))
    worker = OutboxWorker(db_conn=conn, lease_owner="worker-1", fencing_token=7)

    claims = asyncio.run(worker.claim(batch_size=4))

    assert len(claims) == 1
    assert claims[0].message_id == "msg-1"
    assert claims[0].fencing_token == 7
    assert conn.committed == 1
    sql = "\n".join(statement for statement, _params in conn.cursor_state.statements)
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "fencing_token" in sql


def test_postgres_worker_unknown_resolution_never_blind_resends() -> None:
    conn = _RecordingConnection()
    conn.cursor_state.fetchone_values.append(("intent-1",))
    worker = OutboxWorker(db_conn=conn, lease_owner="worker-new", fencing_token=8)

    resolved = asyncio.run(worker.resolve_unknown("msg-1", exchange_order_found=False))

    assert resolved == "PENDING"
    sql = "\n".join(statement for statement, _params in conn.cursor_state.statements)
    assert "status='UNKNOWN'" in sql
    assert "status=%s" in sql
    assert "UNKNOWN_RESOLVED_ABSENT" in sql or any(
        "UNKNOWN_RESOLVED_ABSENT" in str(params) for _statement, params in conn.cursor_state.statements
    )


def test_postgres_worker_requires_fencing_token() -> None:
    worker = OutboxWorker(db_conn=_RecordingConnection(), lease_owner="worker-no-token")
    with pytest.raises(RuntimeError, match="OUTBOX_FENCING_TOKEN_UNKNOWN"):
        asyncio.run(worker.claim())


def test_postgres_worker_failure_moves_to_dead_letter_at_retry_limit() -> None:
    conn = _RecordingConnection()
    conn.cursor_state.fetchone_values.append(("intent-1", "SENDING", 1, 2))
    worker = OutboxWorker(db_conn=conn, lease_owner="worker-1", fencing_token=2)

    result = asyncio.run(worker.mark_failed("msg-1", "HTTP_500"))

    assert result == "DEAD_LETTER"
    sql = "\n".join(statement for statement, _params in conn.cursor_state.statements)
    assert "dead_letter_reason" in sql
    assert conn.committed == 1


def test_postgres_intent_outbox_binds_claim_and_transition_to_service_owner() -> None:
    conn = _RecordingConnection()
    payload = PostgresIntentOutbox._intent_payload(_intent(), "idem-intent-pg-1")
    payload_json = json.dumps(payload)
    conn.cursor_state.fetchone_values.extend(
        [
            ("msg-1", "intent-1", payload_json),
            (payload_json,),
            ("msg-1", "SENDING", "intent-1"),
            ("intent-1",),
        ]
    )
    store = PostgresIntentOutbox(
        connection_factory=lambda: conn,
        lease_owner="beidou-autopilot",
        fencing_token=3,
    )

    with pytest.raises(ValueError, match="OUTBOX_LEASE_OWNER_MISMATCH"):
        store.claim(owner="ephemeral-engine-owner")

    claimed = store.claim(owner="beidou-autopilot")
    assert claimed is not None
    store.ack("intent-1")
    claim_params = [params for sql, params in conn.cursor_state.statements if "SET status='SENDING'" in sql]
    assert claim_params and claim_params[0][0] == "beidou-autopilot"
    transition_params = [params for sql, params in conn.cursor_state.statements if "SET status=%s,last_error" in sql]
    assert transition_params and "beidou-autopilot" in transition_params[0]


def test_postgres_intent_outbox_cannot_ack_pending_or_unowned_intent() -> None:
    conn = _RecordingConnection()
    conn.cursor_state.fetchone_values.extend(
        [
            ("msg-pending", "PENDING", "intent-pending"),
            None,
        ]
    )
    store = PostgresIntentOutbox(
        connection_factory=lambda: conn,
        lease_owner="worker-current",
        fencing_token=4,
    )

    store.ack("intent-pending")

    updates = [(sql, params) for sql, params in conn.cursor_state.statements if "SET status=%s,last_error" in sql]
    assert updates
    sql, params = updates[0]
    assert "status IN" in sql
    assert "lease_owner=%s AND fencing_token=%s" in sql
    assert params[-2:] == ("worker-current", 4)


def test_postgres_worker_fenced_recovery_marks_old_inflight_unknown() -> None:
    conn = _RecordingConnection()
    conn.cursor_state.fetchall_values.append([("msg-1", "intent-1", "SENT")])
    worker = OutboxWorker(db_conn=conn, lease_owner="worker-new", fencing_token=9)

    recovered = asyncio.run(worker.recover_inflight())

    assert recovered == 1
    sql = "\n".join(statement for statement, _params in conn.cursor_state.statements)
    assert "fencing_token<%s" in sql
    assert "FENCED_RECOVERY" in sql or any(
        "FENCED_RECOVERY" in str(params) for _statement, params in conn.cursor_state.statements
    )


def test_postgres_worker_recovery_fences_prior_owner_even_with_same_live_lease() -> None:
    """A restart must not inherit an old generation's unexpired lease."""

    conn = _RecordingConnection()
    conn.cursor_state.fetchall_values.append([("msg-1", "intent-1", "SENDING")])
    worker = OutboxWorker(db_conn=conn, lease_owner="worker-new", fencing_token=9)

    assert asyncio.run(worker.recover_inflight()) == 1

    recovery_select = next(
        (
            params
            for sql, params in conn.cursor_state.statements
            if sql.startswith("SELECT message_id,intent_id,status")
        ),
        (),
    )
    assert "lease_owner IS DISTINCT FROM %s" in "\n".join(
        sql for sql, _params in conn.cursor_state.statements if "SELECT message_id,intent_id,status" in sql
    )
    # 默认宽限为 0(启动恢复语义不变)
    assert recovery_select == ("worker-new", 9, 0)


def test_postgres_worker_runtime_recovery_applies_expired_margin() -> None:
    """运行时周期恢复(根因修复):同 owner 的过期租约只有超过宽限窗口
    才转 UNKNOWN,避免慢 venue 调用被自己围栏;启动恢复仍传 0。"""
    conn = _RecordingConnection()
    conn.cursor_state.fetchall_values.append([("msg-1", "intent-1", "SENDING")])
    worker = OutboxWorker(db_conn=conn, lease_owner="worker-new", fencing_token=9)

    assert asyncio.run(worker.recover_inflight(expired_margin_seconds=120)) == 1

    recovery_select = next(
        (
            params
            for sql, params in conn.cursor_state.statements
            if sql.startswith("SELECT message_id,intent_id,status")
        ),
        (),
    )
    assert recovery_select == ("worker-new", 9, 120)
    sql = "\n".join(statement for statement, _params in conn.cursor_state.statements)
    assert "lease_until<CURRENT_TIMESTAMP - (%s * INTERVAL '1 second')" in sql


def test_postgres_intent_outbox_startup_recovery_marks_fenced_or_expired_unknown() -> None:
    conn = _RecordingConnection()
    conn.cursor_state.fetchall_values.append([("msg-1", "intent-1", "SENDING")])
    child = _children()[0]
    child = replace(child, state=ChildCommandState.SENDING, event_ids=("send-0",))
    conn.cursor_state.fetchall_values.append([("intent-pg-1", 0, "SENDING", json.dumps(child.to_payload()))])
    store = PostgresIntentOutbox(
        connection_factory=lambda: conn,
        lease_owner="worker-current",
        fencing_token=7,
    )

    recovered = store.recover_inflight()

    assert recovered == 1
    assert conn.committed == 1
    sql = "\n".join(statement for statement, _params in conn.cursor_state.statements)
    assert "lease_until<CURRENT_TIMESTAMP" in sql
    assert "fencing_token<%s" in sql
    assert "SET status='UNKNOWN'" in sql
    assert "fencing_token=%s" in sql
    assert "FROM v3_execution_commands" in sql
    assert "JOIN v3_transactional_outbox" in sql
    assert "o.status='UNKNOWN'" in sql
    assert "o.lease_until<CURRENT_TIMESTAMP" in sql
    assert "UPDATE v3_execution_commands SET state='UNKNOWN'" in sql
    assert "INSERT INTO v3_execution_command_events" in sql
    assert any("FENCED_RECOVERY_UNKNOWN" in str(params) for _statement, params in conn.cursor_state.statements)
    assert "blind" not in sql.lower()


def test_postgres_intent_outbox_recovery_fences_prior_owner_even_with_same_live_lease() -> None:
    conn = _RecordingConnection()
    conn.cursor_state.fetchall_values.append([("msg-1", "intent-1", "SENT")])
    store = PostgresIntentOutbox(
        connection_factory=lambda: conn,
        lease_owner="worker-new",
        fencing_token=7,
    )

    assert store.recover_inflight() == 1

    recovery_select = next(
        (
            params
            for sql, params in conn.cursor_state.statements
            if sql.startswith("SELECT message_id,intent_id,status")
        ),
        (),
    )
    sql = "\n".join(statement for statement, _params in conn.cursor_state.statements)
    assert "lease_owner IS DISTINCT FROM %s" in sql
    # 默认宽限 0(启动恢复语义不变)
    assert recovery_select == ("worker-new", 7, 0)


def test_postgres_intent_outbox_startup_recovery_requires_fencing_token() -> None:
    store = PostgresIntentOutbox(connection_factory=_RecordingConnection)

    with pytest.raises(RuntimeError, match="OUTBOX_FENCING_TOKEN_UNKNOWN"):
        store.recover_inflight()


def test_postgres_unknown_intents_expose_only_identity_bound_recovery_fields() -> None:
    conn = _RecordingConnection()
    payload = PostgresIntentOutbox._intent_payload(_intent(), "idem-intent-pg-1")
    conn.cursor_state.fetchall_values.append([(json.dumps(payload),)])
    store = PostgresIntentOutbox(
        connection_factory=lambda: conn,
        lease_owner="worker-current",
        fencing_token=7,
    )

    unknown = store.get_unknown_intents()

    assert unknown == [
        {
            "intent_id": "intent-pg-1",
            "symbol": "BTCUSDT",
            "client_order_id": "cid-pg-1",
        }
    ]
    query, params = conn.cursor_state.statements[-1]
    assert "status IN (%s)" in query
    assert params == ("UNKNOWN",)


def test_postgres_intent_outbox_renew_lease_binds_to_owner_and_fence() -> None:
    """多切片 pacing 期间续租:只允许当前 owner + fencing generation 续租。"""

    conn = _RecordingConnection()
    conn.cursor_state.rowcount = 1
    store = PostgresIntentOutbox(
        connection_factory=lambda: conn,
        lease_owner="worker-current",
        fencing_token=5,
    )

    assert store.renew_lease("intent-1", lease_seconds=90) is True

    sql, params = conn.cursor_state.statements[-1]
    assert "lease_until=CURRENT_TIMESTAMP+(%s * INTERVAL '1 second')" in sql
    assert "intent_id=%s" in sql
    assert "status='SENDING'" in sql
    assert "lease_owner=%s" in sql
    assert "fencing_token=%s" in sql
    assert params == (90, "intent-1", "worker-current", 5)


def test_postgres_intent_outbox_renew_lease_is_noop_when_unowned() -> None:
    """租约已被他人持有(或 parent 不再是 SENDING)时续租不得生效。"""

    conn = _RecordingConnection()
    conn.cursor_state.rowcount = 0
    store = PostgresIntentOutbox(
        connection_factory=lambda: conn,
        lease_owner="worker-stale",
        fencing_token=5,
    )

    assert store.renew_lease("intent-1", lease_seconds=90) is False
