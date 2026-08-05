"""BF-04: 模板网格生成器。

通过 primitive × window × transform × normalization × horizon × regime
的组合生成因子候选。高可解释性、低风险基线。

每个模板必须声明经济假设和预期失效场景。
"""

from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass, field

from beidou_research.mining.contracts import HorizonUnit

# ================================================================
# 模板配置
# ================================================================


@dataclass
class TemplateConfig:
    """模板网格生成配置 — 参数空间由 policy 定义。"""

    primitives: list[str] = field(
        default_factory=lambda: [
            "close",
            "log_return",
            "volume",
            "rsi",
            "spread",
        ]
    )
    windows: list[int] = field(default_factory=lambda: [5, 10, 20, 50, 100])
    transforms: list[str] = field(
        default_factory=lambda: [
            "identity",
            "pct_change",
            "zscore",
            "diff",
        ]
    )
    normalizations: list[str] = field(
        default_factory=lambda: [
            "none",
            "zscore",
            "robust_zscore",
            "rank",
        ]
    )
    horizons: list[int] = field(default_factory=lambda: [1, 4, 12, 24])
    horizon_units: list[HorizonUnit] = field(default_factory=lambda: [HorizonUnit.BAR])
    regimes: list[str] = field(default_factory=lambda: ["all"])
    max_combinations: int = 10000
    random_seed: int = 42


@dataclass
class TemplateSpec:
    """单个模板的定义。"""

    template_id: str
    primitive: str
    window: int
    transform: str
    normalization: str
    horizon: int
    horizon_unit: HorizonUnit
    regime: str
    economic_rationale: str = ""
    expected_failure_scenarios: list[str] = field(default_factory=list)

    def canonical_hash(self) -> str:
        content = (
            f"{self.primitive}:{self.window}:{self.transform}:"
            f"{self.normalization}:{self.horizon}:{self.horizon_unit.value}:"
            f"{self.regime}"
        )
        return hashlib.sha256(content.encode()).hexdigest()[:20]


class TemplateGridGenerator:
    """模板网格生成器。

    将 primitives × windows × transforms × normalizations × horizons
    展开为完整的模板网格，规范化后去重。
    """

    def __init__(self, config: TemplateConfig | None = None) -> None:
        self.config = config or TemplateConfig()

    def generate_templates(self) -> list[TemplateSpec]:
        """生成所有模板组合。

        笛卡尔积展开后：
        1. 过滤无效组合（如 zscore 后再 zscore）
        2. 规范化去重
        """
        cfg = self.config
        templates = []
        seen_hashes: set[str] = set()

        product = itertools.product(
            cfg.primitives,
            cfg.windows,
            cfg.transforms,
            cfg.normalizations,
            cfg.horizons,
            cfg.horizon_units,
            cfg.regimes,
        )

        for primitive, window, transform, norm, horizon, horizon_unit, regime in product:
            # 过滤无效组合
            if not self._is_valid_combination(primitive, window, transform, norm):
                continue

            template = TemplateSpec(
                template_id=f"tmpl_{primitive}_w{window}_{transform}_{norm}_h{horizon}",
                primitive=primitive,
                window=window,
                transform=transform,
                normalization=norm,
                horizon=horizon,
                horizon_unit=horizon_unit,
                regime=regime,
                economic_rationale=self._rationale_for(primitive, window, transform),
                expected_failure_scenarios=self._failure_scenarios_for(regime),
            )

            ch = template.canonical_hash()
            if ch not in seen_hashes:
                seen_hashes.add(ch)
                templates.append(template)

                if len(templates) >= cfg.max_combinations:
                    break

        return templates

    def _is_valid_combination(self, primitive: str, window: int, transform: str, normalization: str) -> bool:
        """过滤无效组合。"""
        # zscore 后不再 zscore
        if transform == "zscore" and normalization == "zscore":
            return False
        # identity transform + none normalization = 啥都没做 → 跳过
        if transform == "identity" and normalization == "none":
            return False
        # window ≤ 0 无意义
        return not window <= 0

    def _rationale_for(self, primitive: str, window: int, transform: str) -> str:
        """生成经济假设说明。"""
        rationales = {
            "close": f"价格在 {window} 期内的趋势方向",
            "log_return": f"{window} 期对数收益的动量效应",
            "volume": f"{window} 期成交量异动反映市场参与度变化",
            "rsi": f"{window} 期 RSI 均值回归信号",
            "spread": f"{window} 期价差反映流动性变化",
        }
        transform_desc = {
            "identity": "原始值",
            "pct_change": "变化率",
            "zscore": "标准化偏离",
            "diff": "一阶差分",
        }
        base = rationales.get(primitive, f"{primitive} 因子")
        tx = transform_desc.get(transform, transform)
        return f"{base}（{tx}）"

    def _failure_scenarios_for(self, regime: str) -> list[str]:
        """预期失效场景。"""
        scenarios = {
            "all": ["极端行情下信号可能失效", "低流动性时换手成本上升"],
            "trending": ["震荡市中产生虚假反转信号"],
            "ranging": ["趋势市中持续亏损"],
            "high_volatility": ["波动率下降后信号衰减"],
        }
        return scenarios.get(regime, ["未指定失效场景"])
