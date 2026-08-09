from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from beidou_core.store import PersistentStore
from beidou_exchange.binance_usdm.adapter import BinanceUsdmAdapter
from beidou_exchange.core.user_stream import UserStreamStatus
from beidou_safety.execution.reconciliation import AccountFactSnapshot
from beidou_safety.execution.user_events import UserProjectionStatus, UserStreamProjector
from beidou_shared.types import AccountId, InstrumentId, MonetaryValue, Quantity, VenueId


def _raw(*, sequence: int, update_id: int, cumulative: str, status: str = "PARTIALLY_FILLED") -> dict:
    return {
        "e": "ORDER_TRADE_UPDATE",
        "E": 1_000 + sequence,
        "T": 999 + sequence,
        "u": sequence,
        "o": {
            "i": 123,
            "I": update_id,
            "c": "cid-123",
            "s": "BTCUSDT",
            "S": "BUY",
            "o": "MARKET",
            "X": status,
            "x": "TRADE",
            "q": "0.30",
            "z": cumulative,
            "l": "0.10",
            "L": "100",
            "ap": "100",
            "t": update_id,
            "n": "0.01",
            "N": "USDT",
            "rp": "0",
        },
    }


def _update(*, sequence: int, update_id: int, cumulative: str, status: str = "PARTIALLY_FILLED"):
    result = BinanceUsdmAdapter.parse_user_order_update(
        _raw(sequence=sequence, update_id=update_id, cumulative=cumulative, status=status)
    )
    assert result.is_success() and result.data is not None
    return result.data


def _account_raw(*, event_time: int, update_id: int = 1, wallet: str = "1010", position: str = "0.20") -> dict:
    return {
        "e": "ACCOUNT_UPDATE",
        "E": event_time,
        "T": event_time,
        "a": {
            "m": "ORDER",
            "B": [{"a": "USDT", "wb": wallet, "cw": wallet, "bc": "0"}],
            "P": [
                {
                    "s": "BTCUSDT",
                    "pa": position,
                    "ep": "100",
                    "bep": "100",
                    "cr": "0",
                    "up": "1",
                    "mt": "cross",
                    "iw": "0",
                    "ps": "BOTH",
                }
            ],
            "I": update_id,
        },
    }


def _account_update(*, event_time: int, wallet: str = "1010", position: str = "0.20"):
    result = BinanceUsdmAdapter.parse_user_account_update(
        _account_raw(event_time=event_time, wallet=wallet, position=position)
    )
    assert result.is_success() and result.data is not None
    return result.data


def test_user_event_parser_rejects_missing_execution_fact() -> None:
    result = BinanceUsdmAdapter.parse_user_order_update({"e": "ORDER_TRADE_UPDATE", "E": 1, "o": {"i": 1}})
    assert not result.is_success()


def test_user_account_parser_requires_complete_rows() -> None:
    parsed = BinanceUsdmAdapter.parse_user_account_update(_account_raw(event_time=2_000))
    assert parsed.is_success() and parsed.data is not None
    assert parsed.data.balances[0].wallet_balance.amount == "1010"
    assert parsed.data.positions[0].position_amount.amount == "0.20"

    malformed = _account_raw(event_time=2_001)
    del malformed["a"]["P"][0]["bep"]
    assert not BinanceUsdmAdapter.parse_user_account_update(malformed).is_success()


def test_user_stream_projection_is_durable_contiguous_and_idempotent(tmp_path) -> None:
    store = PersistentStore(str(tmp_path / "user-events.db"))
    projector = UserStreamProjector(store=store)

    first = _update(sequence=10, update_id=1, cumulative="0.10")
    assert projector.ingest(first).status is UserProjectionStatus.ACCEPTED
    assert projector.fact_snapshot().positions["BTCUSDT"].amount == "0.1"
    assert store.get_user_stream_event(first.event.event_id)["applied_state"] == "APPLIED"

    assert projector.ingest(first).status is UserProjectionStatus.DUPLICATE
    second = _update(sequence=11, update_id=2, cumulative="0.20")
    assert projector.ingest(second).status is UserProjectionStatus.ACCEPTED
    assert projector.fact_snapshot().positions["BTCUSDT"].amount == "0.2"

    gap = _update(sequence=13, update_id=3, cumulative="0.30")
    blocked = projector.ingest(gap)
    assert blocked.status is UserProjectionStatus.BLOCKED
    assert blocked.observation is not None
    assert blocked.observation.status is UserStreamStatus.GAP
    assert len(store.restore_user_stream_events()) == 2

    restored = UserStreamProjector(store=store)
    assert restored.sequencer.status is UserStreamStatus.GAP
    restored.sequencer.mark_replayed(11)
    next_update = _update(sequence=12, update_id=4, cumulative="0.30")
    assert restored.ingest(next_update).status is UserProjectionStatus.ACCEPTED
    assert restored.fact_snapshot().positions["BTCUSDT"].amount == "0.3"


def test_user_event_identity_conflict_is_not_ignored(tmp_path) -> None:
    store = PersistentStore(str(tmp_path / "user-event-conflict.db"))
    update = _update(sequence=10, update_id=1, cumulative="0.10")
    assert store.save_user_stream_event(update, continuity_status="HEALTHY")
    conflicting_event = replace(update.event, raw_event={**update.event.raw_event, "E": 9999})
    conflicting = replace(update, event=conflicting_event)
    with pytest.raises(RuntimeError, match="identity conflict"):
        store.save_user_stream_event(conflicting, continuity_status="HEALTHY")


def test_account_updates_require_explicit_replay_and_restore_fail_closed(tmp_path) -> None:
    store = PersistentStore(str(tmp_path / "account-events.db"))
    projector = UserStreamProjector(store=store)
    update = _account_update(event_time=2_000)
    blocked = projector.ingest_account_update(update)
    assert blocked.status is UserProjectionStatus.BLOCKED
    assert "REPLAY_BASELINE_REQUIRED" in blocked.reason

    baseline = AccountFactSnapshot(
        account_id=AccountId("default"),
        venue_id=VenueId("BINANCE"),
        balance=MonetaryValue(amount="1000", currency="USDT"),
        positions={InstrumentId("BTCUSDT"): Quantity(amount="0.10")},
        open_orders=["order-1"],
        timestamp=datetime.fromtimestamp(1, tz=timezone.utc),
        source="AUTHORIZED_REPLAY_REST_SNAPSHOT",
        fact_version="replay-v1",
        complete=True,
    )
    authorized = projector.authorize_replay_baseline(
        baseline,
        evidence_hash="sha256:replay",
        approval_id="approval-replay",
        allow_unsequenced=True,
    )
    assert authorized.status is UserProjectionStatus.ACCEPTED
    assert projector.ingest_account_update(update).status is UserProjectionStatus.ACCEPTED
    snapshot = projector.fact_snapshot()
    assert snapshot.complete is True
    assert snapshot.balance.amount == "1010"
    assert snapshot.positions[InstrumentId("BTCUSDT")].amount == "0.2"
    assert store.get_user_stream_event(update.event.event_id)["applied_state"] == "APPLIED"

    restored = UserStreamProjector(store=store)
    assert restored.fact_snapshot().complete is False
    assert restored.ingest_account_update(_account_update(event_time=3_000)).status is UserProjectionStatus.BLOCKED
    reauthorized = restored.authorize_replay_baseline(
        snapshot,
        evidence_hash="sha256:replay-2",
        approval_id="approval-replay-2",
        allow_unsequenced=True,
    )
    assert reauthorized.status is UserProjectionStatus.ACCEPTED
    assert (
        restored.ingest_account_update(_account_update(event_time=3_000, wallet="1020", position="0.30")).status
        is UserProjectionStatus.ACCEPTED
    )
