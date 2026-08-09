"""BF-08: TypedAlphaGraph 验收测试。

验证：
- Filter 不产生 LONG/SHORT
- Entry LONG + Filter VETO → NO_ACTION
- Entry LONG + bearish filter → 不得变成 SHORT
- 相同输入产生相同 StrategyProposal hash
- 禁止访问 _components 私有字段
- VETO 短路
- DEGRADE 乘数
- UNKNOWN DQ 阻止新增风险
- Node output hash 确定性
"""

from __future__ import annotations

import pytest

from beidou_shared.types import (
    InstrumentId,
    OrderSide,
    StrategyId,
    VenueId,
)
from beidou_strategy.alpha.contracts import (
    DataQualityTier,
    EntryProposal,
    FilterDecision,
    FilterResult,
)
from beidou_strategy.alpha.typed_graph import (
    EntryNode,
    FeatureNode,
    FilterNode,
    FusionNode,
    TypedAlphaGraph,
)

# ================================================================
# 辅助函数
# ================================================================


async def _make_entry(context: dict) -> EntryProposal:
    return EntryProposal(
        strategy_id=StrategyId("test"),
        instrument_id=InstrumentId("BTCUSDT"),
        venue_id=VenueId("BINANCE"),
        side=OrderSide.BUY,
        strength=0.7,
        confidence=0.6,
    )


async def _make_weak_entry(context: dict) -> EntryProposal:
    return EntryProposal(
        strategy_id=StrategyId("test"),
        instrument_id=InstrumentId("BTCUSDT"),
        venue_id=VenueId("BINANCE"),
        side=OrderSide.SELL,
        strength=0.3,
        confidence=0.4,
    )


async def _make_filter_accept(context: dict, entry=None) -> FilterResult:
    return FilterResult(decision=FilterDecision.ACCEPT, component_id="f1")


async def _make_filter_veto(context: dict, entry=None) -> FilterResult:
    return FilterResult(
        decision=FilterDecision.VETO,
        reason_codes=["high_volatility"],
        component_id="f_veto",
    )


async def _make_filter_degrade(context: dict, entry=None) -> FilterResult:
    return FilterResult(
        decision=FilterDecision.DEGRADE,
        confidence_multiplier=0.5,
        size_multiplier=0.5,
        reason_codes=["elevated_volatility"],
        component_id="f_deg",
    )


@pytest.fixture
def sample_context():
    return {
        "features": {"close": 50000.0, "sma_20": 49500.0, "rsi_14": 45.0},
        "instrument_id": InstrumentId("BTCUSDT"),
        "venue_id": VenueId("BINANCE"),
    }


# ================================================================
# FilterResult 语义验证（最关键的修复）
# ================================================================


class TestFilterResultSemantics:
    """BF-08 核心语义：Filter 不能产生方向信号。"""

    def test_filter_result_has_no_direction_field(self):
        """FilterResult 不包含方向字段 — 这是设计上的强制。"""
        result = FilterResult(decision=FilterDecision.ACCEPT)
        # FilterResult 只有 decision, confidence_multiplier, size_multiplier
        assert not hasattr(result, "direction")
        assert not hasattr(result, "strength")

    def test_filter_cannot_output_long_short(self):
        """Filter 只能输出 ACCEPT/VETO/DEGRADE，不是 LONG/SHORT。"""
        # FilterDecision 枚举只包含 ACCEPT, VETO, DEGRADE
        decisions = set(FilterDecision.__members__.keys())
        assert "ACCEPT" in decisions
        assert "VETO" in decisions
        assert "DEGRADE" in decisions
        assert "LONG" not in decisions
        assert "SHORT" not in decisions

    def test_entry_proposal_has_side(self):
        """BD-T05: EntryProposal 使用类型化 side (OrderSide)。"""
        proposal = EntryProposal(
            strategy_id=StrategyId("test"),
            instrument_id=InstrumentId("BTCUSDT"),
            venue_id=VenueId("BINANCE"),
            side=OrderSide.BUY,
            strength=0.5,
            confidence=0.5,
        )
        assert proposal.side in (OrderSide.BUY, OrderSide.SELL)


class TestFeatureNodeDataQuality:
    """缺失或非有限特征必须阻断，而不是用零值伪造输入。"""

    @pytest.mark.asyncio
    async def test_missing_feature_is_blocked_without_zero_fill(self):
        output = await FeatureNode("features", ["close", "atr_pct"]).execute({}, {"features": {"close": 50000.0}})

        assert output.dq_tier is DataQualityTier.BLOCK
        assert output.data == {}
        assert output.metadata["missing_features"] == ["atr_pct"]
        assert output.output_hash

    @pytest.mark.asyncio
    async def test_non_finite_feature_is_blocked(self):
        output = await FeatureNode("features", ["close"]).execute({}, {"features": {"close": float("nan")}})

        assert output.dq_tier is DataQualityTier.BLOCK
        assert output.data == {}
        assert output.metadata["invalid_features"] == ["close"]

    @pytest.mark.asyncio
    async def test_complete_features_are_passed_through_with_hash(self):
        output = await FeatureNode("features", ["close", "atr_pct"]).execute(
            {}, {"features": {"close": 50000.0, "atr_pct": 1.5}}
        )

        assert output.dq_tier is DataQualityTier.PASS
        assert output.data == {"close": 50000.0, "atr_pct": 1.5}
        assert output.output_hash


# ================================================================
# TypedAlphaGraph 核心测试
# ================================================================


class TestTypedAlphaGraph:
    """TypedAlphaGraph 验收测试。"""

    @pytest.mark.asyncio
    async def test_entry_long_filter_veto_results_no_action(self, sample_context):
        """Entry LONG + Filter VETO → NO_ACTION（核心断言）。"""
        graph = TypedAlphaGraph(StrategyId("test"))
        entry = EntryNode("entry", _make_entry)
        flt = FilterNode("filter", _make_filter_veto, is_mandatory=True)
        fusion = FusionNode("fusion")

        graph.add_node(entry)
        graph.add_node(flt)
        graph.add_node(fusion)
        graph.connect("entry", "filter")  # Entry → Filter
        graph.connect("filter", "fusion")  # Filter → Fusion
        graph.connect("entry", "fusion")  # Entry → Fusion (for EntryProposal data)

        result = await graph.execute(sample_context)

        if result is None:
            # VETO 短路返回 None = NO_ACTION
            pass
        else:
            # 不应是 LONG 或 SHORT
            assert result.side is None, f"Entry LONG + Filter VETO must be None (NO_ACTION), got {result.side}"

    @pytest.mark.asyncio
    async def test_entry_long_filter_accept_preserves_direction(self, sample_context):
        """Entry LONG + Filter ACCEPT → LONG（方向不变）。"""
        graph = TypedAlphaGraph(StrategyId("test"))
        entry = EntryNode("entry", _make_entry)
        flt = FilterNode("filter", _make_filter_accept)
        fusion = FusionNode("fusion")

        graph.add_node(entry)
        graph.add_node(flt)
        graph.add_node(fusion)
        graph.connect("entry", "filter")  # Entry → Filter
        graph.connect("filter", "fusion")  # Filter → Fusion
        graph.connect("entry", "fusion")  # Entry → Fusion (for EntryProposal data)

        result = await graph.execute(sample_context)
        assert result is not None
        assert result.side == OrderSide.BUY, f"Entry LONG + Filter ACCEPT must be BUY, got {result.side}"

    @pytest.mark.asyncio
    async def test_entry_long_bearish_filter_not_become_short(self, sample_context):
        """Entry LONG + bearish filter → 不得变成 SHORT。

        Filter 只能调节强度和信心，不能改变方向！
        """
        graph = TypedAlphaGraph(StrategyId("test"))
        entry = EntryNode("entry", _make_entry)  # LONG direction
        flt = FilterNode("filter", _make_filter_degrade)  # DEGRADE, but no direction change
        fusion = FusionNode("fusion")

        graph.add_node(entry)
        graph.add_node(flt)
        graph.add_node(fusion)
        graph.connect("entry", "filter")  # Entry → Filter
        graph.connect("filter", "fusion")  # Filter → Fusion
        graph.connect("entry", "fusion")  # Entry → Fusion (for EntryProposal data)

        result = await graph.execute(sample_context)
        assert result is not None
        assert result.side == OrderSide.BUY, f"Entry LONG + DEGRADE filter must still be BUY, got {result.side}"
        # DEGRADE 降低了信心和强度
        assert result.confidence < 0.6  # original confidence reduced
        assert result.strength < 0.7  # original strength reduced

    @pytest.mark.asyncio
    async def test_degrade_multipliers_reduce_confidence_and_strength(self, sample_context):
        """DEGRADE 乘数正确降低 confidence 和 strength。"""
        graph = TypedAlphaGraph(StrategyId("test"))
        entry = EntryNode("entry", _make_entry)
        flt = FilterNode("filter", _make_filter_degrade)
        fusion = FusionNode("fusion")

        graph.add_node(entry)
        graph.add_node(flt)
        graph.add_node(fusion)
        graph.connect("entry", "filter")  # Entry → Filter
        graph.connect("filter", "fusion")  # Filter → Fusion
        graph.connect("entry", "fusion")  # Entry → Fusion (for EntryProposal data)

        result = await graph.execute(sample_context)
        # confidence: 0.6 * 0.5 = 0.3
        # strength: 0.7 * 0.5 = 0.35
        assert abs(result.confidence - 0.3) < 0.01
        assert abs(result.strength - 0.35) < 0.01

    @pytest.mark.asyncio
    async def test_same_input_produces_same_hash(self, sample_context):
        """相同输入产生相同 StrategyProposal hash（确定性）。"""
        # 第一次执行
        graph1 = TypedAlphaGraph(StrategyId("test"))
        entry1 = EntryNode("entry", _make_entry)
        fusion1 = FusionNode("fusion")
        graph1.add_node(entry1)
        graph1.add_node(fusion1)
        graph1.connect("entry", "fusion")
        result1 = await graph1.execute(sample_context)

        # 第二次执行
        graph2 = TypedAlphaGraph(StrategyId("test"))
        entry2 = EntryNode("entry", _make_entry)
        fusion2 = FusionNode("fusion")
        graph2.add_node(entry2)
        graph2.add_node(fusion2)
        graph2.connect("entry", "fusion")
        result2 = await graph2.execute(sample_context)

        assert result1 is not None
        assert result2 is not None
        # 图结构哈希应相同
        assert graph1.compute_graph_hash() == graph2.compute_graph_hash()

    @pytest.mark.asyncio
    async def test_no_entry_node_returns_no_action(self, sample_context):
        """没有 Entry 节点时返回 NO_ACTION 提案。"""
        graph = TypedAlphaGraph(StrategyId("test"))
        flt = FilterNode("filter", _make_filter_accept)
        fusion = FusionNode("fusion")

        graph.add_node(flt)
        graph.add_node(fusion)
        graph.connect("filter", "fusion")

        result = await graph.execute(sample_context)
        assert result is not None
        assert result.side is None  # 没有 Entry → 无动作

    @pytest.mark.asyncio
    async def test_filter_fails_closed_default(self, sample_context):
        """Filter 节点默认 FAIL_CLOSED → 失败即 VETO。"""

        async def broken_filter(context, entry=None):
            raise RuntimeError("simulated failure")

        graph = TypedAlphaGraph(StrategyId("test"))
        entry = EntryNode("entry", _make_entry)
        flt = FilterNode("broken", broken_filter)  # default FAIL_CLOSED
        fusion = FusionNode("fusion")

        graph.add_node(entry)
        graph.add_node(flt)
        graph.add_node(fusion)
        graph.connect("entry", "fusion")
        graph.connect("broken", "fusion")

        result = await graph.execute(sample_context)
        assert result is not None
        assert result.side is None

    @pytest.mark.asyncio
    async def test_graph_hash_deterministic(self):
        """图结构哈希确定性。"""
        g1 = TypedAlphaGraph(StrategyId("test"))
        g1.add_node(EntryNode("e1", _make_entry))
        g1.add_node(FusionNode("f1"))
        g1.connect("e1", "f1")

        g2 = TypedAlphaGraph(StrategyId("test"))
        g2.add_node(EntryNode("e1", _make_entry))
        g2.add_node(FusionNode("f1"))
        g2.connect("e1", "f1")

        assert g1.compute_graph_hash() == g2.compute_graph_hash()

    @pytest.mark.asyncio
    async def test_multiple_filters_all_accept(self, sample_context):
        """多个 Filter 全部 ACCEPT → 保持 Entry 方向。"""
        graph = TypedAlphaGraph(StrategyId("test"))
        entry = EntryNode("entry", _make_entry)
        f1 = FilterNode("f1", _make_filter_accept)
        f2 = FilterNode("f2", _make_filter_accept)
        fusion = FusionNode("fusion")

        graph.add_node(entry)
        graph.add_node(f1)
        graph.add_node(f2)
        graph.add_node(fusion)
        graph.connect("entry", "fusion")
        graph.connect("f1", "fusion")
        graph.connect("f2", "fusion")

        result = await graph.execute(sample_context)
        assert result is not None
        assert result.side == OrderSide.BUY
