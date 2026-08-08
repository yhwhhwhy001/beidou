"""自适应止盈止损计算器。

根据每个交易对的市场特征动态计算止损百分比和风险回报比，替代一刀切的固定参数。

止损 = f(ATR, 波动率区间, 点差, 价格档位)
止盈 = f(趋势强度, RSI, 波动率, 市场状态)

价格档位感知: 确保止损距离不小于最小价格精度，避免低价币种四舍五入问题。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

# ================================================================
# 可调常量 — BD-T11 v2 自适应策略参数
# ================================================================
ATR_MULTIPLIER = 2.0  # ATR 止损倍数
BASE_RR_RATIO = 2.0  # 基础风险回报比
MAX_STOP_PCT = 5.0  # 风控硬上限
MIN_RR_RATIO = 1.0  # 最低风险回报比
MAX_RR_RATIO = 5.0  # 最高风险回报比


class VolatilityRegime(str, Enum):
    """波动率区间。"""

    LOW = "LOW"  # 年化波动 < 20%
    NORMAL = "NORMAL"  # 20% - 40%
    HIGH = "HIGH"  # > 40%


class PriceTier(str, Enum):
    """价格档位 — 决定最小止损百分比，避免低价币种精度问题。"""

    MICRO = "MICRO"  # < $1
    LOW = "LOW"  # $1 - $10
    MID = "MID"  # $10 - $100
    HIGH = "HIGH"  # > $100


class MarketRegime(str, Enum):
    """市场状态 — 影响止盈策略。"""

    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING = "RANGING"
    VOLATILE = "VOLATILE"


@dataclass(frozen=True, slots=True)
class AdaptiveProtectionConfig:
    """自适应保护配置 — 计算结果的不可变载体。"""

    stop_loss_config: dict[str, Any]
    take_profit_config: dict[str, Any]
    stop_pct: float
    rr_ratio: float
    atr_pct: float
    volatility_regime: VolatilityRegime
    price_tier: PriceTier
    market_regime: MarketRegime
    metadata: dict[str, float]


class AdaptiveProtectionCalculator:
    """自适应止盈止损计算器。

    使用方式:
        features = feed.get_kline_features("BTCUSDT")
        config = AdaptiveProtectionCalculator.calculate("BTCUSDT", entry_price=65000, features=features)
        mgr.create_protection(..., **config.to_dict())
    """

    # ---- 价格档位 → 最小止损百分比 ----
    _MIN_STOP_BY_TIER: dict[PriceTier, float] = {
        PriceTier.MICRO: 2.5,  # < $1: 至少 2.5% 防止四舍五入
        PriceTier.LOW: 2.0,  # $1-10: 至少 2.0%
        PriceTier.MID: 1.2,  # $10-100: 至少 1.2%
        PriceTier.HIGH: 0.8,  # > $100: 至少 0.8%
    }

    # ---- 波动率区间 → 止损调整系数 ----
    _VOL_REGIME_FACTOR: dict[VolatilityRegime, float] = {
        VolatilityRegime.LOW: 1.5,  # 低波时放宽止损，避免噪音触发
        VolatilityRegime.NORMAL: 1.0,  # 正常区间标准倍数
        VolatilityRegime.HIGH: 1.3,  # 高波时加宽止损，避免频繁触发
    }

    # ---- 波动率区间阈值 ----
    _VOL_LOW_THRESHOLD = 0.20  # 20% 年化
    _VOL_HIGH_THRESHOLD = 0.40  # 40% 年化

    @classmethod
    def _classify_price_tier(cls, price: float) -> PriceTier:
        if price < 1.0:
            return PriceTier.MICRO
        elif price < 10.0:
            return PriceTier.LOW
        elif price < 100.0:
            return PriceTier.MID
        else:
            return PriceTier.HIGH

    @classmethod
    def _classify_volatility(cls, ann_vol: float) -> VolatilityRegime:
        if ann_vol < cls._VOL_LOW_THRESHOLD:
            return VolatilityRegime.LOW
        elif ann_vol > cls._VOL_HIGH_THRESHOLD:
            return VolatilityRegime.HIGH
        else:
            return VolatilityRegime.NORMAL

    @classmethod
    def _classify_market(cls, trend_20: float, rsi: float, ann_vol: float) -> MarketRegime:
        """根据趋势、RSI、波动率判断市场状态。"""
        if ann_vol > 0.50:
            return MarketRegime.VOLATILE
        if abs(trend_20) < 2.0:
            return MarketRegime.RANGING
        if trend_20 > 0:
            return MarketRegime.TRENDING_UP
        return MarketRegime.TRENDING_DOWN

    @classmethod
    def compute_stop_pct(
        cls,
        atr_pct: float,
        ann_vol: float,
        spread_bps: float,
        price: float,
    ) -> tuple[float, VolatilityRegime, PriceTier]:
        """计算自适应止损百分比。

        Args:
            atr_pct: ATR 占价格百分比 (例如 0.54 表示 0.54%)
            ann_vol: 年化波动率 (例如 0.18 表示 18%)
            spread_bps: 买卖点差 (bps, 例如 1.5)
            price: 当前价格

        Returns:
            (stop_pct, volatility_regime, price_tier)
        """
        price_tier = cls._classify_price_tier(price)
        vol_regime = cls._classify_volatility(ann_vol)

        # 核心公式: ATR * 倍数 * 波动率系数 * 点差缓冲
        spread_factor = 1.0 + max(0, spread_bps - 1.0) / 200.0
        vol_factor = cls._VOL_REGIME_FACTOR[vol_regime]

        raw_stop = atr_pct * ATR_MULTIPLIER * vol_factor * spread_factor

        # 应用最小值和最大值约束
        min_stop = cls._MIN_STOP_BY_TIER[price_tier]
        stop_pct = max(min_stop, min(raw_stop, MAX_STOP_PCT))

        return round(stop_pct, 2), vol_regime, price_tier

    @classmethod
    def compute_rr_ratio(
        cls,
        stop_pct: float,
        trend_20_pct: float,
        rsi: float,
        ann_vol: float,
    ) -> tuple[float, MarketRegime]:
        """计算自适应风险回报比。

        Args:
            stop_pct: 已计算的止损百分比
            trend_20_pct: 20周期趋势 (%)
            rsi: RSI-14
            ann_vol: 年化波动率

        Returns:
            (rr_ratio, market_regime)
        """
        market_regime = cls._classify_market(trend_20_pct, rsi, ann_vol)

        # 趋势因子：强趋势让利润跑，震荡快止盈
        abs_trend = abs(trend_20_pct)
        if abs_trend >= 10.0:
            trend_factor = 1.5
        elif abs_trend >= 5.0:
            trend_factor = 1.25
        elif abs_trend >= 2.0:
            trend_factor = 1.0
        else:
            trend_factor = 0.8

        # RSI 因子：超买时快止盈，超卖时放宽（预期反弹）
        if rsi > 75:
            rsi_factor = 0.8
        elif rsi > 65:
            rsi_factor = 0.9
        elif rsi < 25:
            rsi_factor = 1.3
        elif rsi < 35:
            rsi_factor = 1.15
        else:
            rsi_factor = 1.0

        # 波动率因子：高波动时降低 RR（更难达到远距离目标）
        if ann_vol > 0.50:
            vol_rr_factor = 0.7
        elif ann_vol > 0.35:
            vol_rr_factor = 0.85
        else:
            vol_rr_factor = 1.0

        rr_ratio = BASE_RR_RATIO * trend_factor * rsi_factor * vol_rr_factor
        rr_ratio = max(MIN_RR_RATIO, min(rr_ratio, MAX_RR_RATIO))

        return round(rr_ratio, 2), market_regime

    @classmethod
    def calculate(
        cls,
        symbol: str,
        entry_price: float,
        features: dict[str, float] | None = None,
    ) -> AdaptiveProtectionConfig:
        """主入口：计算自适应止盈止损配置。

        Args:
            symbol: 交易对 (如 "BTCUSDT")
            entry_price: 入场价格
            features: 市场特征数据 (来自 MarketDataFeed.get_kline_features)
                      如果为 None，使用保守默认值

        Returns:
            AdaptiveProtectionConfig
        """
        if features is None or not features:
            # BD-T11: 无数据时不使用硬编码回退 — 返回 stop_pct=0 阻断新风险
            price_tier = cls._classify_price_tier(entry_price)
            return AdaptiveProtectionConfig(
                stop_loss_config={"type": "ATR_BASED", "stop_pct": 0.0, "multiplier": ATR_MULTIPLIER},
                take_profit_config={"type": "FIXED_RR", "rr_ratio": BASE_RR_RATIO},
                stop_pct=0.0,
                rr_ratio=BASE_RR_RATIO,
                atr_pct=0.0,
                volatility_regime=VolatilityRegime.NORMAL,
                price_tier=price_tier,
                market_regime=MarketRegime.RANGING,
                metadata={"fallback": True, "blocked": True, "reason": "NO_MARKET_DATA"},
            )

        # 提取特征
        atr_pct = features.get("atr_pct", 1.0)
        ann_vol = features.get("ann_volatility", 0.30)
        rsi = features.get("rsi_14", 50.0)
        trend_20 = features.get("trend_20_pct", 0.0)
        spread_bps = features.get("spread_bps", 1.0)
        current_price = features.get("close", entry_price)

        # 如果 ATR 无效，用波动率估算
        if atr_pct <= 0 or atr_pct > 20:
            atr_pct = ann_vol / (365 * 24) ** 0.5 * 100  # 从年化波动率反推小时 ATR%
            atr_pct = max(0.1, min(atr_pct, 10.0))

        # 计算止损
        stop_pct, vol_regime, price_tier = cls.compute_stop_pct(
            atr_pct=atr_pct,
            ann_vol=ann_vol,
            spread_bps=spread_bps,
            price=current_price,
        )

        # 计算止盈 RR
        rr_ratio, market_regime = cls.compute_rr_ratio(
            stop_pct=stop_pct,
            trend_20_pct=trend_20,
            rsi=rsi,
            ann_vol=ann_vol,
        )

        return AdaptiveProtectionConfig(
            stop_loss_config={
                "type": "ATR_BASED",
                "atr": atr_pct * current_price / 100,  # 转换为绝对 ATR 值
                "multiplier": ATR_MULTIPLIER,
                "stop_pct": stop_pct,
            },
            take_profit_config={
                "type": "FIXED_RR",
                "rr_ratio": rr_ratio,
            },
            stop_pct=stop_pct,
            rr_ratio=rr_ratio,
            atr_pct=atr_pct,
            volatility_regime=vol_regime,
            price_tier=price_tier,
            market_regime=market_regime,
            metadata={
                "atr_pct": atr_pct,
                "ann_vol": ann_vol,
                "rsi": rsi,
                "trend_20_pct": trend_20,
                "spread_bps": spread_bps,
            },
        )
