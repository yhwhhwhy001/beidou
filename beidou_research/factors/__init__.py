"""因子研究与边际贡献分析。因子生命周期、IC/RankIC/ICIR、分层回测、衰减、退役与重启。"""

from .factor import (
    FACTOR_LIFECYCLE_TRANSITIONS,
    FactorDefinition,
    FactorEvaluator,
    FactorLifecycle,
    FactorPerformance,
    FactorRecord,
    FactorRegistry,
    MarginalContribution,
)

__all__ = [
    "FACTOR_LIFECYCLE_TRANSITIONS",
    "FactorDefinition",
    "FactorEvaluator",
    "FactorLifecycle",
    "FactorPerformance",
    "FactorRecord",
    "FactorRegistry",
    "MarginalContribution",
]
