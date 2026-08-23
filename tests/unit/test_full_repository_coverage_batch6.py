"""Behavior-backed coverage for the final small repository boundaries."""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from beidou_certification.evidence_bundle import GateResult, GateRunner
from beidou_certification.evidence_bundle import ScenarioStatus as EvidenceScenarioStatus
from beidou_certification.g5_scenarios.base import (
    NotionalExceededError,
    NotionalLedger,
    ScenarioContext,
    ScenarioStatus,
)
from beidou_certification.g5_scenarios.engine import reconciliation_mismatch as mismatch_module
from beidou_certification.g5_scenarios.engine.partial_fill import PartialFillScenario
from beidou_certification.g5_scenarios.protection import double_worker_fencing as fencing_module
from beidou_certification.g5_scenarios.protocol import clock_skew as clock_module
from beidou_certification.g5_scenarios.protocol import rate_limit as rate_module
from beidou_exchange.binance_usdm.adapter import BinanceUsdmAdapter
from beidou_exchange.binance_usdm.endpoints import Endpoint
from beidou_exchange.binance_usdm.write_guard import classify_terminal_write
from beidou_exchange.core.error_taxonomy import Result, classify_http_error
from beidou_exchange.core.write_authority import (
    TerminalWriteContext,
    TerminalWriteKind,
    TerminalWriteRequest,
    evaluate_terminal_write,
)
from beidou_infra.outbox import OutboxWorker
from beidou_infra.postgres_store import PostgresPersistentStore
from beidou_launcher.cli import _enable_unbuffered_stdout
from beidou_launcher.g7_tracker import G7LiveTracker, SLIState
from beidou_launcher.models import CheckResult, CheckSeverity, CheckStatus, HealthDebounce
from beidou_observability.monitoring.clock_integrity import ClockIntegrity
from beidou_observability.monitoring.contracts import (
    AccountPositionMode,
    PositionKey,
)
from beidou_observability.monitoring.contracts import (
    CheckSeverity as MonitorSeverity,
)
from beidou_observability.monitoring.fact_collector import FactCollector
from beidou_observability.monitoring.incident_manager import IncidentManager
from beidou_observability.monitoring.repository import MonitoringRepository
from beidou_observability.monitoring.service import MonitoringService, create_monitoring_cli
from beidou_research.backtest.replay import ReplayResult, ReplayValidator
from beidou_research.data.dataset_manifest import DatasetManifest
from beidou_research.data.kline_store import KlineStore
from beidou_research.factors.rsi import RSIVerifiability, compute_rsi_simple, compute_rsi_wilder
from beidou_research.kernel import ChampionChallengerSwitch
from beidou_research.mining.evaluation import multiple_testing as multiple_testing_module
from beidou_research.mining.evaluation.fast_screen import _is_missing
from beidou_research.mining.generators.residual import ResidualGenerator
from beidou_research.mining.selection.marginal_contribution import _mean, _sharpe, _std
from beidou_research.mining.selection.redundancy import RedundancyDetector, _ols_r_squared
from beidou_research.statistics.validation import _normal_cdf
from beidou_safety.execution.contracts import ExecutionPlan, PlanSlice, PlanStatus
from beidou_safety.execution.intent import IntentOutbox
from beidou_shared.errors import ErrorCategory
from beidou_shared.types import (
    InstrumentId,
    OrderSide,
    Quantity,
    StrategyId,
    VenueId,
    VenueInstrument,
)
from beidou_strategy.alpha import AlphaComponentType, SignalDirection
from beidou_strategy.alpha.legacy_adapter import (
    _filter_decision,
    build_typed_graph,
    make_entry_node,
    make_exit_node,
)
from beidou_strategy.kernel_parity import StrategyKernelContract
from beidou_strategy.paper_shadow import PaperShadowRunner, ShadowConfig
from beidou_strategy.risk.manager import RiskBudget, StrategyRiskManager
from beidou_strategy.state.cost_model import CostModel
from tests.unit.test_full_repository_coverage_batch2 import _child
from tests.unit.test_g5_scenarios.test_engine_fill_race import _ctx as partial_context
from tests.unit.test_g5_scenarios.test_engine_fill_race import _FakeClient as PartialFakeClient
from tests.unit.test_g5_scenarios.test_engine_fill_race import _noop_sleep
from tests.unit.test_g5_scenarios.test_protocol_basic import FakeClient as ProtocolFakeClient
from tests.unit.test_g5_scenarios.test_protocol_basic import _ctx as protocol_context
from tests.unit.test_legacy_alpha_algorithms import _signal, _StaticComponent


def test_g5_clock_skew_cleanup_notional_and_generic_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _CancelFailureClient(ProtocolFakeClient):
        async def cancel_order(self, symbol: str, order_id: int) -> Result:
            raise RuntimeError("cleanup unavailable")

    cleanup_client = _CancelFailureClient()
    cleanup_result = asyncio.run(clock_module.ClockSkewScenario().run(protocol_context(cleanup_client, tmp_path)))
    assert cleanup_result.status is ScenarioStatus.PASS
    cleanup_step = next(step for step in cleanup_result.evidence["steps"] if step["action"] == "cleanup_cancel")
    assert cleanup_step["ok"] is False

    def reject_notional(*_args: object, **_kwargs: object) -> None:
        raise NotionalExceededError("clock_skew", 51.0, 50.0)

    monkeypatch.setattr(clock_module, "min_gate_quantity", reject_notional)
    with pytest.raises(NotionalExceededError):
        asyncio.run(clock_module.ClockSkewScenario().run(protocol_context(ProtocolFakeClient(), tmp_path)))

    class _ServerTimeFailureClient(ProtocolFakeClient):
        async def get_server_time(self) -> Result:
            raise RuntimeError("server time unavailable")

    failed = asyncio.run(clock_module.ClockSkewScenario().run(protocol_context(_ServerTimeFailureClient(), tmp_path)))
    assert failed.status is ScenarioStatus.FAIL and failed.error_type == "RuntimeError"


def test_g5_rate_limit_notional_and_fencing_boundaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        rate_module,
        "_build_client",
        lambda: (_ for _ in ()).throw(NotionalExceededError("rate_limit", 51.0, 50.0)),
    )
    context = ScenarioContext(None, NotionalLedger(1000.0), tmp_path, "BTCUSDT", False)
    with pytest.raises(NotionalExceededError):
        asyncio.run(rate_module.RateLimitScenario().run(context))
    monkeypatch.setattr(rate_module, "_build_client", lambda: (_ for _ in ()).throw(RuntimeError("client failure")))
    generic_failure = asyncio.run(rate_module.RateLimitScenario().run(context))
    assert generic_failure.status is ScenarioStatus.FAIL and generic_failure.error_type == "RuntimeError"

    class _Lock:
        def __init__(self, path: Path) -> None:
            self.path = path

        def acquire(self) -> tuple[bool, str]:
            if self.path.name == "probe.pid":
                return True, "probe acquired"
            return False, "北斗实例已运行，PID=123"

        def release(self) -> None:
            return None

    def make_lock(path: Path) -> _Lock:
        return _Lock(path)

    scenario = fencing_module.DoubleWorkerFencingScenario(engine_lock_path=tmp_path / "engine.pid", make_lock=make_lock)
    fencing_context = ScenarioContext(None, NotionalLedger(1000.0), tmp_path, "BTCUSDT", False)
    passed = asyncio.run(scenario.run(fencing_context))
    assert passed.status is ScenarioStatus.PASS

    monkeypatch.setattr(fencing_module, "fencing_verdict", lambda *_args: (False, "unexpected_verdict"))
    rejected = asyncio.run(scenario.run(ScenarioContext(None, NotionalLedger(1000.0), tmp_path, "BTCUSDT", False)))
    assert rejected.status is ScenarioStatus.FAIL and rejected.error_type == "UNEXPECTED_VERDICT"

    failing_context = ScenarioContext(None, NotionalLedger(1000.0), tmp_path, "BTCUSDT", False)
    monkeypatch.setattr(
        failing_context.ledger,
        "record",
        lambda *_args: (_ for _ in ()).throw(NotionalExceededError("double_worker_fencing", 51.0, 50.0)),
    )
    with pytest.raises(NotionalExceededError):
        asyncio.run(scenario.run(failing_context))


class _RecordCursor:
    def __init__(self, row: object | None) -> None:
        self.row = row

    def fetchone(self) -> object | None:
        return self.row


class _RecordConnection:
    def __init__(self, row: object | None = None) -> None:
        self.row = row
        self.closed = False

    def execute(self, *_args: object, **_kwargs: object) -> _RecordCursor:
        return _RecordCursor(self.row)

    def transaction(self) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()

    def close(self) -> None:
        self.closed = True


def test_g5_reconciliation_mismatch_read_missing_notional_and_restore_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert mismatch_module._read_record(_RecordConnection(), "type", "id") is None

    baseline_context = ScenarioContext(None, NotionalLedger(1000.0), tmp_path, "BTCUSDT", False)
    baseline_conn = _RecordConnection()
    monkeypatch.setattr(mismatch_module.psycopg, "connect", lambda *_args, **_kwargs: baseline_conn)
    monkeypatch.setattr(mismatch_module, "_read_record", lambda *_args: None)
    missing = asyncio.run(mismatch_module.ReconciliationMismatchScenario().run(baseline_context))
    assert missing.status is ScenarioStatus.FAIL and "基线缺失" in missing.error_message

    monkeypatch.setattr(
        mismatch_module.psycopg,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(NotionalExceededError("reconciliation", 51.0, 50.0)),
    )
    with pytest.raises(NotionalExceededError):
        asyncio.run(mismatch_module.ReconciliationMismatchScenario().run(baseline_context))

    restore_conn = _RecordConnection()
    monkeypatch.setattr(mismatch_module.psycopg, "connect", lambda *_args, **_kwargs: restore_conn)
    payload = {"balance_amount": "100", "positions": {}}
    # 仅基线 key(default:BINANCE)返回 payload,journal key 返回 None —— 否则
    # run() 的 journal 读取会拿到非 journal dict,自愈 _recover_from_journal 抛
    # JOURNAL_PAYLOAD_MISSING 提前 FAIL,覆盖不到「_execute_flow 抛错 → finally
    # 还原」的原路径。
    monkeypatch.setattr(
        mismatch_module,
        "_read_record",
        lambda *_args: payload if len(_args) >= 3 and str(_args[2]) == "default:BINANCE" else None,
    )

    async def fail_flow(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("flow failed")

    monkeypatch.setattr(mismatch_module.ReconciliationMismatchScenario, "_execute_flow", fail_flow)
    monkeypatch.setattr(
        mismatch_module,
        "_upsert_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("restore failed")),
    )
    failed = asyncio.run(mismatch_module.ReconciliationMismatchScenario().run(baseline_context))
    assert failed.status is ScenarioStatus.FAIL and restore_conn.closed
    assert any(step.get("action") == "restore_baseline_finally" and not step["ok"] for step in failed.evidence["steps"])


def test_partial_fill_skips_non_trading_and_non_usdt_entries(tmp_path: Path) -> None:
    info = {
        "symbols": [
            {"symbol": "ETHBTC", "status": "TRADING", "contractType": "PERPETUAL", "underlyingType": "COIN"},
            {"symbol": "BTCUSDT", "status": "HALT", "contractType": "PERPETUAL", "underlyingType": "COIN"},
        ]
    }
    client = PartialFakeClient(exchange_info=info)
    result = asyncio.run(PartialFillScenario(sleep=_noop_sleep).run(partial_context(client, tmp_path)))
    assert result.status is ScenarioStatus.NOT_VERIFIABLE and client.placed == []


def test_g7_tracker_window_and_recovery_boundaries() -> None:
    state = SLIState(name="coverage", threshold=0.5, window_seconds=5.0)
    state.record(1.0, now_wall=0.0, now_mono=0.0)
    state.record(1.0, now_wall=10.0, now_mono=10.0)
    assert len(state.samples) == 1 and state.latest is not None

    tracker = G7LiveTracker(minimum_elapsed_seconds=0.0, minimum_cycles=0)
    tracker.feed(
        [
            CheckResult("runtime.health.market_data", "market", CheckStatus.PASS, CheckSeverity.INFO, "ok"),
            CheckResult("runtime.safety.protection_coverage", "protection", CheckStatus.PASS, CheckSeverity.INFO, "ok"),
            CheckResult(
                "runtime.safety.protection_coverage", "protection", CheckStatus.FAIL, CheckSeverity.P2, "missing"
            ),
        ]
    )
    tracker.feed_recovery_context(1, 0)
    assert tracker.summary()["invalidated_reason"] == "RECOVERY_BUDGET_EXCEEDED"


def test_launcher_cli_stream_and_health_debounce_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BrokenStream:
        def isatty(self) -> bool:
            return False

        def reconfigure(self, **_kwargs: object) -> None:
            raise ValueError("capture stream")

    monkeypatch.setattr("beidou_launcher.cli.sys", SimpleNamespace(stdout=None, stderr=_BrokenStream()))
    _enable_unbuffered_stdout()

    debounce = HealthDebounce(window_seconds=1.0, degrade_after=2, lock_after=4, recover_after=2)
    assert debounce.feed(True, now=0.0) is None
    assert debounce.feed(False, now=0.5) is None
    assert debounce.feed(False, now=2.0) is None


def test_gate_evidence_and_monitoring_small_boundaries(tmp_path: Path) -> None:
    runner = GateRunner()
    assert (
        runner.run_gate("G0", {"lint": EvidenceScenarioStatus.PASS, "typecheck": EvidenceScenarioStatus.PASS})
        is GateResult.PASS
    )
    assert runner.has_p0_blocker() is False

    repo = MonitoringRepository(str(tmp_path / "monitor.db"))
    service = MonitoringService(repo=repo)
    assert service.get_evidence() == []
    assert isinstance(service.get_rate_budget(), dict)
    cli = create_monitoring_cli(service)
    assert set(cli) == {"status", "check--deep", "incidents", "evidence", "rate-budget", "mode-contract"}
    repo.close()


def test_research_statistical_and_selection_boundaries() -> None:
    assert _normal_cdf(-9.0) == 0.0 and _normal_cdf(9.0) == 1.0 and _normal_cdf(0.0) == 0.5
    assert (
        compute_rsi_wilder([1.0, 2.0, 3.0, 4.0, 5.0], period=10, min_periods=5).verifiability
        is RSIVerifiability.NOT_VERIFIABLE
    )
    assert compute_rsi_simple([1.0, 2.0], period=2).verifiability is RSIVerifiability.NOT_VERIFIABLE
    assert compute_rsi_simple([1.0, float("nan"), 2.0], period=2).verifiability is RSIVerifiability.NOT_VERIFIABLE
    assert compute_rsi_simple([1.0, 2.0, 3.0], period=2).value == 100.0
    assert compute_rsi_simple([3.0, 2.0, 1.0], period=2).value == 0.0

    assert _mean([]) == 0.0 and _std([1.0]) == 0.0 and _sharpe([1.0]) == 0.0
    assert _sharpe([1.0, 1.0]) == 0.0
    assert _is_missing(None) and _is_missing(float("nan")) and _is_missing(float("inf")) and not _is_missing("finite")
    residual = ResidualGenerator()
    assert residual.check_independence([1.0] * 12, [0.0] * 12) == (False, 0.0)
    has_info, ratio = residual.check_independence(list(range(12)), [float(i % 2) for i in range(12)])
    assert has_info is True and ratio > 0.01
    assert RedundancyDetector()._cluster_distance([], [], {}) == 1.0
    assert _ols_r_squared([1.0, 2.0, 3.0], [[1.0], [1.0], [1.0]]) == 0.0


def test_multiple_testing_failure_evidence_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    assert multiple_testing_module._spearman_rank_corr([1.0], [1.0]) == 0.0
    monkeypatch.setattr(
        multiple_testing_module,
        "compute_pbo",
        lambda *_args, **_kwargs: SimpleNamespace(pbo=0.5, n_combinations=8),
    )
    monkeypatch.setattr(multiple_testing_module, "benjamini_hochberg", lambda _pvalues: None)
    report = multiple_testing_module.evaluate_multiple_testing(
        [0.01, 0.02],
        observed_sharpe=0.0,
        n_trials=2,
        in_sample_sharpes=[1.0, 2.0, 3.0, 4.0],
        out_of_sample_sharpes=[4.0, 3.0, 2.0, 1.0],
    )
    assert report.verdict == "FAIL"
    assert any(reason.startswith("PBO=") for reason in report.failure_reasons)
    assert "BH-FDR not significant" in report.failure_reasons


def test_strategy_adapter_parity_shadow_cost_and_risk_boundaries() -> None:
    entry = _StaticComponent(
        "entry-bad", AlphaComponentType.ENTRY, _signal(AlphaComponentType.FILTER, SignalDirection.LONG)
    )
    with pytest.raises(TypeError, match="component type"):
        asyncio.run(make_entry_node(entry)._entry_fn({}))

    no_action = _signal(AlphaComponentType.FILTER, SignalDirection.NO_ACTION, confidence=0.9)
    assert _filter_decision(no_action).value == "VETO"
    reason_signal = _signal(AlphaComponentType.FILTER, SignalDirection.LONG, metadata={"reason": "veto extreme"})
    assert _filter_decision(reason_signal).value == "VETO"

    unsafe = _StaticComponent(
        "exit-direct", AlphaComponentType.EXIT, _signal(AlphaComponentType.EXIT, SignalDirection.LONG)
    )
    with pytest.raises(ValueError, match="risk-increasing"):
        asyncio.run(make_exit_node(unsafe)._exit_fn({}))
    safe_signal = _signal(AlphaComponentType.EXIT, SignalDirection.FLAT)
    safe = _StaticComponent("exit-safe", AlphaComponentType.EXIT, safe_signal)
    assert asyncio.run(make_exit_node(safe)._exit_fn({})) is safe_signal
    components = {
        "entry": _StaticComponent(
            "entry", AlphaComponentType.ENTRY, _signal(AlphaComponentType.ENTRY, SignalDirection.LONG)
        ),
        "exit": _StaticComponent(
            "exit", AlphaComponentType.EXIT, _signal(AlphaComponentType.EXIT, SignalDirection.FLAT)
        ),
    }
    graph = build_typed_graph(StrategyId("strategy"), components, {"entry"}, set(), {"exit"})
    assert "exit" in graph.topological_order()

    class _EmptyObject:
        def __str__(self) -> str:
            raise ValueError("cannot stringify")

    assert StrategyKernelContract.compute_proposal_hash(_EmptyObject()) == ""

    shadow = PaperShadowRunner(ShadowConfig(strategy_id=StrategyId("shadow")))
    shadow.record_tick("LONG", 0.5, decision_timestamp=10.0)
    with pytest.raises(ValueError, match="available after"):
        shadow.record_tick(
            "LONG",
            0.5,
            actual_direction="LONG",
            actual_strength=0.5,
            outcome_source="forward-feed",
            decision_timestamp=20.0,
            outcome_available_at=20.0,
        )
    with pytest.raises(ValueError, match="available after"):
        shadow.record_forward_outcome(
            tick=1,
            actual_direction="LONG",
            actual_strength=0.5,
            outcome_source="forward-feed",
            outcome_available_at=10.0,
        )
    with pytest.raises(ValueError, match="direction is required"):
        shadow.record_forward_outcome(
            tick=1,
            actual_direction=" ",
            actual_strength=0.5,
            outcome_source="forward-feed",
            outcome_available_at=11.0,
        )

    model = CostModel()
    assert model.funding_rate_warning(0.0007) == "ELEVATED_FUNDING_RATE"
    instrument = VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT"))
    model.funding_rate_warning = lambda _rate: "ELEVATED_FUNDING_RATE"  # type: ignore[method-assign]
    estimate = model.estimate_order(instrument, Quantity(amount="1"), SimpleNamespace(amount="100"), OrderSide.BUY)
    assert estimate.warnings == ["funding_rate_ELEVATED_FUNDING_RATE"]

    manager = StrategyRiskManager()
    strategy_id = StrategyId("risk")
    manager.set_budget(RiskBudget(strategy_id=strategy_id))
    assert manager.compute_position_size(strategy_id, 1000.0, 100.0, 100.0) == 0.0


def test_exchange_authority_adapter_recovery_and_classification(monkeypatch: pytest.MonkeyPatch) -> None:
    unknown = TerminalWriteRequest(TerminalWriteKind.UNKNOWN, "POST", "/unknown", "acct")
    assert evaluate_terminal_write(None, unknown).reason_code == "UNCLASSIFIED_TERMINAL_WRITE"
    assert (
        evaluate_terminal_write(
            None, TerminalWriteRequest(TerminalWriteKind.INCREASE, "POST", "/order", "acct")
        ).reason_code
        == "WRITE_AUTHORITY_MISSING"
    )
    context = TerminalWriteContext(
        "task", "entry", "owner", "gen", "approval", 4102444800.0, "nonce", intent_id="i", quantity="1"
    )
    request = TerminalWriteRequest(
        TerminalWriteKind.INCREASE,
        "POST",
        "/order",
        "acct",
        symbol="BTCUSDT",
        quantity="1",
        task_id=context.task_id,
        entrypoint=context.entrypoint,
        owner_id=context.owner_id,
        generation=context.generation,
        approval_id=context.approval_id,
        expires_at=context.expires_at,
        nonce=context.nonce,
        intent_id="i",
        dedicated_account=True,
    )

    class _BrokenAuthority:
        def authorize(self, _request: TerminalWriteRequest) -> object:
            raise RuntimeError("authority unavailable")

    assert evaluate_terminal_write(_BrokenAuthority(), request).reason_code == "WRITE_AUTHORITY_ERROR"
    assert classify_http_error(429)[0] is ErrorCategory.RATE_LIMIT
    assert classify_http_error(418)[0] is ErrorCategory.RATE_LIMIT

    from tests.unit.test_full_repository_coverage_adapter_deep import _Transport

    class _NetworkFailure:
        error = SimpleNamespace(category="NETWORK")

        def is_success(self) -> bool:
            return False

    transport = _Transport(Result.success({"ok": True}))
    adapter = BinanceUsdmAdapter(rest_client=transport)
    adapter.health_monitor.update_venue_health("HEALTHY")
    transport.response = Result(is_ok=False, error=SimpleNamespace(category="NETWORK"))
    failed = asyncio.run(adapter.request("GET", "/fapi/v1/time"))
    assert failed.is_success() is False

    recovery_raw = {
        "orderId": "1",
        "clientOrderId": "cid",
        "symbol": "BTCUSDT",
        "status": "NEW",
        "side": "BUY",
        "type": "MARKET",
        "origQty": "0",
        "executedQty": "0",
    }
    transport.response = recovery_raw
    adapter.health_monitor.update_venue_health("HEALTHY")
    recovered = asyncio.run(adapter.query_order_by_client_id("BTCUSDT", "cid"))
    assert recovered.is_success() is False and recovered.error is not None

    guarded = classify_terminal_write("POST", Endpoint.ORDER, {"reduceOnly": "true"}, account_id="acct")
    assert guarded is not None and guarded.kind is TerminalWriteKind.REDUCE_OWNED
    leverage = classify_terminal_write("POST", Endpoint.LEVERAGE, {}, account_id="acct")
    assert leverage is not None and leverage.kind is TerminalWriteKind.INCREASE


def test_infra_observability_and_research_data_boundaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _DeadFallbackConnection:
        closed = 0

        def rollback(self) -> None:
            return None

        def commit(self) -> None:
            return None

        def close(self) -> None:
            return None

    dead = _DeadFallbackConnection()
    store = PostgresPersistentStore("postgresql://coverage", connection_factory=lambda: dead, validate_schema=False)
    with pytest.raises(RuntimeError, match="rollback"), store._transaction():
        dead.closed = 1
        raise RuntimeError("rollback")
    assert store._conn is None

    with pytest.raises(RuntimeError, match="OUTBOX_FENCING_TOKEN_UNKNOWN"):
        asyncio.run(
            OutboxWorker(fencing_token=0)._transition_owned(
                "message", from_states=("PENDING",), to_status="SENT", event_type="sent", fields=""
            )
        )

    from tests.unit.test_full_repository_coverage_batch2 import _intent as memory_intent

    outbox = IntentOutbox()
    intent = memory_intent("replay-boundary")
    outbox.commit(intent)
    assert outbox.claim("coverage") is intent
    outbox.persist_execution_plan(intent.intent_id, [_child(intent.intent_id)])
    monkeypatch.setattr(
        outbox,
        "transition_execution_child",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("TERMINAL_CHILD_STATE")),
    )
    update = SimpleNamespace(
        client_order_id=f"child-{intent.intent_id}",
        order_status=SimpleNamespace(value="NEW"),
        cumulative_quantity=SimpleNamespace(amount="0"),
        order_id="",
        event=SimpleNamespace(event_id="replay"),
    )
    assert outbox.project_user_order_update(update) is not None
    monkeypatch.setattr(
        outbox,
        "transition_execution_child",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("unrelated transition error")),
    )
    with pytest.raises(ValueError, match="unrelated"):
        outbox.project_user_order_update(update)

    groom = IntentOutbox()
    groom._MAX_OUTBOX_SIZE = 2
    original_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("fsync unavailable")))
    groom.commit(memory_intent("groom-batch6-1"))
    groom.commit(memory_intent("groom-batch6-2"))
    groom.commit(memory_intent("groom-batch6-3"))
    assert groom.dead_letter_count == 2
    monkeypatch.setattr(os, "fsync", original_fsync)

    manifest_path = tmp_path / "manifest.json"
    assert DatasetManifest.read(manifest_path) is None
    manifest_path.write_text("not-json")
    assert DatasetManifest.read(manifest_path) is None
    kline = KlineStore(tmp_path / "klines")
    with pytest.raises(FileNotFoundError):
        kline.load("BTCUSDT", "1m")
    replay = ReplayValidator()
    assert replay.check_survivorship({"BTCUSDT"}, {"BTCUSDT", "ETHUSDT"}) == {"ETHUSDT"}
    replay.set_baseline("hash")
    assert replay.verify_determinism(ReplayResult("id", True, output_hash="hash"))
    assert ReplayValidator().verify_determinism(ReplayResult("id", True, output_hash="anything"))
    switch = ChampionChallengerSwitch("s", "old", "new", "m", "d", "c", "p", "cert")
    switch.rollback()
    assert switch.rolled_back


def test_monitoring_fact_clock_incident_and_execution_contract_boundaries() -> None:
    position_key = PositionKey.build(
        venue="BINANCE", account_id="acct", symbol="BTCUSDT", mode=AccountPositionMode.UNKNOWN
    )
    assert position_key.position_side.value == "BOTH" and str(position_key).endswith("/BOTH")
    collector = FactCollector()
    assert collector.get("missing") is None
    assert not collector.get_required("missing").is_valid
    fact = collector.record_derived("known", 1)
    assert collector.get_required("known") is fact

    clock = ClockIntegrity()
    for index in range(22):
        clock.record_exchange_time((index + 1) * 1000)
    assert len(clock._samples) == 20

    incidents = IncidentManager()
    incident, created = incidents.create_or_dedupe("dedupe", severity=MonitorSeverity.P0)
    assert created and incidents.get_active_p0_count() == 1
    assert incidents.escalate_to_locked() == [incident]

    empty_plan = ExecutionPlan()
    assert not empty_plan.is_executable()
    emergency = ExecutionPlan(
        plan_id="emergency",
        slices=[PlanSlice("s", "BTCUSDT", "1", "100", "MARKET", "panic")],
        status=PlanStatus.EXECUTING,
        is_emergency=True,
    )
    assert emergency.is_executable() and emergency.never_increases_absolute_position(1.0)
    assert not emergency.never_increases_absolute_position(0.5)
