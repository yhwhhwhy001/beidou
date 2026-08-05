"""
AlphaGraph 有向无环图测试。
"""

from __future__ import annotations

import pytest

from beidou_shared.types import SchemaVersion, StrategyId
from beidou_strategy.alpha import (
    AlphaComponent,
    AlphaComponentType,
    AlphaGraph,
    AlphaSignal,
    SignalDirection,
)


class FakeComponent(AlphaComponent):
    """测试用 Alpha 组件 — TEST_SYNTHETIC。"""

    def __init__(
        self,
        component_type: AlphaComponentType,
        component_id: str,
        version: SchemaVersion = SchemaVersion("TEST_SYNTHETIC_1.0"),
    ) -> None:
        super().__init__(component_type, component_id, version)
        self._valid = True

    async def generate(self, context: dict) -> AlphaSignal:
        return AlphaSignal(
            strategy_id=StrategyId("TEST_SYNTHETIC"),
            component_type=self.component_type,
            direction=SignalDirection.LONG,
            strength=0.5,
            confidence=0.8,
            instrument_id="TEST_BTCUSDT",
            venue_id="TEST_BINANCE",
        )

    def validate(self) -> bool:
        return self._valid

    def set_valid(self, valid: bool) -> None:
        self._valid = valid


class TestAlphaGraph:
    """AlphaGraph 组件图测试。"""

    def test_empty_graph_no_cycle(self) -> None:
        graph = AlphaGraph(strategy_id=StrategyId("TEST_STRATEGY"))
        assert not graph.has_cycle()

    def test_add_component(self) -> None:
        graph = AlphaGraph(strategy_id=StrategyId("TEST_STRATEGY"))
        entry = FakeComponent(AlphaComponentType.ENTRY, "entry_1")
        graph.add_component(entry)
        assert graph.topological_order() == ["entry_1"]

    def test_linear_pipeline_no_cycle(self) -> None:
        graph = AlphaGraph(strategy_id=StrategyId("TEST_STRATEGY"))
        e = FakeComponent(AlphaComponentType.ENTRY, "entry")
        f = FakeComponent(AlphaComponentType.FILTER, "filter")
        x = FakeComponent(AlphaComponentType.EXIT, "exit")

        graph.add_component(e)
        graph.add_component(f)
        graph.add_component(x)
        graph.connect("entry", "filter")
        graph.connect("filter", "exit")

        assert not graph.has_cycle()
        order = graph.topological_order()
        assert order == ["entry", "filter", "exit"]

    def test_cycle_detection(self) -> None:
        graph = AlphaGraph(strategy_id=StrategyId("TEST_STRATEGY"))
        e = FakeComponent(AlphaComponentType.ENTRY, "entry")
        f = FakeComponent(AlphaComponentType.FILTER, "filter")

        graph.add_component(e)
        graph.add_component(f)
        graph.connect("entry", "filter")
        graph.connect("filter", "entry")  # creates cycle

        assert graph.has_cycle()
        with pytest.raises(ValueError, match="cycle"):
            graph.topological_order()

    def test_duplicate_component_raises(self) -> None:
        graph = AlphaGraph(strategy_id=StrategyId("TEST_STRATEGY"))
        e1 = FakeComponent(AlphaComponentType.ENTRY, "entry")
        e2 = FakeComponent(AlphaComponentType.ENTRY, "entry")
        graph.add_component(e1)
        with pytest.raises(ValueError, match="already exists"):
            graph.add_component(e2)

    def test_connect_missing_component_raises(self) -> None:
        graph = AlphaGraph(strategy_id=StrategyId("TEST_STRATEGY"))
        e = FakeComponent(AlphaComponentType.ENTRY, "entry")
        graph.add_component(e)
        with pytest.raises(ValueError, match="not found"):
            graph.connect("entry", "nonexistent")
        with pytest.raises(ValueError, match="not found"):
            graph.connect("nonexistent", "entry")

    def test_complex_dag_topological_order(self) -> None:
        """验证复杂 DAG 的拓扑排序正确。"""
        graph = AlphaGraph(strategy_id=StrategyId("TEST_STRATEGY"))

        components = {
            name: FakeComponent(AlphaComponentType.ENTRY, name)
            for name in ["a", "b", "c", "d", "e", "f"]
        }
        for c in components.values():
            graph.add_component(c)

        # a -> b -> d -> f
        # a -> c -> e -> f
        # a -> c -> d
        graph.connect("a", "b")
        graph.connect("a", "c")
        graph.connect("b", "d")
        graph.connect("c", "d")
        graph.connect("c", "e")
        graph.connect("d", "f")
        graph.connect("e", "f")

        assert not graph.has_cycle()
        order = graph.topological_order()

        # Basic validity: all components present
        assert set(order) == set(components.keys())
        assert len(order) == 6

        # "a" must come before everything else that depends on it
        assert order.index("a") < order.index("b")
        assert order.index("a") < order.index("c")
        assert order.index("b") < order.index("d")
        assert order.index("c") < order.index("d")
        assert order.index("c") < order.index("e")
        assert order.index("d") < order.index("f")
        assert order.index("e") < order.index("f")

    def test_diamond_dag(self) -> None:
        """验证菱形 DAG。"""
        graph = AlphaGraph(strategy_id=StrategyId("TEST_STRATEGY"))
        a = FakeComponent(AlphaComponentType.ENTRY, "a")
        b = FakeComponent(AlphaComponentType.FILTER, "b")
        c = FakeComponent(AlphaComponentType.FILTER, "c")
        d = FakeComponent(AlphaComponentType.EXIT, "d")

        for comp in [a, b, c, d]:
            graph.add_component(comp)

        graph.connect("a", "b")
        graph.connect("a", "c")
        graph.connect("b", "d")
        graph.connect("c", "d")

        assert not graph.has_cycle()
        order = graph.topological_order()
        assert order.index("a") == 0
        assert order.index("d") == 3
        assert order.index("b") < order.index("d")
        assert order.index("c") < order.index("d")
