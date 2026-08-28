"""Coverage-gap tests for beidou_infra.outbox legacy payload fallback."""

from __future__ import annotations

from datetime import datetime, timezone

from beidou_infra.outbox import PostgresIntentOutbox
from beidou_safety.execution import OrderIntent
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


def _intent(intent_id: str = "intent-coverage-gap") -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        account_ref=AccountRef(venue_id=VenueId("BINANCE"), account_id=AccountId("test")),
        instrument_id=InstrumentId("BTCUSDT"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(amount="0.1"),
        time_in_force=TimeInForce.GTC,
        client_order_id="cid-coverage-gap",
        idempotency_key="idem-coverage-gap",
        risk_approval_id="approval-coverage-gap",
        risk_approval_signature="sig-coverage-gap",
        risk_proposal_hash="proposal-hash",
        risk_intent_hash="intent-hash",
        risk_account_snapshot_hash="account-hash",
        risk_snapshot_hash="risk-hash",
        risk_policy_version="policy-v1",
        risk_nonce="nonce-1",
        risk_expires_at=4102444800.0,
    )


def test_deserialize_intent_payload_normalizes_naive_datetime_created_at() -> None:
    payload = PostgresIntentOutbox._intent_payload(_intent(), "idem-coverage-gap")
    del payload["created_at"]

    naive = datetime(2024, 1, 1, 0, 0)  # noqa: DTZ001 - intentionally naive to exercise the fallback
    restored = PostgresIntentOutbox._deserialize_intent_payload(payload, created_at=naive)

    assert restored.intent_id == "intent-coverage-gap"
    assert restored.created_at == datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)


def test_deserialize_intent_payload_uses_str_for_non_datetime_created_at() -> None:
    payload = PostgresIntentOutbox._intent_payload(_intent("intent-coverage-gap-str"), "idem-coverage-gap-str")
    del payload["created_at"]

    restored = PostgresIntentOutbox._deserialize_intent_payload(payload, created_at="2024-01-01T00:00:00+00:00")

    assert restored.intent_id == "intent-coverage-gap-str"
    assert restored.created_at == datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
