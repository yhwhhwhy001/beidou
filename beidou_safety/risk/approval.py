"""Approval 签名与验证 — BD-07。

Approval 绑定 intent_hash、portfolio_decision_hash、account_fact_version、
policy_version、signer、issued_at、expires_at。

使用 HMAC-SHA256 签名；密钥不可写入数据库明文。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SignedApproval:
    """不可变签名 Approval。

    任何字段被修改后，verify() 返回 False。
    """

    approval_id: str
    intent_hash: str
    portfolio_decision_hash: str
    account_fact_version: str
    policy_version: str
    signer: str
    issued_at: float  # epoch seconds
    expires_at: float  # epoch seconds
    generation: int = 1
    signature: str = ""

    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    def verify(self, signing_key: str) -> bool:
        """验证签名。"""
        if self.is_expired():
            return False
        expected = self._compute_signature(signing_key)
        return hmac.compare_digest(self.signature, expected)

    def sign(self, signing_key: str) -> "SignedApproval":
        """使用 HMAC-SHA256 签名。"""
        sig = self._compute_signature(signing_key)
        return SignedApproval(
            approval_id=self.approval_id,
            intent_hash=self.intent_hash,
            portfolio_decision_hash=self.portfolio_decision_hash,
            account_fact_version=self.account_fact_version,
            policy_version=self.policy_version,
            signer=self.signer,
            issued_at=self.issued_at,
            expires_at=self.expires_at,
            generation=self.generation,
            signature=sig,
        )

    def _compute_signature(self, key: str) -> str:
        payload = f"{self.approval_id}|{self.intent_hash}|{self.portfolio_decision_hash}|{self.account_fact_version}|{self.policy_version}|{self.signer}|{self.issued_at}|{self.expires_at}|{self.generation}"
        return hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()


class ApprovalSigner:
    """Approval 签名器。

    密钥通过环境变量 BEIDOU_SIGNING_KEY 注入。
    密钥不可写入数据库、日志或配置文件。
    """

    def __init__(self, signing_key: str | None = None):
        self._key = signing_key or os.environ.get("BEIDOU_SIGNING_KEY", "")
        if not self._key:
            raise ValueError("BEIDOU_SIGNING_KEY not set — cannot sign approvals")

    def create_approval(
        self,
        approval_id: str,
        intent_hash: str,
        portfolio_hash: str,
        account_version: str,
        policy_version: str,
        ttl_seconds: int = 300,
    ) -> SignedApproval:
        """创建并签名 Approval。"""
        now = time.time()
        unsigned = SignedApproval(
            approval_id=approval_id,
            intent_hash=intent_hash,
            portfolio_decision_hash=portfolio_hash,
            account_fact_version=account_version,
            policy_version=policy_version,
            signer="beidou-autopilot",
            issued_at=now,
            expires_at=now + ttl_seconds,
        )
        return unsigned.sign(self._key)

    def verify(self, approval: SignedApproval) -> bool:
        """验证 Approval 签名、过期和字段完整性。"""
        if not self._key:
            return False
        return approval.verify(self._key)


@dataclass(frozen=True, slots=True)
class PaperDecision:
    """纸面决策 — 仅用于研究和回测，绝不可被 Exchange Executor 接受。

    不变量：
    - mode 永远为 PAPER。
    - non_tradable 永远为 True。
    - Exchange Executor 必须在入口校验 non_tradable，拒绝所有 Paper 决策。
    """

    decision_id: str
    intent_hash: str
    portfolio_hash: str
    policy_version: str
    issued_at: float
    mode: str = "PAPER"
    non_tradable: bool = True
    reason: str = ""


class PaperApprovalPort:
    """纸面审批端口 — 生成明确标记为 PAPER、non_tradable=true 的纸面决策。

    Paper 决策与真实签名审批 (ApprovalSigner) 使用完全不同的类型和路径，
    防止纸面决策被误路由到执行层。
    """

    def decide(
        self, snapshot_hash: str, intent_hash: str = "", portfolio_hash: str = "", policy_version: str = ""
    ) -> PaperDecision:
        """生成纸面交易决策。

        返回的 PaperDecision 携带 mode=PAPER, non_tradable=true，
        Exchange Executor 必须拒绝此类决策。
        """
        import time
        import uuid

        return PaperDecision(
            decision_id=f"paper-{uuid.uuid4().hex[:12]}",
            intent_hash=intent_hash,
            portfolio_hash=portfolio_hash,
            policy_version=policy_version,
            issued_at=time.time(),
            mode="PAPER",
            non_tradable=True,
            reason=f"Paper decision for snapshot {snapshot_hash[:16]}",
        )
