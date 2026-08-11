"""BD-CV30: Hedge/One-way 净额规则 + Target 绑定。

空头 target_position 必须为负。
所有 target 绑定 UniverseSnapshot/InstrumentRuleSnapshot/StrategyDecision/Risk policy ids。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class PositionMode(str, Enum):
    HEDGE = "HEDGE"
    ONEWAY = "ONEWAY"


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


@dataclass(frozen=True)
class NettingResult:
    """BD-CV30: 净额计算结果。"""

    symbol: str
    mode: PositionMode
    long_exposure: float = 0.0
    short_exposure: float = 0.0
    net_position: float = 0.0
    gross_exposure: float = 0.0


@dataclass(frozen=True)
class BoundTarget:
    """BD-CV30: 绑定所有相关 hash 的组合目标。"""

    symbol: str
    direction: Direction
    target_position: float  # 空头为负
    current_position: float
    delta: float
    gross_exposure: float
    net_exposure: float
    margin_used: float = 0.0
    leverage: float = 1.0
    confidence: float = 0.0
    strategy_attribution: str = ""
    universe_snapshot_id: str = ""
    instrument_rule_snapshot_id: str = ""
    strategy_decision_id: str = ""
    risk_policy_id: str = ""
    target_hash: str = ""


def compute_hedge_netting(long_positions: dict[str, float], short_positions: dict[str, float]) -> dict[str, NettingResult]:
    """BD-CV30: Hedge mode — LONG 和 SHORT 分别独立。

    BUY→增加 LONG 仓位, SELL→增加 SHORT 仓位（不互相抵消）。
    """
    results: dict[str, NettingResult] = {}
    all_symbols = set(long_positions.keys()) | set(short_positions.keys())

    for symbol in all_symbols:
        long_qty = long_positions.get(symbol, 0.0)
        short_qty = short_positions.get(symbol, 0.0)
        results[symbol] = NettingResult(
            symbol=symbol,
            mode=PositionMode.HEDGE,
            long_exposure=abs(long_qty),
            short_exposure=abs(short_qty),
            net_position=long_qty - abs(short_qty),
            gross_exposure=abs(long_qty) + abs(short_qty),
        )
    return results


def compute_oneway_netting(positions: dict[str, float]) -> dict[str, NettingResult]:
    """BD-CV30: One-way mode — 单方向。

    BUY 和 SELL 互相抵消，同一 symbol 只能有一个方向。
    """
    results: dict[str, NettingResult] = {}
    for symbol, net in positions.items():
        results[symbol] = NettingResult(
            symbol=symbol,
            mode=PositionMode.ONEWAY,
            long_exposure=max(0.0, net),
            short_exposure=abs(min(0.0, net)),
            net_position=net,
            gross_exposure=abs(net),
        )
    return results


def bind_portfolio_target(
    symbol: str,
    target_position: float,
    current_position: float,
    direction: Direction,
    universe_snapshot_id: str = "",
    rule_snapshot_id: str = "",
    strategy_decision_id: str = "",
    risk_policy_id: str = "",
    confidence: float = 0.5,
    leverage: float = 1.0,
) -> BoundTarget:
    """BD-CV30: 绑定所有相关 hash 的 SignedPortfolioTarget。

    空头 target_position 必须为负。
    gross >= abs(net) 始终成立。
    """
    delta = target_position - current_position
    gross = abs(target_position)
    net = target_position
    margin = gross / max(leverage, 0.01)

    import hashlib, json

    hash_data = {
        "symbol": symbol, "target": target_position, "current": current_position,
        "universe": universe_snapshot_id, "rule": rule_snapshot_id,
        "strategy": strategy_decision_id, "risk": risk_policy_id,
    }
    target_hash = hashlib.sha256(json.dumps(hash_data, sort_keys=True).encode()).hexdigest()[:16]

    return BoundTarget(
        symbol=symbol,
        direction=direction,
        target_position=target_position,
        current_position=current_position,
        delta=delta,
        gross_exposure=gross,
        net_exposure=net,
        margin_used=margin,
        leverage=leverage,
        confidence=confidence,
        universe_snapshot_id=universe_snapshot_id,
        instrument_rule_snapshot_id=rule_snapshot_id,
        strategy_decision_id=strategy_decision_id,
        risk_policy_id=risk_policy_id,
        target_hash=target_hash,
    )
