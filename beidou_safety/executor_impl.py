
"""Trading Cell 单活 Executor。Lease、Fencing 与性能预算。"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from beidou_shared.types import CorrelationId, OrderId, ExecutionId, ResultStatus

class LeaseManager:
    """租约管理器。防止双主。"""
    def __init__(self, lease_timeout: float = 30.0):
        self._leases: dict[str, datetime] = {}
        self._timeout = lease_timeout

    def acquire(self, executor_id: str) -> bool:
        now = datetime.now(timezone.utc)
        existing = self._leases.get(executor_id)
        if existing and (now - existing).total_seconds() < self._timeout:
            return False
        self._leases[executor_id] = now
        return True

    def renew(self, executor_id: str) -> bool:
        now = datetime.now(timezone.utc)
        existing = self._leases.get(executor_id)
        if existing is None:
            return False
        self._leases[executor_id] = now
        return True

    def release(self, executor_id: str) -> None:
        self._leases.pop(executor_id, None)

    def is_active(self, executor_id: str) -> bool:
        existing = self._leases.get(executor_id)
        if existing is None:
            return False
        return (datetime.now(timezone.utc) - existing).total_seconds() < self._timeout

class FencingProtection:
    """Fencing 保护。旧 Executor 不能发送订单。"""
    def __init__(self):
        self._active_generation: dict[str, int] = {}
        self._fenced: set[str] = set()

    def promote(self, executor_id: str, generation: int) -> None:
        self._active_generation[executor_id] = generation

    def fence(self, executor_id: str) -> None:
        self._fenced.add(executor_id)

    def check(self, executor_id: str, generation: int) -> ResultStatus:
        if executor_id in self._fenced:
            return ResultStatus.ERROR
        active_gen = self._active_generation.get(executor_id)
        if active_gen is not None and generation < active_gen:
            return ResultStatus.ERROR
        return ResultStatus.SUCCESS
