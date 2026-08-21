"""Coverage for monitoring state, persistence, and budget boundaries."""

from __future__ import annotations

import time

from beidou_observability.monitoring.contracts import (
    CheckSeverity,
    CheckStatus,
    FrequencyLevel,
    Incident,
    IncidentLink,
    IncidentStatus,
    MonitoringCheckResult,
    RolloutMode,
    RolloutState,
)
from beidou_observability.monitoring.fact_collector import FactCollector, FactDomain
from beidou_observability.monitoring.rate_limit_budget import (
    BudgetExhaustionPolicy,
    EndpointBudget,
    RateLimitBudget,
)
from beidou_observability.monitoring.repository import MonitoringRepository
from beidou_observability.monitoring.rollout import RolloutManager
from beidou_observability.monitoring.snapshot_broker import SnapshotBroker, SnapshotSource


def test_fact_collector_preserves_precedence_unknowns_and_runtime_summary() -> None:
    collector = FactCollector()
    assert not collector.get_required("missing").is_valid
    assert not collector.has_fresh("missing")
    assert collector.record_derived("derived", 1).is_valid
    collector.record_authoritative(FactDomain.ENGINE, "position", {"qty": 1})
    collector.record_authoritative(FactDomain.EXCHANGE, "position", {"qty": 2})
    assert collector.get_authoritative("position") is not None
    assert len(collector.conflicts()) == 1
    error = collector.record_error(FactDomain.ACCOUNT, "balance", "timeout")
    assert not error.is_valid
    status = collector.inspect_runtime_status()
    assert status["conflicts"] == 1
    assert "balance" in status["critical_unknowns"]
    assert collector.validate_no_critical_unknown() == (False, ["balance"])


def test_snapshot_broker_covers_ttl_precedence_and_stale_conflict() -> None:
    broker = SnapshotBroker()
    assert broker.get_snapshot("unknown").source is SnapshotSource.UNKNOWN
    assert broker._ttl_for("account") == broker.account_ttl
    assert broker._ttl_for("openOrders") == broker.algo_ttl
    assert broker._ttl_for("positionSide/dual") == broker.position_mode_ttl
    assert broker._ttl_for("exchangeInfo") == 15.0
    fact = broker.feed_supervisor_snapshot("account", {"ok": True}, account_scope="a")
    assert fact.source is SnapshotSource.SUPERVISOR_FRESH
    assert broker.get_snapshot("account", account_scope="a").is_fresh
    broker.feed_supervisor_snapshot("stale", {"ok": True}, observed_at=time.monotonic() - 20, ttl_seconds=5)
    stale = broker.get_snapshot("stale")
    assert stale.source is SnapshotSource.STALE and stale.is_stale
    conflict = broker.detect_source_conflict("stale")
    assert conflict is not None and conflict["issue"] == "stale"
    assert broker.detect_source_conflict("account", "a") is None
    assert broker.all_snapshots_summary()["account:a"]["is_fresh"]
    broker.invalidate("account", "a")
    assert broker.get_snapshot("account", account_scope="a").source is SnapshotSource.UNKNOWN


def test_rate_limit_budget_fail_closed_policies_and_window_reset() -> None:
    budget = EndpointBudget(
        endpoint="/order",
        monitor_budget=10,
        reserved_execution_budget=5,
        window_start=time.monotonic() - 100,
        window_duration=10,
    )
    budget.record_usage(2, headers={"X-MBX-USED-WEIGHT-1M": "3"})
    assert budget.observed_usage == 3 and budget.monitor_used == 2
    budget.record_usage(1, headers={"X-MBX-USED-WEIGHT-1M": "bad"})
    assert budget.observed_usage == 4
    assert budget.execution_reserve_remaining > 0
    budget.handle_rate_limit(retry_after=0.01)
    assert budget.is_in_backoff
    assert budget.try_consume(1, is_critical=True) == (False, BudgetExhaustionPolicy.UNKNOWN)

    budget.backoff_until = time.monotonic() - 1
    budget.monitor_used = 10
    assert budget.try_consume(1, is_critical=False) == (False, BudgetExhaustionPolicy.DEFER)
    budget.monitor_used = 0
    budget.observed_usage = 20
    assert budget.try_consume(1, is_critical=True) == (False, BudgetExhaustionPolicy.BLOCK)
    budget.observed_usage = 0
    assert budget.try_consume(1, is_critical=False) == (True, BudgetExhaustionPolicy.DEFER)

    limits = RateLimitBudget()
    registered = limits.register("/time", method="GET", weight_limit=100, reserved=20, monitor_budget=30)
    assert limits.get_budget("/time") is registered
    assert limits.summary()["GET:/time"]["endpoint"] == "/time"
    assert limits.is_execution_reserve_intact()
    registered.observed_usage = 100
    assert not limits.is_execution_reserve_intact()


def test_monitoring_repository_roundtrips_state_incidents_rollouts_and_evidence(tmp_path) -> None:
    repo = MonitoringRepository(str(tmp_path / "monitor.db"))
    state = repo.get_frequency_state()
    state.level = FrequencyLevel.STABLE
    state.last_reason = "healthy"
    repo.save_frequency_state(state)
    assert repo.get_frequency_state().level is FrequencyLevel.STABLE

    check = MonitoringCheckResult(
        check_id="check-1",
        entity_type="account",
        entity_id="a",
        status=CheckStatus.PASS,
        severity=CheckSeverity.P1,
        message="ok",
    )
    repo.write_check_evidence(check)
    assert repo.get_recent_evidence("check-1", limit=1)[0]["check_id"] == "check-1"
    assert repo.get_recent_evidence(limit=1)

    incident = Incident(
        incident_id="inc-1",
        dedupe_key="same",
        status=IncidentStatus.CONFIRMED,
        severity=CheckSeverity.P0,
        title="blocked",
        detected_at=time.time(),
        evidence_hashes=["hash"],
        is_systemic=True,
    )
    repo.write_incident(incident)
    assert repo.find_incident_by_dedupe_key("same") is not None
    assert repo.get_active_incidents()[0].is_systemic
    repo.write_incident_link(IncidentLink("inc-1", "inc-2", linked_at=time.time()))
    repo.write_incident(
        Incident("inc-1", "same", status=IncidentStatus.RESOLVED, severity=CheckSeverity.P0, detected_at=time.time())
    )
    assert repo.find_incident_by_dedupe_key("same") is None

    rollout = RolloutState("check-locked", mode=RolloutMode.OBSERVE, locked_safety_check=True)
    repo.upsert_rollout_state(rollout)
    assert repo.get_rollout_state("check-locked").mode is RolloutMode.OBSERVE
    assert repo.can_disable_check("check-locked", "production")[0] is False
    assert repo.can_disable_check("check-locked", "paper")[0] is True
    assert repo.get_rollout_state("missing") is None

    manager = RolloutManager(repo=repo)
    assert manager.register("check-new")
    assert manager.is_enforce("check-new")
    assert manager.should_execute_control_action("check-new")
    assert manager.transition_to_enforce("check-new")
    assert not manager.transition_to_enforce("missing")
    assert manager.is_enforce("missing")
    repo.close()


def test_monitoring_repository_health_probe_fails_closed_when_storage_is_unavailable(monkeypatch, tmp_path) -> None:
    repo = MonitoringRepository(str(tmp_path / "monitor.db"))

    def unavailable():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(repo, "_get_conn", unavailable)
    health = repo.health_probe()
    assert not health.healthy
    assert health.safe_action == "NO_NEW_RISK"
    repo.close()
