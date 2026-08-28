"""Coverage gap tests for beidou_observability.monitoring (bridge module).

Targets the defensive branches the existing monitoring bridge suite does not
exercise: ``_performance_timestamp`` string/int/naive-datetime normalization,
``_strategy_snapshots`` typed-graph and component-registry fallback branches,
and the ``convert`` helper's broken-evidence-hash exception path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from beidou_observability.monitoring import (
    _performance_timestamp,
    _strategy_snapshots,
    collect_monitoring_checks,
)


def test_performance_timestamp_normalizes_naive_datetime() -> None:
    expected = datetime(2026, 8, 22, 10, 30, tzinfo=timezone.utc).timestamp()
    assert _performance_timestamp(datetime(2026, 8, 22, 10, 30)) == expected  # noqa: DTZ001 - naive is intentional


def test_performance_timestamp_accepts_numeric_values() -> None:
    assert _performance_timestamp(1234.5) == 1234.5
    assert _performance_timestamp(5) == 5.0


def test_performance_timestamp_empty_string_is_zero() -> None:
    assert _performance_timestamp("") == 0.0
    assert _performance_timestamp("   ") == 0.0


def test_performance_timestamp_parses_float_string() -> None:
    assert _performance_timestamp("3.5") == 3.5


def test_performance_timestamp_rejects_unparseable_string() -> None:
    assert _performance_timestamp("not-a-timestamp") == 0.0


def test_performance_timestamp_parses_naive_iso_string() -> None:
    assert _performance_timestamp("2026-08-22") == datetime(2026, 8, 22, tzinfo=timezone.utc).timestamp()


def test_performance_timestamp_rejects_unknown_type() -> None:
    assert _performance_timestamp(None) == 0.0


def test_strategy_snapshot_typed_graph_skips_unregistered_and_counts_unauthorized() -> None:
    known_unauth = SimpleNamespace(has_authorized_active_evidence=lambda: False)
    unknown_auth = SimpleNamespace(has_authorized_active_evidence=lambda: True)
    engine = SimpleNamespace(
        _autopilot_strategy_id="autopilot",
        _last_nearline=0.0,
        _factor_registry=SimpleNamespace(_factors={"f_known": known_unauth, "f_unknown": unknown_auth}),
        _typed_graph=SimpleNamespace(_nodes={"f_known": object(), "f_unknown": object()}),
        _factor_component_registry={"f_known": object()},
    )
    snapshot = _strategy_snapshots(engine)
    assert snapshot[0]["stale_factor_count"] == 1


def test_strategy_snapshot_component_registry_fallback_branches() -> None:
    active_unauth = SimpleNamespace(
        lifecycle=SimpleNamespace(value="ACTIVE"), has_authorized_active_evidence=lambda: False
    )
    idea = SimpleNamespace(lifecycle=SimpleNamespace(value="IDEA"), has_authorized_active_evidence=lambda: False)
    active_auth = SimpleNamespace(
        lifecycle=SimpleNamespace(value="ACTIVE"), has_authorized_active_evidence=lambda: True
    )
    not_registered = SimpleNamespace(
        lifecycle=SimpleNamespace(value="ACTIVE"), has_authorized_active_evidence=lambda: False
    )
    engine = SimpleNamespace(
        _autopilot_strategy_id="autopilot",
        _last_nearline=0.0,
        _factor_registry=SimpleNamespace(
            _factors={
                "f1": active_unauth,
                "f2": idea,
                "f3": active_auth,
                "f4": not_registered,
            }
        ),
        _typed_graph=None,
        _factor_component_registry={"f1": object(), "f2": object(), "f3": object()},
    )
    snapshot = _strategy_snapshots(engine)
    assert snapshot[0]["stale_factor_count"] == 1


def test_convert_tolerates_broken_evidence_hash_computation(monkeypatch) -> None:
    import beidou_observability.monitoring.checks.account as account_checks
    from beidou_launcher.models import CheckSeverity, CheckStatus

    class _FakeResult:
        check_id = "fake.check"
        entity_type = "account"
        entity_id = "x"
        status = CheckStatus.UNKNOWN
        severity = CheckSeverity.P2
        message = "msg"
        observed_at = 0.0
        fact_age_ms = None
        source = "test"
        evidence_hash = ""
        correlation_id = ""
        remediation = ""
        policy_version = "1.1"

        def compute_evidence_hash(self):
            raise ValueError("broken evidence hash")

    monkeypatch.setattr(account_checks, "check_account_unknown", lambda _snapshot: _FakeResult())
    results = collect_monitoring_checks(engine=None)
    assert results
    assert results[0].evidence["evidence_hash"] == ""
