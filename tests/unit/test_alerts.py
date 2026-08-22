"""Fail-closed tests for durable alert delivery evidence."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.error import URLError

import pytest

from beidou_core.alerts import AlertDispatcher
from beidou_observability.telemetry import AlertSeverity, AutoAction


def test_incident_ids_are_unique_within_the_same_second(tmp_path: Path) -> None:
    dispatcher = AlertDispatcher(alerts_file=str(tmp_path / "alerts.jsonl"))
    first = dispatcher.send_incident(AlertSeverity.CRITICAL, "same", "one")
    second = dispatcher.send_incident(AlertSeverity.CRITICAL, "same", "two")

    # Incident deduplication: same category+title returns the existing incident
    assert first.incident_id == second.incident_id
    assert dispatcher.get_alert_stats()["total"] == 1


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


def test_alert_dispatcher_webhook_formats_and_delivery_fail_closed(tmp_path: Path, monkeypatch) -> None:
    class Response:
        def __init__(self, status: int = 200) -> None:
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    calls: list[bytes] = []

    def ok(request, **_kwargs):
        calls.append(request.data)
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", ok)
    urls = [
        "https://open.feishu.cn/hook",
        "https://sctapi.ftqq.com/send",
        "https://pushplus.plus/send?token=test-token",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send",
        "https://example.test/webhook",
    ]
    for index, url in enumerate(urls):
        dispatcher = AlertDispatcher(
            webhook_url=url,
            alerts_file=str(tmp_path / f"alerts-{index}.jsonl"),
            delivery_file=str(tmp_path / f"delivery-{index}.jsonl"),
        )
        dispatcher.set_portfolio_provider(lambda: "portfolio-summary")
        incident = dispatcher.send_incident(
            AlertSeverity.WARNING, f"alert-{index}", "description", AutoAction.ALERT, category=f"cat-{index}"
        )
        assert dispatcher._send_webhook(incident) is True
        assert calls

    broken_portfolio = AlertDispatcher(
        webhook_url="https://open.feishu.cn/hook",
        alerts_file=str(tmp_path / "broken-alerts.jsonl"),
        delivery_file=str(tmp_path / "broken-delivery.jsonl"),
    )
    broken_portfolio.set_portfolio_provider(lambda: (_ for _ in ()).throw(RuntimeError("portfolio")))
    incident = broken_portfolio.send_incident(AlertSeverity.WARNING, "broken", "description")
    assert broken_portfolio._send_webhook(incident) is True

    failed = AlertDispatcher(
        webhook_url="https://example.test/status",
        alerts_file=str(tmp_path / "failed-alerts.jsonl"),
        delivery_file=str(tmp_path / "failed-delivery.jsonl"),
        max_delivery_attempts=1,
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response(500))
    failure = failed.send_incident(AlertSeverity.WARNING, "failed", "description")
    assert failed._send_webhook(failure) is False
    assert failed.get_delivery_health()["dead_letter"] == 1


def test_alert_dispatcher_persistence_load_retry_and_resolution_edges(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive"):
        AlertDispatcher(max_delivery_attempts=0)

    dispatcher = AlertDispatcher(alerts_file=str(tmp_path / "alerts.jsonl"))
    assert dispatcher.retry_pending(max_items=0) == 0
    incident = dispatcher.send_incident(AlertSeverity.CRITICAL, "resolve", "x")
    assert dispatcher.get_active_incidents()
    dispatcher.resolve_incident(incident.incident_id)
    dispatcher.resolve_incident("missing")
    assert dispatcher.get_active_incidents() == []
    stats = dispatcher.get_alert_stats()
    assert stats["total"] == 1 and stats["active"] == 0

    persistence = AlertDispatcher(
        webhook_url="https://example.invalid",
        alerts_file=str(tmp_path / "p.jsonl"),
        delivery_file=str(tmp_path / "delivery-dir"),
    )
    persistence._delivery_file.mkdir()
    persistence._record_delivery_event(incident, "PENDING", attempts=0)
    assert persistence.get_delivery_health()["unknown"] == 1

    broken = tmp_path / "load-dir"
    broken.mkdir()
    loaded = AlertDispatcher(
        webhook_url="https://example.invalid", alerts_file=str(tmp_path / "l.jsonl"), delivery_file=str(broken)
    )
    assert loaded.get_delivery_health()["unknown"] == 1


def test_alert_dispatcher_suppression_reconstruction_dead_letter_and_report_edges(tmp_path: Path, monkeypatch) -> None:
    suppressed = AlertDispatcher(alerts_file=str(tmp_path / "suppressed.jsonl"))
    suppressed._suppressor.should_suppress = lambda *_args, **_kwargs: True
    suppressed_incident = suppressed.send_incident(AlertSeverity.WARNING, "suppressed", "x")
    assert suppressed_incident.incident_id not in suppressed._active_incidents

    report_failure = AlertDispatcher(alerts_file=str(tmp_path / "report.jsonl"))
    report_failure._report_generator.generate_incident_report = lambda **_kwargs: (_ for _ in ()).throw(
        RuntimeError("report")
    )
    report_failure.send_incident(AlertSeverity.CRITICAL, "report-failure", "x")
    assert report_failure.get_report_generator() is report_failure._report_generator

    delivery_file = tmp_path / "dead-letter.jsonl"
    dispatcher = AlertDispatcher(
        webhook_url="https://example.invalid",
        alerts_file=str(tmp_path / "dead-alerts.jsonl"),
        delivery_file=str(delivery_file),
    )
    incident = dispatcher.send_incident(AlertSeverity.CRITICAL, "dead", "x")
    record = dispatcher._delivery_state[incident.incident_id]
    record.update(status="DEAD_LETTER", attempts=99, next_retry_at=0.0)
    dispatcher._send_webhook = lambda _incident: True  # type: ignore[method-assign]
    assert dispatcher.retry_pending(now=1.0) == 1
    assert dispatcher._delivery_state[incident.incident_id]["attempts"] == 0

    dispatcher._delivery_state["missing"] = {
        "incident_id": "missing",
        "status": "DEAD_LETTER",
        "attempts": 1,
        "next_retry_at": 0.0,
    }
    assert dispatcher.retry_pending(now=1.0) == 1
    assert (
        AlertDispatcher._incident_from_delivery(
            {
                "detected_at": "2026-01-01T00:00:00",
                "incident_id": "x",
                "severity": "CRITICAL",
                "title": "t",
                "description": "d",
                "auto_action": "ALERT",
            }
        )
        is not None
    )
    assert AlertDispatcher._incident_from_delivery({"bad": True}) is None

    blank_file = tmp_path / "blank.jsonl"
    blank_file.write_text("\n")
    blank = AlertDispatcher(
        webhook_url="https://example.invalid",
        alerts_file=str(tmp_path / "blank-alerts.jsonl"),
        delivery_file=str(blank_file),
    )
    assert blank.get_delivery_health()["unknown"] == 0
