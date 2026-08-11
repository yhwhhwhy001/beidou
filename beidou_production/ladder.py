"""实盘认证阶梯。L0-L5 独立发证。"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from beidou_shared.types import GateResult

logger = logging.getLogger(__name__)


class LadderLevel(str, Enum):
    L0_PAPER = "L0_PAPER"
    L1_SHADOW = "L1_SHADOW"
    L2_CANARY = "L2_CANARY"
    L3_RAMP = "L3_RAMP"
    L4_NORMAL = "L4_NORMAL"
    L5_CHAMPION = "L5_CHAMPION"


LEVEL_CAPITAL_LIMITS: dict[LadderLevel, float] = {}
CAPITAL_LIMITS_VERIFIED = False


def _load_capital_limits() -> tuple[dict[LadderLevel, float], bool]:
    """从唯一配置源加载资本限制；配置未知时只返回零上限并保持阻断。"""
    try:
        from beidou_shared.config import ConfigProvider

        settings = ConfigProvider().load()
        limits = settings.capital_ladder.capital_limits
        expected = {level.name for level in LadderLevel}
        if not limits or set(limits) != expected or settings.source.startswith("safety_only"):
            raise ValueError("capital limits are incomplete or safety-only")
        parsed = {LadderLevel(k): float(v) for k, v in limits.items() if k in LadderLevel.__members__}
        if len(parsed) != len(LadderLevel) or any(not math.isfinite(v) or v < 0 for v in parsed.values()):
            raise ValueError("capital limits contain invalid values")
        return parsed, True
    except Exception as exc:
        logger.warning("capital limits config unavailable; ladder remains blocked: %s", type(exc).__name__)
    return dict.fromkeys(LadderLevel, 0.0), False


LEVEL_CAPITAL_LIMITS, CAPITAL_LIMITS_VERIFIED = _load_capital_limits()


@dataclass
class GateCertificate:
    gate: str
    level: LadderLevel
    result: GateResult
    issued_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    evidence_paths: list[str] = field(default_factory=list)
    capital_limit: float = 0.0
    stop_conditions: list[str] = field(default_factory=list)
    degradation_conditions: list[str] = field(default_factory=list)

    def is_pass(self) -> bool:
        return self.result == GateResult.PASS


class ProductionLadder:
    """实盘阶梯管理器。Gate 独立发证，不可跳级。"""

    def __init__(
        self,
        capital_limits: dict[LadderLevel, float] | None = None,
        *,
        max_drawdown_pct: float | None = None,
        max_incidents: int | None = None,
    ) -> None:
        self._certificates: dict[LadderLevel, GateCertificate] = {}
        self._current_level: LadderLevel = LadderLevel.L0_PAPER
        if capital_limits is None:
            self._capital_limits = dict(LEVEL_CAPITAL_LIMITS)
            self._configuration_verified = CAPITAL_LIMITS_VERIFIED
        else:
            if set(capital_limits) != set(LadderLevel) or any(
                not math.isfinite(float(value)) or float(value) < 0 for value in capital_limits.values()
            ):
                raise ValueError("capital_limits must explicitly define every ladder level")
            self._capital_limits = {level: float(value) for level, value in capital_limits.items()}
            self._configuration_verified = True
        self._max_drawdown_pct = max_drawdown_pct
        self._max_incidents = max_incidents
        if max_drawdown_pct is None or max_incidents is None:
            try:
                from beidou_shared.config import ConfigProvider

                settings = ConfigProvider().load()
                if not settings.source.startswith("safety_only"):
                    if self._max_drawdown_pct is None:
                        self._max_drawdown_pct = float(settings.production.max_drawdown_pct)
                    if self._max_incidents is None:
                        self._max_incidents = int(settings.production.max_consecutive_losses)
            except Exception as exc:
                logger.warning("degradation policy unavailable; ladder remains fail-closed: %s", type(exc).__name__)
        self._degradation_policy_verified = (
            self._max_drawdown_pct is not None
            and self._max_incidents is not None
            and math.isfinite(float(self._max_drawdown_pct))
            and float(self._max_drawdown_pct) >= 0
            and int(self._max_incidents) >= 0
        )

    def certify(self, level: LadderLevel, result: GateResult, evidence: list[str]) -> GateCertificate:
        # BD-CV52: 不可跳级 — 必须验证前置 gate 证书链
        if result == GateResult.PASS:
            if not self._configuration_verified or not evidence or any(not str(e).strip() for e in evidence):
                result = GateResult.UNVERIFIABLE
            elif not self.can_promote_to(level):
                result = GateResult.UNVERIFIABLE
        cert = GateCertificate(
            gate=f"G{list(LadderLevel).index(level) + 4}",
            level=level,
            result=result,
            evidence_paths=evidence,
            capital_limit=self._capital_limits.get(level, 0.0),
        )
        self._certificates[level] = cert
        if result == GateResult.PASS:
            self._current_level = level
        return cert

    def can_promote_to(self, target: LadderLevel) -> bool:
        levels = list(LadderLevel)
        current_idx = levels.index(self._current_level)
        target_idx = levels.index(target)
        if target_idx <= current_idx:
            return False
        if not self._configuration_verified:
            return False
        for i in range(current_idx + 1, target_idx):
            cert = self._certificates.get(levels[i])
            if cert is None or cert.result != GateResult.PASS:
                return False
        return True

    def current_capital_limit(self) -> float:
        return self._capital_limits.get(self._current_level, 0.0)

    def should_degrade(self, pnl_drawdown_pct: float, sharpe_rolling: float | None, incident_count: int) -> bool:
        """晋级不使用短期正 Sharpe 作为单一条件。"""
        if not self._degradation_policy_verified or self._max_drawdown_pct is None or self._max_incidents is None:
            return True
        if incident_count > int(self._max_incidents):
            return True
        return pnl_drawdown_pct > float(self._max_drawdown_pct)
