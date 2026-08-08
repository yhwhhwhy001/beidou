"""BD-T10: Fill 与 Position 权威链。

FillEvent 是仓位的唯一输入源。PositionAggregate 支持
add/reduce/flatten/reverse/partial/fees/realized PnL。
PositionProjection 可从 fills 完全重建。
"""

from beidou_safety.position.projection import (
    FillEvent,
    PositionAggregate,
    PositionProjection,
    PositionSide,
)

__all__ = [
    "FillEvent",
    "PositionAggregate",
    "PositionProjection",
    "PositionSide",
]
