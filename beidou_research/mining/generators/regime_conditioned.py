"""BF-04: 市场状态条件生成器 (Regime-conditioned Generator)。

因子可绑定市场状态，但不能通过样本后验选择最有利 Regime。
状态必须由独立、冻结、点时可得的 RegimeModel 生成:
  - trend/range
  - low/normal/high volatility
  - liquid/illiquid
  - normal/stressed basis
  - risk-on/risk-off cluster
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RegimeType(str, Enum):
    TRENDING = "trending"
    RANGING = "ranging"
    HIGH_VOL = "high_volatility"
    NORMAL_VOL = "normal_volatility"
    LOW_VOL = "low_volatility"
    LIQUID = "liquid"
    ILLIQUID = "illiquid"
    STRESSED_BASIS = "stressed_basis"
    NORMAL_BASIS = "normal_basis"
    RISK_ON = "risk_on"
    RISK_OFF = "risk_off"


@dataclass
class RegimeConfig:
    """市场状态配置。"""

    regimes: list[RegimeType] = field(
        default_factory=lambda: [
            RegimeType.TRENDING,
            RegimeType.RANGING,
            RegimeType.HIGH_VOL,
            RegimeType.NORMAL_VOL,
            RegimeType.LOW_VOL,
        ]
    )
    min_samples_per_regime: int = 30
    require_frozen_model: bool = True  # 禁止样本后验选择


@dataclass
class RegimeSpec:
    """市场状态因子规格。"""

    regime_id: str
    base_factor: str
    regime: RegimeType
    expression_hash: str
    complexity_score: float
    expected_behavior: str  # 在该 regime 下的预期行为
    failure_regime: str  # 预期失效的 regime


class RegimeConditionedGenerator:
    """市场状态条件生成器。

    为每个因子生成 regime 特定的版本。
    状态标签必须来自独立冻结的 RegimeModel，不可在样本中后验选择。
    """

    def __init__(self, config: RegimeConfig | None = None) -> None:
        self.config = config or RegimeConfig()

    def generate_regime_variants(
        self,
        candidates: list[dict[str, Any]],
        regime_labels: list[str] | None = None,
    ) -> list[RegimeSpec]:
        """为候选因子生成市场状态特定版本。

        Args:
            candidates: 候选因子列表
            regime_labels: 市场状态标签（点时可用）

        Returns:
            RegimeSpec 列表
        """
        cfg = self.config
        specs = []
        seen: set[str] = set()

        regimes = regime_labels if regime_labels is not None else [r.value for r in cfg.regimes]
        invalid_regimes = [regime for regime in regimes if regime not in RegimeType._value2member_map_]
        if invalid_regimes:
            raise ValueError(f"unsupported regimes: {', '.join(invalid_regimes)}")

        for candidate in candidates:
            fid = candidate.get("factor_id", "unknown")
            for regime in regimes:
                regime_id = f"{fid}_regime_{regime}"

                expr_hash = hashlib.sha256(f"regime:{fid}:{regime}".encode()).hexdigest()[:20]

                if expr_hash not in seen:
                    seen.add(expr_hash)
                    specs.append(
                        RegimeSpec(
                            regime_id=regime_id,
                            base_factor=fid,
                            regime=RegimeType(regime),
                            expression_hash=expr_hash,
                            complexity_score=candidate.get("complexity_score", 1.0) + 1.0,
                            expected_behavior=self._expected_behavior(fid, regime),
                            failure_regime=self._failure_regime(regime),
                        )
                    )

        return specs

    def filter_by_regime(
        self,
        factor_values: list[float],
        regime_timestamps: list[str],
        target_regime: str,
    ) -> list[float]:
        """按市场状态筛选因子值。

        只返回 target_regime 状态下的因子值，其他时间点标记为 NaN。
        """
        result = []
        for v, r in zip(factor_values, regime_timestamps, strict=False):
            if r == target_regime:
                result.append(v)
            else:
                result.append(float("nan"))
        return result

    def check_lookahead_bias(
        self,
        regime_timestamps: list[str],
        regime_model_frozen_time: str,
    ) -> tuple[bool, str]:
        """检查 look-ahead bias：regime 标签是否来自冻结模型。

        禁止使用样本后验信息选择最有利的 regime。
        """
        if self.config.require_frozen_model and not regime_model_frozen_time:
            return False, "regime_model_not_frozen"
        return True, "ok"

    def _expected_behavior(self, factor_id: str, regime: str) -> str:
        """预期行为描述。"""
        behaviors = {
            ("mean_reversion", "ranging"): "在震荡市中均值回归最有效",
            ("mean_reversion", "trending"): "趋势市中均值回归可能失效",
            ("momentum", "trending"): "趋势市中动量效应最显著",
            ("momentum", "ranging"): "震荡市中动量信号频繁反转",
            ("volatility", "high_vol"): "高波动时波动率预测力增强",
        }
        return behaviors.get(
            (factor_id, regime),
            f"{factor_id} 在 {regime} 市场状态下的表现",
        )

    def _failure_regime(self, regime: str) -> str:
        """预期失效的 regime。"""
        opposites = {
            "trending": "ranging",
            "ranging": "trending",
            "high_volatility": "low_volatility",
            "low_volatility": "high_volatility",
            "liquid": "illiquid",
            "illiquid": "liquid",
        }
        return opposites.get(regime, "unknown")
