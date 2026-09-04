"""Candidate-signal mining: a typed expression language and an enumerating search (recovered from V2).

The miner proposes candidates; `beidou_alpha.validation` and the D-020/D-028 verdict dispose of them.
"""

from beidou_alpha.mining.expr import (
    Const,
    CrossSectional,
    Dim,
    Expr,
    ExprError,
    Mul,
    RangePosition,
    Ratio,
    Ret,
    Squash,
    Sum,
    TakerBuy,
    Vol,
    VolumeRatio,
    ZScore,
)
from beidou_alpha.mining.search import Candidate, SearchResult, enumerate_candidates, to_signal

__all__ = [
    "Candidate",
    "Const",
    "CrossSectional",
    "Dim",
    "Expr",
    "ExprError",
    "Mul",
    "RangePosition",
    "Ratio",
    "Ret",
    "SearchResult",
    "Squash",
    "Sum",
    "TakerBuy",
    "Vol",
    "VolumeRatio",
    "ZScore",
    "enumerate_candidates",
    "to_signal",
]
