"""PKG-MON-00A~12 综合测试 — 62 tests covering all monitoring modules。"""

import tempfile
import time
from pathlib import Path

import pytest

from beidou_observability.monitoring.checks.account import (
    check_account_permissions,
    check_account_unknown,
    check_balance_sanity,
)
from beidou_observability.monitoring.checks.execution import OrderTraceState, check_order_trace
from beidou_observability.monitoring.checks.execution import TraceStage as TS
from beidou_observability.monitoring.checks.factors import FactorState, check_factors
from beidou_observability.monitoring.checks.modules import check_algorithm_probe, check_module_progress
from beidou_observability.monitoring.checks.protection import (
    ExchangeEconomicPosition,
    ProtectionFact,
    ProtectionSemanticResult,
    build_protection_check,
    verify_position_protection,
)
from beidou_observability.monitoring.checks.reconciliation import (
    ReconciliationResult,
    build_reconciliation_check,
    perform_reconciliation,
)
from beidou_observability.monitoring.checks.strategies import check_strategy_risk_drift, check_strategy_signal_silence
from beidou_observability.monitoring.clock_integrity import ClockIntegrity
from beidou_observability.monitoring.contracts import *
from beidou_observability.monitoring.evidence import *
from beidou_observability.monitoring.fact_collector import FactCollector, FactDomain
from beidou_observability.monitoring.frequency_policy import *
from beidou_observability.monitoring.health_aggregator import aggregate_health, health_summary
from beidou_observability.monitoring.incident_manager import IncidentManager
from beidou_observability.monitoring.instrumentation import InstrumentationRecorder, TraceEvent
from beidou_observability.monitoring.rate_limit_budget import EndpointBudget
from beidou_observability.monitoring.repository import MonitoringRepository
from beidou_observability.monitoring.retention import RetentionPolicy, RetentionTier
from beidou_observability.monitoring.rollout import RolloutManager
from beidou_observability.monitoring.scheduler import DeepAuditScheduler
from beidou_observability.monitoring.service import MonitoringService
from beidou_observability.monitoring.snapshot_broker import SnapshotBroker, SnapshotSource
from beidou_observability.monitoring.storm_detector import StormDetector
from beidou_observability.monitoring.watchdog import COMPONENT_FAILURE_MATRIX, Watchdog


# PKG-MON-03 Account
class TestAccount:
    def test_none_fails(self):
        assert check_account_unknown(None).status == CheckStatus.FAIL

    def test_stale_fails(self):
        assert (
            check_account_unknown({"ok": True, "observed_at": time.time() - 60}, max_age=45).status == CheckStatus.FAIL
        )

    def test_fresh_passes(self):
        assert check_account_unknown({"ok": True, "observed_at": time.time()}).status == CheckStatus.PASS

    def test_balance_missing(self):
        assert check_balance_sanity({}).status == CheckStatus.FAIL

    def test_balance_ok(self):
        assert check_balance_sanity({"totalWalletBalance": "1000"}).status == CheckStatus.PASS

    def test_withdrawal_permission_fails_closed(self):
        # Testnet 豁免提款检查，用 monkeypatch 清除豁免以验证 fail-closed 逻辑
        import os

        old_env = os.environ.pop("BEIDOU_ENV", None)
        try:
            result = check_account_permissions(
                {
                    "ok": True,
                    "observed_at": time.time(),
                    "account": {"canTrade": True, "canWithdraw": True},
                }
            )
            assert result.status == CheckStatus.FAIL
            assert result.severity == CheckSeverity.P0
            assert "withdrawal" in result.message.lower()
        finally:
            if old_env is not None:
                os.environ["BEIDOU_ENV"] = old_env

    def test_missing_permission_fact_fails_closed(self):
        result = check_account_permissions({"ok": True, "observed_at": time.time(), "account": {"canTrade": True}})
        assert result.status == CheckStatus.FAIL
        assert result.severity == CheckSeverity.P0

    def test_valid_permissions_pass(self):
        result = check_account_permissions(
            {
                "ok": True,
                "observed_at": time.time(),
                "account": {"canTrade": True, "canWithdraw": False},
            }
        )
        assert result.status == CheckStatus.PASS


# PKG-MON-03 Reconciliation
class TestReconciliation:
    def test_matched(self):
        r = perform_reconciliation(
            [{"symbol": "BTC", "positionAmt": "1"}], [{"symbol": "BTC", "positionAmt": "1"}], None, None, None
        )
        assert r.matched >= 1

    def test_mismatch(self):
        r = perform_reconciliation(
            [{"symbol": "BTC", "positionAmt": "1"}], [{"symbol": "BTC", "positionAmt": "2"}], None, None, None
        )
        assert r.mismatched >= 1

    def test_build_critical(self):
        assert build_reconciliation_check(ReconciliationResult(mismatched=2)).status == CheckStatus.FAIL

    def test_build_pass(self):
        assert build_reconciliation_check(ReconciliationResult(matched=5)).status == CheckStatus.PASS

    def test_unsupported_fact_source_fails_closed(self):
        result = build_reconciliation_check(ReconciliationResult(matched=5, unsupported=1))
        assert result.status == CheckStatus.FAIL
        assert result.severity == CheckSeverity.P0

    def test_unknown_fact_fails_closed(self):
        result = build_reconciliation_check(ReconciliationResult(matched=1, unknown=1))
        assert result.status == CheckStatus.FAIL
        assert result.severity == CheckSeverity.P0


# PKG-MON-04 Protection
class TestProtection:
    def test_missing_sl(self):
        r = verify_position_protection(
            ExchangeEconomicPosition(symbol="BTC", position_amt="1"), [], AccountPositionMode.ONE_WAY
        )
        assert r.missing_sl

    def test_protected(self):
        r = verify_position_protection(
            ExchangeEconomicPosition(symbol="BTC", position_amt="1"),
            [
                ProtectionFact(
                    protection_id="p1",
                    position_key="BTC",
                    kind="SL",
                    order_id="algo-1",
                    status="ACTIVE",
                    origin_trace_id="t1",
                    strategy_id="s1",
                    generation=1,
                )
            ],
            AccountPositionMode.ONE_WAY,
        )
        assert not r.missing_sl

    def test_duplicate(self):
        ps = [
            ProtectionFact(
                protection_id="p1",
                position_key="BTC",
                kind="SL",
                order_id="algo-1",
                status="ACTIVE",
                origin_trace_id="t1",
                strategy_id="s1",
                generation=1,
            ),
            ProtectionFact(
                protection_id="p2",
                position_key="BTC",
                kind="SL",
                order_id="algo-2",
                status="ACTIVE",
                origin_trace_id="t1",
                strategy_id="s1",
                generation=1,
            ),
        ]
        r = verify_position_protection(
            ExchangeEconomicPosition(symbol="BTC", position_amt="1"), ps, AccountPositionMode.ONE_WAY
        )
        assert r.duplicate_count >= 1

    def test_ghost(self):
        r = verify_position_protection(
            ExchangeEconomicPosition(symbol="BTC", position_amt="0"),
            [ProtectionFact(protection_id="p1", position_key="BTC", kind="SL")],
            AccountPositionMode.ONE_WAY,
        )
        assert r.ghost_detected

    def test_local_pending_definition_is_not_venue_coverage(self):
        r = verify_position_protection(
            ExchangeEconomicPosition(symbol="BTC", position_amt="1"),
            [
                ProtectionFact(
                    protection_id="local-sl",
                    position_key="BTC",
                    kind="SL",
                    status="CREATED",
                )
            ],
            AccountPositionMode.ONE_WAY,
        )
        assert r.missing_sl

    def test_build_fail(self):
        r = ProtectionSemanticResult(position_key="BTC")
        r.missing_sl = True
        # PKG02 (BDS-P0-001): 所有环境统一使用 FAIL。
        assert build_protection_check(r).status == CheckStatus.FAIL


# PKG-MON-05 Order Trace
class TestOrderTrace:
    def test_stuck(self):
        t = OrderTraceState(
            correlation_id="c1", client_order_id="o1", current_stage=TS.FILLED, stuck_since=time.time() - 60
        )
        checks = check_order_trace([t])
        # PKG02 (BDS-P0-001): 所有环境统一使用 FAIL。
        assert len(checks) >= 1 and checks[0].status == CheckStatus.FAIL

    def test_healthy(self):
        t = OrderTraceState(correlation_id="c1", client_order_id="o1", current_stage=TS.TERMINAL)
        assert all(c.status != CheckStatus.FAIL for c in check_order_trace([t]))

    def test_duplicate_identity_fails_closed(self):
        t = OrderTraceState(
            correlation_id="c1",
            client_order_id="o1",
            current_stage=TS.EXCHANGE_ACKED,
            duplicate_count=1,
        )
        checks = check_order_trace([t])
        # PKG02 (BDS-P0-001): 所有环境统一使用 FAIL 和 P0。
        assert checks and checks[0].status == CheckStatus.FAIL
        assert checks[0].severity.value == "P0"


# PKG-MON-06 Module Progress
class TestModules:
    def test_stale(self):
        c = ModuleProgressContract(
            module_id="m1", criticality=CheckSeverity.P0, progress_timeout_seconds=30, last_progress_at=time.time() - 60
        )
        assert check_module_progress([c])[0].status == CheckStatus.FAIL

    def test_ok(self):
        c = ModuleProgressContract(
            module_id="m1", criticality=CheckSeverity.P0, progress_timeout_seconds=300, last_progress_at=time.time()
        )
        assert check_module_progress([c])[0].status == CheckStatus.PASS

    def test_probe_fail(self):
        assert check_algorithm_probe(None).status == CheckStatus.FAIL

    def test_probe_ok(self):
        assert check_algorithm_probe({"ok": True, "proposal_hash": "abc123"}).status == CheckStatus.PASS


# PKG-MON-07 Factors/Strategies
class TestFactors:
    def test_nan(self):
        assert check_factors([FactorState(factor_id="f1", value=float("nan"))])[0].status == CheckStatus.FAIL

    def test_retired_refd(self):
        assert (
            check_factors([FactorState(factor_id="f1", value=0.5, lifecycle="RETIRED")], active_strategy_refs={"f1"})[
                0
            ].status
            == CheckStatus.FAIL
        )

    def test_signal_silence(self):
        assert (
            check_strategy_signal_silence([{"strategy_id": "s1", "signal_silence_seconds": 7200}])[0].status
            == CheckStatus.WARN
        )

    def test_risk_drift(self):
        assert (
            check_strategy_risk_drift([{"strategy_id": "s1", "risk_budget_drift_pct": 30}])[0].status
            == CheckStatus.WARN
        )


# PKG-MON-08 Frequency
class TestFrequency:
    def test_init_alert(self):
        assert init_frequency_state().level == FrequencyLevel.ALERT

    def test_promotion(self):
        s = init_frequency_state()
        for _ in range(3):
            s = update_frequency(s, [(CheckStatus.PASS, CheckSeverity.P0)])
        assert s.level == FrequencyLevel.NORMAL

    def test_downgrade(self):
        s = init_frequency_state()
        for _ in range(3):
            s = update_frequency(s, [(CheckStatus.PASS, CheckSeverity.P0)])
        assert update_frequency(s, [(CheckStatus.FAIL, CheckSeverity.P0)]).level == FrequencyLevel.ALERT

    def test_inv007(self):
        assert not is_promotion_clean(
            [(CheckStatus.PASS, CheckSeverity.P0)] * 999 + [(CheckStatus.FAIL, CheckSeverity.P0)]
        )[0]


# PKG-MON-08 Clock
class TestClock:
    def test_init(self):
        assert ClockIntegrity().average_drift_ms == 0.0

    def test_check(self):
        assert ClockIntegrity().check().status == CheckStatus.PASS


# PKG-MON-08 Scheduler
class TestScheduler:
    def test_init_alert(self):
        assert DeepAuditScheduler().level == FrequencyLevel.ALERT


# PKG-MON-09 Storm
class TestStorm:
    def test_no_storm(self):
        assert not StormDetector(unique_dedupe_threshold=5).is_storm_active()

    def test_storm(self):
        sd = StormDetector(unique_dedupe_threshold=3)
        for i in range(5):
            sd.record(f"key_{i}")
        assert sd.is_storm_active()

    def test_build(self):
        sd = StormDetector(unique_dedupe_threshold=3)
        for i in range(5):
            sd.record(f"key_{i}")
        info = sd.record("key_5")
        assert info and info["is_storm"]
        assert sd.build_storm_incident(info, "s1").is_systemic


# PKG-MON-09 Incident
class TestIncident:
    def test_dedupe(self):
        im = IncidentManager()
        _i1, n1 = im.create_or_dedupe("k1")
        assert n1
        _i2, n2 = im.create_or_dedupe("k1")
        assert not n2

    def test_fsm_valid(self):
        im = IncidentManager()
        inc, _ = im.create_or_dedupe("k1")
        assert im.transition(inc.incident_id, IncidentStatus.CONFIRMED) is not None

    def test_fsm_invalid(self):
        im = IncidentManager()
        inc, _ = im.create_or_dedupe("k1")
        assert im.transition(inc.incident_id, IncidentStatus.MITIGATING) is None

    def test_remediation_auto(self):
        im = IncidentManager()
        ok, k = im.is_remediation_allowed("FEED_RECONNECT")
        assert ok and k == "AUTO"

    def test_remediation_never(self):
        im = IncidentManager()
        ok, _k = im.is_remediation_allowed("CHANGE_POSITION_MODE")
        assert not ok

    def test_escalate(self):
        im = IncidentManager(storm_detector=StormDetector(unique_dedupe_threshold=100))
        im.create_or_dedupe("p0", severity=CheckSeverity.P0)
        assert len(im.escalate_to_locked()) >= 1


# PKG-MON-10 Watchdog
class TestWatchdog:
    def test_ok(self):
        w = Watchdog()
        w.heartbeat()
        assert w.check().status == CheckStatus.PASS

    def test_stall(self):
        w = Watchdog(heartbeat_interval=0.1, max_missed_heartbeats=2)
        w.last_heartbeat = time.monotonic() - 10
        assert w.check().status == CheckStatus.FAIL

    def test_matrix(self):
        assert len(COMPONENT_FAILURE_MATRIX) == 8


# PKG-MON-10 Health Aggregator
class TestHealth:
    def test_aggregate_red(self):
        r = [MonitoringCheckResult(check_id="t", status=CheckStatus.FAIL, severity=CheckSeverity.P0)]
        assert aggregate_health(r) == HealthStatus.RED

    def test_aggregate_green(self):
        r = [MonitoringCheckResult(check_id="t", status=CheckStatus.PASS, severity=CheckSeverity.P0)]
        assert aggregate_health(r) == HealthStatus.GREEN

    def test_summary(self):
        s = health_summary([MonitoringCheckResult(check_id="t", status=CheckStatus.FAIL, severity=CheckSeverity.P0)])
        assert s["p0_fails"] == 1


# PKG-MON-10 MonitoringService
class TestService:
    def test_status(self):
        svc = MonitoringService()
        svc.results = [MonitoringCheckResult(check_id="t", status=CheckStatus.PASS, severity=CheckSeverity.P0)]
        s = svc.status()
        assert s["health"] == "GREEN"

    def test_mode_contract(self):
        assert "BOTH" in MonitoringService().get_mode_contract()["ONE_WAY"]


# PKG-MON-10 Rollout
class TestRollout:
    def test_can_disable(self, repo):
        repo.upsert_rollout_state(RolloutState(check_id="c1", locked_safety_check=True))
        assert not RolloutManager(repo=repo).can_disable("c1", "production")[0]


# PKG-MON-12 Retention
class TestRetention:
    def test_tiers(self):
        p = RetentionPolicy()
        assert p.retention_seconds(RetentionTier.FAST_GUARD) == 7 * 86400
        assert p.retention_seconds(RetentionTier.CERTIFICATION) == float("inf")

    def test_open_incident_pin(self):
        assert RetentionPolicy().should_retain(RetentionTier.FAST_GUARD, 8 * 86400, has_open_incident=True)

    def test_expired(self):
        assert not RetentionPolicy().should_retain(RetentionTier.FAST_GUARD, 8 * 86400)


# PKG-MON-02 Budget/Snapshot/Fact
class TestBudget:
    def test_exhaustion(self):
        b = EndpointBudget(endpoint="/t", monitor_budget=5)
        b.record_usage(5)
        assert b.is_exhausted

    def test_execution_reserve(self):
        b = EndpointBudget(endpoint="/t", monitor_budget=50, reserved_execution_budget=200)
        b.record_usage(50)
        assert b.execution_reserve_remaining > 0


class TestSnapshot:
    def test_feed_get(self):
        b = SnapshotBroker()
        b.feed_supervisor_snapshot("/t", {"ok": True})
        assert b.get_snapshot("/t").source == SnapshotSource.SUPERVISOR_FRESH

    def test_stale(self):
        b = SnapshotBroker()
        b.feed_supervisor_snapshot("/t", {"ok": True}, observed_at=time.monotonic() - 30, ttl_seconds=5)
        assert b.get_snapshot("/t").source == SnapshotSource.STALE


class TestFactCollectorSvc:
    def test_authoritative(self):
        c = FactCollector()
        f = c.record_authoritative(FactDomain.EXCHANGE, "pos", {"qty": "1"})
        assert f.is_authoritative

    def test_error(self):
        c = FactCollector()
        f = c.record_error(FactDomain.EXCHANGE, "a", "timeout")
        assert not f.is_valid

    def test_validate(self):
        c = FactCollector()
        c.record_authoritative(FactDomain.EXCHANGE, "a", {"ok": True})
        ok, _ = c.validate_no_critical_unknown()
        assert ok


# PKG-MON-05A Instrumentation
class TestInstrumentation:
    def test_record(self):
        rec = InstrumentationRecorder()
        e = TraceEvent(
            trace_id="t1",
            correlation_id="c1",
            strategy_id="s1",
            intent_id="i1",
            client_order_id="o1",
            stage=TS.OUTBOX_ENQUEUED,
            source_timestamp=time.time(),
        )
        assert rec.record(e) and len(rec.get_by_correlation("c1")) == 1


# Repository fixture
@pytest.fixture
def repo():
    with tempfile.TemporaryDirectory() as tmp:
        MonitoringRepository._instance = None
        r = MonitoringRepository(str(Path(tmp) / "test.db"))
        yield r
        MonitoringRepository._instance = None


class TestRepo:
    def test_schema(self, repo):
        repo._init_schema()
        assert repo.get_frequency_state().level == FrequencyLevel.ALERT

    def test_frequency(self, repo):
        assert repo.get_frequency_state().level == FrequencyLevel.ALERT

    def test_evidence(self, repo):
        repo.write_check_evidence(
            MonitoringCheckResult(check_id="t", status=CheckStatus.PASS, severity=CheckSeverity.P0, message="ok")
        )
        assert repo._get_conn().execute("SELECT COUNT(*) FROM monitor_check_evidence").fetchone()[0] == 1

    def test_health(self, repo):
        assert repo.health_probe().healthy

    def test_incident(self, repo):
        repo.write_incident(Incident(incident_id="i1", dedupe_key="k1", detected_at=time.time()))
        assert repo.find_incident_by_dedupe_key("k1") is not None
