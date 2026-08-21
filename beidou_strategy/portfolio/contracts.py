"""BD-CV30/31/32/33: Wave 3 Portfolio & Risk 合约.

SignedPortfolioTarget、约束优化、自适应仓位、Durable RiskStateAuthority。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from beidou_strategy.alpha.contracts import EnsembleForecast

# ============================================================================
# BD-CV30: SignedPortfolioTarget 与多空暴露代数
# ============================================================================


class PositionSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


@dataclass(frozen=True)
class SignedPortfolioTarget:
    """BD-CV30: 签名组合目标。

    LONG/SHORT/FLAT/reversal 目标的符号与 delta 正确。
    任意组合下 gross>=abs(net) 且 attribution 可守恒。
    """

    target_id: str = ""
    symbol: str = ""
    side: PositionSide = PositionSide.FLAT
    target_exposure: float = 0.0
    delta: float = 0.0
    current_exposure: float | None = None
    inflight_exposure: float | None = None
    signature: str = ""
    created_at: str = ""

    def is_valid(self) -> bool:
        """Validate either a legacy gross bound or an exact target delta.

        A reversal can legitimately trade more than the final gross exposure
        (for example +2 to -4 requires a -6 delta).  When current/in-flight
        facts are supplied, the exact signed equation is authoritative.
        """

        values = [self.target_exposure, self.delta]
        if self.current_exposure is not None:
            values.append(self.current_exposure)
        if self.inflight_exposure is not None:
            values.append(self.inflight_exposure)
        if not all(math.isfinite(value) for value in values) or self.target_exposure < 0:
            return False
        if self.current_exposure is not None or self.inflight_exposure is not None:
            current = float(self.current_exposure or 0.0)
            inflight = float(self.inflight_exposure or 0.0)
            signed_target = (
                self.target_exposure
                if self.side is PositionSide.LONG
                else (-self.target_exposure if self.side is PositionSide.SHORT else 0.0)
            )
            return math.isclose(self.delta, signed_target - current - inflight, rel_tol=1e-12, abs_tol=1e-12)
        return abs(self.target_exposure) >= abs(self.delta) if self.target_exposure != 0 else self.delta == 0.0

    def compute_hash(self) -> str:
        data = {
            "target_id": self.target_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "target_exposure": self.target_exposure,
            "delta": self.delta,
            "current_exposure": self.current_exposure,
            "inflight_exposure": self.inflight_exposure,
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


# ============================================================================
# BD-CV31: 约束组合优化器
# ============================================================================


@dataclass(frozen=True)
class PortfolioConstraints:
    """BD-CV31: 组合约束定义。"""

    max_concentration_pct: float = 25.0
    max_leverage: float = 3.0
    min_positions: int = 1
    max_positions: int = 20
    max_correlation: float = 0.70
    capacity_utilization_pct: float = 80.0


@dataclass(frozen=True)
class OptimizationResult:
    """BD-CV31: 优化结果诊断。"""

    symbols: list[str] = field(default_factory=list)
    weights: list[float] = field(default_factory=list)
    diagnostics: dict[str, str] = field(default_factory=dict)
    constraint_violations: list[str] = field(default_factory=list)

    def is_constrained(self) -> bool:
        """构造高相关资产时风险约束显著改变配置。"""
        return len(self.constraint_violations) == 0


# ============================================================================
# BD-CV32: 自适应仓位与杠杆
# ============================================================================


@dataclass(frozen=True)
class AdaptiveSizing:
    """BD-CV32: 自适应仓位策略。

    不存在固定全局杠杆/固定订单金额主路径。
    风险输入变差时 sizing 不增加。
    """

    base_leverage: float = 1.0
    risk_adjusted_leverage: float = 1.0
    volatility_scalar: float = 1.0
    drawdown_scalar: float = 1.0
    is_safe: bool = True

    def does_size_increase(self, previous: AdaptiveSizing | None) -> bool:
        """风险变差时确保 sizing 不增加。"""
        if previous is None:
            return False
        return self.risk_adjusted_leverage > previous.risk_adjusted_leverage and not self.is_safe


# ============================================================================
# BD-CV33: Durable RiskStateAuthority
# ============================================================================


class RiskState(str, Enum):
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    CORRUPT = "CORRUPT"


@dataclass(frozen=True)
class RiskApproval:
    """BD-CV33: 风险审批。"""

    approval_id: str = ""
    approved_by: str = ""
    approved_at: str = ""
    expires_at: str = ""
    max_exposure: float = 0.0
    is_expired: bool = True

    def is_valid(self) -> bool:
        """过期 approval 无法创建 Intent。"""
        if self.is_expired:
            return False
        try:
            expires = datetime.fromisoformat(self.expires_at)
            return datetime.now(timezone.utc) < expires
        except (ValueError, TypeError):
            return False


@dataclass(frozen=True)
class RiskStateAuthority:
    """BD-CV33: 持久风险状态权威。

    corrupt/missing state 无法得到 NORMAL。
    """

    state: RiskState = RiskState.CORRUPT
    approved_exposure: float = 0.0
    active_approvals: list[RiskApproval] = field(default_factory=list)
    state_hash: str = ""

    def can_create_intent(self) -> bool:
        """corrupt/missing state → 不能创建 Intent。"""
        if self.state in (RiskState.CORRUPT, RiskState.CRITICAL):
            return False
        return any(a.is_valid() for a in self.active_approvals)


# ==========================================================================
# Alpha V3 Exposure/Portfolio contracts
# ==========================================================================


@dataclass(frozen=True, slots=True)
class ExposureTarget:
    """Versioned portfolio-level exposure target produced by a governor."""

    target_beta: float
    beta_min: float
    beta_max: float
    target_gross: float
    gross_min: float
    gross_max: float
    target_net: float
    net_min: float
    net_max: float
    target_volatility: float
    confidence: float
    reason_codes: tuple[str, ...]
    policy_version: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def from_policy_values(cls, **values: object) -> ExposureTarget:
        return cls(**values)  # type: ignore[arg-type]

    def __post_init__(self) -> None:
        numeric_names = (
            "target_beta",
            "beta_min",
            "beta_max",
            "target_gross",
            "gross_min",
            "gross_max",
            "target_net",
            "net_min",
            "net_max",
            "target_volatility",
            "confidence",
        )
        if any(not math.isfinite(float(getattr(self, name))) for name in numeric_names):
            raise ValueError("exposure target values must be finite")
        if self.beta_min > self.beta_max or self.gross_min > self.gross_max or self.net_min > self.net_max:
            raise ValueError("exposure target bounds are invalid")
        if self.target_gross < 0 or self.gross_min < 0 or self.target_volatility < 0:
            raise ValueError("gross and volatility targets must not be negative")
        if not self.gross_min <= self.target_gross <= self.gross_max:
            raise ValueError("target_gross is outside bounds")
        if not self.beta_min <= self.target_beta <= self.beta_max:
            raise ValueError("target_beta is outside bounds")
        if not self.net_min <= self.target_net <= self.net_max:
            raise ValueError("target_net is outside bounds")
        if abs(self.target_net) > self.target_gross + 1e-12:
            raise ValueError("gross exposure must be at least absolute net exposure")
        if not 0 <= self.confidence <= 1 or not self.policy_version.strip():
            raise ValueError("exposure confidence or policy version is invalid")
        if self.timestamp.tzinfo is None:
            raise ValueError("exposure target timestamp must be timezone-aware")
        object.__setattr__(self, "reason_codes", tuple(str(code) for code in self.reason_codes))


@dataclass(frozen=True, slots=True)
class PortfolioOptimizationInput:
    """All inputs required by the V3 active portfolio optimizer."""

    forecasts: Mapping[str, EnsembleForecast]
    current_weights: Mapping[str, float]
    covariance: Mapping[str, Mapping[str, float]]
    asset_betas: Mapping[str, float]
    exposure_target: ExposureTarget
    account_equity: float
    fee_costs: Mapping[str, float]
    slippage_costs: Mapping[str, float]
    funding_costs: Mapping[str, float]
    liquidity_limits: Mapping[str, float]
    capacity_limits: Mapping[str, float]
    min_notional: Mapping[str, float]
    step_sizes: Mapping[str, float]
    tick_sizes: Mapping[str, float]
    policy_version: str

    def __post_init__(self) -> None:
        if not math.isfinite(self.account_equity) or self.account_equity <= 0:
            raise ValueError("account_equity must be finite and positive")
        if not self.policy_version.strip():
            raise ValueError("portfolio policy version is required")
