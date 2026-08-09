"""Parity must be a complete, fail-closed execution contract."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from beidou_shared.types import InstrumentId, OrderSide, SchemaVersion, StrategyId, VenueId
from beidou_strategy.alpha.contracts import EntryProposal, FilterDecision, FilterResult, StrategyProposal
from beidou_strategy.kernel_parity import ParityStatus, StrategyKernel, StrategyKernelContract, parity_check


def _proposal(side: OrderSide = OrderSide.BUY, *, confidence: float = 0.8) -> StrategyProposal:
    return StrategyProposal(
        strategy_id=StrategyId("strategy-v1"),
        instrument_id=InstrumentId("BTCUSDT"),
        venue_id=VenueId("BINANCE"),
        side=side,
        strength=0.4,
        confidence=confidence,
        entry_proposals=[
            EntryProposal(
                strategy_id=StrategyId("entry-v1"),
                instrument_id=InstrumentId("BTCUSDT"),
                venue_id=VenueId("BINANCE"),
                side=side,
                strength=0.4,
                confidence=confidence,
                model_version=SchemaVersion("model-1"),
                metadata={"feature_snapshot": "snap-1"},
            )
        ],
        filter_results=[
            FilterResult(
                decision=FilterDecision.ACCEPT,
                confidence_multiplier=1.0,
                size_multiplier=1.0,
                component_id="filter-v1",
            )
        ],
        policy_version="policy-1",
        feature_snapshot_ref="snap-1",
    )


def test_parity_hash_binds_side_attribution_and_confidence() -> None:
    assert StrategyKernelContract.compute_proposal_hash(
        _proposal(OrderSide.BUY)
    ) != StrategyKernelContract.compute_proposal_hash(_proposal(OrderSide.SELL))
    assert StrategyKernelContract.compute_proposal_hash(
        _proposal(confidence=0.8)
    ) != StrategyKernelContract.compute_proposal_hash(_proposal(confidence=0.9))


def test_parity_requires_backtest_and_paper_and_rejects_not_run() -> None:
    passed, result = parity_check(backtest_proposal=_proposal())
    assert passed is False
    assert result.status is ParityStatus.NOT_RUN
    assert result.discrepancies == ["BACKTEST_AND_PAPER_REQUIRED"]


def test_parity_compares_testnet_when_supplied() -> None:
    passed, result = parity_check(
        backtest_proposal=_proposal(OrderSide.BUY),
        paper_proposal=_proposal(OrderSide.BUY),
        testnet_proposal=_proposal(OrderSide.SELL),
    )
    assert passed is False
    assert result.status is ParityStatus.DISCREPANCY
    assert "Testnet" in result.discrepancies[0]


def test_legacy_object_hash_is_not_only_direction_strength_confidence() -> None:
    base = SimpleNamespace(
        direction="LONG",
        strength=0.5,
        confidence=0.8,
        instrument_id="BTCUSDT",
        metadata={"policy_version": "v1"},
    )
    changed = SimpleNamespace(**{**vars(base), "metadata": {"policy_version": "v2"}})
    assert StrategyKernelContract.compute_proposal_hash(base) != StrategyKernelContract.compute_proposal_hash(changed)


@pytest.mark.asyncio
async def test_typed_kernel_never_falls_back_after_veto() -> None:
    class TypedGraph:
        async def execute(self, _context: dict) -> None:
            return None

        def compute_graph_hash(self) -> str:
            return "typed-graph"

    class LegacyGraph:
        async def generate(self, _context: dict) -> None:
            raise AssertionError("legacy graph must not run after typed veto")

    kernel = StrategyKernel(mode="TESTNET")
    kernel.set_typed_graph(TypedGraph())
    kernel.set_alpha_graph(LegacyGraph())

    result = await kernel.evaluate({})

    assert result == {
        "proposal": None,
        "kernel": "typed_graph",
        "mode": "TESTNET",
        "graph_hash": "typed-graph",
        "blocked_by": "typed_graph_no_proposal",
    }


def test_kernel_proposal_hash_uses_final_result() -> None:
    kernel = StrategyKernel(mode="PAPER")
    proposal = _proposal()
    assert kernel.proposal_hash(proposal) == StrategyKernelContract.compute_proposal_hash(proposal)


def test_retired_research_kernel_cannot_fabricate_walk_forward_folds() -> None:
    from beidou_research.kernel import DatasetManifest, PurgedWalkForward

    manifest = DatasetManifest(
        dataset_id="legacy",
        time_start="2026-01-01T00:00:00Z",
        time_end="2026-01-02T00:00:00Z",
        point_in_time_universe=["BTCUSDT"],
        delisted_samples=[],
        source_checksum="source",
        schema_version="1",
        code_hash="code",
    )
    with pytest.raises(RuntimeError, match="LEGACY_RESEARCH_KERNEL_DISABLED"):
        PurgedWalkForward().run(manifest, [{}])
