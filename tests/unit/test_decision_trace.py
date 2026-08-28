"""PKG-03: durable DecisionTrace and crash-safe execution state."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from beidou_shared.decision_trace import DecisionTrace, DecisionTraceStore, TraceStatus


def _trace() -> DecisionTrace:
    return DecisionTrace(
        trace_id="trace-1",
        intent_id="intent-1",
        client_order_id="client-1",
        timestamp=datetime(2026, 8, 28, tzinfo=timezone.utc),
        pool={"id": "pool-1", "version": "1", "hash": "pool-hash", "symbol": "BTCUSDT"},
        market_data_hash="market-hash",
        factor_outputs=[{"component_id": "factor-1", "version": "v1", "output_hash": "factor-hash"}],
        strategy={"kernel_hash": "kernel-hash", "proposal_hash": "proposal-hash"},
        portfolio={"target_hash": "target-hash"},
        sizing={"input_hash": "sizing-in", "output_hash": "sizing-out", "quantity": "0.001"},
        exchange_rules_hash="rules-hash",
    )


def test_trace_statuses_and_hash_are_durable(tmp_path) -> None:
    path = tmp_path / "trace.jsonl"
    store = DecisionTraceStore(path)
    prepared = store.prepare(_trace())
    assert prepared.status is TraceStatus.PREPARED
    assert prepared.compute_hash()
    assert path.stat().st_mode & 0o077 == 0

    submitted = store.update(
        "trace-1",
        TraceStatus.SUBMITTED,
        order_request={"symbol": "BTCUSDT", "side": "BUY", "quantity": "0.001"},
        order_request_hash="order-hash",
    )
    assert submitted.status is TraceStatus.SUBMITTED
    acked = store.update(
        "trace-1",
        TraceStatus.ACKED,
        exchange_order_id="17",
        order_ack={"orderId": "17", "clientOrderId": "client-1", "origQty": "0.001"},
        order_status="NEW",
    )
    assert acked.exchange_order_id == "17"
    closed = store.update(
        "trace-1",
        TraceStatus.CLOSED,
        order_status="FILLED",
        executed_qty="0.001",
        position_after={"symbol": "BTCUSDT", "quantity": "0"},
        reconciliation={"status": "MATCHED", "unresolved": []},
    )
    assert closed.status is TraceStatus.CLOSED
    assert store.unresolved_count == 0

    restored = DecisionTraceStore(path)
    loaded = restored.get("trace-1")
    assert loaded is not None
    assert loaded.status is TraceStatus.CLOSED
    assert loaded.strategy["proposal_hash"] == "proposal-hash"


def test_prepared_and_unknown_recovery_preserve_identity(tmp_path) -> None:
    path = tmp_path / "trace-recovery.jsonl"
    first = DecisionTraceStore(path)
    first.prepare(_trace())
    first.update("trace-1", TraceStatus.UNKNOWN, error={"reason": "POST_TIMEOUT"})

    second = DecisionTraceStore(path)
    candidates = second.recovery_candidates()
    assert len(candidates) == 1
    assert candidates[0].client_order_id == "client-1"
    assert candidates[0].intent_id == "intent-1"
    assert second.prepare(_trace()).status is TraceStatus.UNKNOWN


def test_invalid_transition_and_ack_without_venue_identity_are_rejected(tmp_path) -> None:
    store = DecisionTraceStore(tmp_path / "trace.jsonl")
    store.prepare(_trace())
    with pytest.raises(ValueError, match="invalid trace transition"):
        store.update("trace-1", TraceStatus.FILLED)
    store.update("trace-1", TraceStatus.SUBMITTED)
    with pytest.raises(ValueError, match="venue order identity"):
        store.update("trace-1", TraceStatus.ACKED)


def test_torn_tail_does_not_erase_last_complete_snapshot(tmp_path) -> None:
    path = tmp_path / "trace-torn.jsonl"
    store = DecisionTraceStore(path)
    store.prepare(_trace())
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"event":"TRACE_SNAPSHOT","trace":')
    restored = DecisionTraceStore(path)
    assert restored.get("trace-1") is not None
    assert restored.corrupt_tail_lines == 1


def test_secrets_are_redacted_and_not_allowed_as_update_fields(tmp_path) -> None:
    trace = _trace()
    trace.metadata = {"api_secret": "do-not-write", "safe": "ok"}
    payload = trace.to_dict()
    assert payload["metadata"]["api_secret"] == "[REDACTED]"
    assert payload["metadata"]["safe"] == "ok"

    store = DecisionTraceStore(tmp_path / "trace-secret.jsonl")
    store.prepare(_trace())
    with pytest.raises(ValueError, match="secret fields"):
        store.update("trace-1", TraceStatus.SUBMITTED, api_secret="forbidden")  # noqa: S106 - negative secret test

    # The journal itself remains valid JSON and contains no raw secret value.
    rows = [json.loads(line) for line in (tmp_path / "trace-secret.jsonl").read_text().splitlines()]
    assert all("forbidden" not in json.dumps(row) for row in rows)
