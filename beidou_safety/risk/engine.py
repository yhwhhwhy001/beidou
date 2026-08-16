"""风险引擎三段式实现。Pre-Risk、Risk Decision、签名 Approval、Post-Risk。"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from inspect import isawaitable
from math import isfinite
from types import MappingProxyType
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
    account_balance: float | None = None  # M10-F01: 保证金检查输入
    risk_increasing: bool | None = None  # M10-R2: 意图方向(保证金缺失 fail-closed 判定)
    correlation_id: CorrelationId | None = None


class RiskSnapshot:
    """PKG (BDS-P1-014): 风险快照 — 真正不可变 + 完整性哈希 + freshness gate。

    所有可变字段（positions, orders）使用 MappingProxyType 确保不可变。
    自动计算快照哈希用于审计和比较。
    """

    total_exposure: float
    margin_used: float
    margin_total: float
    position_count: int
    pending_orders: int
    leverage: float
    concentration_pct: float
    tail_var_95: float | None
    account_id: str
    positions: Mapping[str, Any]
    orders: Mapping[str, Any]
    dq_tier: str
    exchange_health: str
    reconciliation_status: str
    portfolio_hash: str
    policy_version: str
    source_timestamp: str
    observed_at: str
    received_at: str
    timestamp: str
    correlation_id: str
    _hash: str
    _sealed: bool

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
        source_timestamp: str = "",
        observed_at: str = "",
        received_at: str = "",
    ) -> None:
        if timestamp and source_timestamp and timestamp != source_timestamp:
            raise ValueError("Conflicting source timestamps")
        source_timestamp = source_timestamp or timestamp
        numeric_values = {
            "total_exposure": total_exposure,
            "margin_used": margin_used,
            "margin_total": margin_total,
            "leverage": leverage,
            "concentration_pct": concentration_pct,
        }
        normalized_numeric: dict[str, float] = {}
        for name, value in numeric_values.items():
            if isinstance(value, bool):
                raise ValueError(f"{name} must be a finite number")
            try:
                normalized = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be a finite number") from exc
            if not isfinite(normalized) or normalized < 0 or (name == "leverage" and normalized <= 0):
                raise ValueError(f"{name} contains invalid economic data")
            normalized_numeric[name] = normalized
        if not isinstance(position_count, int) or isinstance(position_count, bool) or position_count < 0:
            raise ValueError("position_count must be a non-negative integer")
        if not isinstance(pending_orders, int) or isinstance(pending_orders, bool) or pending_orders < 0:
            raise ValueError("pending_orders must be a non-negative integer")
        if tail_var_95 is not None:
            if isinstance(tail_var_95, bool):
                raise ValueError("tail_var_95 must be finite and non-negative")
            try:
                normalized_tail_var = float(tail_var_95)
            except (TypeError, ValueError) as exc:
                raise ValueError("tail_var_95 must be finite and non-negative") from exc
            if not isfinite(normalized_tail_var) or normalized_tail_var < 0:
                raise ValueError("tail_var_95 must be finite and non-negative")
        else:
            normalized_tail_var = None
        if positions is not None and not isinstance(positions, dict):
            raise ValueError("positions must be a dictionary")
        if orders is not None and not isinstance(orders, dict):
            raise ValueError("orders must be a dictionary")

        object.__setattr__(self, "total_exposure", normalized_numeric["total_exposure"])
        object.__setattr__(self, "margin_used", normalized_numeric["margin_used"])
        object.__setattr__(self, "margin_total", normalized_numeric["margin_total"])
        object.__setattr__(self, "position_count", position_count)
        object.__setattr__(self, "pending_orders", pending_orders)
        object.__setattr__(self, "leverage", normalized_numeric["leverage"])
        object.__setattr__(self, "concentration_pct", normalized_numeric["concentration_pct"])
        object.__setattr__(self, "tail_var_95", normalized_tail_var)
        object.__setattr__(self, "account_id", str(account_id))
        object.__setattr__(self, "positions", self._deep_freeze(positions or {}))
        object.__setattr__(self, "orders", self._deep_freeze(orders or {}))
        object.__setattr__(self, "dq_tier", str(dq_tier).upper())
        object.__setattr__(self, "exchange_health", str(exchange_health).upper())
        object.__setattr__(self, "reconciliation_status", str(reconciliation_status).upper())
        object.__setattr__(self, "portfolio_hash", str(portfolio_hash))
        object.__setattr__(self, "policy_version", str(policy_version))
        object.__setattr__(self, "source_timestamp", str(source_timestamp))
        object.__setattr__(self, "observed_at", str(observed_at))
        object.__setattr__(self, "received_at", str(received_at))
        object.__setattr__(self, "timestamp", str(source_timestamp))
        object.__setattr__(self, "correlation_id", str(correlation_id))
        # PKG (BDS-P1-014): 预计算完整性哈希
        object.__setattr__(self, "_hash", self._compute_hash())
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"RiskSnapshot is immutable: {name}")

    @classmethod
    def _deep_freeze(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return MappingProxyType({key: cls._deep_freeze(item) for key, item in value.items()})
        if isinstance(value, (list, tuple)):
            return tuple(cls._deep_freeze(item) for item in value)
        if isinstance(value, (set, frozenset)):
            return frozenset(cls._deep_freeze(item) for item in value)
        return value

    @classmethod
    def _canonicalize(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): cls._canonicalize(item) for key, item in sorted(value.items(), key=lambda row: str(row[0]))
            }
        if isinstance(value, tuple):
            return [cls._canonicalize(item) for item in value]
        if isinstance(value, frozenset):
            return sorted((cls._canonicalize(item) for item in value), key=repr)
        return value

    def _compute_hash(self) -> str:
        import hashlib
        import json

        payload = {
            "total_exposure": self.total_exposure,
            "margin_used": self.margin_used,
            "margin_total": self.margin_total,
            "position_count": self.position_count,
            "pending_orders": self.pending_orders,
            "leverage": self.leverage,
            "concentration_pct": self.concentration_pct,
            "tail_var_95": self.tail_var_95,
            "account_id": self.account_id,
            "positions": self._canonicalize(self.positions),
            "orders": self._canonicalize(self.orders),
            "dq_tier": self.dq_tier,
            "exchange_health": self.exchange_health,
            "reconciliation_status": self.reconciliation_status,
            "portfolio_hash": self.portfolio_hash,
            "policy_version": self.policy_version,
            "source_timestamp": self.source_timestamp,
            "observed_at": self.observed_at,
            "received_at": self.received_at,
            "correlation_id": self.correlation_id,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

    @property
    def snapshot_hash(self) -> str:
        return self._hash

    @property
    def age_seconds(self) -> float:
        """PKG: 快照年龄（秒）— freshness gate。"""
        received = self._parse_time(self.received_at)
        if received is None:
            return float("inf")
        return (datetime.now(timezone.utc) - received).total_seconds()

    def is_fresh(self, max_age_seconds: float = 60.0) -> bool:
        """PKG (BDS-P1-014): freshness gate — 超过 max_age 的快照不可用。"""
        if not isfinite(max_age_seconds) or max_age_seconds <= 0:
            return False
        age = self.age_seconds
        return isfinite(age) and 0 <= age <= max_age_seconds

    @staticmethod
    def _parse_time(value: str) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)

    def is_complete(self) -> bool:
        source = self._parse_time(self.source_timestamp)
        observed = self._parse_time(self.observed_at)
        received = self._parse_time(self.received_at)
        return bool(
            self.account_id
            and self.portfolio_hash
            and self.policy_version
            and self.correlation_id
            and self.dq_tier in {"OK", "PASS", "CONDITIONAL", "BLOCK"}
            and self.exchange_health in {"HEALTHY", "DEGRADED", "UNSAFE"}
            and self.reconciliation_status in {"MATCHED", "MISMATCHED", "INCOMPLETE", "ERROR"}
            and source is not None
            and observed is not None
            and received is not None
            and source <= observed <= received
        )

    def is_safe_for_risk_increase(self) -> bool:
        if not self.is_complete():
            return False
        if not self.is_fresh():
            return False  # PKG: stale snapshot blocks risk increase
        return (
            self.dq_tier in {"OK", "PASS"}
            and self.exchange_health == "HEALTHY"
            and self.reconciliation_status == "MATCHED"
        )


class PreRiskCheckerImpl:
    """Pre-Risk 同步检查。不变量守卫：保证金、仓位上限、未决订单、Approval有效性。"""

    def __init__(
        self,
        max_leverage: float = 3.0,
        max_concentration_pct: float = 50.0,
        max_position_notional: float = 500000.0,
        max_pending_orders: int = 50,
    ):
        self.max_leverage = max_leverage
        self.max_concentration_pct = max_concentration_pct
        self.max_position_notional = max_position_notional
        self.max_pending_orders = max_pending_orders  # M10-F01: 挂单上限(原内联检查)

    async def check(self, context: _PreRiskContext) -> list[_RiskCheckResult]:
        results: list[_RiskCheckResult] = []
        cid = context.correlation_id or CorrelationId("unknown")
        try:
            leverage = float(context.leverage) if context.leverage is not None else float("nan")
            quantity = float(context.order_quantity.amount)
            price = float(context.order_price.amount) if context.order_price is not None else float("nan")
        except (AttributeError, TypeError, ValueError):
            leverage = quantity = price = float("nan")
        if not all(isfinite(value) and value > 0 for value in (leverage, quantity, price)):
            return [
                _RiskCheckResult(
                    RiskRuleLevel.R0,
                    RiskDecision.REJECTED,
                    "Order economics are missing, non-finite, or non-positive",
                    cid,
                )
            ]
        if leverage > self.max_leverage:
            results.append(
                _RiskCheckResult(
                    RiskRuleLevel.R4,
                    RiskDecision.REJECTED,
                    f"Leverage {leverage} exceeds max {self.max_leverage}",
                    cid,
                )
            )
        notional = quantity * price
        if notional > self.max_position_notional:
            results.append(
                _RiskCheckResult(
                    RiskRuleLevel.R2,
                    RiskDecision.REJECTED,
                    f"Position notional {notional} exceeds max {self.max_position_notional}",
                    cid,
                )
            )
        # M10-F01: 保证金检查（原引擎内联,收敛到真实 checker）
        # M10-R2: balance 缺失对 risk-increasing 意图 fail-closed(否则
        # 未来新调用点漏填即静默失去防线 —— 假 checker 风险);减仓/未知
        # 方向保持跳过(减仓永不封锁,M07-R2)。口径:扣除已用保证金
        # (current_margin 提供时),可用权益 = balance - margin_used。
        if context.account_balance is None or context.account_balance <= 0:
            if context.risk_increasing is True:
                results.append(
                    _RiskCheckResult(
                        RiskRuleLevel.R2,
                        RiskDecision.REJECTED,
                        "Margin check unavailable: account_balance missing for risk-increasing intent",
                        cid,
                    )
                )
        else:
            margin_used = float(context.current_margin.amount) if context.current_margin is not None else 0.0
            effective_balance = context.account_balance - margin_used
            if effective_balance <= 0:
                results.append(
                    _RiskCheckResult(
                        RiskRuleLevel.R2,
                        RiskDecision.REJECTED,
                        f"Available margin exhausted (balance={context.account_balance}, margin_used={margin_used})",
                        cid,
                    )
                )
            elif notional > effective_balance * leverage:
                results.append(
                    _RiskCheckResult(
                        RiskRuleLevel.R2,
                        RiskDecision.REJECTED,
                        f"Notional {notional} exceeds margin (balance={context.account_balance}, leverage={leverage})",
                        cid,
                    )
                )
        # M10-F01: 挂单上限检查（原引擎内联,收敛到真实 checker）
        if len(context.pending_orders) >= self.max_pending_orders:
            results.append(
                _RiskCheckResult(
                    RiskRuleLevel.R2,
                    RiskDecision.REJECTED,
                    f"Too many pending orders ({len(context.pending_orders)} >= {self.max_pending_orders})",
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
        if not callable(check_fn):
            raise TypeError("Risk rule must be callable")
        if level not in self._rules:
            self._rules[level] = []
        self._rules[level].append(check_fn)

    async def evaluate(self, pre_risk_passed: bool, results: list[_RiskCheckResult]) -> RiskDecision:
        if not pre_risk_passed or not results:
            return RiskDecision.REJECTED
        for r in results:
            if r.decision == RiskDecision.REJECTED:
                return RiskDecision.REJECTED
        return RiskDecision.APPROVED

    async def full_evaluate(self, snapshot: RiskSnapshot, rules_config: dict) -> list[_RiskCheckResult]:
        results: list[_RiskCheckResult] = []
        cid = CorrelationId("risk-eval")
        if not snapshot.is_safe_for_risk_increase():
            return [
                _RiskCheckResult(
                    RiskRuleLevel.R0,
                    RiskDecision.REJECTED,
                    "Risk snapshot is incomplete, stale, unhealthy, or unreconciled",
                    cid,
                )
            ]
        try:
            max_leverage = float(rules_config["max_leverage"])
            max_concentration_pct = float(rules_config["max_concentration_pct"])
        except (KeyError, TypeError, ValueError):
            return [
                _RiskCheckResult(
                    RiskRuleLevel.R0,
                    RiskDecision.REJECTED,
                    "Risk policy limits are missing or invalid",
                    cid,
                )
            ]
        if not all(isfinite(value) and value > 0 for value in (max_leverage, max_concentration_pct)):
            return [
                _RiskCheckResult(
                    RiskRuleLevel.R0,
                    RiskDecision.REJECTED,
                    "Risk policy limits are missing or invalid",
                    cid,
                )
            ]
        if snapshot.leverage > max_leverage:
            results.append(
                _RiskCheckResult(RiskRuleLevel.R4, RiskDecision.REJECTED, f"Leverage {snapshot.leverage}", cid)
            )
        if snapshot.concentration_pct > max_concentration_pct:
            results.append(
                _RiskCheckResult(
                    RiskRuleLevel.R5, RiskDecision.REJECTED, f"Concentration {snapshot.concentration_pct}%", cid
                )
            )
        for level, callbacks in sorted(self._rules.items(), key=lambda item: item[0].value):
            for callback in callbacks:
                try:
                    result = callback(snapshot, rules_config)
                    if isawaitable(result):
                        result = await result
                    if not isinstance(result, _RiskCheckResult) or result.rule_level is not level:
                        raise TypeError("Risk rule returned invalid evidence")
                except Exception as exc:
                    result = _RiskCheckResult(
                        level,
                        RiskDecision.REJECTED,
                        f"Risk rule unavailable: {type(exc).__name__}",
                        cid,
                    )
                results.append(result)
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
        # P1-015: 持久化日志路径 — 调用方显式恢复
        self._nonce_log_path = "evidence/BD-01/approval_nonces.jsonl"
        self._revocation_log_path = "evidence/BD-01/approval_revocations.jsonl"

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
        if not str(approval_id).strip():
            raise ValueError("Approval identity is required")
        if not nonce.strip():
            raise ValueError("Approval nonce is required")
        if expires_at is None:
            expires_at = time.time() + DEFAULT_APPROVAL_TTL_SECONDS
        if not isfinite(expires_at) or expires_at <= time.time():
            raise ValueError("Approval expiry must be finite and in the future")
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
        binding = (
            proposal_hash,
            intent_hash,
            account_snapshot_hash,
            risk_snapshot_hash,
            policy_version,
            nonce,
        )
        if any(not value.strip() for value in binding):
            raise ValueError("Complete approval binding is required")
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
            if not isfinite(expires_at) or expires_at <= time.time() or expires_at != stored_expiry:
                return False  # 过期拒绝 (BD-T01)
            effective_expiry = expires_at
        else:
            if stored_expiry <= time.time():
                return False  # 过期拒绝 (BD-T01)
            effective_expiry = stored_expiry
        if not nonce or nonce in self._nonces:
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
        if ok and consume_nonce:
            if not self._persist_nonce(nonce):
                return False
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

    def revoke(self, signature: str) -> bool:
        """BD-T01: 吊销指定签名 — 将其加入撤销集，后续 verify() 将拒绝。"""
        if not signature:
            return False
        self._revoked_sigs.add(signature)
        self._signed_expiry.pop(signature, None)
        return self._persist_revocation(signature)

    def restore_signature(self, signature: str, expires_at: float) -> bool:
        """Restore non-secret signature metadata for an unresolved intent.

        Restart recovery may rehydrate a durable approval envelope, but never
        the signing key.  HMAC verification still recomputes the signature
        with the newly injected key; this method only restores the expiry
        index required by the fail-closed verifier.
        """

        if (
            not self._signing_available
            or not signature
            or signature in self._revoked_sigs
            or not isfinite(expires_at)
            or expires_at <= time.time()
        ):
            return False
        self._signed_expiry[signature] = float(expires_at)
        return True

    # ------------------------------------------------------------------
    # P1-015: Durable nonce/revocation persistence
    # ------------------------------------------------------------------

    def _persist_nonce(self, nonce: str) -> bool:
        """P1-015: 持久化已消费 nonce 到 JSONL。"""
        try:
            import json
            import os

            directory = os.path.dirname(self._nonce_log_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            event = {"action": "consume_nonce", "nonce": nonce, "timestamp": time.time()}
            with open(self._nonce_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, sort_keys=True) + "\n")
                f.flush()
                os.fsync(f.fileno())
            return True
        except OSError:
            return False

    def _persist_revocation(self, signature: str) -> bool:
        """P1-015: 持久化撤销到 JSONL。"""
        try:
            import json
            import os

            directory = os.path.dirname(self._revocation_log_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            event = {"action": "revoke", "signature": signature, "timestamp": time.time()}
            with open(self._revocation_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, sort_keys=True) + "\n")
                f.flush()
                os.fsync(f.fileno())
            return True
        except OSError:
            return False

    def _restore_from_log(self) -> int:
        """P1-015: 从 JSONL 恢复 nonce/revocation 状态。"""
        restored = 0
        for path, expected_action, identity_field, target in [
            (self._nonce_log_path, "consume_nonce", "nonce", self._nonces),
            (self._revocation_log_path, "revoke", "signature", self._revoked_sigs),
        ]:
            try:
                import json
                import os

                if os.path.exists(path):
                    with open(path, encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                event = json.loads(line)
                                identity = event.get(identity_field) if isinstance(event, dict) else None
                                if (
                                    event.get("action") != expected_action
                                    or not isinstance(identity, str)
                                    or not identity
                                ):
                                    continue
                                target.add(identity)
                                restored += 1
                            except (json.JSONDecodeError, AttributeError, KeyError):
                                pass
            except OSError:
                pass
        return restored


class ApprovalLifecycleState(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    CONSUMED = "CONSUMED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    REJECTED = "REJECTED"


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
        if not isfinite(default_ttl_seconds) or default_ttl_seconds <= 0:
            raise ValueError("Default approval TTL must be finite and positive")
        self._approvals: dict[RiskApprovalId, ApprovalLifecycleState] = {}
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
        if not str(aid).strip():
            raise ValueError("Approval identity is required")
        effective_ttl = ttl if ttl is not None else self._default_ttl
        if not isfinite(effective_ttl) or effective_ttl <= 0:
            raise ValueError("Approval TTL must be finite and positive")
        current = self._approvals.get(aid)
        if current in {
            ApprovalLifecycleState.CONSUMED,
            ApprovalLifecycleState.EXPIRED,
            ApprovalLifecycleState.REVOKED,
            ApprovalLifecycleState.REJECTED,
        }:
            return RiskDecision.REJECTED  # 已拒绝不可逆转
        if current is ApprovalLifecycleState.APPROVED:
            existing_context = (
                self._nonces.get(aid, ""),
                self._ttls.get(aid),
                self._risk_snapshots.get(aid, ""),
                self._policy_versions.get(aid, ""),
            )
            requested_context = (nonce, effective_ttl, risk_snapshot_hash, policy_version)
            if existing_context != requested_context:
                raise ValueError("Approval identity has conflicting bound context")
            return RiskDecision.APPROVED  # 幂等：已批准
        self._approvals[aid] = ApprovalLifecycleState.APPROVED
        self._timestamps[aid] = time.time()
        self._ttls[aid] = effective_ttl
        if nonce:
            self._nonces[aid] = nonce
        if risk_snapshot_hash:
            self._risk_snapshots[aid] = risk_snapshot_hash
        if policy_version:
            self._policy_versions[aid] = policy_version
        return RiskDecision.APPROVED

    def reject(self, aid: RiskApprovalId) -> RiskDecision:
        """拒绝审批 — 已 APPROVED 的状态不可被覆盖为 REJECTED。"""
        if self._approvals.get(aid) is ApprovalLifecycleState.APPROVED:
            return RiskDecision.APPROVED
        self._approvals[aid] = ApprovalLifecycleState.REJECTED
        return RiskDecision.REJECTED

    def consume(self, aid: RiskApprovalId) -> RiskDecision:
        """消费审批 — 一次性使用后将 APPROVED → CONSUMED。

        消费后的批准不可再次使用。用于订单提交场景。
        """
        if self._approvals.get(aid) is not ApprovalLifecycleState.APPROVED:
            return self.get(aid)
        if self._is_expired(aid):
            self._approvals[aid] = ApprovalLifecycleState.EXPIRED
            return RiskDecision.PENDING
        self._approvals[aid] = ApprovalLifecycleState.CONSUMED
        self._consumed.add(aid)
        return RiskDecision.APPROVED

    def revoke(self, aid: RiskApprovalId) -> RiskDecision:
        """显式撤销审批 — APPROVED → REVOKED。"""
        if self._approvals.get(aid) is ApprovalLifecycleState.APPROVED:
            self._approvals[aid] = ApprovalLifecycleState.REVOKED
            return RiskDecision.APPROVED  # was approved before revocation
        return self.get(aid)

    def expire(self, aid: RiskApprovalId) -> None:
        """标记过期 — 审批 TTL 超时后自动调用。"""
        if self._approvals.get(aid) is ApprovalLifecycleState.APPROVED:
            self._approvals[aid] = ApprovalLifecycleState.EXPIRED

    def _is_expired(self, aid: RiskApprovalId) -> bool:
        """检查审批是否已过期。"""
        if self._approvals.get(aid) is ApprovalLifecycleState.EXPIRED:
            return True
        if self._approvals.get(aid) is not ApprovalLifecycleState.APPROVED:
            return False
        elapsed = time.time() - self._timestamps[aid]
        return elapsed > self._ttls[aid]

    def is_consumed(self, aid: RiskApprovalId) -> bool:
        """检查审批是否已被消费。"""
        return self._approvals.get(aid) is ApprovalLifecycleState.CONSUMED

    def is_valid_for_use(
        self,
        aid: RiskApprovalId,
        *,
        nonce: str | None = None,
        risk_snapshot_hash: str | None = None,
        policy_version: str | None = None,
    ) -> bool:
        """审批是否有效可用 — APPROVED 且未过期、未被消费。"""
        if self._approvals.get(aid) is not ApprovalLifecycleState.APPROVED:
            return False
        if self._is_expired(aid):
            self._approvals[aid] = ApprovalLifecycleState.EXPIRED
            return False
        expected_context = (
            (nonce, self._nonces.get(aid, "")),
            (risk_snapshot_hash, self._risk_snapshots.get(aid, "")),
            (policy_version, self._policy_versions.get(aid, "")),
        )
        if any(expected is not None and expected != stored for expected, stored in expected_context):
            return False
        return aid not in self._consumed

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
            self._approvals[aid] = ApprovalLifecycleState.REJECTED
            return RiskDecision.REJECTED
        if not risk_check_passed:
            self._approvals[aid] = ApprovalLifecycleState.REJECTED
            return RiskDecision.REJECTED
        return self.approve(
            aid,
            nonce=nonce,
            ttl=ttl,
            risk_snapshot_hash=risk_snapshot_hash,
            policy_version=policy_version,
        )

    def get(self, aid: RiskApprovalId) -> RiskDecision:
        state = self._approvals.get(aid, ApprovalLifecycleState.PENDING)
        if state is ApprovalLifecycleState.APPROVED:
            return RiskDecision.APPROVED
        if state in {ApprovalLifecycleState.REJECTED, ApprovalLifecycleState.REVOKED}:
            return RiskDecision.REJECTED
        return RiskDecision.PENDING

    def get_metadata(self, aid: RiskApprovalId) -> dict:
        """获取审批元数据（审计用）。"""
        if self._approvals.get(aid) is ApprovalLifecycleState.APPROVED and self._is_expired(aid):
            self._approvals[aid] = ApprovalLifecycleState.EXPIRED
        return {
            "status": self._approvals.get(aid, ApprovalLifecycleState.PENDING).value,
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
        return len(self._violations) >= 6

    def cannot_approve(self) -> bool:
        return True  # Post-Risk 永远不能追认
