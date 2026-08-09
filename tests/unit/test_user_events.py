from __future__ import annotations

from dataclasses import replace

import pytest

from beidou_core.store import PersistentStore
from beidou_exchange.binance_usdm.adapter import BinanceUsdmAdapter
from beidou_exchange.core.user_stream import UserStreamStatus
from beidou_safety.execution.user_events import UserProjectionStatus, UserStreamProjector


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


def test_user_event_parser_rejects_missing_execution_fact() -> None:
    result = BinanceUsdmAdapter.parse_user_order_update({"e": "ORDER_TRADE_UPDATE", "E": 1, "o": {"i": 1}})
    assert not result.is_success()


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
