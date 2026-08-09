"""The runtime algorithm probe must use the single typed StrategyKernel path."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from beidou_launcher.runtime import run_read_only_algorithm_probe
from beidou_strategy.kernel_parity import StrategyKernel


@pytest.mark.asyncio
async def test_probe_does_not_execute_legacy_graph_when_typed_graph_vetoes() -> None:
    class Feed:
        async def async_get_kline_features(self, _symbol: str) -> dict[str, float]:
            return {"close": 100.0, "sma_20": 100.0}

        async def async_update_features(self, _symbol: str) -> dict[str, float]:
            return {"price": 100.0, "spread_bps": 1.0}

    class TypedGraph:
        async def execute(self, _context: dict) -> None:
            return None

        def compute_graph_hash(self) -> str:
            return "typed-graph"

        def topological_order(self) -> list[str]:
            return ["entry", "filter", "fusion"]

    class LegacyGraph:
        async def generate(self, _context: dict) -> None:
            raise AssertionError("probe must not touch legacy graph")

    kernel = StrategyKernel(mode="TESTNET")
    kernel.set_typed_graph(TypedGraph())
    kernel.set_alpha_graph(LegacyGraph())
    engine = SimpleNamespace(
        _feed=Feed(),
        _strategy_kernel=kernel,
        _estimate_market_state=lambda _features: {"direction": "UNKNOWN", "stress": "NORMAL"},
    )

    result = await run_read_only_algorithm_probe(engine, ["BTCUSDT"])

    assert result["ok"] is True
    assert result["kernel"] == "typed_graph"
    assert result["blocked_by"] == "typed_graph_no_proposal"
    assert result["proposal_hash"]
    assert result["graph_hash"] == "typed-graph"
    assert set(result["components"]) == {"entry", "filter", "fusion"}
