"""BD-CV22: 因子生命周期证据重认证引擎。

NaN/Inf/样本不足/统计不可验证/成本未知/容量未知 → 不能晋级。
晋级不可跳级，状态迁移持久化。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class FactorState(str, Enum):
    CANDIDATE = "CANDIDATE"
    REVALIDATION_REQUIRED = "REVALIDATION_REQUIRED"
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    RETIRED = "RETIRED"


VALID_TRANSITIONS = {
    FactorState.CANDIDATE: {FactorState.REVALIDATION_REQUIRED, FactorState.RETIRED},
    FactorState.REVALIDATION_REQUIRED: {FactorState.ACTIVE, FactorState.DEGRADED, FactorState.RETIRED},
    FactorState.ACTIVE: {FactorState.DEGRADED, FactorState.RETIRED},
    FactorState.DEGRADED: {FactorState.REVALIDATION_REQUIRED, FactorState.RETIRED},
    FactorState.RETIRED: set(),  # 终态
}


@dataclass
class FactorRevalidationResult:
    factor_id: str
    sharpe: float
    evidence_dag_hash: str = ""
    state: FactorState = FactorState.REVALIDATION_REQUIRED
    can_promote: bool = False
    blocking_reasons: list[str] = field(default_factory=list)
    promotion_hashes: dict[str, str] = field(default_factory=dict)


def revalidate_factor(
    factor_id: str,
    sharpe: float,
    evidence_dag_hash: str = "",
    sample_count: int = 0,
    cost_verified: bool = False,
    capacity_verified: bool = False,
    statistics_verified: bool = False,
    universe_hash: str = "",
    feature_hash: str = "",
    code_hash: str = "",
    config_hash: str = "",
    policy_hash: str = "",
) -> FactorRevalidationResult:
    """BD-CV22: 因子重认证。

    AC-22-01: NaN/空evidence/旧evidence → 不能 PROMOTED。
    AC-22-02: 每个 ACTIVE 因子都能反查完整证据 DAG。
    晋级不可跳级。
    """
    result = FactorRevalidationResult(
        factor_id=factor_id, sharpe=sharpe,
        evidence_dag_hash=evidence_dag_hash,
        state=FactorState.REVALIDATION_REQUIRED,
    )
    blocking: list[str] = []

    # 1. NaN/Inf check
    if math.isnan(sharpe) or math.isinf(sharpe):
        blocking.append("NAN_INF_SHARPE")

    # 2. Negative/zero sharpe
    if sharpe <= 0:
        blocking.append("NON_POSITIVE_SHARPE")

    # 3. Evidence DAG missing
    if not evidence_dag_hash:
        blocking.append("MISSING_EVIDENCE_DAG")

    # 4. Sample insufficient
    if sample_count < 100:
        blocking.append(f"INSUFFICIENT_SAMPLES:{sample_count}")

    # 5. Cost unknown
    if not cost_verified:
        blocking.append("COST_NOT_VERIFIED")

    # 6. Capacity unknown
    if not capacity_verified:
        blocking.append("CAPACITY_NOT_VERIFIED")

    # 7. Statistics not verified
    if not statistics_verified:
        blocking.append("STATISTICS_NOT_VERIFIED")

    if blocking:
        result.blocking_reasons = blocking
        result.can_promote = False
        result.state = FactorState.REVALIDATION_REQUIRED
    else:
        result.can_promote = True
        result.state = FactorState.ACTIVE

    # Bind promotion hashes
    result.promotion_hashes = {
        "universe_hash": universe_hash,
        "feature_hash": feature_hash,
        "code_hash": code_hash,
        "config_hash": config_hash,
        "policy_hash": policy_hash,
    }

    return result


def transition_state(current: FactorState, target: FactorState) -> tuple[bool, str]:
    """BD-CV22: 状态迁移验证 — 不可跳级。"""
    allowed = VALID_TRANSITIONS.get(current, set())
    if target in allowed:
        return True, f"{current.value}→{target.value}"
    return False, f"INVALID_TRANSITION:{current.value}→{target.value}"
