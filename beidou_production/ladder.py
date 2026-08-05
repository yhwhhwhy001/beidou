
"""实盘认证阶梯。L0-L5 独立发证。"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from beidou_shared.types import GateResult, MonetaryValue

class LadderLevel(str, Enum):
    L0_PAPER = "L0_PAPER"
    L1_SHADOW = "L1_SHADOW"
    L2_CANARY = "L2_CANARY"
    L3_RAMP = "L3_RAMP"
    L4_NORMAL = "L4_NORMAL"
    L5_CHAMPION = "L5_CHAMPION"

LEVEL_CAPITAL_LIMITS = {
    LadderLevel.L0_PAPER: 0.0, LadderLevel.L1_SHADOW: 0.0,
    LadderLevel.L2_CANARY: 100.0, LadderLevel.L3_RAMP: 1000.0,
    LadderLevel.L4_NORMAL: 10000.0, LadderLevel.L5_CHAMPION: 50000.0,
}

@dataclass
class GateCertificate:
    gate: str; level: LadderLevel; result: GateResult
    issued_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    evidence_paths: list[str] = field(default_factory=list)
    capital_limit: float = 0.0; stop_conditions: list[str] = field(default_factory=list)
    degradation_conditions: list[str] = field(default_factory=list)

    def is_pass(self) -> bool: return self.result == GateResult.PASS

class ProductionLadder:
    """实盘阶梯管理器。Gate 独立发证，不可跳级。"""
    def __init__(self):
        self._certificates: dict[LadderLevel, GateCertificate] = {}
        self._current_level: LadderLevel = LadderLevel.L0_PAPER

    def certify(self, level: LadderLevel, result: GateResult, evidence: list[str]) -> GateCertificate:
        cert = GateCertificate(gate=f"G{list(LadderLevel).index(level)+4}", level=level, result=result, evidence_paths=evidence, capital_limit=LEVEL_CAPITAL_LIMITS[level])
        self._certificates[level] = cert
        if result == GateResult.PASS: self._current_level = level
        return cert

    def can_promote_to(self, target: LadderLevel) -> bool:
        levels = list(LadderLevel)
        current_idx = levels.index(self._current_level)
        target_idx = levels.index(target)
        if target_idx <= current_idx: return False
        for i in range(current_idx + 1, target_idx):
            cert = self._certificates.get(levels[i])
            if cert is None or cert.result != GateResult.PASS: return False
        return True

    def current_capital_limit(self) -> float:
        return LEVEL_CAPITAL_LIMITS[self._current_level]

    def should_degrade(self, pnl_drawdown_pct: float, sharpe_rolling: float | None, incident_count: int) -> bool:
        """晋级不使用短期正 Sharpe 作为单一条件。"""
        if incident_count > 3: return True
        if pnl_drawdown_pct > 20.0: return True
        return False
