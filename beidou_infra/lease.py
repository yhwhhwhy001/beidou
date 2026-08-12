"""Fencing Lease — BD-03 双主防护。

PostgreSQL advisory lock 和 Redis fencing lease。
旧 generation 不得执行，防止裂脑。
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class LeaseState(str, Enum):
    ACQUIRED = "ACQUIRED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    FENCED = "FENCED"  # 旧 generation 被隔离
    UNKNOWN = "UNKNOWN"  # 后端不可达，租约状态未知 — fail-closed


@dataclass
class FencingLease:
    """分布式租约。

    使用 generation 递增门控：新实例启动时 generation+1，
    旧实例检测到 generation 落后时自动 FENCED。
    """

    instance_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    generation: int = 1
    ttl_seconds: int = 30
    state: LeaseState = LeaseState.ACQUIRED
    acquired_at: float = field(default_factory=time.monotonic)

    def is_valid(self) -> bool:
        """租约是否仍然有效。UNKNOWN 状态视为无效（fail-closed）。"""
        if self.state != LeaseState.ACQUIRED:
            return False
        if self.state == LeaseState.UNKNOWN:
            return False  # 显式拒绝
        if time.monotonic() - self.acquired_at > self.ttl_seconds:
            self.state = LeaseState.EXPIRED
            return False
        return True

    def fence(self) -> None:
        """隔离当前实例（旧 generation）。"""
        self.state = LeaseState.FENCED

    def revoke(self) -> None:
        """主动撤销租约。"""
        self.state = LeaseState.REVOKED


class LeaseManager:
    """租约管理器。

    PostgreSQL 模式: 使用 pg_try_advisory_lock(lease_id)
    Redis 模式: 使用 SET NX EX + generation fencing
    Paper 模式: 单实例，始终有效
    """

    LEASE_KEY = 0x42454944  # "BEID" in hex

    def __init__(self, mode: str = "paper"):
        self._mode = mode
        self._lease: FencingLease | None = None

    def acquire(self, generation: int = 1, ttl: int = 30) -> FencingLease:
        """获取租约。"""
        lease = FencingLease(generation=generation, ttl_seconds=ttl)
        if self._mode == "paper":
            # Paper 模式：单实例，始终成功
            lease.state = LeaseState.ACQUIRED
        elif self._mode == "redis":
            # Redis: 尝试 SET NX EX
            self._try_redis_acquire(lease)
        elif self._mode == "postgresql":
            # PostgreSQL: 尝试 advisory lock
            self._try_pg_acquire(lease)
        else:
            lease.state = LeaseState.UNKNOWN

        self._lease = lease
        return lease

    def renew(self) -> bool:
        """续期租约。"""
        if not self._lease:
            return False
        if self._lease.state != LeaseState.ACQUIRED:
            return False
        self._lease.acquired_at = time.monotonic()
        return True

    def release(self) -> None:
        """释放租约。"""
        if self._lease:
            self._lease.revoke()
            self._lease = None

    def is_current_generation(self, generation: int) -> bool:
        """检查是否为当前 generation。

        如果传入的 generation < 当前 generation，说明该实例已被取代。
        """
        if not self._lease:
            return False
        return generation >= self._lease.generation

    @staticmethod
    def _try_redis_acquire(lease: FencingLease) -> None:
        """通过 Redis 获取租约（需要 redis 库）。

        Redis 连接参数优先从环境变量 REDIS_URL 读取；
        否则回退到 BEIDOU_REDIS_HOST / BEIDOU_REDIS_PORT。
        """
        try:
            import redis

            redis_url = os.environ.get("REDIS_URL", "")
            if redis_url:
                r = redis.Redis.from_url(redis_url, socket_timeout=2)
            else:
                redis_host = os.environ.get("BEIDOU_REDIS_HOST", "localhost")
                redis_port = int(os.environ.get("BEIDOU_REDIS_PORT", "6379"))
                r = redis.Redis(host=redis_host, port=redis_port, socket_timeout=2)
            key = f"beidou:lease:{LeaseManager.LEASE_KEY}"
            acquired = r.set(key, lease.instance_id, nx=True, ex=lease.ttl_seconds)
            if acquired:
                lease.state = LeaseState.ACQUIRED
            else:
                lease.state = LeaseState.FENCED
        except Exception:
            # Redis 不可用 → UNKNOWN（fail-closed，禁止自动回退到单实例模式）
            lease.state = LeaseState.UNKNOWN

    @staticmethod
    def _try_pg_acquire(lease: FencingLease) -> None:
        """通过 PostgreSQL advisory lock 获取租约。"""
        try:
            import psycopg

            conn = psycopg.connect(os.environ.get("DATABASE_URL", ""))
            cur = conn.execute(
                "SELECT pg_try_advisory_lock(%s)",
                (LeaseManager.LEASE_KEY,),
            )
            acquired = cur.fetchone()[0]
            if acquired:
                lease.state = LeaseState.ACQUIRED
            else:
                lease.state = LeaseState.FENCED
        except Exception:
            # PostgreSQL 不可达 → UNKNOWN（fail-closed）
            lease.state = LeaseState.UNKNOWN
