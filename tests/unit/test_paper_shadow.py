"""Paper/Shadow execution realism and gate contracts."""

from types import SimpleNamespace

import pytest

from beidou_shared.types import GateResult, StrategyId
from beidou_strategy.paper_shadow import (
    CostPressureSimulator,
    PaperMatchingEngine,
    PaperShadowRunner,
    ShadowConfig,
    ShadowMetrics,
    ShadowReport,
    ShadowStatus,
    TestnetGate,
)


def test_realised_paper_cost_is_independent_of_spread_estimate() -> None:
    matcher = PaperMatchingEngine(seed=7)
    realised = matcher.realized_cost_bps("BUY", avg_price=101.0, bid=99.0, ask=100.0)

    assert realised > matcher.taker_fee_bps
    assert realised != (100.0 - 99.0) / ((100.0 + 99.0) / 2) * 10000


def test_shadow_gate_blocks_missing_or_deviating_cost_evidence() -> None:
    runner = PaperShadowRunner(
        ShadowConfig(
            strategy_id=StrategyId("test"),
            min_runtime_hours=0.0,
            min_effective_events=1,
            max_cost_deviation_pct=20.0,
        )
    )
    runner.start()
    runner.record_tick(
        "LONG",
        1.0,
        "LONG",
        1.0,
        estimated_cost_bps=5.0,
        actual_cost_bps=10.0,
        decision_timestamp=1_000.0,
        outcome_source="independent_forward_window",
        outcome_available_at=2_000.0,
    )
    report = runner.generate_report()

    assert runner.metrics.cost_observations == 1
    assert report.is_ready_for_testnet()[0] is False
    assert any("cost_deviation" in reason for reason in report.discrepancies)


def test_execution_observation_does_not_self_label_future_direction() -> None:
    runner = PaperShadowRunner(
        ShadowConfig(
            strategy_id=StrategyId("test"),
            min_runtime_hours=0.0,
            min_effective_events=1,
        )
    )
    runner.start()
    runner.record_execution_observation(estimated_cost_bps=5.0, actual_cost_bps=5.5)

    assert runner.metrics.total_ticks == 1
    assert runner.metrics.total_executed == 0
    assert runner.metrics.total_signals == 0
    assert runner.metrics.cost_observations == 1
    assert runner.generate_report().is_ready_for_testnet()[0] is False


def test_same_tick_prediction_self_label_is_rejected() -> None:
    runner = PaperShadowRunner(ShadowConfig(strategy_id=StrategyId("test")))
    runner.start()

    try:
        runner.record_tick("LONG", 1.0, "LONG", 1.0)
    except ValueError as exc:
        assert "INDEPENDENT_FORWARD_LABEL_SOURCE_REQUIRED" in str(exc)
    else:
        raise AssertionError("same-tick prediction self-label unexpectedly accepted")


def test_shadow_forward_labels_incidents_merkle_and_ledger_fail_closed() -> None:
    runner = PaperShadowRunner(
        ShadowConfig(strategy_id=StrategyId("semantic"), min_runtime_hours=0.0, min_effective_events=1)
    )
    runner.start()
    runner.record_tick(
        "LONG",
        0.8,
        decision_timestamp=10.0,
        estimated_cost_bps=5.0,
        actual_cost_bps=5.0,
    )
    runner.record_forward_outcome(
        tick=1,
        actual_direction="NO_ACTION",
        actual_strength=0.0,
        outcome_source="forward-window",
        outcome_available_at=11.0,
    )
    with pytest.raises(ValueError, match="already recorded"):
        runner.record_forward_outcome(
            tick=1,
            actual_direction="LONG",
            actual_strength=0.8,
            outcome_source="forward-window",
            outcome_available_at=12.0,
        )
    runner.record_incident("P0")
    runner.record_drift()
    assert runner.metrics.p0_incidents == 1
    assert runner.metrics.drift_events == 1
    report = runner.generate_report()
    assert report.status is ShadowStatus.RUNNING
    assert report.evidence_hash
    assert report.merkle_manifest["leaf_count"] == 1

    class _Ledger:
        def post(self, _transaction):
            return "paper-tx-1"

    assert runner.record_fill_to_ledger(_Ledger(), "BTCUSDT", "BUY", 0.1, 100.0, fee=0.2) == "paper-tx-1"

    class _BrokenLedger:
        def post(self, _transaction):
            raise RuntimeError("ledger unavailable")

    with pytest.raises(RuntimeError, match="ledger unavailable"):
        runner.record_fill_to_ledger(_BrokenLedger(), "BTCUSDT", "SELL", 0.1, 100.0)
    invalid_report = runner.generate_report()
    assert invalid_report.status is ShadowStatus.INVALID
    assert invalid_report.gate_result is GateResult.FAIL


def test_testnet_gate_requires_shadow_parity_p0_and_mainnet_prohibition() -> None:
    config = ShadowConfig(strategy_id=StrategyId("gate"), min_runtime_hours=0.0, min_effective_events=1)
    ready = ShadowReport(
        config=config,
        metrics=ShadowMetrics(total_executed=1, cost_observations=1),
        status=ShadowStatus.COMPLETED,
    )
    allowed, reasons = TestnetGate.check(ready, parity_passed=True, all_p0_gates_passed=True)
    assert allowed is True
    assert reasons == []
    denied, reasons = TestnetGate.check(
        ready,
        parity_passed=False,
        all_p0_gates_passed=False,
        mainnet_prohibited=False,
    )
    assert denied is False
    assert {
        "MAINNET_MUST_BE_PROHIBITED",
        "parity_not_verified",
        "p0_gates_not_all_passed",
    }.issubset(reasons)


def test_paper_matching_and_cost_pressure_cover_all_deterministic_outcomes() -> None:
    def matcher(values: list[float]) -> PaperMatchingEngine:
        engine = PaperMatchingEngine(seed=1)
        sequence = iter(values)
        engine._rng = SimpleNamespace(random=lambda: next(sequence))
        return engine

    assert matcher([0.1]).match("BTCUSDT", "BUY", 1.0, 99.0, 100.0, 101.0)[0] == "QUEUED"
    assert matcher([0.1, 0.1, 0.99]).match("BTCUSDT", "BUY", 1.0, None, 100.0, 101.0)[0] == "REJECTED"
    partial = matcher([0.1, 0.1, 0.90, 0.2]).match("BTCUSDT", "BUY", 1.0, None, 100.0, 101.0)
    assert partial[0] == "PARTIALLY_FILLED"
    canceled = matcher([0.1, 0.1, 0.05, 0.01]).match("BTCUSDT", "BUY", 1.0, None, 100.0, 101.0)
    assert canceled[0] == "CANCELED"
    filled = matcher([0.1, 0.1, 0.05, 0.99]).match("BTCUSDT", "SELL", 1.0, None, 100.0, 101.0)
    assert filled[0] == "FILLED"
    race_cancel = matcher([0.1]).simulate_cancel_fill_race("o1", "BTCUSDT", "BUY", 1.0)
    race_partial = matcher([0.5, 0.4]).simulate_cancel_fill_race("o2", "BTCUSDT", "BUY", 1.0)
    race_fill = matcher([0.9]).simulate_cancel_fill_race("o3", "BTCUSDT", "BUY", 1.0)
    assert [race_cancel[0], race_partial[0], race_fill[0]] == ["CANCEL_WINS", "FILL_THEN_CANCEL", "FILL_WINS"]
    assert filled[2] > 0.0
    assert matcher([0.1]).realized_cost_bps("BUY", 0.0, 100.0, 101.0) != matcher([0.1]).realized_cost_bps(
        "BUY", 0.0, 100.0, 101.0
    )
    assert matcher([0.1]).compute_hash([], "s", "m", 1) == matcher([0.9]).compute_hash([], "s", "m", 1)

    pressure = CostPressureSimulator()
    pressure.set_pressure(2.0)
    assert pressure.spread_widening(10.0) == 30.0
    assert pressure.slippage_multiplier() == 4.0
    assert pressure.capacity_multiplier() == pytest.approx(0.2)
    assert pressure.is_net_positive(10.0, 5.0) is False
    pressure.set_pressure(-1.0)
    assert pressure.capacity_multiplier() == 1.0
