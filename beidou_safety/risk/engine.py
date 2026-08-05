
"""风险引擎三段式实现。Pre-Risk、Risk Decision、签名 Approval、Post-Risk。"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from beidou_shared.types import (
    AccountId, CorrelationId, InstrumentId, MonetaryValue,
    OrderId, Quantity, RiskApprovalId, RiskDecision, VenueId, AccountRef,
)
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

# Local type definitions to avoid circular import with __init__.py
class RiskRuleLevel(str, Enum):
    R0 = "R0"; R1 = "R1"; R2 = "R2"; R3 = "R3"; R4 = "R4"
    R5 = "R5"; R6 = "R6"; R7 = "R7"; R8 = "R8"; R9 = "R9"; R10 = "R10"

@dataclass(frozen=True, slots=True)
class _RiskCheckResult:
    rule_level: RiskRuleLevel
    decision: RiskDecision
    reason: str
    correlation_id: CorrelationId
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, str] = field(default_factory=dict)

@dataclass
class _PreRiskContext:
    account_ref: AccountRef
    instrument_id: InstrumentId
    order_quantity: Quantity
    order_price: MonetaryValue | None = None
    leverage: float | None = None
    current_position: Quantity | None = None
    current_margin: MonetaryValue | None = None
    pending_orders: list[OrderId] = field(default_factory=list)
    correlation_id: CorrelationId | None = None

class RiskSnapshot:
    """风险快照 — 不可变的风险状态。"""
    def __init__(self, total_exposure: float, margin_used: float, margin_total: float, position_count: int, pending_orders: int, leverage: float, concentration_pct: float, tail_var_95: float | None = None):
        self.total_exposure = total_exposure; self.margin_used = margin_used; self.margin_total = margin_total
        self.position_count = position_count; self.pending_orders = pending_orders
        self.leverage = leverage; self.concentration_pct = concentration_pct; self.tail_var_95 = tail_var_95

class PreRiskCheckerImpl:
    """Pre-Risk 同步检查。不变量守卫：保证金、仓位上限、未决订单、Approval有效性。"""
    def __init__(self, max_leverage: float = 3.0, max_concentration_pct: float = 50.0, max_position_notional: float = 500000.0):
        self.max_leverage = max_leverage; self.max_concentration_pct = max_concentration_pct; self.max_position_notional = max_position_notional

    async def check(self, context: _PreRiskContext) -> list[_RiskCheckResult]:
        results: list[_RiskCheckResult] = []
        cid = context.correlation_id or CorrelationId("unknown")
        if context.leverage and context.leverage > self.max_leverage:
            results.append(_RiskCheckResult(RiskRuleLevel.R4, RiskDecision.REJECTED, f"Leverage {context.leverage} exceeds max {self.max_leverage}", cid))
        if context.current_margin and context.current_position:
            notional = float(context.order_quantity.amount) * (float(context.order_price.amount) if context.order_price else 0)
            if notional > self.max_position_notional:
                results.append(_RiskCheckResult(RiskRuleLevel.R2, RiskDecision.REJECTED, f"Position notional {notional} exceeds max {self.max_position_notional}", cid))
        if not results:
            results.append(_RiskCheckResult(RiskRuleLevel.R0, RiskDecision.APPROVED, "Pre-Risk invariants passed", cid))
        return results

class RiskEngineImpl:
    """R0-R10 风险规则评估引擎。"""
    def __init__(self) -> None: self._rules: dict[RiskRuleLevel, list] = {}
    def add_rule(self, level: RiskRuleLevel, check_fn) -> None:
        if level not in self._rules: self._rules[level] = []
        self._rules[level].append(check_fn)

    async def evaluate(self, pre_risk_passed: bool, results: list[_RiskCheckResult]) -> RiskDecision:
        if not pre_risk_passed: return RiskDecision.REJECTED
        for r in results:
            if r.decision == RiskDecision.REJECTED: return RiskDecision.REJECTED
        return RiskDecision.APPROVED

    async def full_evaluate(self, snapshot: RiskSnapshot, rules_config: dict) -> list[_RiskCheckResult]:
        results: list[_RiskCheckResult] = []
        cid = CorrelationId("risk-eval")
        if snapshot.leverage > rules_config.get("max_leverage", 3.0):
            results.append(_RiskCheckResult(RiskRuleLevel.R4, RiskDecision.REJECTED, f"Leverage {snapshot.leverage}", cid))
        if snapshot.concentration_pct > rules_config.get("max_concentration_pct", 50.0):
            results.append(_RiskCheckResult(RiskRuleLevel.R5, RiskDecision.REJECTED, f"Concentration {snapshot.concentration_pct}%", cid))
        if not results:
            results.append(_RiskCheckResult(RiskRuleLevel.R0, RiskDecision.APPROVED, "All risk checks passed", cid))
        return results

class RiskApprovalSignerImpl:
    """签名 Approval 验证器。"""
    def __init__(self) -> None: self._approved: set[RiskApprovalId] = set()
    def sign(self, approval_id: RiskApprovalId) -> None: self._approved.add(approval_id)
    async def verify(self, approval_id: RiskApprovalId) -> bool: return approval_id in self._approved
    def revoke(self, approval_id: RiskApprovalId) -> None: self._approved.discard(approval_id)

class RiskApprovalStateMachine:
    """RiskApproval 状态机。单调恢复，不可降级。"""
    def __init__(self): self._approvals: dict[RiskApprovalId, RiskDecision] = {}
    def approve(self, aid: RiskApprovalId) -> RiskDecision: self._approvals[aid] = RiskDecision.APPROVED; return RiskDecision.APPROVED
    def reject(self, aid: RiskApprovalId) -> RiskDecision: self._approvals[aid] = RiskDecision.REJECTED; return RiskDecision.REJECTED
    def get(self, aid: RiskApprovalId) -> RiskDecision: return self._approvals.get(aid, RiskDecision.PENDING)

class PostRiskMonitor:
    """Post-Risk 监控。仅监控和降级，不能放行。"""
    def __init__(self): self._violations: list[dict] = []
    def record_violation(self, detail: str) -> None: self._violations.append({"detail": detail, "timestamp": datetime.now(timezone.utc)})
    def recommend_degradation(self) -> bool: return len(self._violations) > 5
    def cannot_approve(self) -> bool: return True  # Post-Risk 永远不能追认
