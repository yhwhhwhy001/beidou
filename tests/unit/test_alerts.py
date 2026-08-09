"""Fail-closed tests for durable alert delivery evidence."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.error import URLError

from beidou_core.alerts import AlertDispatcher
from beidou_observability.telemetry import AlertSeverity


def test_incident_ids_are_unique_within_the_same_second(tmp_path: Path) -> None:
    dispatcher = AlertDispatcher(alerts_file=str(tmp_path / "alerts.jsonl"))
    first = dispatcher.send_incident(AlertSeverity.CRITICAL, "same", "one")
    second = dispatcher.send_incident(AlertSeverity.CRITICAL, "same", "two")

    assert first.incident_id != second.incident_id
    assert dispatcher.get_alert_stats()["total"] == 2


def test_webhook_failure_is_persisted_and_replayable(tmp_path: Path, monkeypatch) -> None:
    delivery_file = tmp_path / "delivery.jsonl"
    dispatcher = AlertDispatcher(
        webhook_url="https://example.invalid/hook",
        alerts_file=str(tmp_path / "alerts.jsonl"),
        delivery_file=str(delivery_file),
    )

    def fail(*_args, **_kwargs):
        raise URLError("offline")

    monkeypatch.setattr("urllib.request.urlopen", fail)
    incident = dispatcher.send_incident(AlertSeverity.CRITICAL, "P0", "delivery test")
    health = dispatcher.get_delivery_health()
    assert health["pending"] == 1
    assert health["failed"] == 1
    assert health["delivered"] == 0
    assert health["unknown"] == 0

    events = [json.loads(line) for line in delivery_file.read_text().splitlines()]
    assert [event["status"] for event in events] == ["PENDING", "FAILED"]
    assert events[-1]["incident_id"] == incident.incident_id

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response())
    # Force the persisted backoff to be due; retry must use the reconstructed
    # incident if the in-memory dispatcher is replaced after a restart.
    restarted = AlertDispatcher(
        webhook_url="https://example.invalid/hook",
        alerts_file=str(tmp_path / "alerts.jsonl"),
        delivery_file=str(delivery_file),
    )
    assert restarted.retry_pending(max_items=1, now=10**12) == 1
    assert restarted.get_delivery_health()["delivered"] == 1


def test_corrupt_delivery_record_is_unknown_not_delivered(tmp_path: Path) -> None:
    delivery_file = tmp_path / "delivery.jsonl"
    delivery_file.write_text('{"status":"DELIVERED"}\nnot-json\n')

    dispatcher = AlertDispatcher(
        webhook_url="https://example.invalid/hook",
        alerts_file=str(tmp_path / "alerts.jsonl"),
        delivery_file=str(delivery_file),
    )

    health = dispatcher.get_delivery_health()
    assert health["unknown"] == 1
    assert health["load_errors"] == 2
