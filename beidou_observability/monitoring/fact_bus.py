"""
PKG25 (BDS-P1-048/049): 操作事实总线。

解耦 Engine 私有字段与监控消费者：
- Engine 发布操作事实到 FactBus
- 监控从 FactBus 订阅事实，不再直接访问私有字段
- 事实类型化、不可变、可追溯来源
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)


class FactDomain(str, Enum):
    """事实域 — 对应执行包中的权威域。"""

    MARKET = "market"
    RESEARCH = "research"
    STRATEGY = "strategy"
    RISK = "risk"
    EXECUTION = "execution"
    LEDGER = "ledger"
    PROTECTION = "protection"
    MONITORING = "monitoring"
    CONTROL = "control"
    LIFECYCLE = "lifecycle"


@dataclass(frozen=True)
class OperationalFact:
    """操作事实 — 不可变、类型化、可追溯来源。

    BDS-P1-048: 替代 getattr(engine, "_field") 模式。
    """

    fact_type: str  # e.g. "reconciliation_result", "order_tracker", "risk_level"
    domain: FactDomain
    payload: dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    source: str = "engine"  # 发布来源
    generation: int = 1
    correlation_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_type": self.fact_type,
            "domain": self.domain.value,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "source": self.source,
            "generation": self.generation,
            "correlation_id": self.correlation_id,
        }


class FactBus:
    """操作事实总线。

    发布/订阅模式，解耦生产者（Engine）和消费者（Monitoring）。
    保留最近 N 个事实以供查询。
    """

    def __init__(self, max_facts: int = 500) -> None:
        self._facts: list[OperationalFact] = []
        self._max_facts = max_facts
        self._subscribers: dict[str, list[Callable]] = {}  # fact_type → callbacks

    def publish(self, fact: OperationalFact) -> None:
        """发布一个操作事实。"""
        self._facts.append(fact)
        if len(self._facts) > self._max_facts:
            self._facts = self._facts[-self._max_facts :]

        # 通知订阅者
        for callback in self._subscribers.get(fact.fact_type, []):
            try:
                callback(fact)
            except Exception as exc:
                logger.warning(
                    "Operational fact subscriber failed for %s: %s",
                    fact.fact_type,
                    type(exc).__name__,
                )

    def subscribe(self, fact_type: str, callback) -> None:
        """订阅特定类型的事实。"""
        if fact_type not in self._subscribers:
            self._subscribers[fact_type] = []
        self._subscribers[fact_type].append(callback)

    def query(
        self,
        fact_type: str = "",
        domain: FactDomain | None = None,
        limit: int = 10,
    ) -> list[OperationalFact]:
        """查询最近的事实。"""
        results = self._facts
        if fact_type:
            results = [f for f in results if f.fact_type == fact_type]
        if domain:
            results = [f for f in results if f.domain == domain]
        return results[-limit:]

    def get_latest(self, fact_type: str, *, source: str | None = None) -> OperationalFact | None:
        """获取最新事实，可按发布来源隔离不同运行实例。"""
        for fact in reversed(self._facts):
            if fact.fact_type == fact_type and (source is None or fact.source == source):
                return fact
        return None

    def get_latest_payload(self, fact_type: str) -> dict[str, Any]:
        """获取最新事实的 payload（便捷方法）。"""
        fact = self.get_latest(fact_type)
        return fact.payload if fact else {}

    def clear(self) -> None:
        self._facts.clear()


# 全局单例
_global_fact_bus: FactBus | None = None


def get_fact_bus() -> FactBus:
    """获取全局 FactBus 单例。"""
    global _global_fact_bus
    if _global_fact_bus is None:
        _global_fact_bus = FactBus()
    return _global_fact_bus


def reset_fact_bus() -> None:
    """重置全局 FactBus（测试用）。"""
    global _global_fact_bus
    _global_fact_bus = None
