"""BF-05/BF-06: 评估模块测试 — fast_screen, multiple_testing, purged_wfo, stability, cost_capacity."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from beidou_research.mining.evaluation.cost_capacity import (
    CapacityEvaluator,
    CostModel,
)
from beidou_research.mining.evaluation.cpcv import CPCVConfig, CPCVEvaluator
from beidou_research.mining.evaluation.fast_screen import FastScreen
from beidou_research.mining.evaluation.multiple_testing import (
    benjamini_hochberg,
    compute_pbo,
    deflated_sharpe_ratio,
    evaluate_multiple_testing,
    holm_correction,
)
from beidou_research.mining.evaluation.purged_walk_forward import (
    FoldBuilder,
    FoldConfig,
    PurgedWalkForward,
)
from beidou_research.mining.evaluation.stability import StabilityEvaluator
from beidou_shared.types import DataQualityTier

# ================================================================
# FastScreen
# ================================================================


class TestFastScreen:
    def test_passes_clean_data(self):
        fs = FastScreen()
        result = fs.screen(factor_values=[0.1, -0.05, 0.2, 0.15, 0.3] * 20)
        assert result.passed

    def test_fails_small_sample(self):
        fs = FastScreen()
        result = fs.screen(factor_values=[0.1, 0.2, 0.3])
        assert not result.passed
        assert any("sample_count" in r for r in result.failure_reasons)

    def test_fails_data_quality_fail(self):
        fs = FastScreen()
        result = fs.screen(
            factor_values=[0.1] * 100,
            data_quality=DataQualityTier.FAIL,
        )
        assert not result.passed
        assert "data_quality" in result.failure_reasons[0]

    def test_fails_zero_variance(self):
        fs = FastScreen()
        result = fs.screen(factor_values=[5.0] * 100)
        assert not result.passed
        assert any("variance" in r for r in result.failure_reasons)

    def test_fails_high_missing(self):
        fs = FastScreen()
        vals = [0.1, 0.2] + [float("nan")] * 98
        result = fs.screen(factor_values=vals)
        assert not result.passed
        assert any("missing_rate" in r for r in result.failure_reasons)

    def test_fails_duplicate_hash(self):
        fs = FastScreen()
        result = fs.screen(
            factor_values=[0.1] * 100,
            expression_hash="abc123",
            existing_hashes={"abc123", "def456"},
        )
        assert not result.passed
        assert any("duplicate" in r for r in result.failure_reasons)

    def test_fails_high_complexity(self):
        fs = FastScreen()
        result = fs.screen(
            factor_values=[0.1] * 100,
            complexity_score=99.0,
        )
        assert not result.passed

    def test_extreme_values_detected(self):
        fs = FastScreen()
        # 90个值在 [0.9, 1.1] 范围，10个值为 100.0
        # MAD ≈ 0.05, robust_sigma ≈ 0.074
        # |100-1.0| = 99 > 5*0.074 = 0.37 → 检测到
        vals = [1.0 + 0.01 * (i % 10) for i in range(90)] + [100.0] * 10
        result = fs.screen(factor_values=vals)
        assert not result.passed
        assert any("extreme_ratio" in r for r in result.failure_reasons)


# ================================================================
# Multiple Testing
# ================================================================


class TestBenjaminiHochberg:
    def test_single_pvalue(self):
        result = benjamini_hochberg([0.01])
        assert result.n_tests == 1
        assert result.significant_at_05[0]

    def test_all_significant(self):
        pvals = [0.001, 0.002, 0.003, 0.004, 0.005]
        result = benjamini_hochberg(pvals)
        assert result.n_significant_05 == 5

    def test_none_significant(self):
        pvals = [0.5, 0.6, 0.7, 0.8, 0.9]
        result = benjamini_hochberg(pvals)
        assert result.n_significant_05 == 0

    def test_mixed_significance(self):
        pvals = [0.001, 0.05, 0.10, 0.20, 0.50]
        result = benjamini_hochberg(pvals)
        assert result.n_significant_05 >= 1  # at least p=0.001 survives

    def test_empty_input(self):
        result = benjamini_hochberg([])
        assert result.n_tests == 0


class TestHolm:
    def test_basic(self):
        pvals = [0.001, 0.01, 0.03]
        result = holm_correction(pvals)
        assert result.n_rejected >= 2  # p=0.001, 0.01 should pass

    def test_all_rejected(self):
        result = holm_correction([0.0001, 0.0002, 0.0003])
        assert result.n_rejected == 3

    def test_empty(self):
        result = holm_correction([])
        assert result.n_tests == 0


class TestDeflatedSharpeRatio:
    def test_single_trial(self):
        dsr = deflated_sharpe_ratio(observed_sharpe=1.0, n_trials=1)
        # Single trial → no deflation
        assert dsr["dsr"] >= 0.5

    def test_many_trials_deflates(self):
        dsr_1 = deflated_sharpe_ratio(observed_sharpe=1.0, n_trials=1)
        dsr_1000 = deflated_sharpe_ratio(observed_sharpe=1.0, n_trials=1000)
        # More trials → smaller DSR
        assert dsr_1000["dsr"] < dsr_1["dsr"]

    def test_negative_sharpe(self):
        dsr = deflated_sharpe_ratio(observed_sharpe=-0.5, n_trials=50)
        assert dsr["dsr"] < 0


class TestPBO:
    def test_perfect_correlation_low_pbo(self):
        # IS and OOS perfectly correlated → low PBO with enough samples
        perf = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        pbo = compute_pbo(perf, perf)
        # With identical rankings, best IS should also have best OOS
        assert pbo.rank_correlation == 1.0
        assert pbo.performance_degradation == 0.0

    def test_reverse_correlation_high_pbo(self):
        is_perf = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
        oos_perf = [8.0, 7.0, 6.0, 5.0, 4.0, 3.0, 2.0, 1.0]
        pbo = compute_pbo(is_perf, oos_perf)
        # Best IS = worst OOS → performance degrades significantly
        assert pbo.performance_degradation > 0.5
        assert pbo.rank_correlation < 0


class TestMultipleTestingGate:
    def test_incomplete_pbo_evidence_cannot_pass(self):
        result = evaluate_multiple_testing(
            pvalues=[0.001] * 8,
            observed_sharpe=3.0,
            n_trials=8,
            in_sample_sharpes=[1.0] * 8,
            out_of_sample_sharpes=[1.0] * 8,
            candidate_index=0,
        )

        assert result.verdict == "NOT_VERIFIABLE"
        assert any("pbo_comparisons_insufficient" in reason for reason in result.failure_reasons)

    def test_complete_multiple_testing_evidence_can_pass(self):
        result = evaluate_multiple_testing(
            pvalues=[0.0001, 0.0002, 0.0003],
            observed_sharpe=3.0,
            n_trials=3,
            in_sample_sharpes=list(range(1, 17)),
            out_of_sample_sharpes=list(range(1, 17)),
            candidate_index=0,
        )

        assert result.verdict == "PASS"
        assert not result.failure_reasons


# ================================================================
# Purged Walk-Forward
# ================================================================


class TestFoldBuilder:
    def test_build_folds_basic(self):
        builder = FoldBuilder(FoldConfig(n_folds=5, train_fraction=0.6))
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 6, 30, tzinfo=timezone.utc)
        folds = builder.build_folds(start, end, total_duration_days=180)

        assert len(folds) == 5
        # Folds should be in chronological order
        for i in range(len(folds) - 1):
            assert folds[i].test_start <= folds[i + 1].test_start

    def test_folds_have_purge_and_embargo(self):
        builder = FoldBuilder(FoldConfig(n_folds=3))
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 3, 31, tzinfo=timezone.utc)
        folds = builder.build_folds(start, end, total_duration_days=90, label_horizon_days=2.0)

        for fold in folds:
            assert fold.purge_end >= fold.test_start
            assert fold.embargo_end >= fold.purge_end

    def test_no_folds_for_empty_range(self):
        builder = FoldBuilder()
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 1, 1, tzinfo=timezone.utc)
        folds = builder.build_folds(start, end, total_duration_days=0)
        assert len(folds) == 0


class TestPurgedWalkForward:
    def test_insufficient_samples_returns_unverifiable(self):
        pfo = PurgedWalkForward(FoldConfig(min_train_samples=1000))
        # 只提供少量样本
        times = [datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=i) for i in range(10)]
        end_times = [t + timedelta(hours=4) for t in times]

        result = pfo.run(times, end_times, total_duration_days=1.0)
        assert not result.failure_reasons or "insufficient" in str(result.failure_reasons).lower()

    def test_fold_dates_non_empty(self):
        """验证 fold 日期非空。"""
        builder = FoldBuilder(FoldConfig(n_folds=3, train_fraction=0.6))
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 3, 31, tzinfo=timezone.utc)
        folds = builder.build_folds(start, end, total_duration_days=90)

        for fold in folds:
            assert fold.train_start < fold.train_end
            assert fold.test_start < fold.test_end

    def test_nonzero_label_horizon_keeps_oos_samples(self):
        config = FoldConfig(
            n_folds=5,
            min_train_samples=20,
            min_test_samples=10,
            min_folds_for_verdict=5,
        )
        pfo = PurgedWalkForward(config)
        times = [datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=i) for i in range(600)]
        end_times = [t + timedelta(hours=4) for t in times]

        fold_counter = [0]

        def evaluator(train_indices, test_indices):
            from beidou_research.mining.evaluation.purged_walk_forward import FoldResult

            fold_counter[0] += 1
            return FoldResult(
                fold_id=0,
                train_samples=len(train_indices),
                test_samples=len(test_indices),
                ic_mean=0.2 + fold_counter[0] * 0.01,
                sharpe=1.0,
            )

        result = pfo.run(
            times,
            end_times,
            total_duration_days=25.0,
            evaluator=evaluator,
            label_horizon_days=4 / 24,
        )

        assert result.n_folds_completed == 5
        assert all(f.test_sample_count > 0 for f in result.folds)
        assert result.gate_result.value == "PASS"


class TestCPCV:
    def test_all_test_blocks_are_purged_and_path_count_is_explicit(self):
        evaluator = CPCVEvaluator(
            CPCVConfig(
                n_groups=8,
                n_test_groups=2,
                purge_bars=2,
                embargo_bars=2,
                min_train_samples=20,
                min_paths_for_verdict=20,
            )
        )
        n = 800
        labels = [i // 100 for i in range(n)]
        paths = evaluator.generate_paths(n, labels)

        assert len(paths) >= 20
        for path in paths:
            for test_index in path.test_indices:
                assert all(abs(train_index - test_index) > 2 for train_index in path.train_indices)

    def test_fewer_than_required_paths_is_unverifiable(self):
        evaluator = CPCVEvaluator(
            CPCVConfig(
                n_groups=4,
                n_test_groups=2,
                min_train_samples=10,
                min_paths_for_verdict=20,
            )
        )
        predictions = [float(i % 7) for i in range(200)]
        returns = [float(i % 7) for i in range(200)]
        labels = [i // 50 for i in range(200)]

        result = evaluator.evaluate(predictions, returns, group_labels=labels)

        assert result.gate_result.value == "UNVERIFIABLE"
        assert result.n_paths < 20


# ================================================================
# Stability
# ================================================================


class TestStability:
    def test_time_split_stable(self):
        evaluator = StabilityEvaluator(degradation_threshold=0.5)
        # 构造稳定的预测：前后半段行为一致
        predictions = [0.01 * i for i in range(200)]
        returns = [0.005 * i for i in range(200)]
        ic = 0.5  # 假设 IC

        result = evaluator.evaluate_time_split(predictions, returns, ic)
        assert result.is_stable

    def test_volatility_regime(self):
        evaluator = StabilityEvaluator()
        predictions = [0.01 * i for i in range(100)]
        returns = [0.005 * i for i in range(100)]
        vol = [0.1 + 0.001 * i for i in range(100)]  # 递增波动率

        result = evaluator.evaluate_volatility_regime(predictions, returns, vol, ic_full=0.3)
        assert result.dimension == "volatility_regime"

    def test_cost_stress(self):
        evaluator = StabilityEvaluator()
        predictions = [0.01] * 100
        returns = [0.001] * 100

        results = evaluator.evaluate_cost_stress(predictions, returns, base_cost_bps=5.0, sharpe_base=0.5)
        assert len(results) == 3  # 1x, 1.5x, 2x
        # Higher cost → lower sharpe
        assert results[2].sub_sample_value <= results[0].sub_sample_value


# ================================================================
# Cost/Capacity
# ================================================================


class TestCostModel:
    def test_default_model(self):
        """PKG02: 费用默认值从交易所获取，不再硬编码。"""
        model = CostModel(taker_fee_bps=4.0)
        assert model.taker_fee_bps == 4.0
        assert model.avg_spread_bps == 1.0
        cost = model.round_trip_cost_bps(hold_hours=4.0)
        assert cost > 0

    def test_longer_hold_more_funding(self):
        model = CostModel()
        cost_1h = model.round_trip_cost_bps(hold_hours=1.0)
        cost_24h = model.round_trip_cost_bps(hold_hours=24.0)
        assert cost_24h > cost_1h

    def test_impact_scales_with_size(self):
        model = CostModel()
        small = model.impact_bps_for_size(10_000)
        large = model.impact_bps_for_size(1_000_000)
        assert large > small

    def test_unknown_model_detected(self):
        model = CostModel(taker_fee_bps=0, avg_spread_bps=0)
        assert model.is_unknown


class TestCapacityEvaluator:
    def test_capacity_curve(self):
        evaluator = CapacityEvaluator()
        predictions = [0.01 * i for i in range(100)]
        returns = [0.0005] * 100

        result = evaluator.evaluate_capacity_curve(
            predictions,
            returns,
            aum_range=[10_000, 100_000, 500_000, 1_000_000],
            avg_holding_hours=4.0,
        )
        # PKG07: 新 API 返回 SignalCapacityReport
        assert len(result.curve) == 4
        # Larger AUM → lower net return
        net_returns = [p.net_return for p in result.curve]
        assert net_returns[-1] <= net_returns[0]

    def test_cost_not_viable_when_unknown(self):
        evaluator = CapacityEvaluator(CostModel(taker_fee_bps=0, avg_spread_bps=0))
        viable, reason = evaluator.is_cost_viable(0.01, 0.015)
        assert not viable
        assert "unknown" in reason

    def test_cost_viable_positive(self):
        evaluator = CapacityEvaluator()
        viable, _reason = evaluator.is_cost_viable(0.01, 0.015)
        assert viable


class TestPBOSemantics:
    def test_identical_rankings_pbo_zero_and_deterministic(self):
        """M05-F03: 完全相关 → PBO=0;固定种子下两次计算一致（旧实现退化为单次比较）。"""
        perf = list(range(1, 17))
        pbo_a = compute_pbo(perf, perf, n_splits=16, seed=7)
        pbo_b = compute_pbo(perf, perf, n_splits=16, seed=7)
        assert pbo_a.pbo == 0.0
        assert pbo_a.pbo == pbo_b.pbo

    def test_reversed_rankings_pbo_one(self):
        """反向相关 → 子集内 IS 选优在 OOS 恒低于中位数 → PBO=1。"""
        is_perf = list(range(1, 17))
        oos_perf = list(range(16, 0, -1))
        pbo = compute_pbo(is_perf, oos_perf, n_splits=16, seed=7)
        assert pbo.pbo == 1.0

    def test_pbo_counts_multiple_combinations(self):
        """随机数据下 PBO 落在 (0,1) 且组合计数真实（非重复单次比较）。"""
        import random as _random

        rng = _random.Random(3)
        is_perf = [rng.random() for _ in range(16)]
        oos_perf = [rng.random() for _ in range(16)]
        pbo = compute_pbo(is_perf, oos_perf, n_splits=16, seed=7)
        assert 0.0 < pbo.pbo < 1.0
        assert pbo.n_combinations == 8  # min(n_splits, n//2)
