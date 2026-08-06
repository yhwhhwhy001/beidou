"""自适应止盈止损计算器测试。覆盖各种价格档位、波动率区间、市场状态。"""

from __future__ import annotations

import pytest

from beidou_strategy.protection.adaptive import (
    ATR_MULTIPLIER,
    BASE_RR_RATIO,
    MAX_STOP_PCT,
    MIN_RR_RATIO,
    AdaptiveProtectionCalculator,
    AdaptiveProtectionConfig,
    MarketRegime,
    PriceTier,
    VolatilityRegime,
)


def _make_features(**overrides) -> dict[str, float]:
    """构造测试用市场特征。"""
    defaults = {
        "close": 100.0,
        "atr_pct": 1.0,
        "ann_volatility": 0.30,
        "rsi_14": 50.0,
        "trend_20_pct": 0.0,
        "spread_bps": 1.0,
    }
    defaults.update(overrides)
    return defaults


class TestPriceTierClassification:
    """价格档位分类测试。"""

    def test_micro_tier(self):
        assert AdaptiveProtectionCalculator._classify_price_tier(0.05) == PriceTier.MICRO
        assert AdaptiveProtectionCalculator._classify_price_tier(0.99) == PriceTier.MICRO

    def test_low_tier(self):
        assert AdaptiveProtectionCalculator._classify_price_tier(1.0) == PriceTier.LOW
        assert AdaptiveProtectionCalculator._classify_price_tier(9.99) == PriceTier.LOW

    def test_mid_tier(self):
        assert AdaptiveProtectionCalculator._classify_price_tier(10.0) == PriceTier.MID
        assert AdaptiveProtectionCalculator._classify_price_tier(99.99) == PriceTier.MID

    def test_high_tier(self):
        assert AdaptiveProtectionCalculator._classify_price_tier(100.0) == PriceTier.HIGH
        assert AdaptiveProtectionCalculator._classify_price_tier(65000.0) == PriceTier.HIGH


class TestVolatilityClassification:
    """波动率区间分类测试。"""

    def test_low_vol(self):
        assert AdaptiveProtectionCalculator._classify_volatility(0.10) == VolatilityRegime.LOW
        assert AdaptiveProtectionCalculator._classify_volatility(0.19) == VolatilityRegime.LOW

    def test_normal_vol(self):
        assert AdaptiveProtectionCalculator._classify_volatility(0.20) == VolatilityRegime.NORMAL
        assert AdaptiveProtectionCalculator._classify_volatility(0.35) == VolatilityRegime.NORMAL

    def test_high_vol(self):
        assert AdaptiveProtectionCalculator._classify_volatility(0.41) == VolatilityRegime.HIGH
        assert AdaptiveProtectionCalculator._classify_volatility(0.80) == VolatilityRegime.HIGH


class TestMarketRegimeClassification:
    """市场状态分类测试。"""

    def test_ranging(self):
        assert AdaptiveProtectionCalculator._classify_market(1.0, 50, 0.30) == MarketRegime.RANGING
        assert AdaptiveProtectionCalculator._classify_market(-1.5, 50, 0.30) == MarketRegime.RANGING

    def test_trending_up(self):
        assert AdaptiveProtectionCalculator._classify_market(5.0, 50, 0.30) == MarketRegime.TRENDING_UP

    def test_trending_down(self):
        assert AdaptiveProtectionCalculator._classify_market(-5.0, 50, 0.30) == MarketRegime.TRENDING_DOWN

    def test_volatile(self):
        assert AdaptiveProtectionCalculator._classify_market(3.0, 50, 0.55) == MarketRegime.VOLATILE


class TestStopPctComputation:
    """止损百分比计算测试。"""

    def test_btc_like_low_vol(self):
        """BTC 类：高价、低波 → 止损约为 ATR * 2 * 1.5(low vol) """
        stop_pct, regime, tier = AdaptiveProtectionCalculator.compute_stop_pct(
            atr_pct=0.30, ann_vol=0.18, spread_bps=1.0, price=65000.0
        )
        # 0.30 * 2 * 1.5 = 0.9%, min_stop for HIGH tier = 0.8%, so 0.9%
        assert stop_pct == pytest.approx(0.9, abs=0.05)
        assert regime == VolatilityRegime.LOW
        assert tier == PriceTier.HIGH

    def test_eth_like_normal_vol(self):
        """ETH 类：中价、正常波 → ATR * 2 * 1.0 """
        stop_pct, regime, tier = AdaptiveProtectionCalculator.compute_stop_pct(
            atr_pct=0.50, ann_vol=0.30, spread_bps=1.0, price=2000.0
        )
        # 0.50 * 2 * 1.0 = 1.0%, min_stop for HIGH tier = 0.8%, so 1.0%
        assert stop_pct == pytest.approx(1.0, abs=0.05)
        assert regime == VolatilityRegime.NORMAL
        assert tier == PriceTier.HIGH

    def test_doge_like_micro_price(self):
        """DOGE 类：极低价、高波 → min_stop 至少 2.5%"""
        stop_pct, regime, tier = AdaptiveProtectionCalculator.compute_stop_pct(
            atr_pct=0.54, ann_vol=0.37, spread_bps=2.0, price=0.07
        )
        # 0.54 * 2 * 1.0 * (1 + 1/200) = 1.08%, but min_stop for MICRO = 2.5%
        assert stop_pct >= 2.5
        assert tier == PriceTier.MICRO

    def test_link_like_high_vol(self):
        """LINK 类：高波 → 止损加宽"""
        stop_pct, regime, tier = AdaptiveProtectionCalculator.compute_stop_pct(
            atr_pct=0.67, ann_vol=0.48, spread_bps=1.5, price=8.26
        )
        # 0.67 * 2 * 1.3 * (1 + 0.5/200) = 1.74%, min_stop for LOW = 2.0%, so 2.0%
        assert stop_pct >= 1.5
        assert regime == VolatilityRegime.HIGH
        assert tier == PriceTier.LOW

    def test_max_stop_cap(self):
        """止损不能超过 MAX_STOP_PCT 5%"""
        stop_pct, _, _ = AdaptiveProtectionCalculator.compute_stop_pct(
            atr_pct=5.0, ann_vol=0.80, spread_bps=10.0, price=100.0
        )
        assert stop_pct <= MAX_STOP_PCT

    def test_spread_widens_stop(self):
        """点差大时止损应更宽。"""
        narrow, _, _ = AdaptiveProtectionCalculator.compute_stop_pct(
            atr_pct=1.0, ann_vol=0.30, spread_bps=1.0, price=100.0
        )
        wide, _, _ = AdaptiveProtectionCalculator.compute_stop_pct(
            atr_pct=1.0, ann_vol=0.30, spread_bps=20.0, price=100.0
        )
        assert wide >= narrow


class TestRRComputation:
    """风险回报比计算测试。"""

    def test_strong_trend_higher_rr(self):
        """强趋势时 RR 更高（让利润跑）。"""
        rr, regime = AdaptiveProtectionCalculator.compute_rr_ratio(
            stop_pct=1.0, trend_20_pct=12.0, rsi=50, ann_vol=0.30
        )
        # BASE 2.0 * 1.5 * 1.0 * 1.0 = 3.0
        assert rr > BASE_RR_RATIO
        assert regime == MarketRegime.TRENDING_UP

    def test_ranging_lower_rr(self):
        """震荡市 RR 降低（快止盈）。"""
        rr, regime = AdaptiveProtectionCalculator.compute_rr_ratio(
            stop_pct=1.0, trend_20_pct=1.0, rsi=50, ann_vol=0.30
        )
        # BASE 2.0 * 0.8 * 1.0 * 1.0 = 1.6
        assert rr < BASE_RR_RATIO
        assert regime == MarketRegime.RANGING

    def test_overbought_lower_rr(self):
        """超买时 RR 降低。"""
        rr_normal, _ = AdaptiveProtectionCalculator.compute_rr_ratio(1.0, 5.0, 50, 0.30)
        rr_overbought, _ = AdaptiveProtectionCalculator.compute_rr_ratio(1.0, 5.0, 80, 0.30)
        assert rr_overbought < rr_normal

    def test_oversold_higher_rr(self):
        """超卖时 RR 提高（预期反弹）。"""
        rr_normal, _ = AdaptiveProtectionCalculator.compute_rr_ratio(1.0, 5.0, 50, 0.30)
        rr_oversold, _ = AdaptiveProtectionCalculator.compute_rr_ratio(1.0, 5.0, 20, 0.30)
        assert rr_oversold > rr_normal

    def test_high_vol_lower_rr(self):
        """高波动时 RR 降低。"""
        rr_normal, _ = AdaptiveProtectionCalculator.compute_rr_ratio(1.0, 5.0, 50, 0.30)
        rr_high_vol, _ = AdaptiveProtectionCalculator.compute_rr_ratio(1.0, 5.0, 50, 0.60)
        assert rr_high_vol < rr_normal

    def test_rr_within_bounds(self):
        """RR 在 MIN_RR 和 MAX_RR 之间。"""
        # Test at extremes
        rr_low, _ = AdaptiveProtectionCalculator.compute_rr_ratio(1.0, 0.0, 90, 0.80)
        assert rr_low >= MIN_RR_RATIO

        rr_high, _ = AdaptiveProtectionCalculator.compute_rr_ratio(1.0, 15.0, 10, 0.10)
        assert rr_high <= 5.0  # MAX_RR_RATIO


class TestFullCalculation:
    """完整 calculate() 流程测试。"""

    def test_btc_scenario(self):
        """BTC 场景：64650 入场，低波，弱趋势。"""
        features = _make_features(
            close=64650.0, atr_pct=0.30, ann_volatility=0.18,
            rsi_14=55.0, trend_20_pct=1.5, spread_bps=0.5,
        )
        cfg = AdaptiveProtectionCalculator.calculate("BTCUSDT", 64650.0, features)
        assert cfg.stop_pct >= 0.8  # min for HIGH tier
        assert cfg.rr_ratio > 0
        assert cfg.stop_loss_config["type"] == "ATR_BASED"
        assert cfg.take_profit_config["type"] == "FIXED_RR"
        assert cfg.price_tier == PriceTier.HIGH

    def test_doge_scenario(self):
        """DOGE 场景：0.07 入场，高波。"""
        features = _make_features(
            close=0.0693, atr_pct=0.54, ann_volatility=0.37,
            rsi_14=48.0, trend_20_pct=-3.0, spread_bps=5.0,
        )
        cfg = AdaptiveProtectionCalculator.calculate("DOGEUSDT", 0.0693, features)
        # Must be at least 2.5% for MICRO tier
        assert cfg.stop_pct >= 2.5
        assert cfg.price_tier == PriceTier.MICRO

    def test_link_scenario(self):
        """LINK 场景：8.26 入场，极高波动。"""
        features = _make_features(
            close=8.26, atr_pct=0.67, ann_volatility=0.48,
            rsi_14=62.0, trend_20_pct=8.0, spread_bps=3.0,
        )
        cfg = AdaptiveProtectionCalculator.calculate("LINKUSDT", 8.26, features)
        assert cfg.volatility_regime == VolatilityRegime.HIGH
        assert cfg.market_regime == MarketRegime.TRENDING_UP
        assert cfg.stop_pct >= 1.5

    def test_fallback_no_features(self):
        """无市场数据时使用保守默认值。"""
        cfg = AdaptiveProtectionCalculator.calculate("UNKNOWN", 100.0, features=None)
        assert cfg.stop_pct >= 3.0  # fallback: max(min_stop, 3.0)
        assert cfg.rr_ratio == BASE_RR_RATIO
        assert cfg.metadata.get("fallback") is True

    def test_fallback_empty_features(self):
        """空特征时使用默认值。"""
        cfg = AdaptiveProtectionCalculator.calculate("UNKNOWN", 100.0, features={})
        assert cfg.metadata.get("fallback") is True

    def test_metadata_included(self):
        """计算结果包含诊断元数据。"""
        features = _make_features()
        cfg = AdaptiveProtectionCalculator.calculate("TEST", 100.0, features)
        assert "atr_pct" in cfg.metadata
        assert "ann_vol" in cfg.metadata
        assert "rsi" in cfg.metadata

    def test_rr_frozen_dataclass(self):
        """配置是不可变的。"""
        cfg = AdaptiveProtectionCalculator.calculate("TEST", 100.0, _make_features())
        with pytest.raises(Exception):
            cfg.stop_pct = 99.0  # type: ignore[misc]
