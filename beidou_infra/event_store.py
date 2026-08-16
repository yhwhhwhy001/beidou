"""Append-only 事件存储 — 内存实现(未接线组件)。

M16-F01 诚实化:本类为纯内存实现(``_events`` 列表),生产零接线
(engine 的持久化由 PostgresPersistentStore/IntentOutbox 承担,非事件
溯源模型)。append-only/乐观并发/checksum 契约由单测锁定,事件溯源
持久化(BD-03)属架构演进,接线前不得宣称 PostgreSQL 持久化。
所有历史事实只追加(INSERT)，更新通过新事件表达。禁止 UPDATE/DELETE 历史记录。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any  # M21: mypy 清偿


@dataclass(frozen=True, slots=True)
class DomainEvent:
    """不可变领域事件。"""

    stream_id: str
    aggregate_type: str
    sequence: int
    event_type: str
    payload: dict
    metadata: dict = field(default_factory=dict)
    event_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ingest_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    schema_version: str = "2.0.0"
    correlation_id: str | None = None
    causation_id: str | None = None
    checksum: str = ""

    def __post_init__(self) -> None:
        if not self.checksum:
            object.__setattr__(self, "checksum", self._compute_checksum())

    def _compute_checksum(self) -> str:
        """计算事件内容的 SHA256。"""
        content = json.dumps(
            {
                "stream_id": self.stream_id,
                "aggregate_type": self.aggregate_type,
                "sequence": self.sequence,
                "event_type": self.event_type,
                "payload": self.payload,
                "event_time": self.event_time.isoformat(),
                "schema_version": self.schema_version,
                "correlation_id": self.correlation_id,
                "causation_id": self.causation_id,
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(content.encode()).hexdigest()


class EventStore:
    """Append-only 事件存储。

    特性:
    - INSERT-only，禁止 UPDATE/DELETE
    - 乐观并发控制（stream + sequence 唯一约束）
    - 事件溯源投影（replay 重建状态）
    - checksum 完整性验证
    """

    def __init__(self, connection_pool: Any = None) -> None:
        self._conn = connection_pool
        self._events: list[DomainEvent] = []  # 内存回退 (SQLite/Paper模式)

    def append(self, event: DomainEvent) -> bool:
        """追加事件。失败时抛异常（乐观并发冲突）。"""
        # 检查 sequence 冲突
        for existing in self._events:
            if existing.stream_id == event.stream_id and existing.sequence == event.sequence:
                raise ConcurrencyConflictError(f"Sequence {event.sequence} already exists for stream {event.stream_id}")

        # 验证 checksum
        expected = event._compute_checksum()
        if event.checksum and event.checksum != expected:
            raise ChecksumMismatchError(f"Checksum mismatch for event in stream {event.stream_id}")

        self._events.append(event)
        return True

    def get_stream(self, stream_id: str) -> list[DomainEvent]:
        """按 sequence 排序获取 stream 全部事件。"""
        return sorted(
            [e for e in self._events if e.stream_id == stream_id],
            key=lambda e: e.sequence,
        )

    def get_by_correlation(self, correlation_id: str) -> list[DomainEvent]:
        """按 correlation_id 查询事件。"""
        return [e for e in self._events if e.correlation_id == correlation_id]

    def replay(self, aggregate_type: str, stream_id: str) -> list[DomainEvent]:
        """事件溯源重放 — 返回该 aggregate 的所有历史事件。"""
        return sorted(
            [e for e in self._events if e.aggregate_type == aggregate_type and e.stream_id == stream_id],
            key=lambda e: e.sequence,
        )

    def verify_integrity(self) -> list[str]:
        """验证所有事件的 checksum 完整性。"""
        errors = []
        for event in self._events:
            expected = event._compute_checksum()
            if event.checksum != expected:
                errors.append(f"Checksum mismatch: {event.stream_id}#{event.sequence}")
        return errors


class ConcurrencyConflictError(Exception):
    """乐观并发冲突 — 同一 stream 的 sequence 已被占用。"""

    __slots__ = ()


class ChecksumMismatchError(Exception):
    """事件 checksum 不匹配 — 数据可能被篡改。"""

    __slots__ = ()
