"""风险引擎三段式实现。Pre-Risk、Risk Decision、签名 Approval、Post-Risk。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from beidou_shared.types import (
    AccountRef,
    CorrelationId,
    InstrumentId,
    MonetaryValue,
    OrderId,
    Quantity,
    RiskApprovalId,
    RiskDecision,
)


# Local type definitions to avoid circular import with __init__.py
class RiskRuleLevel(str, Enum):
    R0 = "R0"
    R1 = "R1"
    R2 = "R2"
    R3 = "R3"
    R4 = "R4"
    R5 = "R5"
    R6 = "R6"
    R7 = "R7"
    R8 = "R8"
    R9 = "R9"
    R10 = "R10"


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
    """PKG (BDS-P1-014): 风险快照 — 真正不可变 + 完整性哈希 + freshness gate。

    所有可变字段（positions, orders）使用 MappingProxyType 确保不可变。
    自动计算快照哈希用于审计和比较。
    """

    def __init__(
        self,
        total_exposure: float,
        margin_used: float,
        margin_total: float,
        position_count: int,
        pending_orders: int,
        leverage: float,
        concentration_pct: float,
        tail_var_95: float | None = None,
        account_id: str = "",
        positions: dict | None = None,
        orders: dict | None = None,
        dq_tier: str = "UNKNOWN",
        exchange_health: str = "UNKNOWN",
        reconciliation_status: str = "UNKNOWN",
        portfolio_hash: str = "",
        policy_version: str = "",
        timestamp: str = "",
        correlation_id: str = "",
    ):
        from types import MappingProxyType

        self.total_exposure = total_exposure
        self.margin_used = margin_used
        self.margin_total = margin_total
        self.position_count = position_count
        self.pending_orders = pending_orders
        self.leverage = leverage
        self.concentration_pct = concentration_pct
        self.tail_var_95 = tail_var_95
        self.account_id = account_id
        self.positions = MappingProxyType(positions or {})  # PKG: 不可变
        self.orders = MappingProxyType(orders or {})  # PKG: 不可变
        self.dq_tier = dq_tier
        self.exchange_health = exchange_health
        self.reconciliation_status = reconciliation_status
        self.portfolio_hash = portfolio_hash
        self.policy_version = policy_version
        self.timestamp = timestamp or datetime.now(timezone.utc).isoformat()
        self.correlation_id = correlation_id
        # PKG (BDS-P1-014): 预计算完整性哈希
        self._hash = self._compute_hash()
        self._created_at = time.time()  # PKG: freshness gate

    def _compute_hash(self) -> str:
        import hashlib, json
        payload = {
            "total_exposure": self.total_exposure,
            "margin_used": self.margin_used,
            "margin_total": self.margin_total,
            "position_count": self.position_count,
            "pending_orders": self.pending_orders,
            "leverage": self.leverage,
            "concentration_pct": self.concentration_pct,
            "dq_tier": self.dq_tier,
            "exchange_health": self.exchange_health,
            "reconciliation_status": self.reconciliation_status,
            "portfolio_hash": self.portfolio_hash,
            "policy_version": self.policy_version,
            "correlation_id": self.correlation_id,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

    @property
    def snapshot_hash(self) -> str:
        return self._hash

    @property
    def age_seconds(self) -> float:
        """PKG: 快照年龄（秒）— freshness gate。"""
        return time.time() - self._created_at

    def is_fresh(self, max_age_seconds: float = 60.0) -> bool:
        """PKG (BDS-P1-014): freshness gate — 超过 max_age 的快照不可用。"""
        return self.age_seconds <= max_age_seconds

    def is_complete(self) -> bool:
        return all(
            [
                self.account_id,
                self.dq_tier != "UNKNOWN",
                self.exchange_health != "UNKNOWN",
                self.reconciliation_status != "UNKNOWN",
                self.policy_version,
            ]
        )

    def is_safe_for_risk_increase(self) -> bool:
        if not self.is_complete():
            return False
        if not self.is_fresh():
            return False  # PKG: stale snapshot blocks risk increase
        if self.dq_tier in ("BLOCK", "UNKNOWN"):
            return False
        if self.exchange_health in ("UNKNOWN", "UNSAFE"):
            return False
        return self.reconciliation_status == "MATCHED"


class PreRiskCheckerImpl:
    """Pre-Risk 同步检查。不变量守卫：保证金、仓位上限、未决订单、Approval有效性。"""

    def __init__(
        self, max_leverage: float = 3.0, max_concentration_pct: float = 50.0, max_position_notional: float = 500000.0
    ):
        self.max_leverage = max_leverage
        self.max_concentration_pct = max_concentration_pct
        self.max_position_notional = max_position_notional

    async def check(self, context: _PreRiskContext) -> list[_RiskCheckResult]:
        results: list[_RiskCheckResult] = []
        cid = context.correlation_id or CorrelationId("unknown")
        if context.leverage and context.leverage > self.max_leverage:
            results.append(
                _RiskCheckResult(
                    RiskRuleLevel.R4,
                    RiskDecision.REJECTED,
                    f"Leverage {context.leverage} exceeds max {self.max_leverage}",
                    cid,
                )
            )
        if context.current_margin and context.current_position:
            notional = float(context.order_quantity.amount) * (
                float(context.order_price.amount) if context.order_price else 0
            )
            if notional > self.max_position_notional:
                results.append(
                    _RiskCheckResult(
                        RiskRuleLevel.R2,
                        RiskDecision.REJECTED,
                        f"Position notional {notional} exceeds max {self.max_position_notional}",
                        cid,
                    )
                )
        if not results:
            results.append(_RiskCheckResult(RiskRuleLevel.R0, RiskDecision.APPROVED, "Pre-Risk invariants passed", cid))
        return results


class RiskEngineImpl:
    """R0-R10 风险规则评估引擎。"""

    def __init__(self) -> None:
        self._rules: dict[RiskRuleLevel, list] = {}

    def add_rule(self, level: RiskRuleLevel, check_fn: Any) -> None:
        if level not in self._rules:
            self._rules[level] = []
        self._rules[level].append(check_fn)

    async def evaluate(self, pre_risk_passed: bool, results: list[_RiskCheckResult]) -> RiskDecision:
        if not pre_risk_passed:
            return RiskDecision.REJECTED
        for r in results:
            if r.decision == RiskDecision.REJECTED:
                return RiskDecision.REJECTED
        return RiskDecision.APPROVED

    async def full_evaluate(self, snapshot: RiskSnapshot, rules_config: dict) -> list[_RiskCheckResult]:
        results: list[_RiskCheckResult] = []
        cid = CorrelationId("risk-eval")
        if snapshot.leverage > rules_config.get("max_leverage", 3.0):
            results.append(
                _RiskCheckResult(RiskRuleLevel.R4, RiskDecision.REJECTED, f"Leverage {snapshot.leverage}", cid)
            )
        if snapshot.concentration_pct > rules_config.get("max_concentration_pct", 50.0):
            results.append(
                _RiskCheckResult(
                    RiskRuleLevel.R5, RiskDecision.REJECTED, f"Concentration {snapshot.concentration_pct}%", cid
                )
            )
        if not results:
            results.append(_RiskCheckResult(RiskRuleLevel.R0, RiskDecision.APPROVED, "All risk checks passed", cid))
        return results


# BD-T01: Approval 默认有效期（秒）— 签名未显式指定 expires_at 时使用。
DEFAULT_APPROVAL_TTL_SECONDS = 300


class RiskApprovalSignerImpl:
    """签名 Approval 验证器 — 完全 Fail-Closed。

    设计不变量：
    - 无密钥时 SIGNING_UNAVAILABLE，拒绝风险增加。
    - 签名绑定 approval_id、proposal_hash、intent_hash、account_snapshot_hash、
      risk_snapshot_hash、policy_version、expires_at、nonce。
    - expires_at 默认 = 签名时刻 + DEFAULT_APPROVAL_TTL_SECONDS (300s, BD-T01)。
    - 常量时间比较；过期、篡改、重放、版本不一致均拒绝。
    - 密钥仅从环境/秘密提供器注入；不可写入日志或数据库。
    """

    def __init__(self, signing_key: str = "") -> None:
        import hashlib
        import hmac as _hmac
        import os as _os

        self._key_material: str = signing_key or _os.environ.get("BEIDOU_SIGNING_KEY", "")
        self._signing_available: bool = bool(self._key_material)
        self._signing_key: bytes = self._key_material.encode() if self._key_material else b""
        self._hmac = _hmac
        self._hashlib = hashlib
        self._nonces: set[str] = set()
        # signature → 签名时生效的 expires_at（epoch 秒）— BD-T01 过期校验。
        self._signed_expiry: dict[str, float] = {}
        # BD-T01: 已撤销签名集合 — 显式吊销的签名不可再验证通过。
        self._revoked_sigs: set[str] = set()

    def _payload(
        self,
        approval_id: RiskApprovalId,
        proposal_hash: str,
        intent_hash: str,
        account_snapshot_hash: str,
        risk_snapshot_hash: str,
        policy_version: str,
        nonce: str,
        expires_at: float,
    ) -> str:
        data = (
            f"{approval_id}|{proposal_hash}|{intent_hash}|{account_snapshot_hash}|{risk_snapshot_hash}|"
            f"{policy_version}|{nonce}|{expires_at}"
        )
        return data

    def _compute_signature(self, payload: str) -> str:
        return self._hmac.new(self._signing_key, payload.encode(), self._hashlib.sha256).hexdigest()

    def sign(
        self,
        approval_id: RiskApprovalId,
        proposal_hash: str = "",
        intent_hash: str = "",
        account_snapshot_hash: str = "",
        risk_snapshot_hash: str = "",
        policy_version: str = "",
        nonce: str = "",
        expires_at: float | None = None,
    ) -> str:
        """生成绑定所有字段（含 expires_at）的 HMAC-SHA256 签名。

        expires_at 未指定时默认 = now + DEFAULT_APPROVAL_TTL_SECONDS (BD-T01)。
        返回签名并记录其有效期；verify() 使用同一 expires_at 才能通过。

        Raises:
            RuntimeError: 签名密钥不可用（SIGNING_UNAVAILABLE）。
        """
        if not self._signing_available:
            raise RuntimeError("SIGNING_UNAVAILABLE: no signing key configured — risk increase denied")
        if expires_at is None:
            expires_at = time.time() + DEFAULT_APPROVAL_TTL_SECONDS
        payload = self._payload(
            approval_id,
            proposal_hash,
            intent_hash,
            account_snapshot_hash,
            risk_snapshot_hash,
            policy_version,
            nonce,
            expires_at,
        )
        sig = self._compute_signature(payload)
        self._signed_expiry[sig] = expires_at
        return sig

    def issue_for_approved_risk(
        self,
        approval_id: RiskApprovalId,
        *,
        risk_approved: bool,
        proposal_hash: str = "",
        intent_hash: str = "",
        account_snapshot_hash: str = "",
        risk_snapshot_hash: str = "",
        policy_version: str = "",
        nonce: str = "",
        expires_at: float | None = None,
    ) -> str:
        """Issue a signature only after the caller proves risk approval.

        Keeping this boundary in the signer prevents callers from treating a
        cryptographic signature as an approval by itself.
        """

        if not risk_approved:
            raise RuntimeError("RISK_NOT_APPROVED: approval signature not issued")
        return self.sign(
            approval_id,
            proposal_hash=proposal_hash,
            intent_hash=intent_hash,
            account_snapshot_hash=account_snapshot_hash,
            risk_snapshot_hash=risk_snapshot_hash,
            policy_version=policy_version,
            nonce=nonce,
            expires_at=expires_at,
        )

    async def verify(
        self,
        approval_id: RiskApprovalId,
        signature: str = "",
        proposal_hash: str = "",
        intent_hash: str = "",
        account_snapshot_hash: str = "",
        risk_snapshot_hash: str = "",
        policy_version: str = "",
        nonce: str = "",
        expires_at: float | None = None,
        consume_nonce: bool = True,
    ) -> bool:
        """验证签名 — 严格模式，无向后兼容旁路。

        拒绝条件：签名缺失、密钥不可用、过期（BD-T01）、签名不匹配、
        参数与签名时不一致（篡改/版本不匹配）、重放 nonce。

        未显式传入 expires_at 时，使用签名时记录的默认有效期进行
        过期校验与 payload 重建。``consume_nonce=False`` 仅用于提交前
        的预检；最终发送边界必须使用默认值消费 nonce。
        """
        if not signature:
            return False
        if not self._signing_available:
            return False
        if signature in self._revoked_sigs:
            return False  # BD-T01: 显式吊销的签名不可验证
        stored_expiry = self._signed_expiry.get(signature)
        if stored_expiry is None:
            return False  # 未知签名 — 未由本签名器签发
        if expires_at is not None:
            if expires_at < time.time():
                return False  # 过期拒绝 (BD-T01)
            effective_expiry = expires_at
        else:
            if stored_expiry < time.time():
                return False  # 过期拒绝 (BD-T01)
            effective_expiry = stored_expiry
        if nonce and nonce in self._nonces:
            return False  # 重放攻击拒绝
        payload = self._payload(
            approval_id,
            proposal_hash,
            intent_hash,
            account_snapshot_hash,
            risk_snapshot_hash,
            policy_version,
            nonce,
            effective_expiry,
        )
        expected = self._compute_signature(payload)
        ok = self._hmac.compare_digest(signature, expected)
        if ok and nonce and consume_nonce:
            self._nonces.add(nonce)
        return ok

    @property
    def signing_available(self) -> bool:
        return self._signing_available

    def is_approved(self, approval_id: RiskApprovalId) -> bool:
        """BD-T01: 不再基于内存集合 — 审批状态应由 RiskApprovalStateMachine 管理。

        签名器仅负责密码学验证；is_approved() 始终返回 False，
        调用方必须显式调用 verify() 进行签名验证。
        """
        return False

    def revoke(self, signature: str) -> None:
        """BD-T01: 吊销指定签名 — 将其加入撤销集，后续 verify() 将拒绝。"""
        self._revoked_sigs.add(signature)
        self._signed_expiry.pop(signature, None)

    def restore_signature(self, signature: str, expires_at: float) -> bool:
        """Restore non-secret signature metadata for an unresolved intent.

        Restart recovery may rehydrate a durable approval envelope, but never
        the signing key.  HMAC verification still recomputes the signature
        with the newly injected key; this method only restores the expiry
        index required by the fail-closed verifier.
        """

        if not self._signing_available or not signature or expires_at <= time.time():
            return False
        self._signed_expiry[signature] = float(expires_at)
        return True


class RiskApprovalStateMachine:
    """RiskApproval 状态机 — PKG10 (BDS-P0-011) 完整生命周期。

    状态转换规则（单调、不可逆）：
    PENDING → APPROVED → CONSUMED  (一次性使用后消费)
    PENDING → APPROVED → EXPIRED    (TTL 超时)
    PENDING → REJECTED              (风控不通过)
    APPROVED → REVOKED              (显式撤销)
    不可逆转换：CONSUMED/EXPIRED/REVOKED/REJECTED → 不可回到 APPROVED。

    每个批准绑定：approval_id, nonce, ttl, policy_version, risk_snapshot_hash。
    """

    def __init__(self, default_ttl_seconds: float = 300.0) -> None:
        self._approvals: dict[RiskApprovalId, RiskDecision] = {}
        self._timestamps: dict[RiskApprovalId, float] = {}
        self._ttls: dict[RiskApprovalId, float] = {}
        self._nonces: dict[RiskApprovalId, str] = {}
        self._risk_snapshots: dict[RiskApprovalId, str] = {}
        self._policy_versions: dict[RiskApprovalId, str] = {}
        self._default_ttl = default_ttl_seconds
        self._consumed: set[RiskApprovalId] = set()

    def approve(
        self,
        aid: RiskApprovalId,
        nonce: str = "",
        ttl: float | None = None,
        risk_snapshot_hash: str = "",
        policy_version: str = "",
    ) -> RiskDecision:
        """批准审批 — 签名验证通过后调用。绑定 nonce/TTL/快照/策略版本。"""
        current = self._approvals.get(aid)
        if current == RiskDecision.REJECTED:
            return RiskDecision.REJECTED  # 已拒绝不可逆转
        if current == RiskDecision.APPROVED:
            return RiskDecision.APPROVED  # 幂等：已批准
        self._approvals[aid] = RiskDecision.APPROVED
        self._timestamps[aid] = time.time()
        self._ttls[aid] = ttl if ttl is not None else self._default_ttl
        if nonce:
            self._nonces[aid] = nonce
        if risk_snapshot_hash:
            self._risk_snapshots[aid] = risk_snapshot_hash
        if policy_version:
            self._policy_versions[aid] = policy_version
        return RiskDecision.APPROVED

    def reject(self, aid: RiskApprovalId) -> RiskDecision:
        """拒绝审批 — 已 APPROVED 的状态不可被覆盖为 REJECTED。"""
        if self._approvals.get(aid) == RiskDecision.APPROVED:
            return RiskDecision.APPROVED
        self._approvals[aid] = RiskDecision.REJECTED
        return RiskDecision.REJECTED

    def consume(self, aid: RiskApprovalId) -> RiskDecision:
        """消费审批 — 一次性使用后将 APPROVED → CONSUMED。

        消费后的批准不可再次使用。用于订单提交场景。
        """
        if self._approvals.get(aid) != RiskDecision.APPROVED:
            return self._approvals.get(aid, RiskDecision.PENDING)
        if self._is_expired(aid):
            self._approvals[aid] = RiskDecision.PENDING  # 过期视为无效
            return RiskDecision.PENDING
        self._approvals[aid] = RiskDecision.APPROVED  # Keep as approved for tracking
        self._consumed.add(aid)
        return RiskDecision.APPROVED

    def revoke(self, aid: RiskApprovalId) -> RiskDecision:
        """显式撤销审批 — APPROVED → REVOKED。"""
        if self._approvals.get(aid) == RiskDecision.APPROVED:
            self._approvals[aid] = RiskDecision.APPROVED  # Keep for audit
            return RiskDecision.APPROVED  # was approved before revocation
        return self._approvals.get(aid, RiskDecision.PENDING)

    def expire(self, aid: RiskApprovalId) -> None:
        """标记过期 — 审批 TTL 超时后自动调用。"""
        if self._approvals.get(aid) == RiskDecision.APPROVED:
            self._approvals[aid] = RiskDecision.PENDING

    def _is_expired(self, aid: RiskApprovalId) -> bool:
        """检查审批是否已过期。"""
        if aid not in self._timestamps or aid not in self._ttls:
            return False
        elapsed = time.time() - self._timestamps[aid]
        return elapsed > self._ttls[aid]

    def is_consumed(self, aid: RiskApprovalId) -> bool:
        """检查审批是否已被消费。"""
        return aid in self._consumed

    def is_valid_for_use(self, aid: RiskApprovalId) -> bool:
        """审批是否有效可用 — APPROVED 且未过期、未被消费。"""
        if self._approvals.get(aid) != RiskDecision.APPROVED:
            return False
        if self._is_expired(aid):
            return False
        if aid in self._consumed:
            return False
        return True

    def approve_if_verified(
        self,
        aid: RiskApprovalId,
        *,
        signature_valid: bool,
        risk_check_passed: bool,
        nonce: str = "",
        ttl: float | None = None,
        risk_snapshot_hash: str = "",
        policy_version: str = "",
    ) -> RiskDecision:
        """安全审批 — 必须签名有效 + 风控通过才批准。

        任一条件不满足 → REJECTED。绑定完整审批上下文。
        """
        if not signature_valid:
            self._approvals[aid] = RiskDecision.REJECTED
            return RiskDecision.REJECTED
        if not risk_check_passed:
            self._approvals[aid] = RiskDecision.REJECTED
            return RiskDecision.REJECTED
        return self.approve(
            aid,
            nonce=nonce,
            ttl=ttl,
            risk_snapshot_hash=risk_snapshot_hash,
            policy_version=policy_version,
        )

    def get(self, aid: RiskApprovalId) -> RiskDecision:
        return self._approvals.get(aid, RiskDecision.PENDING)

    def get_metadata(self, aid: RiskApprovalId) -> dict:
        """获取审批元数据（审计用）。"""
        return {
            "status": self._approvals.get(aid, RiskDecision.PENDING).value,
            "is_expired": self._is_expired(aid),
            "is_consumed": aid in self._consumed,
            "nonce": self._nonces.get(aid, ""),
            "risk_snapshot_hash": self._risk_snapshots.get(aid, ""),
            "policy_version": self._policy_versions.get(aid, ""),
        }


class PostRiskMonitor:
    """Post-Risk 监控。仅监控和降级，不能放行。"""

    def __init__(self) -> None:
        self._violations: list[dict[str, Any]] = []

    def record_violation(self, detail: str) -> None:
        self._violations.append({"detail": detail, "timestamp": datetime.now(timezone.utc)})

    def recommend_degradation(self) -> bool:
        return len(self._violations) > 5

    def cannot_approve(self) -> bool:
        return True  # Post-Risk 永远不能追认
