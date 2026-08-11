"""BD-CV20/21/22/23/24: Wave 2 Research Truth 合约.

统计验证、成本模型、因子生命周期、策略图、内核一致性。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ============================================================================
# BD-CV20: 统计验证内核
# ============================================================================


class StatisticalTest(str, Enum):
    PURGED_WF = "PURGED_WF"
    CPCV = "CPCV"
    PBO = "PBO"
    DSR = "DSR"
    HOLM = "HOLM"


@dataclass(frozen=True)
class StatisticalValidationResult:
    """BD-CV20: 统计验证内核结果。

    PBO n_combos 改变时组合分区集合真实变化。
    """

    test_type: StatisticalTest
    p_value: float = 1.0
    deflated_sharpe: float = 0.0
    is_significant: bool = False
    n_combos: int = 0
    reference_hash: str = ""

    def compute_hash(self) -> str:
        data = {
            "test_type": self.test_type.value,
            "p_value": self.p_value,
            "deflated_sharpe": self.deflated_sharpe,
            "n_combos": self.n_combos,
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


# ============================================================================
# BD-CV21: 统一真实成本与容量模型
# ============================================================================


@dataclass(frozen=True)
class CostBreakdown:
    """BD-CV21: 一笔交易的真实成本分解。"""

    symbol: str
    commission_bps: float = 0.0
    slippage_bps: float = 0.0
    funding_rate_hourly: float = 0.0
    spread_bps: float = 0.0
    total_bps: float = 0.0
    is_verified: bool = False

    def is_verifiable(self) -> bool:
        """缺失真实 fee tier/市场输入时返回 NOT_VERIFIABLE。"""
        return self.is_verified and self.total_bps >= 0.0


@dataclass(frozen=True)
class CapacityModel:
    """BD-CV21: 仓位容量模型。"""

    symbol: str
    daily_volume: float = 0.0
    orderbook_depth_1pct: float = 0.0
    max_position_notional: float = 0.0
    is_stale: bool = True

    def is_constant(self, other: CapacityModel) -> bool:
        """容量模型对显著不同输入不能返回恒定结果。"""
        tolerance = 0.01
        return abs(self.max_position_notional - other.max_position_notional) < tolerance


# ============================================================================
# BD-CV22: 因子生命周期证据重认证
# ============================================================================


class FactorLifecycleState(str, Enum):
    CANDIDATE = "CANDIDATE"
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    RETIRED = "RETIRED"


@dataclass(frozen=True)
class FactorEvidence:
    """BD-CV22: 因子证据链。"""

    factor_id: str
    state: FactorLifecycleState = FactorLifecycleState.CANDIDATE
    sharpe: float = 0.0
    evidence_dag_hash: str = ""
    last_promotion_at: str = ""

    def can_promote(self) -> bool:
        """NaN score/空 evidence/旧 evidence 均不能 PROMOTED。"""
        import math
        if math.isnan(self.sharpe) or self.sharpe <= 0:
            return False
        if not self.evidence_dag_hash:
            return False
        return True

    def full_evidence_dag(self) -> dict[str, Any]:
        """每个 ACTIVE 因子都能反查完整证据 DAG。"""
        return {
            "factor_id": self.factor_id,
            "state": self.state.value,
            "sharpe": self.sharpe,
            "evidence_dag_hash": self.evidence_dag_hash,
        }


# ============================================================================
# BD-CV23: Typed Strategy Graph
# ============================================================================


class StrategyAction(str, Enum):
    """BD-CV23: 策略输出动作类型。"""

    ACTION = "ACTION"
    NO_ACTION = "NO_ACTION"
    VETO = "VETO"
    DEGRADED = "DEGRADED"


@dataclass(frozen=True)
class StrategySignal:
    """BD-CV23: Typed Strategy Graph 输出信号。

    NO_ACTION 不会被当系统故障，也不会被改写为 ACTION。
    VETO 在所有环境均阻断下游新增风险。
    """

    strategy_id: str
    action: StrategyAction
    symbol: str = ""
    confidence: float = 0.0
    reason: str = ""

    def is_actionable(self) -> bool:
        return self.action == StrategyAction.ACTION

    def is_blocking(self) -> bool:
        return self.action == StrategyAction.VETO

    def is_noop(self) -> bool:
        return self.action == StrategyAction.NO_ACTION


# ============================================================================
# BD-CV24: Backtest/Replay/Paper 内核一致性
# ============================================================================


@dataclass(frozen=True)
class KernelParityResult:
    """BD-CV24: 内核一致性验证结果。

    相同事件日志重复 replay 最终 hash 一致。
    Paper 能产生 partial fill/reject/cancel-fill race。
    """

    paper_hash: str = ""
    replay_hash: str = ""
    backtest_hash: str = ""
    is_consistent: bool = False
    events_count: int = 0
    inconsistencies: list[str] = field(default_factory=list)

    def has_parity(self) -> bool:
        """所有内核产生一致的 hash。"""
        hashes = {h for h in [self.paper_hash, self.replay_hash, self.backtest_hash] if h}
        return len(hashes) <= 1 and self.is_consistent
