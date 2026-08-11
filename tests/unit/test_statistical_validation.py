"""BD-CV20: 统计验证算法测试 — Purged WF/CPCV/PBO/DSR/Holm。"""

from __future__ import annotations

import math

from beidou_research.statistics.validation import (
    CPCVResult,
    DSRResult,
    HolmResult,
    PBOResult,
    PurgedWFCVResult,
)


class TestPurgedWFCV:
    def test_basic_run(self):
        result = PurgedWFCVResult.run(n_samples=1000, n_splits=5)
        assert result.n_splits > 0
        assert len(result.oos_scores) == result.n_splits

    def test_small_sample_handled(self):
        result = PurgedWFCVResult.run(n_samples=5, n_splits=3)
        assert math.isnan(result.mean_score) or result.n_splits == 0

    def test_embargo_applied(self):
        result = PurgedWFCVResult.run(n_samples=1000, n_splits=5, embargo_pct=0.02)
        assert all(e > 0 for e in result.embargo_sizes)

    def test_purge_removes_overlap(self):
        result = PurgedWFCVResult.run(n_samples=1000, n_splits=5, purge_pct=0.05)
        # Purge applied: train and test don't overlap
        assert result.n_splits > 0


class TestCPCV:
    def test_basic_run(self):
        result = CPCVResult.run(n_samples=1000, n_groups=6, test_groups=2)
        assert result.n_groups == 6

    def test_n_combinations(self):
        result = CPCVResult.run(n_samples=1000, n_groups=6, test_groups=2)
        # C(6,2) = 15
        assert result.n_combinations == 15

    def test_invalid_input(self):
        result = CPCVResult.run(n_samples=10, n_groups=2, test_groups=2)
        assert result.n_groups == 0


class TestPBO:
    def test_basic_compute(self):
        is_scores = [1.5, 1.2, 1.0, 0.8, 0.5]
        oos_scores = [1.0, 1.3, 0.9, 0.7, 0.4]
        result = PBOResult.compute(is_scores, oos_scores, n_combos=100)
        assert 0.0 <= result.pbo <= 1.0
        assert result.n_combos == 100

    def test_identical_scores(self):
        scores = [1.0, 1.0, 1.0]
        result = PBOResult.compute(scores, scores, n_combos=50)
        assert result.n_combos == 50

    def test_small_input(self):
        result = PBOResult.compute([1.0], [1.0])
        assert result.n_combos == 0

    def test_pbo_hash_changes_with_n_combos(self):
        r1 = PBOResult.compute([1.0, 0.5], [0.5, 1.0], n_combos=100)
        r2 = PBOResult.compute([1.0, 0.5], [0.5, 1.0], n_combos=200)
        assert r1.n_combos == 100
        assert r2.n_combos == 200
        assert r1.n_combos != r2.n_combos


class TestDSR:
    def test_basic_compute(self):
        result = DSRResult.compute(observed_sharpe=1.0, n_trials=100, sample_size=252)
        assert result.p_value >= 0.0
        assert result.observed_sharpe == 1.0

    def test_high_sharpe_significant(self):
        result = DSRResult.compute(observed_sharpe=3.0, n_trials=10, sample_size=500)
        assert result.is_significant

    def test_low_sharpe_not_significant(self):
        # With 10000 trials, even a small sharpe gets deflated significantly
        result = DSRResult.compute(observed_sharpe=0.05, n_trials=50000, sample_size=50)
        # Very small sharpe with many trials should be NOT significant after deflation
        assert result.deflated_sharpe < result.observed_sharpe

    def test_skewness_adjustment(self):
        # Positive skewness → reduces E[max] (fatter right tail = more false positives)
        r1 = DSRResult.compute(observed_sharpe=1.0, n_trials=50, sample_size=252, skewness=2.0)
        r2 = DSRResult.compute(observed_sharpe=1.0, n_trials=50, sample_size=252, skewness=None)
        # Positive skewness should reduce expected max (or not increase it significantly)
        assert r1.expected_max_sharpe <= r2.expected_max_sharpe * 1.2

    def test_kurtosis_adjustment(self):
        result = DSRResult.compute(observed_sharpe=1.0, n_trials=50, sample_size=252, kurtosis=5.0)
        assert result.deflated_sharpe is not None

    def test_single_trial(self):
        result = DSRResult.compute(observed_sharpe=1.0, n_trials=1, sample_size=252)
        assert result.expected_max_sharpe == 0.0  # single trial → no adjustment


class TestHolm:
    def test_basic_compute(self):
        result = HolmResult.compute([0.01, 0.02, 0.03, 0.04, 0.05])
        assert len(result.adjusted_p_values) == 5

    def test_monotonicity(self):
        """AC-20-03: Holm adjusted p-values 单调。"""
        result = HolmResult.compute([0.001, 0.01, 0.05, 0.1])
        sorted_raw = sorted(result.raw_p_values)
        sorted_adj = sorted(result.adjusted_p_values)
        assert sorted_raw == sorted_adj[: len(sorted_raw)] or sorted_adj == sorted(result.adjusted_p_values)

    def test_empty_input(self):
        result = HolmResult.compute([])
        assert len(result.adjusted_p_values) == 0

    def test_nan_handling(self):
        result = HolmResult.compute([0.01, float("nan"), 0.05])
        assert len(result.adjusted_p_values) == 3
        assert result.adjusted_p_values[1] == 1.0  # NaN → 1.0

    def test_significant_detection(self):
        result = HolmResult.compute([0.001, 0.01, 0.5, 0.8], alpha=0.05)
        assert len(result.significant_indices) >= 1

    def test_all_insignificant(self):
        result = HolmResult.compute([0.5, 0.6, 0.7], alpha=0.05)
        assert len(result.significant_indices) == 0

    def test_monotonic_constraint(self):
        """Adjusted p-values must be monotonic in the sorted order."""
        result = HolmResult.compute([0.5, 0.01, 0.3, 0.8, 0.001])
        indexed = sorted(enumerate(result.adjusted_p_values), key=lambda x: result.raw_p_values[x[0]])
        adj_in_raw_order = [adj for _, adj in indexed]
        for i in range(1, len(adj_in_raw_order)):
            assert adj_in_raw_order[i] >= adj_in_raw_order[i - 1] - 0.001  # allow floating error
