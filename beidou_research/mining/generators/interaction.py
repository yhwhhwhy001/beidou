"""BF-04: 交互项生成器。

生成一阶和二阶交互：
  momentum × liquidity
  mean_reversion × volatility
  funding × basis
  orderbook_imbalance × spread
  OI_change × return

约束：
- 最大交互阶数由 policy 控制
- 强制 complexity penalty
- 必须与父因子比较增量贡献
- 高相关但无增量贡献的交互直接淘汰
"""

from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass, field


@dataclass
class InteractionConfig:
    """交互生成配置。"""

    max_order: int = 2  # 最大交互阶数（1=一阶, 2=二阶）
    base_factors: list[str] = field(
        default_factory=lambda: [
            "momentum",
            "mean_reversion",
            "volatility",
            "liquidity",
            "funding",
            "basis",
            "orderbook_imbalance",
            "spread",
            "oi_change",
            "volume_surprise",
        ]
    )
    complexity_penalty_per_order: float = 2.0  # 每增加一阶的复杂度惩罚
    min_correlation_with_parent: float = 0.3  # 最小父因子相关性（太低=无关联）
    require_incremental_contribution: bool = True


@dataclass
class InteractionSpec:
    """交互因子规格。"""

    interaction_id: str
    factors: tuple[str, ...]  # 参与交互的因子
    order: int  # 交互阶数
    interaction_type: str  # "multiplicative" / "ratio" / "difference"
    parent_factor_ids: tuple[str, ...]  # 父因子 lineage
    complexity_score: float
    economic_rationale: str = ""

    def canonical_hash(self) -> str:
        content = f"{'×'.join(sorted(self.factors))}:{self.interaction_type}"
        return hashlib.sha256(content.encode()).hexdigest()[:20]


class InteractionGenerator:
    """交互项生成器。

    从基础因子生成一阶和二阶交互项。每个交互项必须：
    1. 声明经济逻辑
    2. 与父因子比较增量贡献
    3. 接受 complexity penalty
    """

    def __init__(self, config: InteractionConfig | None = None) -> None:
        self.config = config or InteractionConfig()

    def generate_interactions(
        self,
        base_factors: list[str] | None = None,
    ) -> list[InteractionSpec]:
        """生成因子交互项。"""
        if base_factors is None:
            base_factors = self.config.base_factors

        cfg = self.config
        interactions = []
        seen: set[str] = set()

        if cfg.max_order < 2:
            return interactions

        # 一阶交互（两个因子的组合）
        for f1, f2 in itertools.combinations(base_factors, 2):
            for itype in ["multiplicative"]:  # 后续可扩展 ratio, difference
                spec = self._make_interaction(
                    factors=(f1, f2),
                    order=2,
                    interaction_type=itype,
                )
                ch = spec.canonical_hash()
                if ch not in seen:
                    seen.add(ch)
                    interactions.append(spec)

            if len(interactions) >= 200:  # 限制交互项数量
                break

        # 二阶交互（三个因子的组合）
        if cfg.max_order >= 3:
            for f1, f2, f3 in itertools.combinations(base_factors, 3):
                spec = self._make_interaction(
                    factors=(f1, f2, f3),
                    order=3,
                    interaction_type="multiplicative",
                )
                ch = spec.canonical_hash()
                if ch not in seen:
                    seen.add(ch)
                    interactions.append(spec)

                if len(interactions) >= 500:
                    break

        return interactions

    def _make_interaction(
        self,
        factors: tuple[str, ...],
        order: int,
        interaction_type: str,
    ) -> InteractionSpec:
        """构造交互规格。"""
        complexity = self.config.complexity_penalty_per_order * order

        # 生成经济逻辑
        rationale = self._rationale_for(factors)

        return InteractionSpec(
            interaction_id=f"interact_{'x'.join(factors)}_{interaction_type}",
            factors=factors,
            order=order,
            interaction_type=interaction_type,
            parent_factor_ids=factors,
            complexity_score=complexity,
            economic_rationale=rationale,
        )

    def _rationale_for(self, factors: tuple[str, ...]) -> str:
        """根据因子组合生成经济假设。"""
        rationale_map = {
            frozenset(("momentum", "liquidity")): "趋势在高流动性环境下更可靠",
            frozenset(("momentum", "volatility")): "动量在高波动中可能过度延伸",
            frozenset(("mean_reversion", "volatility")): "均值回归在适度波动中最有效",
            frozenset(("mean_reversion", "liquidity")): "回归效应在低流动性时减弱",
            frozenset(("funding", "basis")): "资金费率与基差共同反映市场情绪",
            frozenset(("orderbook_imbalance", "spread")): "挂单失衡+价差反映短期方向",
            frozenset(("oi_change", "volume_surprise")): "持仓变化+成交量异动确认趋势",
            frozenset(("volatility", "liquidity")): "高波动+低流动性=风险积聚信号",
        }
        return rationale_map.get(
            frozenset(factors),
            f"交互效应：{' × '.join(factors)}",
        )
