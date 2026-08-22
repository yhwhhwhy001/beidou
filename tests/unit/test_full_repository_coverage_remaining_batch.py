"""Behavioral boundary coverage for the remaining small compatibility modules."""

from __future__ import annotations

import asyncio
import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from beidou_research.kernel import DatasetManifest, PurgedWalkForward, StrategyKernel
from beidou_research.mining.evaluation.cost_capacity import CostModel as MiningCostModel
from beidou_research.mining.evaluation.fast_screen import FastScreen, FastScreenConfig
from beidou_research.mining.evaluation.multiple_testing import (
    compute_pbo,
    deflated_sharpe_ratio,
    evaluate_multiple_testing,
)
from beidou_research.mining.runner import (
    MiningRunner,
    PipelineConfig,
    _compute_ic,
    _compute_ic_np,
    _compute_sharpe,
    _compute_sharpe_np,
    _correlation_p_value,
    _estimate_adv_from_price_data,
    _is_finite,
    _timeframe_to_hours,
    compute_feature_manifest_hash,
)
from beidou_shared.types import DataQualityTier, InstrumentId, OrderSide, Price, Quantity, VenueId, VenueInstrument
from beidou_strategy.kernel_parity import (
    KernelMode,
    ParityStatus,
    StrategyKernelContract,
    parity_check,
)
from beidou_strategy.kernel_parity import (
    StrategyKernel as ParityKernel,
)
from beidou_strategy.paper_shadow import (
    PaperMatchingEngine,
    PaperShadowRunner,
    ShadowConfig,
    ShadowMetrics,
    ShadowReport,
    ShadowStatus,
    TestnetGate,
)
from beidou_strategy.state.cost_model import CostModel as LegacyCostModel


def test_runner_remaining_helpers_and_policy_loading() -> None:
    manifest = compute_feature_manifest_hash()
    assert len(manifest) == 64
    assert MiningRunner._compute_dataset_hash([]) == "UNKNOWN"
    assert MiningRunner._compute_dataset_hash([{} for _ in range(100)]) == "UNKNOWN"

    config = PipelineConfig.from_yaml("config/factor_mining_policy.yaml")
    assert config.strict_policy is True
    assert config.policy_version == "2.0.0"
    assert config.time_budget_minutes == 120.0
    assert config.cost_model is not None


def test_runner_disjoint_derived_candidates_cover_fail_closed_residual_path(monkeypatch, tmp_path) -> None:
    """Two independently usable candidates with no common observations stay diagnostic."""
    from tests.unit.test_full_repository_coverage_runner_remaining import (
        _patch_pipeline_dependencies,
        _pipeline_price_data,
        _runner_labels,
    )

    runner = MiningRunner(
        PipelineConfig(
            run_id="disjoint-derived",
            evidence_dir=str(tmp_path),
            cost_model=MiningCostModel(taker_fee_bps=4.0),
            dataset_manifest_hash="d" * 64,
            feature_manifest_hash="f" * 64,
            policy_version="policy-v1",
        )
    )
    _patch_pipeline_dependencies(monkeypatch, runner, _runner_labels(), capacity_ok=True)
    monkeypatch.setattr(
        runner,
        "_generate_candidates",
        lambda *_args: [
            {"expression_string": "close", "expression_hash": "a" * 16, "factor_id": "a", "scale": 1.0},
            {"expression_string": "close", "expression_hash": "b" * 16, "factor_id": "b", "scale": 2.0},
        ],
    )

    def disjoint_values(candidate, _points, **_kwargs):
        values = [math.nan] * 120
        start, stop = (0, 60) if candidate["factor_id"] == "a" else (60, 120)
        for index in range(start, stop):
            values[index] = float(index + 1)
        return values

    monkeypatch.setattr(runner, "_evaluate_candidate", disjoint_values)
    result = runner.run(_pipeline_price_data())
    assert result.candidates_passed == 2
    assert not any(bundle.factor_id.startswith("interact_") for bundle in result.evidence_bundles)
    assert not any(bundle.factor_id.startswith("residual_") for bundle in result.evidence_bundles)


def test_paper_shadow_gate_errors_and_matching_boundaries() -> None:
    config = ShadowConfig(
        strategy_id="shadow-boundaries",
        min_runtime_hours=1.0,
        min_effective_events=2,
        max_prediction_deviation_pct=1.0,
        max_cost_deviation_pct=1.0,
    )
    report = ShadowReport(
        config=config,
        metrics=ShadowMetrics(
            prediction_vs_simulation_mae=2.0,
            cost_estimated_vs_actual_mae=2.0,
            p0_incidents=1,
        ),
    )
    ready, reason = report.is_ready_for_testnet()
    assert ready is False
    assert all(
        token in reason
        for token in ("runtime:", "events:", "prediction_deviation:", "cost_evidence_missing", "p0_incidents:")
    )

    runner = PaperShadowRunner(ShadowConfig(strategy_id="invalid-inputs"))
    with pytest.raises(ValueError, match="decision_timestamp"):
        runner.record_tick("LONG", 1.0, decision_timestamp=math.nan)
    with pytest.raises(ValueError, match="INDEPENDENT_FORWARD_LABEL_REQUIRED"):
        runner.record_tick("LONG", 1.0, actual_direction="LONG", decision_timestamp=1.0)
    with pytest.raises(ValueError, match="SOURCE_REQUIRED"):
        runner.record_tick(
            "LONG",
            1.0,
            actual_direction="LONG",
            actual_strength=1.0,
            decision_timestamp=1.0,
            outcome_available_at=2.0,
        )
    with pytest.raises(ValueError, match="timestamp/strength is invalid"):
        runner.record_tick(
            "LONG",
            1.0,
            actual_direction="LONG",
            actual_strength=1.0,
            decision_timestamp=1.0,
            outcome_source="forward",
            outcome_available_at="bad",
        )

    runner.record_tick("LONG", 0.5, decision_timestamp=10.0)
    prediction_tick = runner.metrics.total_ticks
    with pytest.raises(ValueError, match="positive integer"):
        runner.record_forward_outcome(
            tick=0,
            actual_direction="LONG",
            actual_strength=0.5,
            outcome_source="forward",
            outcome_available_at=11.0,
        )
    with pytest.raises(ValueError, match="INDEPENDENT_FORWARD_LABEL_REQUIRED"):
        runner.record_forward_outcome(
            tick=prediction_tick,
            actual_direction=None,
            actual_strength=0.5,
            outcome_source="forward",
            outcome_available_at=11.0,
        )
    with pytest.raises(ValueError, match="SOURCE_REQUIRED"):
        runner.record_forward_outcome(
            tick=prediction_tick,
            actual_direction="LONG",
            actual_strength=0.5,
            outcome_source="",
            outcome_available_at=11.0,
        )
    with pytest.raises(ValueError, match="timestamp/strength is invalid"):
        runner.record_forward_outcome(
            tick=prediction_tick,
            actual_direction="LONG",
            actual_strength="bad",
            outcome_source="forward",
            outcome_available_at=11.0,
        )
    with pytest.raises(ValueError, match="must be finite"):
        runner.record_forward_outcome(
            tick=prediction_tick,
            actual_direction="LONG",
            actual_strength=math.inf,
            outcome_source="forward",
            outcome_available_at=11.0,
        )
    with pytest.raises(ValueError, match="unknown prediction"):
        runner.record_forward_outcome(
            tick=prediction_tick + 100,
            actual_direction="LONG",
            actual_strength=0.5,
            outcome_source="forward",
            outcome_available_at=11.0,
        )
    with pytest.raises(ValueError, match="available after"):
        runner.record_forward_outcome(
            tick=prediction_tick,
            actual_direction="LONG",
            actual_strength=0.5,
            outcome_source="forward",
            outcome_available_at=10.0,
        )

    matching = PaperMatchingEngine(seed=1)
    assert matching.match("BTCUSDT", "SELL", 1.0, 101.0, 100.0, 101.0)[0] == "QUEUED"
    assert matching.realized_cost_bps("SELL", 99.0, 99.0, 101.0) >= matching.taker_fee_bps
    matching.simulate_cancel_fill_race("race", "BTCUSDT", "BUY", 1.0)
    assert matching.race_event_count() == 1


def test_paper_shadow_report_and_testnet_gate_states() -> None:
    initial = PaperShadowRunner(ShadowConfig(strategy_id="initial"))
    initial_report = initial.generate_report()
    assert initial_report.status is ShadowStatus.INITIALIZING

    ready_runner = PaperShadowRunner(ShadowConfig(strategy_id="ready", min_runtime_hours=0.0, min_effective_events=0))
    ready_runner.record_execution_observation(5.0, 5.0)
    ready_report = ready_runner.generate_report()
    assert ready_report.status is ShadowStatus.COMPLETED

    denied_report = ShadowReport(
        config=ShadowConfig(strategy_id="denied", min_runtime_hours=1.0),
        metrics=ShadowMetrics(p0_incidents=1),
    )
    allowed, reasons = TestnetGate.check(
        denied_report,
        parity_passed=False,
        all_p0_gates_passed=False,
        mainnet_prohibited=False,
    )
    assert allowed is False
    assert "shadow_not_ready" in reasons
    assert "p0_incidents: 1" in reasons


def test_legacy_research_kernel_contract_and_manifest() -> None:
    manifest = DatasetManifest(
        dataset_id="d",
        time_start="2026-01-01",
        time_end="2026-01-02",
        point_in_time_universe=["ETH", "BTC"],
        delisted_samples=[],
        source_checksum="source",
        schema_version="1",
        code_hash="code",
    )
    assert len(manifest.compute_manifest_hash()) == 64

    class Market:
        def __init__(self, features):
            self.features = features

        def get_features(self, _symbol):
            return self.features

        def get_price(self, _symbol, _timestamp):
            return 100.0

    class Exchange:
        pass

    kernel = StrategyKernel(Market({}), Exchange())
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert kernel.run_tick("BTCUSDT", now) is None
    kernel = StrategyKernel(Market({"momentum": 1.0}), Exchange())
    tick = kernel.run_tick("BTCUSDT", now)
    assert tick and tick["price"] == 100.0
    assert kernel.verify_parity({"a": 1}, {"a": 1}) is True
    assert kernel.verify_parity({"a": 1}, {"a": 2}) is False

    legacy = PurgedWalkForward()
    assert legacy.best_fold() is None
    with pytest.raises(RuntimeError, match="LEGACY_RESEARCH_KERNEL_DISABLED"):
        legacy.run(manifest, [])
    legacy._results = [SimpleNamespace(test_sharpe=0.2), SimpleNamespace(test_sharpe=0.5)]
    assert legacy.best_fold().test_sharpe == 0.5


def test_kernel_parity_canonicalization_and_alpha_fallback() -> None:
    assert StrategyKernelContract.compute_proposal_hash(None) == ""
    assert StrategyKernelContract.compute_proposal_hash({"value": math.nan}) == ""
    assert StrategyKernelContract.compute_proposal_hash(object())
    assert StrategyKernelContract.compute_proposal_hash({})
    assert StrategyKernelContract._canonicalize(SimpleNamespace(value=1))["value"] == 1
    with pytest.raises(ValueError, match="non-finite"):
        StrategyKernelContract._canonicalize(float("inf"))


@dataclass
class _DataclassProposal:
    value: float
    timestamp: str = "ignored"


def test_kernel_parity_async_alpha_and_statuses() -> None:
    class Graph:
        async def generate(self, _context):
            return {"signal": "LONG"}

    async def run() -> None:
        kernel = ParityKernel(KernelMode.PAPER.value)
        kernel.set_alpha_graph(Graph())
        result = await kernel.evaluate({"instrument_id": "BTCUSDT", "features": {"x": 1}})
        assert result and result["kernel"] == "alpha_graph"
        assert await ParityKernel().evaluate({}) is None

    asyncio.run(run())
    proposal = _DataclassProposal(1.0)
    assert StrategyKernelContract.compute_proposal_hash(proposal)
    passed, parity = parity_check(proposal, proposal)
    assert passed is True and parity.status is ParityStatus.MATCH
    passed, parity = parity_check(proposal, {"value": 2.0})
    assert passed is False and parity.status is ParityStatus.DISCREPANCY
    passed, parity = parity_check(None, proposal)
    assert passed is False and parity.status is ParityStatus.NOT_RUN


def test_fast_screen_and_legacy_cost_model_edges() -> None:
    screen = FastScreen(FastScreenConfig(min_sample_count=2, min_effective_samples=1))
    failed = screen.screen([1.0, 2.0], dq_failure_count=4)
    assert failed.passed is False
    assert any("data_quality" in reason for reason in failed.failure_reasons)
    results = screen.screen_batch(
        [
            {"factor_values": [1.0, 2.0], "expression_hash": "same"},
            {"factor_values": [1.0, 2.0], "expression_hash": "same"},
        ]
    )
    assert results[0].passed is True
    assert any("duplicate" in reason for reason in results[1].failure_reasons)
    assert screen.screen([1.0, 2.0], data_quality=DataQualityTier.FAIL).passed is False

    model = LegacyCostModel()
    assert math.isinf(model.estimate_impact(1.0, 0.0, 0.1))
    estimate = model.estimate_order(
        VenueInstrument(venue_id=VenueId("BINANCE"), instrument_id=InstrumentId("BTCUSDT")),
        Quantity(amount="2"),
        Price(amount="100"),
        OrderSide.BUY,
        urgency=1.0,
    )
    assert estimate.net_alpha_after_costs(100.0) < 100.0
    model.set_fee_tier(VenueId("BINANCE"), "vip", 1.0, 2.0)
    maker = model.estimate(InstrumentId("BTCUSDT"), VenueId("BINANCE"), 100.0, 1_000_000.0, 2.0, 0.01, False)
    assert maker.fee_rate_maker == 1.0


def test_multiple_testing_remaining_evidence_branches() -> None:
    assert deflated_sharpe_ratio(1.0, 0, sample_length=0)["p_value"] == 1.0
    assert compute_pbo([1.0], [1.0]).interpretation == "insufficient_data"

    rng = random.Random(5)
    in_sample = [rng.random() for _ in range(16)]
    out_of_sample = [rng.random() for _ in range(16)]
    assert compute_pbo(in_sample, out_of_sample).interpretation == "high_overfitting_risk"

    missing = evaluate_multiple_testing([0.01], observed_sharpe=0.0, n_trials=0, candidate_index=4)
    assert missing.verdict == "NOT_VERIFIABLE"
    failing = evaluate_multiple_testing(
        [0.9] * 8,
        observed_sharpe=-1.0,
        n_trials=8,
        in_sample_sharpes=list(range(8)),
        out_of_sample_sharpes=list(reversed(range(8))),
        candidate_index=0,
    )
    assert failing.verdict in {"FAIL", "NOT_VERIFIABLE"}


def test_runner_scalar_helpers_boundaries() -> None:
    assert _compute_ic([], []) == 0.0
    assert _compute_ic([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) == 0.0
    assert _compute_sharpe([]) == 0.0
    assert _compute_sharpe([1.0, 1.0]) == 0.0
    assert _compute_ic_np(np.array([1.0, 2.0]), np.array([1.0, 2.0])) == 0.0
    assert _compute_ic_np(np.array([1.0, 1.0, 1.0]), np.array([1.0, 2.0, 3.0])) == 0.0
    assert _compute_sharpe_np(np.array([1.0])) == 0.0
    assert _compute_sharpe_np(np.array([1.0, 1.0])) == 0.0
    assert _timeframe_to_hours("") == 0.0
    assert _timeframe_to_hours("invalid") == 0.0
    assert _correlation_p_value(math.nan, 20) == 1.0
    assert _is_finite(None) is False
    assert _is_finite(True) is False
    assert _is_finite("bad") is False
    assert _estimate_adv_from_price_data([], "BTCUSDT") == 0.0
    assert _estimate_adv_from_price_data([{"symbol": "ETHUSDT", "close": 1, "volume": 1}], "BTCUSDT") == 1.0


def test_partial_fill_helper_and_attempt_place_fail_closed() -> None:
    from beidou_certification.g5_scenarios.engine.partial_fill import (
        PartialFillScenario,
        _min_qty_and_step,
        _require_ok,
    )
    from beidou_exchange.core.error_taxonomy import Result

    with pytest.raises(RuntimeError, match="exchange failed"):
        _require_ok(Result.failure(message="exchange failed", http_status=500), "probe")
    with pytest.raises(ValueError, match="LOT_SIZE filter missing"):
        _min_qty_and_step("BTCUSDT", {"symbols": [{"symbol": "BTCUSDT", "filters": []}]})
    with pytest.raises(ValueError, match="not in exchange info"):
        _min_qty_and_step("BTCUSDT", {"symbols": []})

    from tests.unit.test_g5_scenarios.test_engine_fill_race import _FakeClient, _noop_sleep

    async def exercise() -> None:
        scenario = PartialFillScenario(sleep=_noop_sleep)
        for depth in ({"bids": [], "asks": []}, {"bids": [["1", "1"]], "asks": [["0.01", "1"]]}):
            client = _FakeClient(depth_map={"INJUSDT": depth})
            steps: list[dict] = []
            result = await scenario._attempt_place(
                client,
                "INJUSDT",
                {"INJUSDT": (Decimal("0.1"), Decimal("0.1"))},
                steps,
            )
            assert result is None
        client = _FakeClient(order_error_times=1, order_error_msg="Exceeded the maximum allowable position")
        steps = []
        result = await scenario._attempt_place(
            client,
            "INJUSDT",
            {"INJUSDT": (Decimal("0.1"), Decimal("0.1"))},
            steps,
        )
        assert result is None
        assert any(step.get("action") == "candidate_ineligible" for step in steps)

    asyncio.run(exercise())


def test_partial_fill_runtime_cleanup_and_guard_failures(tmp_path) -> None:
    from beidou_certification.g5_scenarios.engine import partial_fill as partial_module
    from tests.unit.test_g5_scenarios.test_engine_fill_race import _ctx, _FakeClient, _noop_sleep

    async def exercise() -> None:
        guard_client = _FakeClient()
        original_guard = partial_module.terminal_monotonic_guard
        partial_module.terminal_monotonic_guard = lambda *_args: "NEW"
        try:
            result = await partial_module.PartialFillScenario(sleep=_noop_sleep).run(_ctx(guard_client, tmp_path))
        finally:
            partial_module.terminal_monotonic_guard = original_guard
        assert result.status.value == "FAIL"
        assert result.error_type == "GUARD_MISMATCH"

        close_failure_client = _FakeClient()
        close_failure_client.fail_close = True
        result = await partial_module.PartialFillScenario(sleep=_noop_sleep).run(
            _ctx(close_failure_client, tmp_path / "close-failure")
        )
        assert result.error_type == "CLOSE_FAILED"

        cleanup_client = _FakeClient(order_statuses=["NEW"], new_executed_qty="0")

        async def cancel_error(*_args, **_kwargs):
            raise RuntimeError("cancel cleanup failed")

        cleanup_client.cancel_order = cancel_error
        result = await partial_module.PartialFillScenario(sleep=_noop_sleep).run(
            _ctx(cleanup_client, tmp_path / "cancel-cleanup")
        )
        assert result.status.value == "NOT_VERIFIABLE"
        assert any(
            step.get("ok") is False for step in result.evidence["steps"] if step.get("action") == "cleanup_cancel"
        )

        close_cleanup_client = _FakeClient(order_statuses=["NEW"], new_executed_qty="1")
        close_cleanup_client.fail_close = True
        close_cleanup_client.cancel_order = cancel_error
        result = await partial_module.PartialFillScenario(sleep=_noop_sleep).run(
            _ctx(close_cleanup_client, tmp_path / "close-cleanup")
        )
        assert result.status.value == "NOT_VERIFIABLE"
        assert any(
            step.get("phase") == "cleanup" and step.get("action") == "close_failed" for step in result.evidence["steps"]
        )

    asyncio.run(exercise())


def test_partial_fill_exchange_info_skips_invalid_candidates(tmp_path) -> None:
    from beidou_certification.g5_scenarios.engine.partial_fill import PartialFillScenario
    from tests.unit.test_g5_scenarios.test_engine_fill_race import _ctx, _FakeClient, _noop_sleep

    class Client(_FakeClient):
        async def get_exchange_info(self, symbol=None):
            return type(await super().get_exchange_info(symbol)).success(
                {
                    "symbols": [
                        {
                            "symbol": "BROKENUSDT",
                            "status": "TRADING",
                            "contractType": "PERPETUAL",
                            "underlyingType": "COIN",
                            "filters": [],
                        },
                        {
                            "symbol": "TRADIFIUSDT",
                            "status": "TRADING",
                            "contractType": "TRADIFI_PERPETUAL",
                            "underlyingType": "EQUITY",
                            "filters": [],
                        },
                    ]
                }
            )

    result = asyncio.run(PartialFillScenario(sleep=_noop_sleep).run(_ctx(Client(), tmp_path)))
    assert result.status.value == "NOT_VERIFIABLE"


def test_unattended_window_lifecycle_and_state_boundaries(tmp_path, monkeypatch) -> None:
    import beidou_certification.unattended as unattended_module

    certification_window = unattended_module.CertificationWindow
    incident_record = unattended_module.IncidentRecord
    incident_severity = unattended_module.IncidentSeverity
    sli_category = unattended_module.SLICategory
    sli_sample = unattended_module.SLISample
    unattended_certification = unattended_module.UnattendedCertification

    assert certification_window("no-start", "v", started_at=None).elapsed_days() == 0.0
    cert = unattended_certification(str(tmp_path / "windows"))
    with pytest.raises(ValueError, match="Invalid G7 window_id"):
        cert.create_window("v", window_id="bad id")

    collision_id = "g7-20260101-000000-000000"
    (tmp_path / "windows" / f"{collision_id}-state.json").write_text("{}", encoding="utf-8")
    real_datetime = unattended_module.datetime

    class SequencedDateTime(real_datetime):
        calls = 0

        @classmethod
        def now(cls, tz=None):
            cls.calls += 1
            if cls.calls == 1:
                return real_datetime(2026, 1, 1, tzinfo=tz)
            return real_datetime(2026, 1, 2, tzinfo=tz)

    monkeypatch.setattr(unattended_module, "datetime", SequencedDateTime)
    collision_window = cert.create_window("v", window_id=None)
    assert collision_window.window_id != collision_id
    monkeypatch.setattr(unattended_module, "datetime", real_datetime)

    with pytest.raises(ValueError, match="already exists"):
        cert.create_window("v", window_id=collision_window.window_id)
    with pytest.raises(ValueError, match="not found"):
        cert.start_window("missing")
    with pytest.raises(ValueError, match="not found"):
        cert.reset_window("missing", "test")
    cert.pause_window("missing", "ignored")

    window = cert.create_window("v", duration_days=0, window_id="lifecycle")
    cert.start_window(window.window_id)
    cert.pause_window(window.window_id, "maintenance")
    cert.open_incident(
        window.window_id,
        incident_record("i1", incident_severity.P1, "minor", "description"),
    )
    cert.close_incident(window.window_id, "i1")
    assert cert.generate_daily_report("missing") is None
    first_report = cert.generate_daily_report(window.window_id)
    assert first_report is not None
    assert cert.generate_daily_report(window.window_id) is first_report
    assert cert.evaluate("missing")["status"] == "FAIL"

    incomplete = cert.create_window("v", duration_days=1, window_id="incomplete")
    assert cert.evaluate(incomplete.window_id)["status"] == "NOT_VERIFIABLE"
    incomplete.duration_days = 0
    incomplete.started_at = datetime.now(timezone.utc) - timedelta(days=1)
    incomplete.minimum_sli_samples = 0
    incomplete.sli_samples = [
        sli_sample(sli_category.DATA_QUALITY, 1.0, 0.9, True, timestamp=datetime.now(timezone.utc))
    ]
    assert cert.evaluate(incomplete.window_id)["status"] == "NOT_VERIFIABLE"

    assert cert.fast_forward("missing")["status"] == "FAIL"
    simulated = cert.create_window("v", duration_days=0, window_id="simulated")
    monkeypatch.setattr(cert, "evaluate", lambda _window_id: {"status": "PASS"})
    assert cert.fast_forward(simulated.window_id, duration_days=0)["status"] == "PASS"

    incident = incident_record("hash-i", incident_severity.P2, "title", "details")
    incident.closed_at = datetime.now(timezone.utc)
    incident.resolved = True
    hashed = cert._compute_evidence_hash(
        certification_window("hash-window", "v", incidents=[incident], started_at=datetime.now(timezone.utc))
    )
    assert len(hashed) == 64

    with pytest.raises(ValueError, match="ISO timestamp"):
        cert._parse_datetime("not-a-date", "field")
    with pytest.raises(ValueError, match="window state"):
        cert._deserialize_window([])
    with pytest.raises(ValueError, match="ISO timestamp"):
        cert._parse_datetime(None, "field")


def test_unattended_loads_and_reports_corrupt_duplicate_and_legacy_state(tmp_path) -> None:
    import json

    import beidou_certification.unattended as unattended_module

    evidence = tmp_path / "state"
    evidence.mkdir()
    (evidence / "bad-state.json").write_text("{bad", encoding="utf-8")
    legacy = {
        "window_id": "legacy",
        "plan_version": "v",
        "status": "RUNNING",
        "started_at": "2026-01-01T00:00:00+00:00",
        "state_schema_version": 1,
        "duration_days": 30,
        "reset_count": 0,
    }
    (evidence / "legacy-state.json").write_text(json.dumps(legacy), encoding="utf-8")
    (evidence / "legacy-copy-state.json").write_text(json.dumps({**legacy, "window_id": "legacy"}), encoding="utf-8")
    cert = unattended_module.UnattendedCertification(str(evidence))
    assert cert.state_load_errors
    assert any("bad-state.json" in item for item in cert.state_load_errors)
    assert any("duplicate window_id" in item for item in cert.state_load_errors)
    assert cert.get_window("legacy") is not None
    assert cert.get_window("legacy").evidence_state_complete is False


def test_process_restart_helpers_and_unreachable_poll_boundaries(monkeypatch, tmp_path) -> None:
    import urllib.request

    import beidou_certification.g5_scenarios.restart.process_restart as process_module
    from beidou_certification.g5_scenarios.base import NotionalExceededError, NotionalLedger, ScenarioContext
    from beidou_certification.g5_scenarios.restart.process_restart import (
        ProcessRestartScenario,
        StatusUnreachableError,
        _process_comms,
        engine_pid_os,
        fetch_status_http,
    )

    with pytest.raises(StatusUnreachableError):
        monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("down")))
        fetch_status_http()

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return self.payload

    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: Response(b"not-json"))
    with pytest.raises(StatusUnreachableError, match="decode"):
        fetch_status_http()
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: Response(b"[]"))
    with pytest.raises(StatusUnreachableError, match="not an object"):
        fetch_status_http()
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: Response(b'{"ok": true}'))
    assert fetch_status_http() == {"ok": True}

    assert _process_comms([]) == {}
    monkeypatch.setattr(
        process_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=""),
    )
    assert engine_pid_os() is None

    class Clock:
        def __init__(self):
            self.value = 0.0

        def now(self):
            return self.value

        async def sleep(self, _seconds):
            self.value = 1.0

    clock = Clock()
    status_calls = iter([StatusUnreachableError("down"), {"trading_ready": True}])

    def next_status(values):
        item = next(values)
        if isinstance(item, Exception):
            raise item
        return item

    scenario = ProcessRestartScenario(
        now=clock.now,
        sleep=clock.sleep,
        engine_pid=lambda: 2,
        fetch_status=lambda: next_status(status_calls),
        process_deadline=10.0,
        ready_deadline=10.0,
    )
    steps: list[dict] = []
    assert asyncio.run(scenario._wait_for_new_process(1, since=0.0, steps=steps)) == 2

    ready_clock = Clock()
    ready_status = iter(
        [StatusUnreachableError("down"), {"trading_ready": True, "last_reconciliation": {"status": "MATCHED"}}]
    )
    ready_scenario = ProcessRestartScenario(
        now=ready_clock.now,
        sleep=ready_clock.sleep,
        fetch_status=lambda: next_status(ready_status),
        process_deadline=10.0,
        ready_deadline=10.0,
    )
    assert asyncio.run(ready_scenario._wait_for_ready(since=0.0, steps=[]))["trading_ready"] is True

    monkeypatch.setattr(process_module, "fetch_status_http", lambda: {"ok": True})
    assert ProcessRestartScenario()._fetch_status_http() == {"ok": True}
    monkeypatch.setattr(process_module, "engine_pid_os", lambda: 77)
    assert ProcessRestartScenario()._engine_pid_os() == 77
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(process_module.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    ProcessRestartScenario._sigkill_os(7)
    assert killed and killed[0][0] == 7
    ran: list[list[str]] = []
    monkeypatch.setattr(process_module.subprocess, "run", lambda cmd, **_kwargs: ran.append(cmd))
    ProcessRestartScenario._kickstart_os()
    assert ran and ran[0][-1] == "gui/501/com.beidou.autopilot"

    ctx = ScenarioContext(
        client=None,
        ledger=NotionalLedger(1000.0),
        evidence_dir=tmp_path,
        symbol="BTCUSDT",
        dry_run=False,
    )

    def raise_cap():
        raise NotionalExceededError("process_restart", 1001.0, 1000.0)

    scenario = ProcessRestartScenario(
        fetch_status=lambda: {"trading_ready": True, "last_reconciliation": {"status": "MATCHED"}},
        engine_pid=raise_cap,
        connect=lambda _dsn: SimpleNamespace(
            execute=lambda *_args, **_kwargs: SimpleNamespace(fetchone=lambda: ("{}",), fetchall=lambda: []),
            close=lambda: None,
        ),
    )
    with pytest.raises(NotionalExceededError):
        asyncio.run(scenario.run(ctx))


def test_incident_manager_storm_transitions_and_allowlist() -> None:
    from beidou_observability.monitoring.contracts import CheckSeverity, IncidentStatus, check_invariant
    from beidou_observability.monitoring.incident_manager import IncidentManager
    from beidou_observability.monitoring.storm_detector import StormDetector

    manager = IncidentManager(storm_detector=StormDetector(unique_dedupe_threshold=2))
    first, created = manager.create_or_dedupe("same", check_id="check", severity=CheckSeverity.P0)
    assert created is True
    duplicate, created = manager.create_or_dedupe("same", check_id="check", severity=CheckSeverity.P0)
    assert duplicate is first and created is False
    second, created = manager.create_or_dedupe("other", check_id="check", severity=CheckSeverity.P1)
    assert created is True
    assert second.is_systemic is True and second.parent_incident_id
    assert manager.links

    assert manager.transition(first.incident_id, IncidentStatus.CONFIRMED) is not None
    assert manager.transition(first.incident_id, IncidentStatus.MITIGATING) is not None
    assert manager.transition(first.incident_id, IncidentStatus.VERIFYING) is not None
    assert manager.transition(first.incident_id, IncidentStatus.RESOLVED) is not None
    assert manager.transition("missing", IncidentStatus.CONFIRMED) is None
    assert manager.transition(second.incident_id, IncidentStatus.LOCKED) is None
    assert manager.is_remediation_allowed("FEED_RECONNECT") == (True, "AUTO")
    assert manager.is_remediation_allowed("REINITIALIZE_NEARLINE_FROM_VALIDATED_CHECKPOINT") == (True, "CONDITIONAL")
    assert manager.is_remediation_allowed("INCREASE_LEVERAGE") == (False, "NEVER_AUTO")
    assert manager.is_remediation_allowed("unknown") == (False, "NOT_IN_ALLOWLIST")
    assert check_invariant("INV-001", True) == (True, "INV-001: OK")
    assert check_invariant("INV-001", False, "missing protection")[0] is False


def test_monitoring_contract_position_keys_evidence_and_clock_edges(monkeypatch) -> None:
    from beidou_observability.monitoring.clock_integrity import ClockIntegrity
    from beidou_observability.monitoring.contracts import (
        AccountPositionMode,
        CheckSeverity,
        PositionKey,
        PositionSide,
        compute_evidence_hash,
    )
    from beidou_observability.monitoring.fact_collector import FactCollector

    one_way = PositionKey.build(
        venue="BINANCE",
        account_id="default",
        symbol="BTCUSDT",
        mode=AccountPositionMode.ONE_WAY,
    )
    assert one_way.position_side is PositionSide.BOTH
    with pytest.raises(ValueError, match="HEDGE requires"):
        PositionKey.build(
            venue="BINANCE",
            account_id="default",
            symbol="BTCUSDT",
            mode=AccountPositionMode.HEDGE,
            position_side=PositionSide.BOTH,
        )
    assert (
        PositionKey.build(
            venue="BINANCE",
            account_id="default",
            symbol="BTCUSDT",
            mode=AccountPositionMode.HEDGE,
            position_side=PositionSide.LONG,
        ).position_side
        is PositionSide.LONG
    )
    assert len(compute_evidence_hash({"key": "value"})) == 64

    collector = FactCollector()
    assert collector.get_required("missing").error
    assert collector.has_fresh("missing") is False
    collector.record_derived("derived", 1.0)
    assert collector.get_authoritative("derived") is None
    assert collector.has_fresh("derived") is True
    collector.record_error("risk", "bad", "failed")
    assert collector.has_fresh("bad") is False

    clock = ClockIntegrity()
    for index in range(22):
        clock.record_exchange_time(index * 1000)
    assert len(clock._samples) == 20
    assert clock.monotonic_elapsed(0.0) >= 0.0
    clock._samples = [6000.0]
    assert clock.check().severity is CheckSeverity.P0
    clock._samples = [3000.0]
    assert clock.check().severity is CheckSeverity.P1


def test_control_api_registry_routes_and_optional_fastapi_import(monkeypatch) -> None:
    import builtins
    import sys
    from types import ModuleType

    from beidou_control.api import ControlPlaneAPI, ReadinessResponse, create_app
    from beidou_research.factors.factor import FactorDefinition, FactorRecord
    from beidou_shared.types import SchemaVersion

    api = ControlPlaneAPI()
    assert api.list_factors() == []
    assert api.promote_factor("missing", "ACTIVE")["error"] == "factor_registry not wired"
    assert api.promote_all_to_active()["error"] == "factor_registry not wired"

    definition = FactorDefinition(
        factor_id="api-factor",
        name="api-factor",
        version=SchemaVersion("1.0.0"),
        description="coverage fixture",
        author="test",
        category="momentum",
        universe=frozenset(),
        instrument_types=frozenset({"perpetual"}),
        economic_rationale="coverage",
        lookback_period="1h",
        rebalance_interval="1h",
    )
    record = FactorRecord(definition=definition)
    api.wire_factor_registry(
        SimpleNamespace(
            _factors={"api-factor": record},
            get=lambda factor_id: record if factor_id == "api-factor" else None,
        )
    )
    promotion = api.promote_factor("api-factor", "GENERATED")
    assert promotion["factor_id"] == "api-factor"
    assert "decision_id" in promotion
    assert api.list_factors()[0]["factor_id"] == "api-factor"

    class _HTTPException(Exception):
        def __init__(self, *, status_code: int, detail: str) -> None:
            super().__init__(detail)
            self.status_code = status_code

    class _FakeApp:
        def __init__(self, **_kwargs: object) -> None:
            self.routes: dict[tuple[str, str], object] = {}

        def _decorator(self, method: str, path: str):
            def decorate(fn):
                self.routes[(method, path)] = fn
                return fn

            return decorate

        def get(self, path: str):
            return self._decorator("GET", path)

        def post(self, path: str):
            return self._decorator("POST", path)

    fastapi = ModuleType("fastapi")
    fastapi.FastAPI = _FakeApp  # type: ignore[attr-defined]
    fastapi.HTTPException = _HTTPException  # type: ignore[attr-defined]
    fastapi.Request = object  # type: ignore[attr-defined]
    cors = ModuleType("fastapi.middleware.cors")
    cors.CORSMiddleware = object  # type: ignore[attr-defined]
    middleware = ModuleType("fastapi.middleware")
    middleware.cors = cors  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastapi", fastapi)
    monkeypatch.setitem(sys.modules, "fastapi.middleware", middleware)
    monkeypatch.setitem(sys.modules, "fastapi.middleware.cors", cors)

    api.readiness = lambda _checks=None: ReadinessResponse(True, {}, {})  # type: ignore[method-assign]
    api.wire_control_plane(SimpleNamespace(execute_action=lambda _action: None))
    app = create_app(api)
    assert app is not None
    assert asyncio.run(app.routes[("GET", "/ready")]())["ready"] is True
    assert asyncio.run(app.routes[("GET", "/trading-eligibility")]())["eligible"] is False
    assert asyncio.run(app.routes[("GET", "/factors")]())[0]["factor_id"] == "api-factor"
    assert asyncio.run(app.routes[("POST", "/factors/promote-all")]())["success"] is False
    assert asyncio.run(app.routes[("POST", "/factors/promote/{factor_id}")]("missing"))["success"] is False
    assert asyncio.run(app.routes[("POST", "/emergency/{action}")]("NO_NEW_RISK", object()))["success"] is True

    real_import = builtins.__import__

    def fail_fastapi(name, *args, **kwargs):
        if name == "fastapi":
            raise ImportError("optional dependency absent")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_fastapi)
    assert create_app(ControlPlaneAPI()) is None


def test_exchange_error_taxonomy_alias_and_business_code_boundaries() -> None:
    from beidou_exchange.core.error_taxonomy import Result, classify_http_error
    from beidou_shared.errors import ErrorCategory

    failed = Result.fail(ErrorCategory.ORDER_REJECTED, "bad", code=-1102, source="venue")
    assert failed.is_success() is False
    assert failed.error is not None and failed.error.raw == {"code": -1102}
    assert failed.source == "venue"
    for code, category in {
        -4015: ErrorCategory.RATE_LIMIT,
        -2010: ErrorCategory.INSUFFICIENT_BALANCE,
        -2019: ErrorCategory.INSUFFICIENT_MARGIN,
        -2011: ErrorCategory.ORDER_REJECTED,
        -2022: ErrorCategory.POSITION_LIMIT,
        -4141: ErrorCategory.ORDER_REJECTED,
        -1111: ErrorCategory.ORDER_REJECTED,
        -1102: ErrorCategory.ORDER_REJECTED,
        -9999: ErrorCategory.UNKNOWN,
    }.items():
        assert classify_http_error(200, binance_code=code)[0] is category
    assert classify_http_error(418)[0] is ErrorCategory.RATE_LIMIT
    assert classify_http_error(400)[0] is ErrorCategory.ORDER_REJECTED
    assert classify_http_error(200)[0] is ErrorCategory.UNKNOWN


def test_preflight_authority_warn_port_dirty_and_policy_edges(monkeypatch, tmp_path: Path) -> None:
    import socket
    import sys
    from types import ModuleType

    from beidou_launcher.models import CheckSeverity, CheckStatus

    broken_postgres = ModuleType("beidou_infra.postgres_store")
    monkeypatch.setitem(sys.modules, "beidou_infra.postgres_store", broken_postgres)
    ok, message, _evidence = __import__("beidou_launcher.preflight", fromlist=["_"])._postgres_authority_probe(
        tmp_path, "postgresql://redacted/db"
    )
    assert ok is False and message == "PostgreSQL runtime contract unavailable"

    from beidou_launcher import preflight

    result = preflight._result(
        "warn",
        "warn",
        False,
        CheckSeverity.P2,
        "pass",
        "warn message",
        warn=True,
    )
    assert result.status is CheckStatus.WARN

    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("0.0.0.0", 0))
    occupied.listen(1)
    try:
        available, _ = preflight._port_available(occupied.getsockname()[1])
        assert available is False
    finally:
        occupied.close()

    root = Path(__file__).resolve().parents[2]
    monkeypatch.setattr(preflight, "current_commit", lambda _root: "a" * 40)
    monkeypatch.setattr(preflight, "_git_worktree_state", lambda _root: (True, [" M source.py"], ""))
    monkeypatch.setattr(preflight, "_port_available", lambda _port: (True, "available"))
    monkeypatch.setattr(preflight, "check_package_imports", lambda: [])
    monkeypatch.setenv("BEIDOU_BINANCE_API_KEY", "testnet-api-key")
    monkeypatch.setenv("BEIDOU_BINANCE_API_SECRET", "testnet-api-secret")
    monkeypatch.setenv("BEIDOU_SIGNING_KEY", "testnet-signing-key")

    from beidou_policy.loader import PolicyLoader

    monkeypatch.setattr(
        PolicyLoader,
        "load",
        lambda self, policy_id: SimpleNamespace(
            validate_risk_parameters=lambda: (policy_id == "risk_parameters", "complete")
        ),
    )
    checks, _settings = preflight._run_preflight(root, "testnet", 19105, require_g5_certificate=False)
    assert next(item for item in checks if item.check_id == "preflight.git_worktree").status is CheckStatus.FAIL
    assert next(item for item in checks if item.check_id == "preflight.signed_policy").status is CheckStatus.PASS

    monkeypatch.setattr(PolicyLoader, "load", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("policy")))
    checks, _settings = preflight._run_preflight(root, "testnet", 19106, require_g5_certificate=False)
    assert next(item for item in checks if item.check_id == "preflight.signed_policy").status is CheckStatus.FAIL


@pytest.mark.asyncio
async def test_runtime_probe_and_runtime_check_exception_boundaries(monkeypatch) -> None:
    import beidou_launcher.runtime as runtime
    from beidou_launcher.models import CheckStatus

    class Feed:
        async def async_get_kline_features(self, _symbol):
            return {"close": 100.0}

        async def async_update_features(self, _symbol):
            return {}

    incomplete = SimpleNamespace(_feed=Feed(), _strategy_kernel=None)
    result = await runtime.run_read_only_algorithm_probe(incomplete, ["BTCUSDT"])
    assert result["ok"] is False and "ticker/orderbook" in result["error"]

    class RaisingShadow:
        def __init__(self, _registry):
            raise RuntimeError("shadow")

    import beidou_strategy.alpha.pipeline as pipeline

    original_shadow = pipeline.AlphaV3ShadowEngine
    pipeline.AlphaV3ShadowEngine = RaisingShadow  # type: ignore[assignment]
    try:
        engine = SimpleNamespace(
            _feed=SimpleNamespace(
                async_get_kline_features=lambda _symbol: asyncio.sleep(0, result={"close": 100.0}),
                async_update_features=lambda _symbol: asyncio.sleep(0, result={"price": 100.0}),
            ),
            _strategy_kernel=SimpleNamespace(
                evaluate=lambda _context: asyncio.sleep(
                    0, result={"kernel": "typed_graph", "proposal": None, "graph_hash": "graph"}
                ),
                compute_proposal_hash=lambda _proposal: "hash",
                _typed_graph=SimpleNamespace(
                    topological_order=lambda: ["node"],
                    _nodes={"node": SimpleNamespace(node_type=SimpleNamespace(value="ENTRY"))},
                ),
            ),
            _estimate_market_state=lambda features: features,
        )
        result = await runtime.run_read_only_algorithm_probe(engine, ["BTCUSDT"])
    finally:
        pipeline.AlphaV3ShadowEngine = original_shadow
    assert result["ok"] is True
    assert result["v3_shadow_status"] == "NOT_VERIFIABLE"

    no_hash_engine = SimpleNamespace(
        _feed=SimpleNamespace(
            async_get_kline_features=lambda _symbol: asyncio.sleep(0, result={"close": 100.0}),
            async_update_features=lambda _symbol: asyncio.sleep(0, result={"price": 100.0}),
        ),
        _strategy_kernel=SimpleNamespace(
            evaluate=lambda _context: asyncio.sleep(0, result={"kernel": "typed_graph", "graph_hash": "graph"}),
            _typed_graph=SimpleNamespace(topological_order=lambda: ["node"], _nodes={"node": object()}),
        ),
    )
    result = await runtime.run_read_only_algorithm_probe(no_hash_engine, ["BTCUSDT"])
    assert "compute_proposal_hash" in result["error"]

    monkeypatch.setattr(runtime, "inspect_engine_wiring", lambda _engine, _mode: [])
    base = SimpleNamespace(
        _lifecycle=SimpleNamespace(state=SimpleNamespace(value="ACTIVE")),
        _control=SimpleNamespace(get_status=lambda: "RESUME"),
        _feed=SimpleNamespace(
            is_healthy=lambda: (_ for _ in ()).throw(RuntimeError("feed")),
            _last_ticker={},
            _last_orderbook={},
        ),
        _tick_count=1,
        _health=SimpleNamespace(_thread=SimpleNamespace(is_alive=lambda: True)),
        _last_realtime_mono=__import__("time").monotonic(),
        _running=True,
        _last_reconciliation_result=SimpleNamespace(
            status=SimpleNamespace(value="MATCHED"),
            matched=True,
            checked_at=datetime.fromisoformat("2026-01-01"),
            differences=[],
        ),
        _can_write=True,
        _user_stream_readiness=lambda: (_ for _ in ()).throw(RuntimeError("stream")),
        _last_nearline=__import__("time").time(),
        _error_count=0,
        _alerts=SimpleNamespace(get_active_incidents=lambda: []),
    )
    checks, _ = runtime.collect_runtime_checks(
        engine=base,
        mode="testnet",
        port=9090,
        resume_authorized=True,
        algorithm_probe={"ok": True},
        last_error_count=0,
    )
    assert next(item for item in checks if item.check_id == "runtime.health.market_data").status is CheckStatus.FAIL
    assert next(item for item in checks if item.check_id == "runtime.safety.user_stream").status is CheckStatus.FAIL

    base._last_reconciliation_result.checked_at = object()
    checks, _ = runtime.collect_runtime_checks(
        engine=base,
        mode="testnet",
        port=9090,
        resume_authorized=True,
        algorithm_probe={"ok": True},
        last_error_count=0,
    )
    assert (
        next(item for item in checks if item.check_id == "runtime.safety.reconciliation_authority").status
        is CheckStatus.FAIL
    )


def test_reporting_incidents_attribution_getters_and_reference_validation() -> None:
    from beidou_reporting.engine import (
        EvidenceTier,
        Report,
        ReportGenerator,
        ReportReference,
        ReportType,
        ReportValidator,
    )
    from beidou_reporting.pnl_attribution import AttributionRecord, DecisionTrace
    from beidou_shared.types import CorrelationId, SchemaVersion

    generator = ReportGenerator()
    incident = generator.generate_daily_report(
        date=datetime(2026, 1, 15, tzinfo=timezone.utc),
        strategies=[],
        account_id="account",  # type: ignore[arg-type]
        venue_id="BINANCE",  # type: ignore[arg-type]
        active_incidents=["inc-1"],
    )
    active = next(section for section in incident.sections if section.title == "Active Incidents")
    assert active.status is EvidenceTier.VERIFIED
    assert active.entries[0].key == "incident_inc-1"

    with pytest.raises(TypeError, match="AttributionRecord"):
        generator.generate_pnl_attribution_report("2026-01", object())
    attribution = generator.generate_pnl_attribution_report(
        "2026-01", AttributionRecord(decision_id="decision-1", correlation_id=CorrelationId("corr-1"))
    )
    assert generator.generate_attribution_report("2026-01", AttributionRecord(decision_id="decision-2"))
    assert generator.get_pnl_attribution_reports()
    assert attribution.overall_tier() is EvidenceTier.NOT_VERIFIABLE

    with pytest.raises(TypeError, match="DecisionTrace"):
        generator.record_decision_trace(object())
    trace = DecisionTrace.from_probe({"features": {}, "state": {}, "proposal_hash": "p"})
    generator.record_decision_trace(trace)
    assert generator.get_decision_traces() == [trace]
    assert generator.get_weekly_reports() == []
    assert generator.get_incident_reports() == []

    unknown = Report(report_id="unknown", report_type=ReportType.DAILY, title="unknown")
    unknown.references.append(
        ReportReference(
            source="ledger",
            query_id="q",
            schema_version=SchemaVersion("unknown"),
            timestamp=datetime.now(timezone.utc),
            correlation_id=None,
        )
    )
    valid, issues = ReportValidator.validate_references(unknown)
    assert valid is False
    assert any("Unknown schema" in issue for issue in issues)
    assert any("correlation_id" in issue for issue in issues)


def test_stable_client_order_id_failure_idempotency_and_cleanup_boundaries(monkeypatch, tmp_path: Path) -> None:
    import beidou_certification.g5_scenarios.protocol.stable_client_order_id as stable
    from beidou_certification.g5_scenarios.base import NotionalExceededError
    from beidou_exchange.core.error_taxonomy import Result
    from tests.unit.test_g5_scenarios.test_protocol_basic import FakeClient, _ctx

    assert stable.assert_no_duplicate({}, []) == (False, "unrecognized_response")

    first_failure = FakeClient()
    first_failure.create_order_results = [Result.failure("first order failed", raw={"code": -2010})]
    failed = asyncio.run(stable.StableClientOrderIdScenario().run(_ctx(first_failure, tmp_path)))
    assert failed.status.value == "FAIL"
    assert failed.error_type == "RuntimeError"

    class NewOrderClient(FakeClient):
        async def get_open_orders(self, symbol=None):
            return Result.ok([])

    duplicate = NewOrderClient()
    duplicate.create_order_results = [Result.ok({"orderId": 1}), Result.ok({"orderId": 2})]
    result = asyncio.run(stable.StableClientOrderIdScenario().run(_ctx(duplicate, tmp_path)))
    assert result.status.value == "FAIL"
    second = next(step for step in result.evidence["steps"] if step["action"] == "second_order")
    assert second["verdict"] == "new_order_created"
    assert len([call for call in duplicate.calls if call[0] == "cancel_order"]) == 2

    class CancelFailureClient(FakeClient):
        async def cancel_order(self, symbol, order_id):
            raise RuntimeError("cancel unavailable")

    cancel_failed = CancelFailureClient()
    cancel_failed.create_order_results = [
        Result.ok({"orderId": 1}),
        Result.failure("duplicate", raw={"code": -4015}),
    ]
    cleanup = asyncio.run(stable.StableClientOrderIdScenario().run(_ctx(cancel_failed, tmp_path)))
    assert cleanup.status.value == "PASS"
    assert any(step.get("ok") is False for step in cleanup.evidence["steps"] if step["action"] == "cleanup_cancel")

    monkeypatch.setattr(
        stable,
        "min_gate_quantity",
        lambda *_args: (_ for _ in ()).throw(NotionalExceededError("stable", 1001.0, 1000.0)),
    )
    with pytest.raises(NotionalExceededError):
        asyncio.run(stable.StableClientOrderIdScenario().run(_ctx(FakeClient(), tmp_path)))


def test_launcher_registry_import_graph_and_budget_exception_boundaries(monkeypatch) -> None:
    import importlib

    from beidou_launcher import registry

    original_import = importlib.import_module
    failing_package = registry.REQUIRED_PACKAGES[0]

    def import_with_one_failure(package: str):
        if package == failing_package:
            raise ImportError("package missing")
        return original_import(package)

    monkeypatch.setattr(registry.importlib, "import_module", import_with_one_failure)
    results = registry.check_package_imports()
    failed = next(item for item in results if item.check_id.endswith(failing_package))
    assert failed.status.value == "FAIL"
    assert "ImportError" in failed.message

    class InvalidComponent:
        def validate(self):
            return False

    class RaisingComponent:
        def validate(self):
            raise RuntimeError("component")

    class RaisingGraph:
        def __init__(self):
            self._components = {"invalid": InvalidComponent(), "raising": RaisingComponent()}

        def topological_order(self):
            raise RuntimeError("topology")

    engine = SimpleNamespace(
        _alpha_graph=RaisingGraph(),
        _factor_registry=SimpleNamespace(_factors={}),
        _trading_pool=None,
        _strategy_risk=SimpleNamespace(get_budget=lambda _sid: (_ for _ in ()).throw(RuntimeError("budget"))),
        _autopilot_strategy_id="strategy",
    )
    checks = registry.inspect_engine_wiring(engine, "paper")
    graph = next(item for item in checks if item.check_id == "runtime.algorithms.alpha_graph")
    assert graph.status.value == "FAIL"
    assert graph.evidence["graph_error"]
    budget = next(item for item in checks if item.check_id == "runtime.algorithms.risk_budget")
    assert budget.status.value == "FAIL"

    class BadBudget:
        max_drawdown_pct = "bad"
        max_daily_loss_pct = 1.0
        max_position_notional = 1.0
        max_leverage = 1.0
        risk_per_trade_pct = 1.0
        max_consecutive_losses = 1

    engine._strategy_risk = SimpleNamespace(get_budget=lambda _sid: BadBudget())
    checks = registry.inspect_engine_wiring(engine, "paper")
    budget = next(item for item in checks if item.check_id == "runtime.algorithms.risk_budget")
    assert budget.status.value == "FAIL"
    assert budget.evidence["fields"]["max_drawdown_pct"] == 0.0


def test_strategy_risk_manager_remaining_state_and_persistence_boundaries() -> None:
    from beidou_strategy.risk.manager import (
        CircuitBreakerReason,
        RiskBudget,
        StrategyRiskLevel,
        StrategyRiskManager,
        _is_new_day,
    )

    sid = __import__("beidou_shared.types", fromlist=["StrategyId"]).StrategyId("risk-boundaries")
    assert _is_new_day() is True
    manager = StrategyRiskManager()
    assert manager.compute_position_size(sid, 1000.0, 100.0, 100.0) == 0.0
    assert manager.update_sharpe(sid, 0.1)["action"] == "NOOP"
    manager.set_budget(RiskBudget(sid, min_sharpe_rolling=0.0))
    assert manager.update_sharpe(sid, 1.0)["action"] == "NOOP"

    state = manager.get_state(sid)
    assert state is not None
    state.risk_level = StrategyRiskLevel.LOCKED
    state.active_circuit_breakers = []
    manager.reset_daily_pnl()
    assert state.risk_level is StrategyRiskLevel.CAUTION

    class FailingStore:
        def save_strategy_risk_state(self, *_args):
            raise RuntimeError("save")

        def restore_strategy_risk_states(self):
            raise RuntimeError("restore")

    manager.save_state(FailingStore())
    manager.restore_state(FailingStore())
    manager.restore_state(
        SimpleNamespace(restore_strategy_risk_states=lambda: {"bad": {"active_circuit_breakers": ["UNKNOWN_BREAKER"]}})
    )
    assert manager.get_circuit_breaker_log() == []
    assert CircuitBreakerReason.DRAWDOWN_LIMIT.value == "DRAWDOWN_LIMIT"


def test_bayesian_search_validation_single_fold_and_early_stop() -> None:
    from beidou_research.mining.generators.bayesian_search import (
        BayesianConfig,
        BayesianParameterSearch,
        ParameterSpace,
    )

    search = BayesianParameterSearch()
    invalid_spaces = [
        ParameterSpace("x", "unsupported"),
        ParameterSpace("x", "float"),
        ParameterSpace("x", "float", low=2.0, high=1.0),
        ParameterSpace("x", "categorical", choices=[]),
    ]
    for space in invalid_spaces:
        with pytest.raises(ValueError):
            search.define_search_space([space])
    assert 0.0 <= search.sample_parameters([ParameterSpace("x", "float", low=0.0, high=1.0)], 1)["x"] <= 1.0

    single = BayesianParameterSearch(BayesianConfig(n_inner_folds=1))
    trial = single.compute_objective({"window": 1}, lambda _params: {"metric": 1.0})
    assert trial.status == "completed" and trial.stability_penalty == 0.0

    empty = BayesianParameterSearch(BayesianConfig(n_inner_folds=0))
    assert empty.compute_objective({}, lambda _params: {"metric": 1.0}).status == "no_results"
    with pytest.raises(ValueError, match="positive"):
        BayesianParameterSearch(BayesianConfig(n_trials=0)).search([], lambda _params: {"metric": 1.0})

    calls = 0

    def evaluator(_params):
        nonlocal calls
        calls += 1
        return {"metric": 10.0 if calls == 1 else 0.0}

    result = BayesianParameterSearch(BayesianConfig(n_trials=4, n_inner_folds=1, early_stopping_rounds=1)).search(
        [ParameterSpace("window", "int", low=1, high=1)], evaluator
    )
    assert result.n_trials_completed == 2
