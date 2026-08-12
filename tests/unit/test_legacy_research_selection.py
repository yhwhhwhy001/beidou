"""Behavioral tests for previously uncovered research selection utilities."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from beidou_research.mining.canonicalizer import ExpressionCanonicalizer
from beidou_research.mining.contracts import HorizonUnit, PredictionKey, PredictionRecord
from beidou_research.mining.leakage_guard import LeakageGuard
from beidou_research.mining.migration import FactorMigration, MigrationStatus
from beidou_research.mining.selection.ensemble import FactorEnsemble
from beidou_research.mining.selection.marginal_contribution import compute_incremental_contribution
from beidou_research.mining.selection.pareto import ParetoCandidate, ParetoSelector
from beidou_research.mining.selection.redundancy import RedundancyDetector, _ols_r_squared, _pearson
from beidou_shared.types import FactorId, InstrumentId, SchemaVersion, VenueId


def test_redundancy_analysis_handles_empty_short_constant_and_correlated_inputs() -> None:
    detector = RedundancyDetector(corr_threshold=0.8, vif_threshold=2)
    assert detector.compute_vif_scores({}) == {}
    assert detector.compute_correlation_matrix({}) == {}
    assert detector.hierarchical_clustering({}) == []
    assert detector.compute_vif_scores({"a": [1, 2]}) == {"a": 1.0}
    assert _pearson([1, 1, 1], [1, 2, 3]) == 0
    assert _ols_r_squared([1, 2], [[1], [2]]) == 0

    values = {
        "a": [float(i) for i in range(20)],
        "b": [float(i * 2) for i in range(20)],
        "c": [float((-1) ** i) for i in range(20)],
    }
    matrix = detector.compute_correlation_matrix(values)
    assert matrix["a"]["a"] == 1
    assert abs(matrix["a"]["b"]) == pytest.approx(1)
    pairs = detector.find_high_correlation_pairs(matrix)
    assert pairs[0][:2] == ("a", "b")
    assert detector._cluster_distance(["a"], ["b"], matrix) == pytest.approx(0)
    result = detector.analyze(values)
    assert ("a", "b") in result.high_correlation_pairs
    assert any({"a", "b"}.issubset(cluster) for cluster in result.clusters)
    assert result.redundant_cluster_count == len(result.clusters)


def test_pareto_fronts_and_diversity_selection() -> None:
    selector = ParetoSelector()
    assert selector.compute_pareto_fronts([]).front_size == 0
    result = selector.compute_pareto_fronts(
        [
            {"candidate_id": "best", "icir": 2, "sharpe": 2, "fold_consistency": 1, "turnover": 1},
            {"candidate_id": "weak", "icir": 1, "sharpe": 1, "fold_consistency": 0.5, "turnover": 2},
            {"candidate_id": "low-turnover", "icir": 1.5, "sharpe": 1.5, "fold_consistency": 1, "turnover": 0.1},
        ],
        minimize_metrics=["turnover"],
    )
    assert result.front_size == 2
    assert result.dominated_count == 1
    assert {item.candidate_id for item in result.fronts[0]} == {"best", "low-turnover"}
    assert selector._dominates([2, 2], [1, 2])
    assert not selector._dominates([1, 2], [2, 1])

    front = [ParetoCandidate(str(i), {"x": float(i), "y": float(i * i)}) for i in range(6)]
    selected = selector.select_diverse(front, max_count=3)
    assert len(selected) == 3 and selected[0] is front[0]
    assert selector.select_diverse(front[:2], max_count=3) == front[:2]
    assert selector._metric_distance(front[0], front[1]) > 0


def test_factor_ensemble_weights_are_normalized_and_use_common_sample_window() -> None:
    ensemble = FactorEnsemble()
    assert ensemble.equal_risk_contribution({}).weights == {}
    short = ensemble.equal_risk_contribution({"a": [1], "b": [2]})
    assert sum(short.weights.values()) == pytest.approx(1)

    returns = {
        "stable": [0.01 + i * 0.0001 for i in range(12)],
        "volatile": [(-1) ** i * 0.1 for i in range(15)],
    }
    erc = ensemble.equal_risk_contribution(returns)
    assert erc.weights["stable"] > erc.weights["volatile"]
    shrink = ensemble.covariance_shrinkage(returns, shrinkage=0.5)
    assert sum(shrink.weights.values()) == pytest.approx(1)
    fallback = ensemble.covariance_shrinkage({"a": [1, 2], "b": [2, 3]})
    assert fallback.weights == {"a": 0.5, "b": 0.5}
    nonnegative = ensemble.non_negative_linear(returns)
    assert all(weight >= 0 for weight in nonnegative.weights.values())
    assert ensemble.combine_signals({"stable": 1, "volatile": -1, "missing": 100}, nonnegative) == pytest.approx(
        nonnegative.weights["stable"] - nonnegative.weights["volatile"]
    )


def test_incremental_contribution_covers_short_flat_and_improving_samples() -> None:
    short = compute_incremental_contribution([0.1], [0.2], "short")
    assert short.sample_count == 1 and not short.is_positive_contribution

    flat = compute_incremental_contribution([0.1] * 12, [0.1] * 12, "flat")
    assert flat.delta_sharpe == 0
    base = [0.01, -0.02, 0.01, -0.01, 0.02, -0.02, 0.01, -0.01, 0.02, -0.01, 0.01, -0.01]
    improved = [value + 0.01 for value in base]
    result = compute_incremental_contribution(base, improved, "candidate")
    assert result.candidate_id == "candidate"
    assert result.sample_count == 12
    assert result.delta_sharpe > 0 and result.is_positive_contribution


class CanonicalExpression:
    def __init__(self, value: str) -> None:
        self.value = value

    def canonicalize(self):
        return CanonicalExpression(self.value.lower())

    def canonical_hash(self) -> str:
        return self.value.lower()


def test_expression_canonicalizer_deduplicates_and_resets() -> None:
    canonicalizer = ExpressionCanonicalizer()
    expression = CanonicalExpression("ABC")
    assert canonicalizer.canonicalize(expression).value == "abc"
    assert canonicalizer.canonicalize("raw") == "raw"
    assert canonicalizer.are_equivalent(CanonicalExpression("ABC"), CanonicalExpression("abc"))
    assert canonicalizer.compute_hash("raw") == canonicalizer.compute_hash("raw")
    unique = canonicalizer.deduplicate([CanonicalExpression("ABC"), CanonicalExpression("abc"), "raw"])
    assert len(unique) == 2
    assert canonicalizer.stats == {"total_processed": 3, "duplicates_removed": 1, "unique_hashes": 2}
    canonicalizer.reset()
    assert canonicalizer.stats == {"total_processed": 0, "duplicates_removed": 0, "unique_hashes": 0}


def _prediction(at: datetime) -> PredictionRecord:
    key = PredictionKey(
        venue=VenueId("BINANCE"),
        symbol=InstrumentId("BTCUSDT"),
        timeframe="1h",
        prediction_time=at,
        data_available_time=at,
        horizon=1,
        horizon_unit=HorizonUnit.BAR,
        factor_id=FactorId("factor"),
        factor_version=SchemaVersion("1.0.0"),
    )
    return PredictionRecord(prediction_key=key, prediction_value=0.1)


def test_leakage_guard_tracks_bar_timezone_batch_and_boundary_violations() -> None:
    guard = LeakageGuard()
    aware = _prediction(datetime(2026, 1, 1, tzinfo=timezone.utc))
    passed = guard.check_prediction(aware, bar_provider=lambda **_: True)
    assert passed.passed
    failed_bar = guard.check_prediction(aware, bar_provider=lambda **_: False)
    assert not failed_bar.passed and "OPEN_BAR" in failed_bar.violations[0]

    naive = _prediction(datetime(2026, 1, 2, tzinfo=timezone.utc).replace(tzinfo=None))
    missing_timezone = guard.check_prediction(naive, bar_provider=lambda **_: True)
    assert "MISSING_TIMEZONE" in missing_timezone.violations
    batch = guard.check_batch([aware])
    assert not batch.passed and batch.details["open_bar_count"] == 1

    start = datetime(2026, 1, 10, tzinfo=timezone.utc)
    overlap = guard.check_train_test_boundary(start, start, embargo_end_time=start + timedelta(days=1))
    assert not overlap.passed and len(overlap.violations) == 2
    clean = guard.check_train_test_boundary(
        start, start + timedelta(days=2), embargo_end_time=start + timedelta(days=1)
    )
    assert clean.passed
    assert guard.has_violations
    assert guard.violation_summary["OPEN_BAR"] >= 1


def test_factor_migration_persists_completed_records_and_supports_rollback(tmp_path) -> None:
    migration = FactorMigration(str(tmp_path))
    with pytest.raises(RuntimeError, match="COMPLETED"):
        migration.rollback()
    records = migration.migrate_legacy_factors(["legacy-a", "legacy-b"], reason="revalidate")
    assert len(records) == 2
    assert migration.verify_no_active_legacy(["legacy-a", "new"]) == ["Legacy factor legacy-a is still ACTIVE"]

    restored = FactorMigration.from_state(str(tmp_path))
    assert restored._status == MigrationStatus.COMPLETED
    assert [record.old_factor_id for record in restored._records] == ["legacy-a", "legacy-b"]
    rolled_back = restored.rollback()
    assert len(rolled_back) == 2 and restored._status == MigrationStatus.ROLLED_BACK

    empty = FactorMigration.from_state(str(tmp_path / "empty"))
    assert empty._status == MigrationStatus.PENDING
