"""Semantic coverage for runtime authority and read-only probe contracts."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import ClassVar

import pytest

import beidou_launcher.runtime as runtime
from beidou_launcher.models import CheckSeverity, CheckStatus
from beidou_observability.monitoring.contracts import AccountPositionMode, PositionModeEvidence


def _check(checks: list, check_id: str):
    return next(item for item in checks if item.check_id == check_id)


def _reconciliation(*, status: str = "MATCHED", matched: bool = True, age_seconds: float = 1.0) -> object:
    return SimpleNamespace(
        status=SimpleNamespace(value=status),
        matched=matched,
        checked_at=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
        differences=[] if matched else ["POSITION_MISMATCH"],
    )


def _engine(
    *,
    lifecycle: str = "ACTIVE",
    control_state: str = "RESUME",
    resume_authorized: bool = True,
    can_write: bool = False,
    stream_result: object | None = None,
    reconciliation: object | None = None,
    incidents: object = (),
    delivery: object | None = None,
    tick_count: int = 1,
    running: bool = True,
    error_count: int = 0,
    heartbeat_age: float = 1.0,
    nearline_age: float = 1.0,
) -> SimpleNamespace:
    class Feed:
        _last_ticker: ClassVar[dict[str, object]] = {"BTCUSDT": {"lastPrice": "100"}}
        _last_orderbook: ClassVar[dict[str, object]] = {"BTCUSDT": {"bids": [["99", "1"]], "asks": [["101", "1"]]}}

        def is_healthy(self) -> bool:
            return True

    class Thread:
        def is_alive(self) -> bool:
            return True

    def get_status() -> str:
        if control_state == "RAISE":
            raise RuntimeError("control unavailable")
        return control_state

    alerts = SimpleNamespace(get_active_incidents=lambda: incidents)
    if delivery is not None:
        alerts.get_delivery_health = lambda: delivery

    engine = SimpleNamespace(
        _lifecycle=SimpleNamespace(state=SimpleNamespace(value=lifecycle)),
        _control=SimpleNamespace(get_status=get_status),
        _supervisor_blocked_writes=[],
        _feed=Feed(),
        _tick_count=tick_count,
        _health=SimpleNamespace(_thread=Thread()),
        _last_realtime_mono=time.monotonic() - heartbeat_age,
        _last_realtime=time.time() - heartbeat_age,
        _running=running,
        _last_reconciliation_result=reconciliation,
        _can_write=can_write,
        _last_nearline=time.time() - nearline_age,
        _error_count=error_count,
        _alerts=alerts,
    )
    if stream_result is not None:
        engine._user_stream_readiness = lambda: stream_result
    if not resume_authorized:
        engine._last_realtime_mono = time.monotonic() - 1
    return engine


def test_incident_helpers_distinguish_resolved_p1_and_p0_facts() -> None:
    assert runtime.active_incident_blocking_severity([]) is None
    assert runtime.active_incident_blocking_severity([{"severity": "HIGH", "status": "DETECTED"}]) is CheckSeverity.P1
    assert (
        runtime.active_incident_blocking_severity([{"severity": "CRITICAL", "status": "DETECTED"}]) is CheckSeverity.P0
    )
    assert runtime.active_incident_blocking_severity([{"severity": "HIGH", "status": "RESOLVED"}]) is None
    assert (
        runtime.active_incident_blocking_severity(
            [SimpleNamespace(severity=SimpleNamespace(value="P0"), status=SimpleNamespace(value="OPEN"))]
        )
        is CheckSeverity.P0
    )

    assert runtime.has_active_trading_incident(SimpleNamespace()) is False
    assert (
        runtime.has_active_trading_incident(SimpleNamespace(_alerts=SimpleNamespace(get_active_incidents=lambda: [])))
        is False
    )
    assert (
        runtime.has_active_trading_incident(
            SimpleNamespace(_alerts=SimpleNamespace(get_active_incidents=lambda: [{"severity": "HIGH"}]))
        )
        is True
    )

    def raise_incident_query() -> list[object]:
        raise RuntimeError("incident store unavailable")

    assert (
        runtime.has_active_trading_incident(
            SimpleNamespace(_alerts=SimpleNamespace(get_active_incidents=raise_incident_query))
        )
        is True
    )


def test_position_mode_check_is_warn_fail_closed_or_pass() -> None:
    checks: list = []
    runtime._append_position_mode_check(checks, None, time.time())
    assert checks[-1].status is CheckStatus.WARN
    assert checks[-1].severity is CheckSeverity.P1

    checks.clear()
    runtime._append_position_mode_check(
        checks, SimpleNamespace(mode=AccountPositionMode.UNKNOWN, error="timeout"), time.time()
    )
    assert checks[-1].status is CheckStatus.FAIL
    assert checks[-1].severity is CheckSeverity.P0

    checks.clear()
    runtime._append_position_mode_check(
        checks,
        PositionModeEvidence(
            account_id="paper",
            venue="BINANCE",
            mode=AccountPositionMode.ONE_WAY,
            source="snapshot",
            source_timestamp=10.0,
            observed_at=11.0,
        ),
        time.time(),
    )
    assert checks[-1].status is CheckStatus.PASS
    assert checks[-1].evidence["mode"] == "ONE_WAY"


def test_collect_runtime_checks_covers_zero_write_and_readiness_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "inspect_engine_wiring", lambda _engine, _mode: [])
    engine = _engine(
        reconciliation=None,
        delivery={"critical_pending": 0, "dead_letter": 0, "unknown": 0, "pending": 0, "configured": True},
    )
    checks, last_errors = runtime.collect_runtime_checks(
        engine=engine,
        mode="paper",
        port=9090,
        resume_authorized=True,
        algorithm_probe={"ok": True},
        last_error_count=0,
        position_mode_evidence=PositionModeEvidence(
            account_id="paper", venue="BINANCE", mode=AccountPositionMode.HEDGE
        ),
        exchange_algo_snapshot={},
        exchange_account_snapshot={},
    )
    assert last_errors == 0
    assert _check(checks, "runtime.safety.reconciliation_authority").status is CheckStatus.PASS
    assert _check(checks, "runtime.safety.user_stream").status is CheckStatus.PASS
    assert _check(checks, "runtime.health.market_data").status is CheckStatus.PASS
    assert _check(checks, "runtime.health.alert_delivery").status is CheckStatus.PASS
    assert _check(checks, "runtime.health.engine_loop").status is CheckStatus.PASS

    degraded = _engine(
        lifecycle="DEGRADED",
        control_state="NO_NEW_RISK",
        resume_authorized=False,
        tick_count=0,
        running=False,
        error_count=9,
        heartbeat_age=120,
        nearline_age=1,
        incidents=[{"severity": "HIGH", "status": "DETECTED"}],
        delivery={"critical_pending": 0, "dead_letter": 2, "unknown": 0, "pending": 1, "configured": False},
    )
    degraded._last_realtime_mono = time.monotonic() - 120
    checks, _ = runtime.collect_runtime_checks(
        engine=degraded,
        mode="shadow",
        port=9091,
        resume_authorized=False,
        algorithm_probe={"ok": False, "error": "not ready"},
        last_error_count=0,
    )
    assert _check(checks, "runtime.health.lifecycle").status is CheckStatus.WARN
    assert _check(checks, "runtime.safety.control_plane").status is CheckStatus.WARN
    assert _check(checks, "runtime.health.market_data").status is CheckStatus.WARN
    assert _check(checks, "runtime.health.realtime_heartbeat").status is CheckStatus.WARN
    assert _check(checks, "runtime.health.nearline_heartbeat").status is CheckStatus.FAIL
    assert _check(checks, "runtime.health.incidents").status is CheckStatus.FAIL
    assert _check(checks, "runtime.health.alert_delivery").status is CheckStatus.WARN


@pytest.mark.parametrize(
    ("lifecycle", "control", "expected"),
    [
        ("LOCKED", "RESUME", CheckStatus.FAIL),
        ("FAILED", "EXIT_ONLY", CheckStatus.FAIL),
        ("UNKNOWN", "RAISE", CheckStatus.FAIL),
    ],
)
def test_collect_runtime_checks_never_converts_unknown_authority_to_pass(
    monkeypatch: pytest.MonkeyPatch, lifecycle: str, control: str, expected: CheckStatus
) -> None:
    monkeypatch.setattr(runtime, "inspect_engine_wiring", lambda _engine, _mode: [])
    engine = _engine(
        lifecycle=lifecycle,
        control_state=control,
        resume_authorized=True,
        reconciliation=_reconciliation(status="MISMATCHED", matched=False, age_seconds=200),
        can_write=True,
        stream_result=(False, {"required": True, "status": "UNKNOWN"}),
    )
    checks, _ = runtime.collect_runtime_checks(
        engine=engine,
        mode="testnet",
        port=9090,
        resume_authorized=True,
        algorithm_probe={"ok": False},
        last_error_count=0,
    )
    assert _check(checks, "runtime.health.lifecycle").status is expected
    expected_control = {
        "RESUME": CheckStatus.PASS,
        "EXIT_ONLY": CheckStatus.WARN,
        "RAISE": CheckStatus.FAIL,
    }[control]
    assert _check(checks, "runtime.safety.control_plane").status is expected_control
    assert _check(checks, "runtime.safety.reconciliation_authority").status is CheckStatus.FAIL
    assert _check(checks, "runtime.safety.user_stream").status is CheckStatus.FAIL


def test_collect_runtime_checks_covers_reconciliation_delivery_and_query_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "inspect_engine_wiring", lambda _engine, _mode: [])
    matched = _engine(
        reconciliation=_reconciliation(),
        delivery={"critical_pending": 0, "dead_letter": 0, "unknown": 0, "pending": 0, "configured": True},
    )
    checks, _ = runtime.collect_runtime_checks(
        engine=matched,
        mode="testnet",
        port=9090,
        resume_authorized=True,
        algorithm_probe={"ok": True},
        last_error_count=0,
    )
    assert _check(checks, "runtime.safety.reconciliation_authority").status is CheckStatus.PASS

    delivery_cases = [
        {"critical_pending": 1, "dead_letter": 0, "unknown": 0, "pending": 0, "configured": True},
        {"critical_pending": 0, "dead_letter": 0, "unknown": 1, "pending": 0, "configured": True},
        {"critical_pending": 0, "dead_letter": 0, "unknown": 0, "pending": 0, "configured": False},
    ]
    for delivery in delivery_cases:
        engine = _engine(reconciliation=_reconciliation(), delivery=delivery)
        checks, _ = runtime.collect_runtime_checks(
            engine=engine,
            mode="testnet",
            port=9090,
            resume_authorized=True,
            algorithm_probe={"ok": True},
            last_error_count=0,
        )
        assert _check(checks, "runtime.health.alert_delivery").status in {CheckStatus.FAIL, CheckStatus.WARN}

    broken_alerts = _engine(reconciliation=_reconciliation())
    broken_alerts._alerts = SimpleNamespace(
        get_active_incidents=lambda: (_ for _ in ()).throw(RuntimeError("incident query")),
        get_delivery_health=lambda: (_ for _ in ()).throw(RuntimeError("delivery query")),
    )
    checks, _ = runtime.collect_runtime_checks(
        engine=broken_alerts,
        mode="testnet",
        port=9090,
        resume_authorized=True,
        algorithm_probe={"ok": True},
        last_error_count=0,
    )
    assert _check(checks, "runtime.health.incidents").status is CheckStatus.FAIL
    assert _check(checks, "runtime.health.alert_delivery").status is CheckStatus.FAIL


class _ProbeFeed:
    def __init__(self, *, kline: dict, live: dict) -> None:
        self.kline = kline
        self.live = live

    async def async_get_kline_features(self, _symbol: str) -> dict:
        return self.kline

    async def async_update_features(self, _symbol: str) -> dict:
        return self.live


class _ProbeGraph:
    def topological_order(self) -> list[str]:
        return ["entry"]


class _ProbeKernel:
    def __init__(self, result: object, *, graph_hash: str = "graph", proposal_hash: str = "proposal") -> None:
        self.result = result
        self._typed_graph = SimpleNamespace(
            topological_order=lambda: ["entry"],
            _nodes={"entry": SimpleNamespace(node_type=SimpleNamespace(value="ENTRY"))},
        )
        self.graph_hash = graph_hash
        self.proposal_hash = proposal_hash

    async def evaluate(self, _context: dict) -> object:
        return self.result

    def compute_proposal_hash(self, _proposal: object) -> str:
        return self.proposal_hash


@pytest.mark.asyncio
async def test_probe_success_path_can_include_real_shadow_payload_and_proposal() -> None:
    class ShadowResult:
        ensemble_forecast = SimpleNamespace(schema_version="schema", model_version="model", policy_version="policy")

        def to_dict(self) -> dict[str, str]:
            return {
                "status": "PASS",
                "trace_hash": "trace",
                "benchmark_snapshot_hash": "benchmark",
                "alpha_forecast_hash": "alpha",
                "ensemble_forecast_hash": "ensemble",
                "exposure_target_hash": "exposure",
                "portfolio_target_hash": "portfolio",
            }

    class ShadowEngine:
        def __init__(self, _registry: object) -> None:
            pass

        def evaluate(self, _context: dict) -> ShadowResult:
            return ShadowResult()

    import beidou_strategy.alpha.pipeline as pipeline

    original = pipeline.AlphaV3ShadowEngine
    pipeline.AlphaV3ShadowEngine = ShadowEngine  # type: ignore[assignment]
    try:
        state = SimpleNamespace(state_hash="state-hash", to_dict=lambda: {"direction": "UP"})
        engine = SimpleNamespace(
            _feed=_ProbeFeed(kline={"close": 100.0}, live={"price": 100.0}),
            _strategy_kernel=_ProbeKernel(
                {"kernel": "typed_graph", "proposal": {"side": "BUY"}, "graph_hash": "graph", "context_hash": "context"}
            ),
            _estimate_market_state=lambda features: {"direction": "UP", "close": features["close"]},
            _last_market_state=state,
            _alpha_v3_calibration_registry=None,
        )
        result = await runtime.run_read_only_algorithm_probe(engine, ["BTCUSDT"])
    finally:
        pipeline.AlphaV3ShadowEngine = original

    assert result["ok"] is True
    assert result["v3_shadow_status"] == "PASS"
    assert result["components"]["entry"]["node_type"] == "ENTRY"
    assert result["proposal_hash"] == "proposal"
    assert result["decision_trace"]["state_hash"]


@pytest.mark.asyncio
async def test_probe_failures_return_auditable_error_without_writes() -> None:
    incomplete = SimpleNamespace(
        _feed=_ProbeFeed(kline={}, live={"price": 0}),
        _strategy_kernel=None,
    )
    result = await runtime.run_read_only_algorithm_probe(incomplete, ["BTCUSDT"])
    assert result["ok"] is False and "不完整" in result["error"]

    missing_kernel = SimpleNamespace(
        _feed=_ProbeFeed(kline={"close": 100}, live={"price": 100}),
        _strategy_kernel=SimpleNamespace(),
    )
    result = await runtime.run_read_only_algorithm_probe(missing_kernel, ["BTCUSDT"])
    assert "evaluate" in result["error"]

    cases = [
        ({"kernel": "wrong", "graph_hash": "graph"}, "typed"),
        ({"kernel": "typed_graph", "graph_hash": ""}, "graph hash"),
        ({"kernel": "typed_graph", "graph_hash": "graph"}, "节点"),
    ]
    for kernel_result, expected in cases:
        kernel = _ProbeKernel(kernel_result)
        if expected == "节点":
            kernel._typed_graph = SimpleNamespace(topological_order=lambda: [], _nodes={})
        result = await runtime.run_read_only_algorithm_probe(
            SimpleNamespace(_feed=_ProbeFeed(kline={"close": 100}, live={"price": 100}), _strategy_kernel=kernel),
            ["BTCUSDT"],
        )
        assert result["ok"] is False
        assert expected in result["error"]

    no_hash = _ProbeKernel({"kernel": "typed_graph", "graph_hash": "graph"}, proposal_hash="")
    result = await runtime.run_read_only_algorithm_probe(
        SimpleNamespace(_feed=_ProbeFeed(kline={"close": 100}, live={"price": 100}), _strategy_kernel=no_hash),
        ["BTCUSDT"],
    )
    assert "proposal hash" in result["error"]
