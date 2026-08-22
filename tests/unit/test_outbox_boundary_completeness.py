"""Additional state-machine coverage for durable outbox adapters.

All database objects below are recording doubles.  The tests verify SQL
branching and conservative state transitions; they never connect to a real
database or send an order.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from beidou_infra.outbox import (
    OutboxWorker,
    PostgresIntentOutbox,
    _json_payload,
    _row_value,
)
from beidou_safety.execution import OrderIntent
from beidou_safety.execution.command_aggregate import ChildCommandState, ExecutionChildCommand
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


class _Cursor:
    def __init__(self) -> None:
        self.one: list[object] = []
        self.many: list[list[object]] = []
        self.statements: list[tuple[str, tuple[object, ...]]] = []
        self.rowcount = 1

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        self.statements.append((sql, params))

    def fetchone(self) -> object:
        return self.one.pop(0) if self.one else None

    def fetchall(self) -> list[object]:
        return self.many.pop(0) if self.many else []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


class _Conn:
    def __init__(self, cursor: _Cursor) -> None:
        self.cursor_state = cursor
        self.closed = False
        self.committed = 0
        self.rolled_back = 0

    def cursor(self) -> _Cursor:
        return self.cursor_state

    def close(self) -> None:
        self.closed = True


class _BareConn:
    def __init__(self, cursor: _Cursor) -> None:
        self.cursor_state = cursor
        self.committed = 0
        self.rolled_back = 0

    def cursor(self) -> _Cursor:
        return self.cursor_state

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1


def _child(intent_id: str = "intent-1", sequence: int = 0) -> ExecutionChildCommand:
    return ExecutionChildCommand.create(
        parent_intent_id=intent_id,
        sequence=sequence,
        symbol="BTCUSDT",
        side="BUY",
        quantity="0.1",
        order_type="MARKET",
        time_in_force="GTC",
        client_order_id=f"client-{sequence}",
    )


def _intent(intent_id: str = "intent-pg-complete") -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
        instrument_id=InstrumentId("BTCUSDT"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(amount="0.1"),
        time_in_force=TimeInForce.GTC,
        client_order_id=f"cid-{intent_id}",
        idempotency_key=f"idem-{intent_id}",
        risk_approval_id="approval",
        risk_approval_signature="signature",
        risk_proposal_hash="proposal",
        risk_intent_hash="intent",
        risk_account_snapshot_hash="account",
        risk_snapshot_hash="risk",
        risk_policy_version="policy",
        risk_nonce="nonce",
        risk_expires_at=4102444800.0,
    )


def test_outbox_helpers_and_connection_contexts_fail_closed() -> None:
    assert _row_value({"x": 1}, "x", 0) == 1
    assert _row_value(("a",), "missing", 0) == "a"
    assert _json_payload({"x": 1}) == {"x": 1}
    assert _json_payload('{"x": 1}') == {"x": 1}
    with pytest.raises(ValueError):
        _json_payload("[]")

    cursor = _Cursor()
    conn = _BareConn(cursor)
    with OutboxWorker._transaction(conn):
        pass
    assert conn.committed == 1
    with pytest.raises(RuntimeError), OutboxWorker._transaction(conn):
        raise RuntimeError("rollback")
    assert conn.rolled_back == 1
    with OutboxWorker._cursor_scope(conn) as managed:
        assert managed is cursor
    assert OutboxWorker._claim_from_row(("m", "i", None, '{"x": 1}'), "w", 2).payload == {"x": 1}


@pytest.mark.asyncio
async def test_worker_unknown_resolution_recovery_and_retry_branches() -> None:
    no_db = OutboxWorker(fencing_token=1)
    assert await no_db.resolve_unknown("m", exchange_order_found=False) == "UNKNOWN"
    assert await no_db.mark_failed("m", "no-db") == "UNKNOWN"
    with pytest.raises(RuntimeError, match="FENCING"):
        await OutboxWorker().resolve_unknown("m", exchange_order_found=True)

    cursor = _Cursor()
    cursor.many = [[{"intent_id": "i", "message_id": "m", "status": "SENT"}]]
    conn = _Conn(cursor)
    worker = OutboxWorker(db_conn=conn, lease_owner="worker", fencing_token=4)
    assert await worker.recover_inflight(3) == 1
    assert any("FENCED_RECOVERY_UNKNOWN" in repr(params) or "UNKNOWN" in sql for sql, params in cursor.statements)

    cursor = _Cursor()
    cursor.one = [["i", "SENDING", 0, 3]]
    conn = _Conn(cursor)
    worker = OutboxWorker(db_conn=conn, lease_owner="worker", fencing_token=4)
    assert await worker.mark_failed("m", "temporary") == "PENDING"
    cursor.rowcount = 0
    assert await worker.mark_failed("m", "lost") == "UNKNOWN"

    cursor = _Cursor()
    cursor.one = [{"intent_id": "i"}]
    conn = _Conn(cursor)
    worker = OutboxWorker(db_conn=conn, lease_owner="worker", fencing_token=4)
    assert await worker.resolve_unknown("m", exchange_order_found=True) == "ACKED"


@pytest.mark.asyncio
async def test_worker_claim_and_transition_races_do_not_invent_success() -> None:
    row = {"message_id": "m", "intent_id": "i", "client_order_id": "cid", "payload": {"x": 1}}
    cursor = _Cursor()
    cursor.many = [[row]]
    cursor.one = [None]
    worker = OutboxWorker(db_conn=_Conn(cursor), lease_owner="w", fencing_token=1)
    assert await worker.claim(1) == []

    cursor = _Cursor()
    cursor.one = [None]
    worker = OutboxWorker(db_conn=_Conn(cursor), lease_owner="w", fencing_token=1)
    assert await worker.mark_sent("m") is False
    assert await worker.ack("m") is False
    assert await worker.mark_unknown("m", "ambiguous") is False

    async def sender(_claim: object) -> bool:
        return True

    assert await OutboxWorker(sender=sender, fencing_token=1).send(SimpleNamespace()) is True
    assert await OutboxWorker(sender=lambda _claim: False, fencing_token=1).send(SimpleNamespace()) is False
    assert (
        await OutboxWorker(
            sender=lambda _claim: (_ for _ in ()).throw(RuntimeError("transport")), fencing_token=1
        ).send(SimpleNamespace())
        is False
    )


def test_postgres_constructor_commit_and_plan_guards() -> None:
    with pytest.raises(ValueError, match="both"):
        PostgresIntentOutbox("dsn", connection_factory=lambda: _Conn(_Cursor()))
    with pytest.raises(ValueError, match="DSN"):
        PostgresIntentOutbox()
    with pytest.raises(ValueError, match="positive"):
        PostgresIntentOutbox(connection_factory=lambda: _Conn(_Cursor()), lease_seconds=0)

    cursor = _Cursor()
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), lease_owner="w", fencing_token=3)
    assert store.commit(_intent())
    assert any("INSERT INTO v3_transactional_outbox" in sql for sql, _ in cursor.statements)
    with pytest.raises(RuntimeError, match="FENCING"):
        PostgresIntentOutbox(connection_factory=lambda: _Conn(_Cursor())).persist_execution_plan("i", [_child()])


def test_postgres_plan_restore_and_child_transition_unknowns() -> None:
    cursor = _Cursor()
    cursor.one = [None]
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), lease_owner="w", fencing_token=3)
    with pytest.raises(ValueError, match="PARENT_NOT_CLAIMED"):
        store.persist_execution_plan("i", [_child("i")])
    assert store.restore_execution_plan("i") is None

    cursor = _Cursor()
    cursor.many = [[(json.dumps(_child("i").to_payload()),)]]
    cursor.one = [None]
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), lease_owner="w", fencing_token=3)
    assert store.transition_execution_child("i", 0, ChildCommandState.SENDING, event_id="e").state.value == "IN_FLIGHT"

    cursor = _Cursor()
    cursor.many = [[]]
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), fencing_token=3)
    with pytest.raises(ValueError, match="PLAN_NOT_FOUND"):
        store.transition_execution_child("i", 0, ChildCommandState.SENDING, event_id="e")


def test_postgres_diagnostics_and_claim_lease_queries_are_identity_bound() -> None:
    sending_child = _child().transition(ChildCommandState.SENDING, event_id="send")
    child_payload = json.dumps(sending_child.to_payload())
    cursor = _Cursor()
    cursor.many = [[(child_payload,)], [("i", 0, "SENDING", "BTCUSDT", "cid", "", "0", "old")]]
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), lease_owner="w", fencing_token=3)
    assert store.inflight_signed_quantity("BTCUSDT") == Decimal("0.1")
    assert store.stale_child_commands(10)[0]["parent_intent_id"] == "i"

    cursor = _Cursor()
    cursor.one = [None]
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), lease_owner="w", fencing_token=3)
    assert store.project_order_terminal("missing", "FILLED") is None
    assert store.claim("w") is None
    assert store.renew_lease("i") is True
    with pytest.raises(ValueError, match="OWNER_MISMATCH"):
        store.claim("other")

    empty = PostgresIntentOutbox.__new__(PostgresIntentOutbox)
    empty._connection_factory = None
    empty._fencing_token = 0
    empty._lease_seconds = 30
    empty._lease_owner = "w"
    assert empty.pending_count() == 0
    assert empty.recover_inflight() == 0
    assert empty.duplicate_order_count_24h() is None
    assert empty.stats["pending_count"] == 0
    assert empty.claim("", 1) is None


def test_postgres_user_order_projection_maps_all_terminal_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _Cursor()
    cursor.one = [{"parent_intent_id": "i", "sequence": 0}]
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), lease_owner="w", fencing_token=3)
    calls: list[tuple[object, ...]] = []

    def fake_transition(*args: object, **kwargs: object) -> str:
        calls.append((*args, *kwargs.values()))
        return "projected"

    monkeypatch.setattr(store, "transition_execution_child", fake_transition)
    with pytest.raises(ValueError, match="CLIENT_ID_MISSING"):
        store.project_user_order_update(SimpleNamespace(client_order_id=""))
    for status in (
        "NEW",
        "PENDING_CANCEL",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCELED",
        "EXPIRED",
        "REJECTED",
        "UNKNOWN",
        "OTHER",
    ):
        cursor.one = [{"parent_intent_id": "i", "sequence": 0}]
        result = store.project_user_order_update(
            SimpleNamespace(
                client_order_id="cid",
                order_status=SimpleNamespace(value=status),
                cumulative_quantity=SimpleNamespace(
                    amount="0.1" if status in {"PENDING_CANCEL", "CANCELED", "EXPIRED"} else "0"
                ),
                order_id="exchange-1",
                event=SimpleNamespace(event_id=f"event-{status}"),
            )
        )
        assert result == "projected"
    no_factory = PostgresIntentOutbox.__new__(PostgresIntentOutbox)
    no_factory._connection_factory = None
    no_factory._fencing_token = 0
    no_factory._lease_owner = "w"
    with pytest.raises(RuntimeError, match="FENCING"):
        no_factory.project_user_order_update(SimpleNamespace(client_order_id="cid"))


def test_postgres_stale_child_and_order_terminal_projection_are_governed(monkeypatch: pytest.MonkeyPatch) -> None:
    child = (
        _child("i")
        .transition(ChildCommandState.SENDING, event_id="send")
        .transition(ChildCommandState.ACKED, event_id="ack", exchange_order_id="ex")
    )
    cursor = _Cursor()
    cursor.one = [None, None]
    cursor.many = [[(json.dumps(child.to_payload()),)]]
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), lease_owner="w", fencing_token=3)
    store.recover_stale_child(
        "i", 0, ChildCommandState.FILLED, event_id="fill", exchange_order_id="ex", cumulative_filled_quantity="0.1"
    )

    cursor = _Cursor()
    cursor.one = [{"parent_intent_id": "i", "sequence": 0, "state": "ACKED"}]
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), lease_owner="w", fencing_token=3)
    monkeypatch.setattr(store, "transition_execution_child", lambda *args, **kwargs: "terminal")
    assert store.project_order_terminal("ex", "FILLED", "0.1") == "terminal"

    for row in (
        {"parent_intent_id": "i", "sequence": 0, "state": "FILLED"},
        {"parent_intent_id": "i", "sequence": 0, "state": "PLANNED"},
    ):
        cursor = _Cursor()
        cursor.one = [row]
        store = PostgresIntentOutbox(
            connection_factory=lambda cursor=cursor: _Conn(cursor), lease_owner="w", fencing_token=3
        )
        assert store.project_order_terminal("ex", "FILLED") is None

    no_factory = PostgresIntentOutbox.__new__(PostgresIntentOutbox)
    no_factory._connection_factory = None
    no_factory._fencing_token = 0
    with pytest.raises(RuntimeError, match="FENCING"):
        no_factory.recover_stale_child("i", 0, ChildCommandState.FILLED, event_id="x")
    with pytest.raises(RuntimeError, match="FENCING"):
        no_factory.project_order_terminal("ex", "FILLED")


def test_postgres_read_diagnostics_and_transition_ownership_branches() -> None:
    from beidou_safety.execution.intent import IntentOutbox

    payload = IntentOutbox._serialize_intent(_intent(), "idem-intent-pg-complete")
    cursor = _Cursor()
    cursor.many = [
        [
            {
                "approval_id": "a",
                "intent_id": "i",
                "decision": "APPROVED",
                "signature": "s",
                "proposal_hash": "p",
                "account_snapshot_hash": "a",
                "risk_snapshot_hash": "r",
                "intent_hash": "i",
                "policy_version": "v",
                "nonce": "n",
                "expires_at": 1,
            }
        ],
        [{"payload": payload}],
    ]
    cursor.one = [{"count": 2}, {"total": 3, "identified": 3, "distinct_client_ids": 2, "distinct_idempotency_keys": 2}]
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), lease_owner="w", fencing_token=3)
    assert store.restore_pending_approvals()[0]["approval_id"] == "a"
    assert store.unacked()[0].intent_id == _intent().intent_id
    assert store.pending_count() == 2
    assert store.duplicate_order_count_24h() == 1
    assert store._outbox == []

    for owner_required, target in ((True, "ACKED"), (False, "ACKED"), (None, "FAILED")):
        cursor = _Cursor()
        cursor.one = [
            {"message_id": "m", "status": "SENDING" if owner_required is not None else "PENDING", "intent_id": "i"},
            {"intent_id": "i"},
        ]
        store = PostgresIntentOutbox(
            connection_factory=lambda cursor=cursor: _Conn(cursor), lease_owner="w", fencing_token=3
        )
        assert (
            store._transition_intent(
                "i",
                target=target,
                reason="reason",
                event_type="TEST",
                from_states=("PENDING", "SENDING"),
                owner_required=owner_required,
            )
            is True
        )
    cursor = _Cursor()
    cursor.one = [{"message_id": "m", "status": "ACKED", "intent_id": "i"}]
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), lease_owner="w", fencing_token=3)
    assert store._transition_intent(
        "i", target="ACKED", event_type="TEST", from_states=("SENDING",), owner_required=True
    )
    with pytest.raises(ValueError, match="from_states"):
        store._transition_intent("i", target="X", event_type="TEST", from_states=(), owner_required=None)


@pytest.mark.asyncio
async def test_outbox_worker_factory_cursor_and_transition_races(monkeypatch: pytest.MonkeyPatch) -> None:
    class _NoContextCursor:
        def __init__(self) -> None:
            self.closed = False

        def execute(self, *_args: object) -> None:
            return None

        def close(self) -> None:
            self.closed = True

        def fetchone(self) -> object:
            return None

        def fetchall(self) -> list[object]:
            return []

    cursor = _NoContextCursor()

    class _NoContextConn:
        def __init__(self) -> None:
            self.closed = False

        def cursor(self) -> _NoContextCursor:
            return cursor

        def close(self) -> None:
            self.closed = True

    conn = _NoContextConn()
    worker = OutboxWorker(connection_factory=lambda: conn, lease_owner="w", fencing_token=1)
    with worker._connection_scope() as scoped:
        assert scoped is conn
    assert conn.closed
    with worker._cursor_scope(conn) as scoped_cursor:
        assert scoped_cursor is cursor
    assert cursor.closed

    with pytest.raises(RuntimeError, match="FENCING"):
        await OutboxWorker().recover_inflight()
    with pytest.raises(RuntimeError, match="FENCING"):
        await OutboxWorker().mark_failed("m", "bad")
    assert not await OutboxWorker(fencing_token=1)._transition_owned(
        "m", from_states=("SENDING",), to_status="SENT", event_type="x", fields="sent_at=NULL"
    )

    cursor = _Cursor()
    cursor.one = [None]
    worker = OutboxWorker(db_conn=_Conn(cursor), lease_owner="w", fencing_token=1)
    assert await worker.resolve_unknown("m", exchange_order_found=False) == "UNKNOWN"

    sending_child = _child().transition(ChildCommandState.SENDING, event_id="send")
    child_payload = json.dumps(sending_child.to_payload())
    cursor = _Cursor()
    cursor.many = [
        [{"message_id": "m", "intent_id": "i", "status": "SENDING"}],
        [{"parent_intent_id": "i", "sequence": 0, "state": "SENDING", "payload": child_payload}],
    ]
    cursor.rowcount = 0
    worker = OutboxWorker(db_conn=_Conn(cursor), lease_owner="w", fencing_token=1)
    assert await worker.recover_inflight() == 0

    cursor = _Cursor()
    cursor.one = [{"intent_id": "i", "status": "SENDING", "retry_count": 1, "max_retries": 5}]
    cursor.rowcount = 0
    worker = OutboxWorker(db_conn=_Conn(cursor), lease_owner="w", fencing_token=1)
    assert await worker.mark_failed("m", "retry") == "UNKNOWN"

    cursor = _Cursor()
    cursor.one = [{"intent_id": "i", "status": "SENDING"}, None]
    worker = OutboxWorker(db_conn=_Conn(cursor), lease_owner="w", fencing_token=1)
    assert not await worker._transition_owned(
        "m", from_states=("SENDING",), to_status="SENT", event_type="x", fields="sent_at=NULL"
    )

    processed: list[str] = []
    worker = OutboxWorker(fencing_token=1)

    async def claim(*, batch_size: int = 10) -> list[object]:
        assert batch_size == 10
        return [SimpleNamespace(message_id="ok"), SimpleNamespace(message_id="unknown")]

    worker.claim = claim  # type: ignore[method-assign]

    async def send(claim: object) -> bool:
        return claim.message_id == "ok"

    async def mark_sent(message_id: str) -> bool:
        processed.append("sent:" + message_id)
        return True

    async def mark_unknown(message_id: str, reason: str) -> bool:
        processed.append(f"unknown:{message_id}:{reason}")
        return True

    worker.send = send  # type: ignore[method-assign]
    worker.mark_sent = mark_sent  # type: ignore[method-assign]
    worker.mark_unknown = mark_unknown  # type: ignore[method-assign]
    assert await worker.process_batch() == 1
    assert processed == ["sent:ok", "unknown:unknown:TRANSPORT_RESULT_UNKNOWN"]

    class _Psycopg:
        @staticmethod
        def connect(dsn: str) -> object:
            return dsn

    monkeypatch.setitem(sys.modules, "psycopg", _Psycopg())
    dsn_store = PostgresIntentOutbox("postgresql://unit")
    assert dsn_store._connection_factory() == "postgresql://unit"


def test_postgres_commit_and_plan_contract_rejections() -> None:
    no_key = replace(_intent("no-key"), idempotency_key="")
    assert len(PostgresIntentOutbox._intent_key(no_key)) == 32

    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(_Cursor()), lease_owner="w", fencing_token=3)
    with pytest.raises(ValueError, match="ENVELOPE_INCOMPLETE"):
        store.commit(replace(_intent("incomplete"), risk_approval_signature=""))
    with pytest.raises(ValueError, match="APPROVAL_REQUIRED"):
        assert store.commit(
            replace(
                _intent("missing-approval"),
                risk_approval_id="",
                risk_approval_signature="",
                risk_expires_at=None,
            )
        )
    with pytest.raises(ValueError, match="FIELDS_REQUIRED"):
        store.commit(replace(_intent("missing-field"), risk_nonce=""))
    with pytest.raises(ValueError, match="EXPIRY_REQUIRED"):
        store.commit(replace(_intent("reduce-no-expiry"), reduce_only=True, risk_expires_at=None))
    with pytest.raises(ValueError, match="EXPIRED"):
        store.commit(replace(_intent("expired"), risk_expires_at=1.0))

    naive = replace(_intent("naive"), created_at=__import__("datetime").datetime.now())
    cursor = _Cursor()
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), lease_owner="w", fencing_token=3)
    assert store.commit(naive)
    assert cursor.statements

    intent = _intent("idempotent")
    key = store._intent_key(intent)
    payload = store._intent_payload(intent, key)
    cursor = _Cursor()
    cursor.one = [{"intent_id": intent.intent_id, "payload": json.dumps(payload)}]
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), lease_owner="w", fencing_token=3)
    assert store.commit(intent) == key
    cursor = _Cursor()
    cursor.one = [{"intent_id": intent.intent_id, "payload": json.dumps({"different": True})}]
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), lease_owner="w", fencing_token=3)
    with pytest.raises(ValueError, match="PAYLOAD_CONFLICT"):
        store.commit(intent)

    child = _child("plan")
    cursor = _Cursor()
    cursor.one = [{"intent_id": "plan"}]
    cursor.many = [[(json.dumps(child.to_payload()),)]]
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), lease_owner="w", fencing_token=3)
    restored = store.persist_execution_plan("plan", [child])
    assert restored.children[0].command_hash == child.command_hash

    other = ExecutionChildCommand.create(
        parent_intent_id="plan",
        sequence=0,
        symbol="ETHUSDT",
        side="BUY",
        quantity="0.1",
        order_type="MARKET",
        time_in_force="GTC",
        client_order_id="client-other",
    )
    cursor = _Cursor()
    cursor.one = [{"intent_id": "plan"}]
    cursor.many = [[(json.dumps(child.to_payload()),)]]
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), lease_owner="w", fencing_token=3)
    with pytest.raises(ValueError, match="PLAN_CONFLICT"):
        store.persist_execution_plan("plan", [other])

    empty = PostgresIntentOutbox.__new__(PostgresIntentOutbox)
    empty._connection_factory = None
    assert empty.restore_execution_plan("missing") is None


def test_postgres_execution_projection_and_stale_recovery_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    child_payload = json.dumps(_child("exec").to_payload())
    store = PostgresIntentOutbox.__new__(PostgresIntentOutbox)
    store._connection_factory = None
    store._fencing_token = 0
    with pytest.raises(RuntimeError, match="FENCING"):
        store.transition_execution_child("exec", 0, ChildCommandState.SENDING, event_id="x")
    with pytest.raises(RuntimeError, match="STORE_UNKNOWN"):
        store.inflight_signed_quantity("BTCUSDT")
    with pytest.raises(RuntimeError, match="FENCING"):
        store.recover_stale_child("exec", 0, ChildCommandState.REJECTED, event_id="x")

    cursor = _Cursor()
    cursor.many = [[(child_payload,)]]
    cursor.one = [{"event_id": "already"}]
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), lease_owner="w", fencing_token=3)
    aggregate = store.transition_execution_child("exec", 0, ChildCommandState.SENDING, event_id="already")
    assert aggregate.children[0].state is ChildCommandState.PLANNED

    cursor = _Cursor()
    cursor.many = [[(child_payload,)]]
    cursor.one = [None, None]
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), lease_owner="w", fencing_token=3)
    with pytest.raises(ValueError, match="UNKNOWN_CHILD_SEQUENCE"):
        store.transition_execution_child("exec", 99, ChildCommandState.SENDING, event_id="bad-sequence")

    cursor = _Cursor()
    cursor.many = [[(child_payload,)]]
    cursor.one = [None, None]
    cursor.rowcount = 0
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), lease_owner="w", fencing_token=3)
    with pytest.raises(ValueError, match="FENCED_OR_CONCURRENT"):
        store.transition_execution_child("exec", 0, ChildCommandState.SENDING, event_id="race")

    cursor = _Cursor()
    cursor.one = [None]
    cursor.many = [[]]
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), lease_owner="w", fencing_token=3)
    with pytest.raises(ValueError, match="PLAN_NOT_FOUND"):
        store.recover_stale_child("exec", 0, ChildCommandState.REJECTED, event_id="missing")

    cursor = _Cursor()
    cursor.one = [None, None]
    cursor.many = [[(child_payload,)]]
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), lease_owner="w", fencing_token=3)
    with pytest.raises(ValueError, match="UNKNOWN_CHILD_SEQUENCE"):
        store.recover_stale_child("exec", 99, ChildCommandState.REJECTED, event_id="bad")

    cursor = _Cursor()
    cursor.one = [None, {"event_id": "duplicate"}]
    cursor.many = [[(child_payload,)]]
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), lease_owner="w", fencing_token=3)
    assert store.recover_stale_child("exec", 0, ChildCommandState.REJECTED, event_id="duplicate") is None

    cursor = _Cursor()
    cursor.one = [None, None]
    cursor.many = [[(child_payload,)]]
    cursor.rowcount = 0
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), lease_owner="w", fencing_token=3)
    with pytest.raises(ValueError, match="STALE_CHILD_CONCURRENT"):
        store.recover_stale_child("exec", 0, ChildCommandState.REJECTED, event_id="race-stale")

    update = SimpleNamespace(
        client_order_id="cid",
        order_status=SimpleNamespace(value="NEW"),
        cumulative_quantity=SimpleNamespace(amount="0"),
        order_id="ex",
        event=SimpleNamespace(event_id="event"),
    )
    cursor = _Cursor()
    cursor.one = [{"parent_intent_id": "exec", "sequence": 0}]
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), lease_owner="w", fencing_token=3)
    monkeypatch.setattr(
        store,
        "transition_execution_child",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("INVALID_CHILD_TRANSITION")),
    )
    monkeypatch.setattr(store, "restore_execution_plan", lambda _intent_id: "restored")
    assert store.project_user_order_update(update) == "restored"
    cursor = _Cursor()
    cursor.one = [{"parent_intent_id": "exec", "sequence": 0}]
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), lease_owner="w", fencing_token=3)
    monkeypatch.setattr(
        store, "transition_execution_child", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("OTHER"))
    )
    with pytest.raises(ValueError, match="OTHER"):
        store.project_user_order_update(update)

    cursor = _Cursor()
    cursor.one = [{"parent_intent_id": "exec", "sequence": 0, "state": "ACKED"}]
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), lease_owner="w", fencing_token=3)
    monkeypatch.setattr(
        store,
        "transition_execution_child",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("TERMINAL_CHILD_STATE")),
    )
    assert store.project_order_terminal("ex", "FILLED") is None
    cursor = _Cursor()
    cursor.one = [{"parent_intent_id": "exec", "sequence": 0, "state": "ACKED"}]
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), lease_owner="w", fencing_token=3)
    monkeypatch.setattr(
        store, "transition_execution_child", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("OTHER"))
    )
    with pytest.raises(ValueError, match="OTHER"):
        store.project_order_terminal("ex", "FILLED")


def test_postgres_diagnostics_stats_and_wrapper_fail_closed_paths() -> None:
    empty = PostgresIntentOutbox.__new__(PostgresIntentOutbox)
    empty._connection_factory = None
    empty._fencing_token = 0
    empty._lease_owner = "w"
    assert empty.restore_pending_approvals() == []
    assert empty.unacked() == []
    assert empty.renew_lease("i") is False
    assert (
        empty._transition_intent("i", target="ACKED", event_type="x", from_states=("SENDING",), owner_required=True)
        is False
    )
    empty.dead_letter("i", "reason")
    empty.mark_unknown("i", "reason")
    empty.resolve_unknown("i", exchange_order_found=True)
    empty.resolve_unknown("i", exchange_order_found=False)

    cursor = _Cursor()
    cursor.many = [
        [{"status": "PENDING", "count": 2}, {"status": "UNKNOWN", "count": 1}, {"status": "DEAD_LETTER", "count": 3}]
    ]
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), lease_owner="w", fencing_token=3)
    assert store.stats == {
        "state_counts": {"PENDING": 2, "UNKNOWN": 1, "DEAD_LETTER": 3},
        "pending_count": 3,
        "outbox_size": 6,
        "unknown_count": 1,
        "dead_letter_count": 3,
    }

    cursor = _Cursor()
    cursor.one = [
        {"message_id": "m", "intent_id": "i", "payload": json.dumps(store._intent_payload(_intent("claim"), "k"))},
        None,
    ]
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), lease_owner="w", fencing_token=3)
    assert store.claim("w") is None

    cursor = _Cursor()
    cursor.one = [None]
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), lease_owner="w", fencing_token=3)
    assert store.duplicate_order_count_24h() is None
    cursor = _Cursor()
    cursor.one = [{"total": 2, "identified": 1, "distinct_client_ids": 1, "distinct_idempotency_keys": 1}]
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), lease_owner="w", fencing_token=3)
    assert store.duplicate_order_count_24h() is None

    class _BrokenCursor(_Cursor):
        def execute(self, *_args: object) -> None:
            raise RuntimeError("schema")

    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(_BrokenCursor()), lease_owner="w", fencing_token=3)
    assert store.duplicate_order_count_24h() is None

    cursor = _Cursor()
    cursor.many = [[{"message_id": "m", "intent_id": "i", "status": "SENDING"}], []]
    cursor.rowcount = 0
    store = PostgresIntentOutbox(connection_factory=lambda: _Conn(cursor), lease_owner="w", fencing_token=3)
    assert store.recover_inflight() == 0
