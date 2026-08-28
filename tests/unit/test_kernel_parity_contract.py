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


def test_parity_matches_when_backtest_and_paper_agree() -> None:
    """AC-STR-004: 同一 StrategyKernel 合同下 backtest/paper 一致 → MATCH。"""

    passed, result = parity_check(
        backtest_proposal=_proposal(OrderSide.BUY),
        paper_proposal=_proposal(OrderSide.BUY),
    )
    assert passed is True
    assert result.status is ParityStatus.MATCH
    assert result.discrepancies == []


def test_frozen_input_proposal_hash_is_reproducible() -> None:
    """AC-STR-005: 同一冻结输入的 proposal hash 可复现。"""

    kernel = StrategyKernel(mode="PAPER")
    proposal = _proposal(OrderSide.BUY, confidence=0.8)
    first = kernel.proposal_hash(proposal)
    second = kernel.proposal_hash(_proposal(OrderSide.BUY, confidence=0.8))
    assert first == second
    assert len(first) >= 16  # canonical truncated sha256 hex digest
    assert kernel.proposal_hash(_proposal(OrderSide.BUY, confidence=0.9)) != first


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

    assert result.get("proposal") is None
    assert result.get("kernel") == "typed_graph"
    assert result.get("mode") == "TESTNET"
    assert result.get("graph_hash") == "typed-graph"
    assert result.get("blocked_by") == "typed_graph_no_proposal"


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


def _exit_kernel_with_graph() -> StrategyKernel:
    """构造带一个 EXIT 节点的 typed graph 内核（M06-F01 契约测试）。"""
    from beidou_strategy.alpha.typed_graph import ExitNode, TypedAlphaGraph

    class _ExitProposal:
        """side=None 的退出提案（Exit 不得新增风险,side 必须为 None）。"""

        side = None
        strength = 0.6
        confidence = 0.7

        def hash(self) -> str:
            return "exit-proposal-hash-1"

    exit_proposal = _ExitProposal()

    async def _exit_fn(_context: dict):
        return exit_proposal

    graph = TypedAlphaGraph(strategy_id="exit-test")
    graph.add_node(ExitNode("exit-1", exit_fn=_exit_fn))
    kernel = StrategyKernel(mode="paper")
    kernel.set_typed_graph(graph)
    return kernel


@pytest.mark.asyncio
async def test_typed_kernel_propagates_exit_signals() -> None:
    """M06-F01 (P0-15): kernel 必须显式返回 exit_signals（旧实现死键）。"""
    kernel = _exit_kernel_with_graph()
    result = await kernel.evaluate({})
    assert result is not None
    assert result["kernel"] == "typed_graph"
    assert "exit_signals" in result
    assert len(result["exit_signals"]) == 1
    # 无入场 proposal 时 blocked_by 语义保持
    assert result["blocked_by"] == "typed_graph_no_proposal"


@pytest.mark.asyncio
async def test_typed_kernel_no_exit_outputs_empty_list() -> None:
    """无 EXIT 输出时 exit_signals 必须为空列表（消费方契约稳定）。"""
    from beidou_strategy.alpha.typed_graph import TypedAlphaGraph

    graph = TypedAlphaGraph(strategy_id="empty-test")
    kernel = StrategyKernel(mode="paper")
    kernel.set_typed_graph(graph)
    result = await kernel.evaluate({})
    assert result is not None
    assert result["exit_signals"] == []


def test_veto_short_circuit_preserves_exit_outputs() -> None:
    """M06-R2: 必选 Filter VETO 不得截断 EXIT 节点输出。"""
    import asyncio

    from beidou_strategy.alpha.contracts import FilterDecision, FilterResult
    from beidou_strategy.alpha.typed_graph import ExitNode, FilterNode, TypedAlphaGraph

    class _ExitProposal2:
        side = None

        def hash(self) -> str:
            return "exit-hash-2"

    async def _veto_fn(_inputs, _context):
        return FilterResult(
            decision=FilterDecision.VETO,
            confidence_multiplier=0.0,
            size_multiplier=0.0,
            component_id="mandatory-filter",
        )

    async def _exit_fn(_context):
        return _ExitProposal2()

    graph = TypedAlphaGraph(strategy_id="veto-exit-test")
    graph.add_node(FilterNode("f1", _veto_fn, is_mandatory=True))
    graph.add_node(ExitNode("exit-1", exit_fn=_exit_fn))
    graph.connect("f1", "exit-1")

    async def _run():
        detailed = await graph._execute_detailed({})
        outputs = detailed.get("node_outputs", {})
        exit_out = outputs.get("exit-1")
        assert exit_out is not None, "EXIT 节点在 VETO 短路后未执行"
        assert exit_out.data is not None

    asyncio.run(_run())
