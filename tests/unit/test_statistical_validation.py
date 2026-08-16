"""BD-CV20 统计验证测试（M05-F02 后）。

M05-F02: statistics/validation.py 的假内核已退役为 fail-closed 桩
（占位实现若被接线会静默放行假证据）。真实内核在
mining/evaluation/{purged_walk_forward,cpcv,multiple_testing}。
本文件断言: 桩在正常输入上 raise、小样本边界保持原语义、Holm 真实现
继续可用。
"""

from __future__ import annotations

import math

import pytest

from beidou_research.statistics.validation import (
    CPCVResult,
    DSRResult,
    HolmResult,
    PBOResult,
    PurgedWFCVResult,
)


class TestRetiredPurgedWFCV:
    def test_normal_input_raises(self):
        with pytest.raises(NotImplementedError):
            PurgedWFCVResult.run(n_samples=1000, n_splits=5)

    def test_small_sample_handled(self):
        result = PurgedWFCVResult.run(n_samples=5, n_splits=3)
        assert math.isnan(result.mean_score) or result.n_splits == 0


class TestRetiredCPCV:
    def test_normal_input_raises(self):
        with pytest.raises(NotImplementedError):
            CPCVResult.run(n_samples=1000, n_groups=6, test_groups=2)

    def test_invalid_input(self):
        result = CPCVResult.run(n_samples=10, n_groups=2, test_groups=2)
        assert result.n_groups == 0


class TestRetiredPBO:
    def test_normal_input_raises(self):
        with pytest.raises(NotImplementedError):
            PBOResult.compute([1.5, 1.2, 1.0], [1.0, 1.3, 0.9], n_combos=100)

    def test_small_input(self):
        result = PBOResult.compute([1.0], [1.0])
        assert result.is_overfit is True


class TestRetiredDSR:
    def test_normal_input_raises(self):
        with pytest.raises(NotImplementedError):
            DSRResult.compute(observed_sharpe=1.0, n_trials=10, sample_size=100)

    def test_single_trial(self):
        result = DSRResult.compute(observed_sharpe=1.0, n_trials=1, sample_size=10)
        assert result.is_significant is False


class TestHolmStillWorks:
    """Holm-Bonferroni 是真实现（非占位），保留功能性测试。"""

    def test_basic_compute(self):
        result = HolmResult.compute([0.001, 0.01, 0.1], alpha=0.05)
        assert len(result.adjusted_p_values) == 3
        assert 0 in result.significant_indices

    def test_monotonicity(self):
        result = HolmResult.compute([0.2, 0.05, 0.3, 0.1], alpha=0.05)
        assert all(0.0 <= a <= 1.0 for a in result.adjusted_p_values)

    def test_empty_input(self):
        assert HolmResult.compute([]).raw_p_values == []

    def test_nan_handling(self):
        result = HolmResult.compute([float("nan"), 0.01])
        assert result.adjusted_p_values[0] == 1.0  # NaN → 1.0

    def test_significant_detection(self):
        result = HolmResult.compute([0.001, 0.001], alpha=0.05)
        assert result.significant_indices == [0, 1]

    def test_all_insignificant(self):
        result = HolmResult.compute([0.9, 0.8], alpha=0.05)
        assert result.significant_indices == []

    def test_monotonic_constraint(self):
        result = HolmResult.compute([0.01, 0.5, 0.02], alpha=0.05)
        adjusted = result.adjusted_p_values
        # 按原始索引: [0.01, 0.5, 0.02] → 单调约束下 adjusted[0] <= adjusted[2]
        assert adjusted[0] <= adjusted[2] + 1e-9
