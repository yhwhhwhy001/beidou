"""Behavior-backed coverage for monitoring fail-closed and cadence edges."""

from __future__ import annotations

import time
from dataclasses import replace

from beidou_observability.monitoring.checks.account import (
    check_account_permissions,
    check_account_unknown,
    check_balance_sanity,
)
from beidou_observability.monitoring.checks.factors import FactorState, check_factors, check_strategies
from beidou_observability.monitoring.checks.monitor_self import check_component_health, check_monitor_loop_health
from beidou_observability.monitoring.checks.reconciliation import (
    ReconciliationResult,
    build_reconciliation_check,
    perform_reconciliation,
)
from beidou_observability.monitoring.contracts import (
    CheckSeverity,
    CheckStatus,
    ComponentHealth,
    FrequencyLevel,
    HealthStatus,
    MonitoringCheckResult,
)
from beidou_observability.monitoring.evidence import (
    EvidenceBundle,
    EvidenceRecord,
    from_check_result,
    verify_evidence_integrity,
)
from beidou_observability.monitoring.frequency_policy import (
    init_frequency_state,
    is_due,
    is_promotion_clean,
    restart_frequency_state,
    update_frequency,
)
from beidou_observability.monitoring.health_aggregator import (
    aggregate_health,
    enforce_inv006,
    enforce_inv007,
    health_summary,
)


def _result(status: CheckStatus, severity: CheckSeverity) -> MonitoringCheckResult:
    return MonitoringCheckResult(check_id=f"{status.value}-{severity.value}", status=status, severity=severity)


def test_account_permissions_fail_closed_matrix() -> None:
    now = time.time()
    assert check_account_permissions(required=False).status is CheckStatus.PASS
    assert check_account_permissions(None).status is CheckStatus.FAIL
    assert check_account_permissions({"ok": False}).status is CheckStatus.FAIL
    assert check_account_permissions({"ok": True, "observed_at": now + 10, "account": {}}).status is CheckStatus.FAIL
    assert check_account_permissions({"ok": True, "observed_at": now - 100, "account": {}}).status is CheckStatus.FAIL
    assert check_account_permissions({"ok": True, "observed_at": now, "account": None}).status is CheckStatus.FAIL
    assert (
        check_account_permissions(
            {"ok": True, "observed_at": now, "account": {"canTrade": "yes", "canWithdraw": False}}
        ).status
        is CheckStatus.FAIL
    )
    assert (
        check_account_permissions(
            {"ok": True, "observed_at": now, "account": {"canTrade": True, "canWithdraw": True}}
        ).status
        is CheckStatus.FAIL
    )
    assert (
        check_account_permissions(
            {"ok": True, "observed_at": now, "account": {"canTrade": False, "canWithdraw": False}}
        ).status
        is CheckStatus.FAIL
    )
    assert (
        check_account_permissions(
            {"ok": True, "observed_at": now, "account": {"canTrade": True, "canWithdraw": False}}
        ).status
        is CheckStatus.PASS
    )
    assert check_account_unknown(None).status is CheckStatus.FAIL
    assert check_account_unknown({"ok": True, "observed_at": now - 100}).status is CheckStatus.FAIL
    assert check_account_unknown({"ok": True, "observed_at": now}).status is CheckStatus.PASS
    assert check_balance_sanity(None).status is CheckStatus.FAIL
    assert check_balance_sanity({"totalWalletBalance": "100"}).status is CheckStatus.PASS


def test_factor_and_strategy_health_edges() -> None:
    now = time.time()
    results = check_factors(
        [
            FactorState("nan", value=float("nan")),
            FactorState("retired", lifecycle="RETIRED", last_evaluation=now),
            FactorState("unknown", last_evaluation=0.0),
            FactorState("stale", last_evaluation=now - 1000),
        ],
        active_strategy_refs={"retired"},
    )
    assert len(results) == 4
    strategy_results = check_strategies(
        [{"strategy_id": "s", "signal_silence_seconds": 3601, "risk_budget_drift_pct": 21}]
    )
    assert len(strategy_results) == 2


def test_reconciliation_orders_positions_and_result_states() -> None:
    missing = perform_reconciliation(None, [], None, None, None)
    assert not missing.all_matched
    assert missing.unknown == 1

    result = perform_reconciliation(
        [{"symbol": "BTCUSDT", "positionAmt": "1"}],
        [{"symbol": "BTCUSDT", "positionAmt": "1.001"}],
        [{"orderId": "venue-only"}],
        [{"order_id": "local-only"}],
        None,
        step_size_map={"BTCUSDT": 0.0001},
    )
    assert result.mismatched == 1
    assert result.unknown == 2
    assert result.unsupported == 1
    assert (
        perform_reconciliation(
            [{"symbol": "BTCUSDT", "positionAmt": "1"}],
            [{"symbol": "BTCUSDT", "positionAmt": "1"}],
            [],
            [],
            [],
        ).matched
        == 1
    )
    assert build_reconciliation_check(result).status is CheckStatus.FAIL
    assert build_reconciliation_check(ReconciliationResult(unknown=1)).status is CheckStatus.FAIL
    assert build_reconciliation_check(ReconciliationResult(matched=1)).status is CheckStatus.PASS
    missing_side = perform_reconciliation(
        [{"symbol": "BTCUSDT", "positionAmt": "1"}],
        [],
        [],
        [],
        [],
    )
    assert missing_side.unknown == 1
    assert perform_reconciliation([], [], [], [], []).matched == 0
    assert build_reconciliation_check(ReconciliationResult(unsupported=1)).status is CheckStatus.FAIL


def test_monitor_self_health_and_aggregates() -> None:
    unhealthy = check_component_health(
        {
            "safe": ComponentHealth(component="safe", healthy=False, safe_action="NO_NEW_RISK"),
            "warn": ComponentHealth(component="warn", healthy=False, safe_action="OBSERVE"),
        }
    )
    assert {item.status for item in unhealthy} == {CheckStatus.FAIL, CheckStatus.WARN}
    assert check_monitor_loop_health(time.monotonic() - 100, max_stall=1).status is CheckStatus.FAIL
    assert check_monitor_loop_health(time.monotonic(), max_stall=100).status is CheckStatus.PASS

    assert aggregate_health([_result(CheckStatus.WARN, CheckSeverity.P2)]) is HealthStatus.YELLOW
    assert aggregate_health([_result(CheckStatus.UNKNOWN, CheckSeverity.P0)]) is HealthStatus.RED
    assert aggregate_health([]) is HealthStatus.GREEN
    assert enforce_inv006(HealthStatus.GREEN, True) is HealthStatus.UNKNOWN
    assert enforce_inv006(HealthStatus.YELLOW, True) is HealthStatus.YELLOW
    assert enforce_inv007(1, 3)[0] is False
    assert enforce_inv007(0, 3) == (True, "")
    summary = health_summary(
        [_result(CheckStatus.FAIL, CheckSeverity.P0), _result(CheckStatus.UNKNOWN, CheckSeverity.P1)]
    )
    assert summary == {"status": "RED", "p0_fails": 1, "p1_fails": 0, "unknowns": 1, "total": 2}


def test_frequency_policy_restart_promotion_and_blockers() -> None:
    fast = init_frequency_state()
    assert restart_frequency_state().last_reason == "restart_or_recovery"
    assert is_due(replace(fast, next_due_at=0.0))
    assert not is_due(replace(fast, next_due_at=time.monotonic() + 1000), tolerance=1)
    assert is_promotion_clean([(_result(CheckStatus.FAIL, CheckSeverity.P1).status, CheckSeverity.P1)])[0] is False
    assert is_promotion_clean([(_result(CheckStatus.WARN, CheckSeverity.P2).status, CheckSeverity.P2)])[0] is False
    assert is_promotion_clean([(_result(CheckStatus.WARN, CheckSeverity.P2).status, CheckSeverity.P2)], {"P2_WARN"})[0]
    assert is_promotion_clean([(_result(CheckStatus.UNKNOWN, CheckSeverity.P0).status, CheckSeverity.P0)])[0] is False

    for kwargs in (
        {"restart_detected": True},
        {"clock_reversal": True},
        {"self_heal_active": True},
        {"p0_failed": True},
        {"p1_failed": True},
    ):
        assert update_frequency(fast, [], **kwargs).level is FrequencyLevel.FAST
    assert (
        update_frequency(fast, [(_result(CheckStatus.FAIL, CheckSeverity.P0).status, CheckSeverity.P0)]).level
        is FrequencyLevel.FAST
    )
    blocked = update_frequency(fast, [(_result(CheckStatus.WARN, CheckSeverity.P2).status, CheckSeverity.P2)])
    assert blocked.level is FrequencyLevel.FAST

    normal = fast
    for _ in range(3):
        normal = update_frequency(normal, [])
    assert normal.level is FrequencyLevel.NORMAL
    stable = normal
    for _ in range(3):
        stable = update_frequency(stable, [])
    assert stable.level is FrequencyLevel.STABLE


def test_evidence_records_bundles_and_integrity() -> None:
    unknown = EvidenceRecord("unknown", CheckStatus.UNKNOWN, CheckSeverity.P1, message="missing")
    p0_fail = EvidenceRecord("p0", CheckStatus.FAIL, CheckSeverity.P0, message="bad")
    passed = EvidenceRecord("pass", CheckStatus.PASS, CheckSeverity.P0, message="ok")
    assert unknown.is_blocking()
    assert p0_fail.is_blocking()
    assert not passed.is_blocking()
    assert unknown.compute_hash()

    bundle = EvidenceBundle("bundle")
    bundle.add(unknown)
    bundle.add(p0_fail)
    bundle.add(passed)
    bundle.finalize()
    assert bundle.bundle_hash
    assert bundle.has_blocking_failure()
    assert bundle.p0_count() == 1
    assert bundle.summary()["total"] == 3
    assert verify_evidence_integrity(unknown)

    result = _result(CheckStatus.PASS, CheckSeverity.P1)
    converted = from_check_result(result)
    assert converted.check_id == result.check_id
    assert converted.evidence_hash
