"""因子研究与边际贡献分析。因子生命周期、IC/RankIC/ICIR、分层回测、衰减、退役与重启。"""
from .factor import (
    FactorLifecycle,
    FACTOR_LIFECYCLE_TRANSITIONS,
    FactorDefinition,
    FactorPerformance,
    MarginalContribution,
    FactorRecord,
    FactorEvaluator,
    FactorRegistry,
)

__all__ = [
    "FactorLifecycle",
    "FACTOR_LIFECYCLE_TRANSITIONS",
    "FactorDefinition",
    "FactorPerformance",
    "MarginalContribution",
    "FactorRecord",
    "FactorEvaluator",
    "FactorRegistry",
]
