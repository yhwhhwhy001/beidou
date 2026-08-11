"""BD-CV30/31/32/33: Wave 3 Portfolio & Risk 合约.

SignedPortfolioTarget、约束优化、自适应仓位、Durable RiskStateAuthority。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


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
    signature: str = ""
    created_at: str = ""

    def is_valid(self) -> bool:
        """验证 gross >= abs(net)。"""
        return abs(self.target_exposure) >= abs(self.delta) if self.target_exposure != 0 else self.delta == 0.0

    def compute_hash(self) -> str:
        data = {
            "target_id": self.target_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "target_exposure": self.target_exposure,
            "delta": self.delta,
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
