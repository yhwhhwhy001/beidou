"""Coverage gap tests for beidou_strategy.kernel_parity.

Targets ``StrategyKernel.active_component_manifest`` branches that the existing
parity contract suite does not exercise: the no-typed-graph early return and
the skip of structural nodes absent from the ``_nodes`` registry.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

from beidou_strategy.kernel_parity import StrategyKernel


def test_active_component_manifest_empty_without_typed_graph() -> None:
    assert StrategyKernel().active_component_manifest() == []


def test_active_component_manifest_skips_missing_nodes() -> None:
    node = SimpleNamespace(node_type=SimpleNamespace(value="ENTRY"), factor_version="v1")

    class _Graph:
        _nodes: ClassVar[dict] = {"a": node}

        def topological_order(self) -> list[str]:
            return ["a", "ghost"]

        def compute_graph_hash(self) -> str:
            return "gh"

    kernel = StrategyKernel()
    kernel.set_typed_graph(_Graph())
    manifest = kernel.active_component_manifest()
    assert manifest == [
        {
            "component_id": "a",
            "component_type": "ENTRY",
            "version": "v1",
            "graph_hash": "gh",
        }
    ]
