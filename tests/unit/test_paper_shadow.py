"""Paper/Shadow execution realism and gate contracts."""

from beidou_shared.types import StrategyId
from beidou_strategy.paper_shadow import PaperMatchingEngine, PaperShadowRunner, ShadowConfig


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
