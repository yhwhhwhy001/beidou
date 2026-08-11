"""
P1-004 (BDS-P1-004): Exit Recovery Contract — 退出路径故障安全降险合同。

当策略退出或紧急平仓路径出现故障时：
1. 所有开放仓位必须被 cancel 或 reduce-only
2. 不得创建新仓位
3. 退出意图持久化，进程重启后继续
4. 退出完成前阻止任何增加风险的操作
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class ExitPhase(str, Enum):
    """退出阶段。"""

    INTENT_RECORDED = "INTENT_RECORDED"  # 退出意图已记录
    CANCEL_PENDING = "CANCEL_PENDING"  # 取消进行中
    POSITIONS_CLOSING = "POSITIONS_CLOSING"  # 仓位平仓中
    EXIT_COMPLETE = "EXIT_COMPLETE"  # 退出完成
    EXIT_FAILED = "EXIT_FAILED"  # 退出失败（需人工介入）


@dataclass
class ExitRecoveryContract:
    """退出恢复合同 — 绑定退出意图与安全约束。

    P1-004: 确保退出路径故障时有明确的恢复合同，不静默失败。
    """

    strategy_id: str
    phase: ExitPhase = ExitPhase.INTENT_RECORDED
    initiated_at: float = field(default_factory=time.time)
    cancel_order_ids: list[str] = field(default_factory=list)
    position_symbols: list[str] = field(default_factory=list)
    last_attempt_at: float = 0.0
    attempt_count: int = 0
    max_attempts: int = 10
    failures: list[str] = field(default_factory=list)
    recovery_evidence: dict = field(default_factory=dict)

    def is_complete(self) -> bool:
        return self.phase == ExitPhase.EXIT_COMPLETE

    def is_blocking(self) -> bool:
        """退出期间阻止任何新风险。"""
        return self.phase in (
            ExitPhase.INTENT_RECORDED,
            ExitPhase.CANCEL_PENDING,
            ExitPhase.POSITIONS_CLOSING,
            ExitPhase.EXIT_FAILED,
        )

    def record_failure(self, reason: str) -> None:
        self.failures.append(reason)
        self.attempt_count += 1
        self.last_attempt_at = time.time()
        if self.attempt_count >= self.max_attempts:
            self.phase = ExitPhase.EXIT_FAILED

    def advance(self, new_phase: ExitPhase) -> bool:
        """仅允许向前推进阶段。"""
        valid_transitions = {
            ExitPhase.INTENT_RECORDED: {ExitPhase.CANCEL_PENDING, ExitPhase.EXIT_FAILED},
            ExitPhase.CANCEL_PENDING: {ExitPhase.POSITIONS_CLOSING, ExitPhase.EXIT_FAILED},
            ExitPhase.POSITIONS_CLOSING: {ExitPhase.EXIT_COMPLETE, ExitPhase.EXIT_FAILED},
            ExitPhase.EXIT_FAILED: {ExitPhase.CANCEL_PENDING},  # 人工干预后重试
            ExitPhase.EXIT_COMPLETE: set(),  # 终态
        }
        if new_phase in valid_transitions.get(self.phase, set()):
            self.phase = new_phase
            return True
        return False


class ExitRecoveryManager:
    """退出恢复管理器 — 管理所有活跃的退出合同。"""

    def __init__(self) -> None:
        self._contracts: dict[str, ExitRecoveryContract] = {}

    def initiate_exit(self, strategy_id: str, positions: list[str], open_orders: list[str]) -> ExitRecoveryContract:
        contract = ExitRecoveryContract(
            strategy_id=strategy_id,
            position_symbols=positions,
            cancel_order_ids=open_orders,
        )
        self._contracts[strategy_id] = contract
        return contract

    def get_contract(self, strategy_id: str) -> ExitRecoveryContract | None:
        return self._contracts.get(strategy_id)

    def is_any_exit_blocking(self) -> bool:
        return any(c.is_blocking() for c in self._contracts.values())

    def complete_exit(self, strategy_id: str) -> bool:
        contract = self._contracts.get(strategy_id)
        if contract is None:
            return False
        return contract.advance(ExitPhase.EXIT_COMPLETE)
