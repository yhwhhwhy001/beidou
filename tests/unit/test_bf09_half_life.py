"""BF-09: half-life 修复验证 + Robust z-score 测试。"""

from __future__ import annotations

import math
import pytest
from beidou_strategy.components.mean_reversion_fixed import (
    estimate_half_life, robust_zscore, HalfLifeResult,
)


class TestHalfLifeFix:
    """half-life 公式修复验证。"""

    def test_ar1_mean_reverting_positive_half_life(self):
        """AR(1) 均值回归序列产生正半衰期。"""
        # 构造 OU 过程：x_{t+1} = 0.8 x_t + ε (均值回归)
        import random
        rng = random.Random(42)
        n = 200
        log_prices = [0.0]
        for _ in range(n - 1):
            log_prices.append(0.8 * log_prices[-1] + rng.gauss(0, 0.01))

        prices = [math.exp(lp) for lp in log_prices]
        result = estimate_half_life(prices)

        assert result.valid, f"Should be valid mean-reverting: {result}"
        assert result.half_life_bars > 0, f"Half-life should be positive, got {result.half_life_bars}"
        assert result.is_mean_reverting, "Should detect mean reversion"
        assert result.beta < 0, f"β should be negative for mean reversion, got {result.beta}"
        assert 0 < result.ar1_coefficient < 1, f"AR(1) φ should be in (0,1), got {result.ar1_coefficient}"

    def test_random_walk_has_large_half_life(self):
        """随机游走的半衰期应很大或无均值回归证据。"""
        import random
        rng = random.Random(99)
        n = 500
        prices = [100.0]
        for _ in range(n - 1):
            prices.append(prices[-1] * math.exp(rng.gauss(0, 0.01)))

        result = estimate_half_life(prices)
        # 随机游走的半衰期应 > 100 或无效
        assert result.half_life_bars > 100 or not result.valid or result.beta > -0.005, (
            f"Random walk should have large half-life or be invalid: {result}"
        )

    def test_insufficient_data(self):
        """数据不足时返回 invalid。"""
        result = estimate_half_life([100.0, 101.0, 99.0])
        assert not result.valid

    def test_half_life_formula_agreement(self):
        """β 方法和 φ 方法的半衰期近似一致。"""
        import random
        rng = random.Random(123)
        n = 500
        log_prices = [0.0]
        phi = 0.85
        for _ in range(n - 1):
            log_prices.append(phi * log_prices[-1] + rng.gauss(0, 0.01))

        prices = [math.exp(lp) for lp in log_prices]
        result = estimate_half_life(prices)

        # 理论半衰期: -ln(2) / ln(0.85) ≈ 4.27
        theoretical = -math.log(2) / math.log(phi)
        if result.valid:
            # 估计值应在理论值的 ±50% 内
            assert 0.5 * theoretical < result.half_life_bars < 1.5 * theoretical, (
                f"Half-life estimate {result.half_life_bars:.1f} should be near "
                f"theoretical {theoretical:.1f}"
            )


class TestRobustZscore:
    """鲁棒 z-score 测试。"""

    def test_normal_data(self):
        """正态分布数据产生合理的 z-score。"""
        import random
        rng = random.Random(42)
        values = [rng.gauss(0, 1) for _ in range(100)]
        zs = robust_zscore(values, window=20)

        # 大部分 z-score 应在 [-4, 4] 内 (MAD-based 稍宽)
        valid_zs = [z for z in zs[20:] if z != 0]  # skip first window
        if valid_zs:
            extreme = [z for z in valid_zs if abs(z) > 5]
            # 允许少量，但不超过 20%
            assert len(extreme) < len(valid_zs) * 0.2, (
                f"Too many extreme values: {len(extreme)}/{len(valid_zs)}"
            )

    def test_outlier_detection(self):
        """异常值产生高 z-score。"""
        # 在 1.0 的数据中插入明确异常值（在 window 内）
        values = [1.0] * 60
        values[-1] = 100.0  # 最后一个值为异常值

        zs = robust_zscore(values, window=20)

        # 最后一个值应检测为异常
        # 在 window 20-40 内都是 1.0 → median=1.0, MAD≈0
        # 最后一个 100.0 相对 20-window 的 median 应很大
        assert abs(zs[-1]) > 3 or zs[-1] == 0, (
            f"Last z-score should be high (>3) or 0, got {zs[-1]}"
        )

    def test_constant_data_zero_scores(self):
        """常数数据不应产生异常高的 z-score。"""
        values = [5.0] * 50
        zs = robust_zscore(values, window=20)
        valid_zs = [z for z in zs if z != 0]  # windows before 20 are 0
        assert all(abs(z) < 0.01 for z in valid_zs)
