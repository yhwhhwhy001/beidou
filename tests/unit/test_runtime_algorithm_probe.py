"""The runtime algorithm probe must use the single typed StrategyKernel path."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from beidou_launcher.runtime import build_decision_trace, run_read_only_algorithm_probe
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
    assert result["decision_trace"]["context_hash"]
    assert set(result["components"]) == {"entry", "filter", "fusion"}
    assert result["decision_trace"]["market_data_hash"]
    assert result["decision_trace"]["state_hash"]
    assert result["v3_shadow_status"] == "NOT_VERIFIABLE"
    assert result["v3_shadow"]["trace_hash"]
    assert result["decision_trace"]["benchmark_snapshot_hash"]
    assert result["decision_trace"]["ensemble_forecast_hash"]
    assert result["decision_trace"]["exposure_target_hash"]
    assert result["decision_trace"]["attribution_record_id"] is None


@pytest.mark.asyncio
async def test_kernel_context_hash_changes_when_market_state_changes() -> None:
    class TypedGraph:
        async def execute(self, _context: dict) -> None:
            return None

        def compute_graph_hash(self) -> str:
            return "typed-graph"

    kernel = StrategyKernel(mode="PAPER")
    kernel.set_typed_graph(TypedGraph())
    first = await kernel.evaluate(
        {"instrument_id": "ETHUSDT", "features": {"close": 100.0}, "state": {"direction": "RANGING"}}
    )
    second = await kernel.evaluate(
        {"instrument_id": "ETHUSDT", "features": {"close": 100.0}, "state": {"direction": "TRENDING_UP"}}
    )

    assert first is not None and second is not None
    assert first["context_hash"] != second["context_hash"]


def test_build_decision_trace_is_explicitly_not_verifiable_without_pnl() -> None:
    trace = build_decision_trace(
        {
            "symbol": "BTCUSDT",
            "features": {"close": 100.0},
            "state": {"direction": "RANGING"},
            "graph_hash": "graph",
            "proposal_hash": "proposal",
        }
    )

    assert trace["decision_id"]
    assert trace["proposal_hash"] == "proposal"
    assert trace["context_hash"] == ""
    assert trace["status"] == "NOT_VERIFIABLE"
    assert trace["attribution_record_id"] is None
