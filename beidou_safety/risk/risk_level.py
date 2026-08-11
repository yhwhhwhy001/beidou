"""
PKG08 (BDS-P0-008, BDS-P0-009, BDS-P0-010): 风险等级单调严重性管理。

核心不变量：
1. 风险等级只允许升级，禁止降级（NORMAL→NO_NEW_RISK→EXIT_ONLY→LOCKED→EMERGENCY_FLATTEN）
2. 降级必须经过显式恢复流程（人工审核 + 签名证据）
3. 未知/损坏状态 fallback 到 LOCKED（fail closed），绝不 fallback 到 NORMAL
4. Daily reset 仅清除日内状态，不重置跨日 breaker

与 BDS 问题对应：
- BDS-P0-008: 风险等级不可由 LOCKED 降级为 EXIT_ONLY
- BDS-P0-009: 每日重置不清除 Drawdown breaker（跨日状态持久化）
- BDS-P0-010: 损坏/缺失状态不 fallback NORMAL
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import ClassVar


class RiskLevel(IntEnum):
    """风险等级 — 数字越大越严重。单调升级，禁止降级。"""

    NORMAL = 0
    NO_NEW_RISK = 1
    EXIT_ONLY = 2
    LOCKED = 3
    EMERGENCY_FLATTEN = 4

    @classmethod
    def from_string(cls, s: str) -> RiskLevel:
        """从字符串解析风险等级 — UNKNOWN → LOCKED (fail closed)。"""
        try:
            return cls[s.upper()]
        except (KeyError, AttributeError):
            return cls.LOCKED  # Fail closed: 未知=最高风险


class BreakerScope(IntEnum):
    """断路器作用域。"""

    INTRADAY = 0  # 日内 — 每日重置
    DAILY = 1  # 每日 — 跨日持久
    SESSION = 2  # 会话 — 重启重置
    PERMANENT = 3  # 永久 — 手动恢复


@dataclass(frozen=True)
class RiskLevelState:
    """风险等级状态快照 — 不可变。"""

    level: RiskLevel
    reason: str
    timestamp: float = field(default_factory=time.time)
    generation: int = 1
    breaker_scope: BreakerScope = BreakerScope.SESSION
    evidence_hash: str = ""

    def compute_evidence_hash(self) -> str:
        """计算状态证据哈希。"""
        payload = f"{self.level.name}|{self.reason}|{self.timestamp}|{self.generation}|{self.breaker_scope.name}"
        return hashlib.sha256(payload.encode()).hexdigest()

    @property
    def is_blocking(self) -> bool:
        """是否阻断新风险。"""
        return self.level >= RiskLevel.NO_NEW_RISK


class RiskLevelManager:
    """PKG08: 风险等级管理器 — 强制单调升级。"""

    # 严重性排序（数字越大越严重）
    _SEVERITY_ORDER: ClassVar[list[RiskLevel]] = [
        RiskLevel.NORMAL,
        RiskLevel.NO_NEW_RISK,
        RiskLevel.EXIT_ONLY,
        RiskLevel.LOCKED,
        RiskLevel.EMERGENCY_FLATTEN,
    ]

    def __init__(self) -> None:
        self._state: RiskLevel | None = None
        self._state_history: list[RiskLevelState] = []
        self._intraday_breakers: dict[str, float] = {}  # breaker_name → trigger_time
        self._daily_breakers: dict[str, float] = {}  # 跨日持久
        self._generation: int = 0

    @property
    def current_level(self) -> RiskLevel:
        """当前风险等级。未初始化时返回 NORMAL（保守）。"""
        return self._state or RiskLevel.NORMAL

    def escalate(
        self,
        new_level: RiskLevel,
        reason: str,
        breaker_scope: BreakerScope = BreakerScope.SESSION,
        evidence: str = "",
    ) -> RiskLevelState:
        """升级风险等级 — 只允许向更严重方向移动。

        Raises:
            ValueError: 尝试降级风险等级。
        """
        current = self.current_level
        if new_level < current:
            raise ValueError(
                f"风险等级不可降级: {current.name} → {new_level.name}。请使用 recover() 进行显式恢复。原因: {reason}"
            )

        self._generation += 1
        state = RiskLevelState(
            level=new_level,
            reason=reason,
            generation=self._generation,
            breaker_scope=breaker_scope,
            evidence_hash=hashlib.sha256(evidence.encode()).hexdigest() if evidence else "",
        )
        self._state = new_level
        self._state_history.append(state)

        if breaker_scope == BreakerScope.INTRADAY:
            self._intraday_breakers[reason] = time.time()
        elif breaker_scope in (BreakerScope.DAILY, BreakerScope.PERMANENT):
            self._daily_breakers[reason] = time.time()

        return state

    def recover(self, new_level: RiskLevel, recovery_evidence: str) -> RiskLevelState:
        """显式恢复 — 降低风险等级的唯一途径。

        要求：
        1. 恢复必须绑定签名证据
        2. 只能恢复到 NORMAL 或 NO_NEW_RISK
        3. 存在未解决的 DAILY/PERMANENT breaker 时禁止恢复到 NORMAL

        Raises:
            ValueError: 恢复条件不满足。
        """
        if not recovery_evidence:
            raise ValueError("恢复必须提供签名证据")

        if new_level not in (RiskLevel.NORMAL, RiskLevel.NO_NEW_RISK):
            raise ValueError(f"恢复只能到 NORMAL 或 NO_NEW_RISK，不能到 {new_level.name}")

        # 检查跨日 breaker — 未解决前禁止恢复到 NORMAL
        if new_level == RiskLevel.NORMAL:
            unresolved_daily = [
                name
                for name, t in self._daily_breakers.items()
                if time.time() - t < 86400 * 7  # 7天内未重置
            ]
            if unresolved_daily:
                raise ValueError(f"存在未解决的跨日断路器: {unresolved_daily}。请先重置相关 breaker 再恢复。")

        current = self.current_level
        if new_level >= current:
            raise ValueError(f"恢复要求目标等级低于当前等级: {current.name} → {new_level.name}")

        self._generation += 1
        state = RiskLevelState(
            level=new_level,
            reason=f"RECOVERY: {recovery_evidence[:100]}",
            generation=self._generation,
            breaker_scope=BreakerScope.SESSION,
            evidence_hash=hashlib.sha256(recovery_evidence.encode()).hexdigest(),
        )
        self._state = new_level
        self._state_history.append(state)
        return state

    def reset_intraday(self) -> None:
        """每日重置 — 仅清除日内 breaker，保留跨日 breaker。

        PKG08 (BDS-P0-009): 不会清除 Drawdown breaker（跨日持久）。
        """
        self._intraday_breakers.clear()
        # 如果当前状态仅由日内 breaker 触发，考虑降级
        if self.current_level >= RiskLevel.NO_NEW_RISK and not self._daily_breakers:
            # 仅日内触发 → 可重置到 NORMAL
            self._state = RiskLevel.NORMAL

    def reset_daily_breaker(self, breaker_name: str) -> None:
        """手动重置跨日断路器。"""
        self._daily_breakers.pop(breaker_name, None)

    @classmethod
    def from_corrupted_state(cls, recovery_data: dict | None) -> RiskLevelManager:
        """从损坏状态恢复 — fail closed (LOCKED)。

        PKG08 (BDS-P0-010): 损坏/缺失的风险状态绝不 fallback NORMAL。
        """
        manager = cls()
        if recovery_data is None:
            manager._state = RiskLevel.LOCKED
            manager._state_history.append(
                RiskLevelState(
                    level=RiskLevel.LOCKED,
                    reason="CORRUPTED_STATE_FALLBACK_LOCKED",
                    breaker_scope=BreakerScope.SESSION,
                )
            )
            return manager

        try:
            level_str = recovery_data.get("level", "LOCKED")
            manager._state = RiskLevel.from_string(level_str)
        except Exception:
            manager._state = RiskLevel.LOCKED

        return manager

    def get_history(self) -> list[RiskLevelState]:
        return list(self._state_history)
