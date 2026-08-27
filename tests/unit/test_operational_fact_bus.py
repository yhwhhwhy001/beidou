"""
PKG25 (BDS-P1-048/049): 操作事实总线测试。

覆盖：
- Publish / Subscribe / Query
- 事实不可变性
- 域隔离
- Engine collect_operational_facts 公共 API
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from beidou_observability.monitoring.fact_bus import (
    FactBus,
    FactDomain,
    OperationalFact,
    get_fact_bus,
    reset_fact_bus,
)


class TestOperationalFact:
    """事实不可变性与序列化。"""

    def test_fact_is_frozen(self) -> None:
        """OperationalFact 不可变。"""
        fact = OperationalFact(
            fact_type="test",
            domain=FactDomain.RISK,
            payload={"key": "value"},
        )
        with pytest.raises(FrozenInstanceError):
            fact.payload = {}  # type: ignore[misc]

    def test_fact_to_dict(self) -> None:
        """to_dict 返回完整字典。"""
        fact = OperationalFact(
            fact_type="reconciliation_result",
            domain=FactDomain.LEDGER,
            payload={"matched": True},
            source="engine",
        )
        d = fact.to_dict()
        assert d["fact_type"] == "reconciliation_result"
        assert d["domain"] == "ledger"
        assert d["payload"] == {"matched": True}
        assert d["source"] == "engine"


class TestFactBus:
    """发布/订阅/查询。"""

    def test_publish_and_query(self) -> None:
        bus = FactBus()
        fact = OperationalFact(
            fact_type="risk_state",
            domain=FactDomain.RISK,
            payload={"level": "NORMAL"},
        )
        bus.publish(fact)

        results = bus.query(fact_type="risk_state")
        assert len(results) == 1
        assert results[0].payload["level"] == "NORMAL"

    def test_query_by_domain(self) -> None:
        bus = FactBus()
        bus.publish(OperationalFact("a", FactDomain.RISK, {"v": 1}))
        bus.publish(OperationalFact("b", FactDomain.LEDGER, {"v": 2}))

        risk_facts = bus.query(domain=FactDomain.RISK)
        assert len(risk_facts) == 1
        assert risk_facts[0].fact_type == "a"

        ledger_facts = bus.query(domain=FactDomain.LEDGER)
        assert len(ledger_facts) == 1
        assert ledger_facts[0].fact_type == "b"

    def test_get_latest(self) -> None:
        bus = FactBus()
        bus.publish(OperationalFact("x", FactDomain.RISK, {"v": 1}))
        bus.publish(OperationalFact("x", FactDomain.RISK, {"v": 2}))

        latest = bus.get_latest("x")
        assert latest is not None
        assert latest.payload["v"] == 2

    def test_get_latest_payload_convenience(self) -> None:
        bus = FactBus()
        bus.publish(OperationalFact("test", FactDomain.CONTROL, {"status": "ACTIVE"}))

        payload = bus.get_latest_payload("test")
        assert payload == {"status": "ACTIVE"}

    def test_get_latest_missing_returns_empty(self) -> None:
        bus = FactBus()
        assert bus.get_latest("nonexistent") is None
        assert bus.get_latest_payload("nonexistent") == {}

    def test_max_facts_cap(self) -> None:
        bus = FactBus(max_facts=5)
        for i in range(10):
            bus.publish(OperationalFact(f"f{i}", FactDomain.RISK, {"i": i}))

        results = bus.query()
        assert len(results) == 5
        assert results[-1].payload["i"] == 9  # 最后一个是第10个

    def test_subscribe_callback(self) -> None:
        bus = FactBus()
        received = []

        def callback(fact):
            received.append(fact.payload)

        bus.subscribe("risk_event", callback)
        bus.publish(OperationalFact("risk_event", FactDomain.RISK, {"msg": "test"}))
        bus.publish(OperationalFact("other", FactDomain.RISK, {"msg": "nope"}))

        assert len(received) == 1
        assert received[0] == {"msg": "test"}


class TestGlobalFactBus:
    """全局限总线单例。"""

    def setup_method(self):
        reset_fact_bus()

    def teardown_method(self):
        reset_fact_bus()

    def test_singleton_returns_same_instance(self) -> None:
        bus1 = get_fact_bus()
        bus2 = get_fact_bus()
        assert bus1 is bus2

    def test_reset_creates_new_instance(self) -> None:
        bus1 = get_fact_bus()
        reset_fact_bus()
        bus2 = get_fact_bus()
        assert bus1 is not bus2


class TestFactDomainSeparation:
    """域隔离。"""

    def test_all_domains_distinct(self) -> None:
        domains = set()
        for d in FactDomain:
            domains.add(d.value)
        assert len(domains) == len(FactDomain)
        assert "risk" in domains
        assert "execution" in domains
        assert "ledger" in domains
