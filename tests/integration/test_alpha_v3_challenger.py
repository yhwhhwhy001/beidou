from __future__ import annotations

from beidou_research.backtest.alpha_v3_challenger import run_alpha_v3_challenger
from beidou_research.backtest.replay import PaperReplayResult


def _run(**overrides: object):
    values: dict[str, object] = {
        "benchmark_returns": (0.01, 0.008, -0.006, 0.004, -0.005, 0.009, 0.006, -0.004, 0.007, -0.003, 0.005, 0.004),
        "v2_gross_returns": (0.008, 0.006, -0.004, 0.002, -0.003, 0.006, 0.004, -0.002, 0.004, -0.001, 0.003, 0.002),
        "v3_gross_returns": (0.010, 0.009, -0.003, 0.005, -0.002, 0.008, 0.007, -0.001, 0.006, -0.001, 0.004, 0.003),
        "v2_positions": (1.0,) * 12,
        "v3_positions": (1.0,) * 12,
        "cost_bps": 5.0,
        "data_snapshot_hash_v2": "dataset-v1",
        "data_snapshot_hash_v3": "dataset-v1",
        "cost_model_hash_v2": "cost-v1",
        "cost_model_hash_v3": "cost-v1",
        "train_bars": 4,
        "oos_bars": 4,
        "regimes": ("BULL", "BULL", "RANGE", "RANGE", "BEAR", "BEAR", "BULL", "BULL", "RANGE", "RANGE", "BEAR", "BEAR"),
        "v2_beta_exposure": (1.0,) * 12,
        "v3_beta_exposure": (1.0,) * 12,
        "point_in_time_validated": True,
        "paper_evidence": PaperReplayResult(window_bars=800, n_trades=20, paper_ir=0.12),
    }
    values.update(overrides)
    return run_alpha_v3_challenger(**values)


def test_challenger_uses_same_cost_and_walk_forward_oos_metrics() -> None:
    report = _run()

    assert report.status == "PASS"
    assert report.decision == "PROMOTE"
    assert len(report.folds) == 2
    assert report.v2.cost_paid == report.v3.cost_paid
    assert report.v3.net_return > report.v2.net_return
    assert set(report.by_regime) == {"BEAR", "BULL", "RANGE"}
    assert report.checks["same_data"] is True
    assert report.checks["same_cost_model"] is True


def test_missing_paper_or_point_in_time_evidence_is_not_verifiable() -> None:
    report = _run(paper_evidence=None, point_in_time_validated=False)

    assert report.status == "NOT_VERIFIABLE"
    assert report.decision == "DO_NOT_PROMOTE"
    assert "PAPER_SHADOW" in report.reasons
    assert "POINT_IN_TIME" in report.reasons


def test_leverage_only_outperformance_is_rejected() -> None:
    report = _run(
        v2_gross_returns=(0.010, 0.009, -0.006, 0.004, -0.005, 0.009, 0.006, -0.004, 0.007, -0.003, 0.005, 0.004),
        v3_gross_returns=(0.020, 0.018, -0.012, 0.008, -0.010, 0.018, 0.012, -0.008, 0.014, -0.006, 0.010, 0.008),
        v2_positions=(1.0,) * 12,
        v3_positions=(2.0,) * 12,
        v2_beta_exposure=(1.0,) * 12,
        v3_beta_exposure=(2.0,) * 12,
    )

    assert report.leverage_only_improvement is True
    assert report.status == "FAIL"
    assert report.decision == "DO_NOT_PROMOTE"
    assert "NO_LEVERAGE_ONLY_IMPROVEMENT" in report.reasons
