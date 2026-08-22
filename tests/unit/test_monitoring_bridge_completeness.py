"""Coverage for the monitoring-to-supervisor bridge.

The fixtures are local fact objects.  No exchange client or write authority is
created; the tests assert that missing and malformed operational facts remain
visible as blocking results.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import ClassVar

from beidou_observability.monitoring import (
    _factor_state_snapshots,
    _map_trace_stage,
    _protection_order_fact,
    _strategy_snapshots,
    collect_monitoring_checks,
)
from beidou_observability.monitoring.contracts import (
    AccountPositionMode,
    CheckSeverity,
    CheckStatus,
    ComponentHealth,
    MonitoringCheckResult,
    PositionModeEvidence,
    TraceStage,
)
from beidou_observability.monitoring.fact_bus import FactDomain, OperationalFact, get_fact_bus, reset_fact_bus


def test_monitoring_bridge_helpers_preserve_unknown_and_audit_identity() -> None:
    assert _map_trace_stage("NEW") is TraceStage.INTENT_CREATED
    assert _map_trace_stage("PARTIALLY_FILLED") is TraceStage.EXCHANGE_ACKED
    assert _map_trace_stage("FILLED") is TraceStage.FILLED
    assert _map_trace_stage("CANCELED") is TraceStage.TERMINAL
    assert _map_trace_stage("new-venue-status") is TraceStage.UNKNOWN

    sl = SimpleNamespace(
        protection_id="sl-1",
        exchange_order_id=None,
        side=SimpleNamespace(value="SELL"),
        quantity="1",
        trigger_price="90",
        status=SimpleNamespace(value="PENDING"),
    )
    fact = _protection_order_fact(sl, kind="SL", symbol="BTCUSDT")
    assert fact.protection_id == "sl-1"
    assert fact.order_id == ""
    assert fact.position_key == "BTCUSDT"

    good_record = SimpleNamespace(
        lifecycle=SimpleNamespace(value="ACTIVE"),
        performance=[SimpleNamespace(ic_mean=0.12)],
    )
    bad_record = SimpleNamespace(performance=[SimpleNamespace(ic_mean="bad")])
    states = _factor_state_snapshots(SimpleNamespace(_factors={"f1": good_record, "f2": bad_record}))
    assert len(states) == 1 and states[0].factor_id == "f1"
    assert _factor_state_snapshots(SimpleNamespace(_factors=[])) == []


def test_factor_snapshot_uses_live_performance_timestamp() -> None:
    observed_at = datetime(2026, 8, 22, 10, 30, tzinfo=timezone.utc)
    record = SimpleNamespace(
        lifecycle=SimpleNamespace(value="ACTIVE"),
        performance=[SimpleNamespace(ic_mean=0.12, timestamp=observed_at)],
    )

    states = _factor_state_snapshots(SimpleNamespace(_factors={"f1": record}))

    assert states[0].last_evaluation == observed_at.timestamp()


def test_strategy_snapshot_excludes_research_inventory_from_stale_dependencies() -> None:
    active = SimpleNamespace(has_authorized_active_evidence=lambda: True)
    idea = SimpleNamespace(has_authorized_active_evidence=lambda: False)
    engine = SimpleNamespace(
        _factor_registry=SimpleNamespace(_factors={"active": active, "idea": idea}),
        _typed_graph=SimpleNamespace(_nodes={"active": object()}),
    )

    assert _strategy_snapshots(engine)[0]["stale_factor_count"] == 0


def test_strategy_snapshot_handles_risk_and_factor_evidence_fail_closed() -> None:
    budget = SimpleNamespace(max_drawdown_pct=2.0)
    state = SimpleNamespace(current_drawdown_pct=5.0)
    risk = SimpleNamespace(get_budget=lambda _sid: budget, get_state=lambda _sid: state)
    factor = SimpleNamespace(has_authorized_active_evidence=lambda: False)
    engine = SimpleNamespace(
        _autopilot_strategy_id="alpha",
        _last_nearline=time.time() - 3,
        _strategy_risk=risk,
        _factor_registry=SimpleNamespace(_factors={"f1": factor}),
    )
    snapshot = _strategy_snapshots(engine)
    assert snapshot[0]["strategy_id"] == "alpha"
    assert snapshot[0]["risk_budget_drift_pct"] == 3.0
    assert snapshot[0]["stale_factor_count"] == 1

    broken = SimpleNamespace(
        _autopilot_strategy_id="alpha",
        _last_nearline=0,
        _strategy_risk=SimpleNamespace(
            get_budget=lambda _sid: (_ for _ in ()).throw(RuntimeError("risk unavailable")),
            get_state=lambda _sid: None,
        ),
        _factor_registry=SimpleNamespace(_factors={"bad": SimpleNamespace(has_authorized_active_evidence=None)}),
    )
    assert _strategy_snapshots(broken)[0]["risk_budget_drift_pct"] == 0.0
    assert _strategy_snapshots(None) == []


def test_collect_monitoring_checks_without_engine_returns_account_facts() -> None:
    reset_fact_bus()
    results = collect_monitoring_checks(engine=None, exchange_account_snapshot=None, algorithm_probe=None)
    ids = {item.check_id for item in results}
    assert ids == {
        "runtime.safety.account",
        "runtime.safety.balance_sanity",
        "runtime.safety.account_permissions",
        "runtime.health.algorithm_probe",
    }
    statuses = {item.check_id: item.status.value for item in results}
    assert statuses["runtime.safety.account"] == CheckStatus.FAIL.value
    assert statuses["runtime.safety.balance_sanity"] == CheckStatus.FAIL.value
    assert statuses["runtime.safety.account_permissions"] == CheckStatus.PASS.value


def test_collect_monitoring_checks_covers_progress_orders_self_health_and_chaos() -> None:
    reset_fact_bus()
    now = time.time()

    class _Tracker:
        status: ClassVar[SimpleNamespace] = SimpleNamespace(value="NEW")
        correlation_id: ClassVar[str] = "corr-1"
        events: ClassVar[list[object]] = [("created", SimpleNamespace(timestamp=lambda: now - 1000))]

        def is_terminal(self) -> bool:
            return False

    chaos_calls: list[str] = []
    engine = SimpleNamespace(
        _can_write=False,
        _env_mode=SimpleNamespace(value="paper"),
        _last_realtime=now,
        _last_nearline=now,
        _last_offline=now,
        _last_recon=now,
        _last_account={"totalWalletBalance": "100", "positions": []},
        _protection=SimpleNamespace(all_positions=lambda: {}),
        _ledger=SimpleNamespace(_transactions=[]),
        _order_trackers={"order-1": _Tracker()},
        _active_order_ids={"order-1"},
        _order_symbols={"order-1": "BTCUSDT"},
        _factor_registry=SimpleNamespace(_factors={}),
        _strategy_risk=None,
        _chaos_engine=SimpleNamespace(run_chaos_cycle=lambda: chaos_calls.append("cycle")),
    )
    supervisor = SimpleNamespace(
        _monitoring_component_health=[
            ComponentHealth(component="fact-bus", healthy=False, last_error="down", consecutive_failures=2)
        ]
    )
    results = collect_monitoring_checks(
        engine=engine,
        supervisor=supervisor,
        exchange_account_snapshot={"ok": True, "account": engine._last_account, "observed_at": now},
        algorithm_probe={"ok": True},
        position_mode_evidence=PositionModeEvidence(
            account_id="test",
            venue="BINANCE",
            mode=AccountPositionMode.ONE_WAY,
            observed_at=now,
        ),
        last_loop_at=now,
        monitor_stall_threshold=30,
    )
    ids = {item.check_id for item in results}
    assert "runtime.execution.order_trace" in ids
    assert "runtime.monitoring.self_health" in ids or "runtime.monitoring.component_health" in ids
    assert chaos_calls == ["cycle"]


def test_collect_monitoring_checks_factbus_and_writable_authority_paths() -> None:
    reset_fact_bus()
    now = time.time()
    base = SimpleNamespace(
        _can_write=True,
        _env_mode=SimpleNamespace(value="paper"),
        _last_account={"totalWalletBalance": "100", "positions": []},
        _protection=SimpleNamespace(all_positions=lambda: {}),
        _ledger=SimpleNamespace(_transactions=[]),
        _factor_registry=None,
        _last_realtime=now,
        _last_nearline=now,
        _last_offline=now,
        _last_recon=now,
        _last_reconciliation_result=None,
    )
    failed = collect_monitoring_checks(
        engine=base,
        exchange_account_snapshot={"ok": True, "account": base._last_account, "observed_at": now},
        algorithm_probe={"ok": True},
    )
    recon = next(item for item in failed if item.check_id == "runtime.safety.reconciliation")
    assert recon.status.value == CheckStatus.FAIL.value

    base._last_reconciliation_result = SimpleNamespace(
        matched=True,
        status=SimpleNamespace(value="MATCHED"),
        checked_at=datetime.fromtimestamp(now, tz=timezone.utc),
    )
    passed = collect_monitoring_checks(
        engine=base,
        exchange_account_snapshot={"ok": True, "account": base._last_account, "observed_at": now},
        algorithm_probe={"ok": True},
    )
    recon = next(item for item in passed if item.check_id == "runtime.safety.reconciliation")
    assert recon.status.value == CheckStatus.PASS.value


def test_collect_monitoring_checks_exercises_positions_orders_factors_and_factbus() -> None:
    reset_fact_bus()
    now = time.time()

    class _Position:
        instrument_id = "BTCUSDT"
        quantity = 1.0
        side = SimpleNamespace(value="BUY")
        stop_loss = SimpleNamespace(
            protection_id="sl-1",
            exchange_order_id="algo-sl",
            side=SimpleNamespace(value="SELL"),
            quantity="1",
            trigger_price="90",
            status=SimpleNamespace(value="ACTIVE"),
        )
        take_profits: ClassVar[list[SimpleNamespace]] = [
            SimpleNamespace(
                protection_id="tp-1",
                exchange_order_id="algo-tp",
                side=SimpleNamespace(value="SELL"),
                quantity="1",
                trigger_price="110",
                status=SimpleNamespace(value="ACTIVE"),
            )
        ]

    class _Tracker:
        status: ClassVar[SimpleNamespace] = SimpleNamespace(value="PENDING_CANCEL")
        correlation_id: ClassVar[str] = "corr-1"
        events: ClassVar[list[object]] = [("created", datetime.fromtimestamp(now - 1000, tz=timezone.utc))]

        def is_terminal(self) -> bool:
            return False

    record = SimpleNamespace(
        lifecycle=SimpleNamespace(value="ACTIVE"),
        performance=[SimpleNamespace(ic_mean=0.1)],
        has_authorized_active_evidence=lambda: True,
    )
    risk = SimpleNamespace(
        get_budget=lambda _sid: SimpleNamespace(max_drawdown_pct=2.0),
        get_state=lambda _sid: SimpleNamespace(current_drawdown_pct=2.0),
    )
    engine = SimpleNamespace(
        _can_write=False,
        _env_mode=SimpleNamespace(value="paper"),
        _last_realtime=now,
        _last_nearline=now,
        _last_offline=now,
        _last_recon=now,
        _last_account={
            "totalWalletBalance": "100",
            "positions": [
                {"symbol": "BTCUSDT", "positionAmt": "1", "entryPrice": "100", "positionSide": "BOTH"},
                {"symbol": "ETHUSDT", "positionAmt": "bad"},
                {"symbol": "XRPUSDT", "positionAmt": "0"},
            ],
        },
        _protection=SimpleNamespace(all_positions=lambda: {"BTCUSDT": _Position()}),
        _ledger=SimpleNamespace(_transactions=[]),
        _order_trackers={"order-1": _Tracker(), "terminal": SimpleNamespace(is_terminal=lambda: True)},
        _active_order_ids={"order-1", "terminal"},
        _order_symbols={"order-1": "BTCUSDT"},
        _factor_registry=SimpleNamespace(_factors={"factor-1": record}),
        _strategy_risk=risk,
        _autopilot_strategy_id="alpha",
        _chaos_engine=SimpleNamespace(run_chaos_cycle=lambda: None),
    )
    get_fact_bus().publish(
        OperationalFact(
            "reconciliation_result",
            FactDomain.LEDGER,
            {"matched": False, "status": "MISMATCH"},
            timestamp=now - 1000,
        )
    )
    failed = collect_monitoring_checks(
        engine=engine,
        exchange_account_snapshot={"ok": True, "account": engine._last_account, "observed_at": now},
        algorithm_probe={"ok": True},
        position_mode_evidence=PositionModeEvidence("test", "BINANCE", AccountPositionMode.ONE_WAY),
        last_loop_at=now,
    )
    assert any(
        item.check_id == "runtime.safety.reconciliation" and item.status.value == CheckStatus.FAIL.value
        for item in failed
    )
    assert any(item.check_id == "runtime.execution.order_trace" for item in failed)
    assert any(item.check_id == "runtime.health.factor_lifecycle" for item in failed)

    reset_fact_bus()
    get_fact_bus().publish(
        OperationalFact(
            "reconciliation_result",
            FactDomain.LEDGER,
            {"matched": True, "status": "MATCHED"},
            timestamp=time.time(),
        )
    )
    passed = collect_monitoring_checks(
        engine=engine,
        exchange_account_snapshot={"ok": True, "account": engine._last_account, "observed_at": now},
        algorithm_probe={"ok": True},
        position_mode_evidence=PositionModeEvidence("test", "BINANCE", AccountPositionMode.ONE_WAY),
        last_loop_at=now,
    )
    assert any(item.check_id == "runtime.safety.protection_coverage" for item in passed)
    assert any(item.check_id == "runtime.safety.reconciliation" for item in passed)


def test_collect_monitoring_checks_converts_component_failures_to_blocking_facts() -> None:
    reset_fact_bus()
    now = time.time()
    engine = SimpleNamespace(
        _can_write=False,
        _env_mode=SimpleNamespace(value="paper"),
        _last_account={"positions": []},
        _protection=SimpleNamespace(
            all_positions=lambda: (_ for _ in ()).throw(RuntimeError("protection unavailable"))
        ),
        _ledger=SimpleNamespace(_transactions=[]),
        _factor_registry=None,
        _last_realtime=now,
        _last_nearline=now,
        _last_offline=now,
        _last_recon=now,
        _chaos_engine=SimpleNamespace(run_chaos_cycle=lambda: (_ for _ in ()).throw(RuntimeError("chaos unavailable"))),
    )
    results = collect_monitoring_checks(
        engine=engine,
        exchange_account_snapshot={"ok": True, "account": engine._last_account},
        algorithm_probe={"ok": True},
    )
    assert any(
        item.check_id == "runtime.chaos.health" and item.status.value == CheckStatus.FAIL.value for item in results
    )


def test_monitoring_bridge_covers_malformed_optional_sources(monkeypatch) -> None:
    import beidou_observability.monitoring as monitoring
    import beidou_observability.monitoring.checks.execution as execution_checks
    import beidou_observability.monitoring.checks.factors as factor_checks
    import beidou_observability.monitoring.checks.modules as module_checks
    import beidou_observability.monitoring.checks.monitor_self as monitor_self_checks
    import beidou_observability.monitoring.checks.strategies as strategy_checks

    class _BrokenRecords(dict):
        def values(self):
            raise RuntimeError("factor records unavailable")

    broken_engine = SimpleNamespace(
        _autopilot_strategy_id="alpha",
        _last_nearline=0.0,
        _strategy_risk=SimpleNamespace(
            get_budget=lambda _sid: (_ for _ in ()).throw(RuntimeError("risk unavailable")),
            get_state=lambda _sid: None,
        ),
        _factor_registry=SimpleNamespace(_factors=_BrokenRecords()),
    )
    assert _strategy_snapshots(broken_engine)[0]["stale_factor_count"] == 0

    now = time.time()

    class _BadPosition:
        instrument_id = "BTCUSDT"
        quantity = "bad"
        side = SimpleNamespace(value="BUY")

    class _InactiveTracker:
        def __init__(self):
            self.status = SimpleNamespace(value="NEW")
            self.events = []

        def is_terminal(self):
            return False

    engine = SimpleNamespace(
        _can_write=True,
        _env_mode=SimpleNamespace(value="paper"),
        _last_account={"positions": []},
        _protection=SimpleNamespace(all_positions=lambda: {"zero": SimpleNamespace(quantity=0), "bad": _BadPosition()}),
        _ledger=SimpleNamespace(_transactions=[]),
        _factor_registry=SimpleNamespace(_factors={}),
        _strategy_risk=SimpleNamespace(),
        _autopilot_strategy_id="alpha",
        _last_realtime=now,
        _last_nearline=now,
        _last_offline=now,
        _last_recon=now,
        _last_reconciliation_result=SimpleNamespace(matched=True, status="MATCHED", checked_at=object()),
        _order_trackers={"inactive": _InactiveTracker()},
        _active_order_ids=set(),
        _order_symbols={},
        _chaos_engine=None,
        collect_operational_facts=lambda: (_ for _ in ()).throw(RuntimeError("collector unavailable")),
    )
    snapshot = {"ok": True, "account": {"positions": [{"symbol": "BTCUSDT", "positionAmt": "0"}]}}

    monkeypatch.setattr(
        module_checks, "check_module_progress", lambda _contracts: (_ for _ in ()).throw(RuntimeError("progress"))
    )
    monkeypatch.setattr(
        execution_checks, "check_order_trace", lambda _traces: (_ for _ in ()).throw(RuntimeError("trace"))
    )
    monkeypatch.setattr(
        monitor_self_checks,
        "check_component_health",
        lambda _health: [
            MonitoringCheckResult(
                check_id="component",
                status=CheckStatus.PASS,
                severity=CheckSeverity.P1,
                message="ok",
            )
        ],
    )
    monkeypatch.setattr(factor_checks, "check_factors", lambda _states: (_ for _ in ()).throw(RuntimeError("factors")))
    monkeypatch.setattr(
        factor_checks,
        "check_strategies",
        lambda _states: (_ for _ in ()).throw(RuntimeError("factor strategies")),
    )
    monkeypatch.setattr(
        strategy_checks,
        "check_strategy_signal_silence",
        lambda _states: (_ for _ in ()).throw(RuntimeError("silence")),
    )
    monkeypatch.setattr(
        strategy_checks,
        "check_strategy_risk_drift",
        lambda _states: (_ for _ in ()).throw(RuntimeError("drift")),
    )
    monkeypatch.setattr(
        strategy_checks,
        "check_strategy_version_drift",
        lambda _states: (_ for _ in ()).throw(RuntimeError("version")),
    )

    results = collect_monitoring_checks(
        engine=engine,
        exchange_account_snapshot=snapshot,
        algorithm_probe={"ok": True},
        position_mode_evidence=SimpleNamespace(mode="INVALID_MODE"),
        supervisor=SimpleNamespace(
            _monitoring_component_health=[
                ComponentHealth(component="bridge", healthy=True, last_error="", consecutive_failures=0)
            ]
        ),
    )
    ids = {item.check_id for item in results}
    assert "runtime.monitoring.module_progress" in ids
    assert "runtime.execution.order_trace" in ids
    assert "runtime.factors.health" in ids
    assert "runtime.factors.strategies" in ids
    assert "runtime.strategy.silence" in ids
    assert "runtime.strategy.risk_drift" in ids
    assert "runtime.strategy.version_drift" in ids

    # A successful collector refreshes the source identity; a broken registry
    # is reported as an explicit discovery failure rather than disappearing.
    engine.collect_operational_facts = lambda: None
    collect_monitoring_checks(engine=engine, exchange_account_snapshot=snapshot, algorithm_probe={"ok": True})

    class _BrokenRegistry:
        @property
        def _factors(self):
            raise RuntimeError("registry unavailable")

    discovery_engine = SimpleNamespace(
        _can_write=False,
        _env_mode=SimpleNamespace(value="paper"),
        _last_account={"positions": []},
        _protection=SimpleNamespace(all_positions=lambda: {}),
        _ledger=SimpleNamespace(_transactions=[]),
        _factor_registry=_BrokenRegistry(),
        _strategy_risk=None,
        _last_realtime=now,
        _last_nearline=now,
        _last_offline=now,
        _last_recon=now,
        _chaos_engine=None,
    )
    discovery_results = collect_monitoring_checks(
        engine=discovery_engine,
        exchange_account_snapshot=snapshot,
        algorithm_probe={"ok": True},
    )
    assert any(item.check_id == "runtime.factors.discovery" for item in discovery_results)

    strategy_engine = SimpleNamespace(
        _can_write=False,
        _env_mode=SimpleNamespace(value="paper"),
        _last_account={"positions": []},
        _protection=SimpleNamespace(all_positions=lambda: {}),
        _ledger=SimpleNamespace(_transactions=[]),
        _factor_registry=None,
        _strategy_risk=object(),
        _autopilot_strategy_id="alpha",
        _last_realtime=now,
        _last_nearline=now,
        _last_offline=now,
        _last_recon=now,
        _chaos_engine=None,
    )
    monkeypatch.setattr(
        monitoring, "_strategy_snapshots", lambda _engine: (_ for _ in ()).throw(RuntimeError("snapshot"))
    )
    strategy_results = collect_monitoring_checks(
        engine=strategy_engine,
        exchange_account_snapshot=snapshot,
        algorithm_probe={"ok": True},
    )
    assert any(item.check_id == "runtime.strategy.discovery" for item in strategy_results)
