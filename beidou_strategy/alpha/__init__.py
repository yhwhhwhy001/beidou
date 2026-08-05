"""复合 Alpha SDK 与策略组件图。Entry/Filter/Exit/PositionManager 可组合 DAG。"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from beidou_shared.types import InstrumentId, ModelId, SchemaVersion, StrategyId, VenueId

class AlphaComponentType(str, Enum):
    ENTRY = "ENTRY"; FILTER = "FILTER"; EXIT = "EXIT"; POSITION_MANAGER = "POSITION_MANAGER"

class SignalDirection(str, Enum):
    LONG = "LONG"; SHORT = "SHORT"; FLAT = "FLAT"; NO_ACTION = "NO_ACTION"

@dataclass(frozen=True, slots=True)
class AlphaSignal:
    strategy_id: StrategyId
    component_type: AlphaComponentType
    direction: SignalDirection
    strength: float
    confidence: float
    instrument_id: InstrumentId
    venue_id: VenueId
    model_version: SchemaVersion
    model_id: ModelId | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

class AlphaComponent(ABC):
    def __init__(self, component_type: AlphaComponentType, component_id: str, version: SchemaVersion) -> None:
        self.component_type = component_type
        self.component_id = component_id
        self.version = version
        self._input_dependencies: list[str] = []

    @property
    def input_dependencies(self) -> list[str]:
        return self._input_dependencies

    @abstractmethod
    async def generate(self, context: dict[str, Any]) -> AlphaSignal: ...
    @abstractmethod
    def validate(self) -> bool: ...

class AlphaGraph:
    def __init__(self, strategy_id: StrategyId) -> None:
        self.strategy_id = strategy_id
        self._components: dict[str, AlphaComponent] = {}
        self._edges: dict[str, list[str]] = {}

    def add_component(self, component: AlphaComponent) -> None:
        if component.component_id in self._components:
            raise ValueError(f"Component {component.component_id} already exists")
        self._components[component.component_id] = component
        self._edges[component.component_id] = []

    def connect(self, from_id: str, to_id: str) -> None:
        if from_id not in self._components:
            raise ValueError(f"Source component {from_id} not found")
        if to_id not in self._components:
            raise ValueError(f"Target component {to_id} not found")
        self._edges[from_id].append(to_id)

    def has_cycle(self) -> bool:
        visited: set[str] = set()
        stack: set[str] = set()
        def dfs(n: str) -> bool:
            visited.add(n); stack.add(n)
            for nb in self._edges.get(n, []):
                if nb not in visited:
                    if dfs(nb): return True
                elif nb in stack: return True
            stack.discard(n); return False
        for node in self._components:
            if node not in visited and dfs(node): return True
        return False

    def topological_order(self) -> list[str]:
        if self.has_cycle():
            raise ValueError("AlphaGraph contains a cycle")
        in_deg = {n: 0 for n in self._components}
        for src, tgts in self._edges.items():
            for t in tgts: in_deg[t] = in_deg.get(t, 0) + 1
        queue = [n for n, d in in_deg.items() if d == 0]
        order: list[str] = []
        while queue:
            n = queue.pop(0); order.append(n)
            for nb in self._edges.get(n, []):
                in_deg[nb] -= 1
                if in_deg[nb] == 0: queue.append(nb)
        if len(order) != len(self._components):
            raise ValueError("AlphaGraph has unresolved dependencies")
        return order
