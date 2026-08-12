"""Trading Cell 单活 Executor。Lease、Fencing 与性能预算。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from beidou_shared.types import ResultStatus


class LeaseManager:
    """租约管理器。防止双主。"""

    def __init__(self, lease_timeout: float = 30.0):
        if lease_timeout <= 0:
            raise ValueError("lease_timeout must be positive")
        self._owner: str | None = None
        self._expires_at: datetime | None = None
        self._timeout = float(lease_timeout)

    def acquire(self, executor_id: str) -> bool:
        if not executor_id.strip():
            return False
        now = datetime.now(timezone.utc)
        if self._owner is not None and self._expires_at is not None and now < self._expires_at:
            return False
        self._owner = executor_id
        self._expires_at = now + timedelta(seconds=self._timeout)
        return True

    def renew(self, executor_id: str) -> bool:
        now = datetime.now(timezone.utc)
        if self._owner != executor_id or self._expires_at is None or now >= self._expires_at:
            return False
        self._expires_at = now + timedelta(seconds=self._timeout)
        return True

    def release(self, executor_id: str) -> None:
        if self._owner == executor_id:
            self._owner = None
            self._expires_at = None

    def is_active(self, executor_id: str) -> bool:
        return (
            self._owner == executor_id
            and self._expires_at is not None
            and datetime.now(timezone.utc) < self._expires_at
        )


class FencingProtection:
    """Fencing 保护。旧 Executor 不能发送订单。"""

    def __init__(self) -> None:
        self._active_executor_id: str | None = None
        self._active_generation = 0
        self._fenced: set[str] = set()

    def promote(self, executor_id: str, generation: int) -> None:
        if not executor_id.strip() or generation <= self._active_generation:
            raise ValueError("promotion requires a non-empty executor and a strictly newer generation")
        if self._active_executor_id is not None and self._active_executor_id != executor_id:
            self._fenced.add(self._active_executor_id)
        self._active_executor_id = executor_id
        self._active_generation = generation

    def fence(self, executor_id: str) -> None:
        self._fenced.add(executor_id)

    def check(self, executor_id: str, generation: int) -> ResultStatus:
        if executor_id in self._fenced:
            return ResultStatus.ERROR
        if executor_id != self._active_executor_id or generation != self._active_generation:
            return ResultStatus.ERROR
        return ResultStatus.SUCCESS
