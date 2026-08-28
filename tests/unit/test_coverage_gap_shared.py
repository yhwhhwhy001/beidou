"""Coverage gap tests for beidou_shared.decision_trace.

These tests target the defensive/error branches that the existing
``test_decision_trace.py`` suite does not exercise: naive datetimes, identity
validation, unknown journal events, directory-fsync failures, and the
``find_by_client_order_id`` / unknown-field / unknown-trace rejection paths.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from beidou_shared.decision_trace import DecisionTrace, DecisionTraceStore, TraceStatus


def _trace() -> DecisionTrace:
    return DecisionTrace(
        trace_id="trace-1",
        intent_id="intent-1",
        client_order_id="client-1",
        timestamp=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )


def test_naive_datetime_is_normalized_to_utc() -> None:
    trace = DecisionTrace(
        trace_id="trace-1",
        intent_id="intent-1",
        client_order_id="client-1",
        timestamp=datetime(2026, 8, 28, 12, 0, 0),  # noqa: DTZ001 - naive is intentional
    )
    assert trace.timestamp.tzinfo is timezone.utc


def test_identity_fields_are_required() -> None:
    with pytest.raises(ValueError, match="required"):
        DecisionTrace(trace_id="", intent_id="intent-1", client_order_id="client-1")
    with pytest.raises(ValueError, match="required"):
        DecisionTrace(trace_id="trace-1", intent_id="  ", client_order_id="client-1")
    with pytest.raises(ValueError, match="required"):
        DecisionTrace(trace_id="trace-1", intent_id="intent-1", client_order_id="")


def test_environment_must_be_testnet() -> None:
    with pytest.raises(ValueError, match="TESTNET"):
        DecisionTrace(trace_id="trace-1", intent_id="intent-1", client_order_id="client-1", environment="LIVE")


def test_from_dict_rejects_non_object() -> None:
    with pytest.raises(ValueError, match="must be an object"):
        DecisionTrace.from_dict("not-a-dict")


def test_load_skips_blank_lines_and_counts_unknown_events(tmp_path) -> None:
    path = tmp_path / "trace.jsonl"
    store = DecisionTraceStore(path)
    store.prepare(_trace())
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n")
        handle.write('{"event": "NOT_A_SNAPSHOT", "trace": {}}\n')
    restored = DecisionTraceStore(path)
    assert restored.get("trace-1") is not None
    assert restored.corrupt_tail_lines == 1


def test_directory_fsync_failure_is_tolerated(tmp_path, monkeypatch) -> None:
    import beidou_shared.decision_trace as decision_trace_module

    real_open = decision_trace_module.os.open

    def fake_open(path, flags, *args, **kwargs):
        if flags == os.O_RDONLY:
            raise OSError("directory fsync unsupported on this platform")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(decision_trace_module.os, "open", fake_open)
    store = DecisionTraceStore(tmp_path / "trace.jsonl")
    prepared = store.prepare(_trace())
    assert prepared.status is TraceStatus.PREPARED
    assert store.get("trace-1") is not None


def test_prepare_requires_prepared_status(tmp_path) -> None:
    store = DecisionTraceStore(tmp_path / "trace.jsonl")
    trace = _trace()
    trace.status = TraceStatus.SUBMITTED
    with pytest.raises(ValueError, match="must start PREPARED"):
        store.prepare(trace)


def test_prepare_rejects_identity_conflict(tmp_path) -> None:
    store = DecisionTraceStore(tmp_path / "trace.jsonl")
    store.prepare(_trace())
    conflict = _trace()
    conflict.intent_id = "other-intent"
    with pytest.raises(ValueError, match="identity conflict"):
        store.prepare(conflict)


def test_find_by_client_order_id(tmp_path) -> None:
    store = DecisionTraceStore(tmp_path / "trace.jsonl")
    store.prepare(_trace())
    found = store.find_by_client_order_id("client-1")
    assert found is not None
    assert found.trace_id == "trace-1"
    assert store.find_by_client_order_id("missing") is None


def test_update_unknown_trace_raises_keyerror(tmp_path) -> None:
    store = DecisionTraceStore(tmp_path / "trace.jsonl")
    with pytest.raises(KeyError, match="unknown trace_id"):
        store.update("nope", TraceStatus.SUBMITTED)


def test_update_rejects_unknown_fields(tmp_path) -> None:
    store = DecisionTraceStore(tmp_path / "trace.jsonl")
    store.prepare(_trace())
    with pytest.raises(ValueError, match="unknown trace fields"):
        store.update("trace-1", TraceStatus.SUBMITTED, bogus_field="x")
