"""Compatibility exports for the consolidated mean-reversion math.

New V3 callers must import from ``beidou_strategy.alpha.mean_reversion_math``.
This module remains only because the V2 engine and older research callers use
its historical path; it intentionally contains no independent implementation.
Removal is gated by the Alpha V3 production-caller migration task.
"""

from beidou_strategy.alpha.mean_reversion_math import HalfLifeResult, estimate_half_life, robust_zscore

__all__ = ["HalfLifeResult", "estimate_half_life", "robust_zscore"]
