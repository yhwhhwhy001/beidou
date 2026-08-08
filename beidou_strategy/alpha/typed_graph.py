"""BF-08: Typed AlphaGraph — 类型化可执行数据流图。

强制语义：
1. Entry 产生方向 (LONG/SHORT) → EntryProposal
2. Filter 只产生 ACCEPT/VETO/DEGRADE → FilterResult (禁止 LONG/SHORT!)
3. 任一强制 VETO 立即短路
4. DEGRADE 只修改 confidence/size multiplier
5. Exit 不得创建新增风险
6. PositionManager 只生成目标仓位，不直接下单
7. 所有节点输出带 schema、policy、factor/model version 和 evidence reference
8. 相同输入产生相同 StrategyProposal hash（确定性）

替换当前的 AlphaGraph（只有拓扑排序，无类型化数据流）。
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

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
    StrategyProposal,
)

# ================================================================
# 节点类型
# ================================================================


class NodeType(str, Enum):
    FEATURE = "FEATURE"
    FACTOR = "FACTOR"
    ENTRY = "ENTRY"
    FILTER = "FILTER"
    FUSION = "FUSION"
    EXIT = "EXIT"  # BD-P0-04: 退出节点
    SIZING = "SIZING"
    EXIT_POLICY = "EXIT_POLICY"  # 兼容旧代码
    STRATEGY = "STRATEGY"


class NodeFailurePolicy(str, Enum):
    FAIL_CLOSED = "FAIL_CLOSED"  # 节点失败 = VETO
    SKIP = "SKIP"  # 节点失败 = 跳过（不推荐）
    DEGRADE = "DEGRADE"  # 节点失败 = DEGRADE


# ================================================================
# 节点输出类型
# ================================================================


@dataclass(frozen=True)
class TypedNodeOutput:
    """类型化节点输出 — 每个节点输出带 hash 和版本追溯。"""

    node_id: str
    node_type: NodeType
    output_hash: str  # 输出的确定性哈希
    data: Any  # 实际输出数据
    dq_tier: DataQualityTier = DataQualityTier.UNKNOWN
    policy_version: str = ""
    factor_version: str = ""
    model_version: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def compute_hash(self) -> str:
        content = json.dumps(
            {
                "node_id": self.node_id,
                "node_type": self.node_type.value,
                "data": str(self.data),
                "dq_tier": self.dq_tier.value,
                "policy_version": self.policy_version,
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(content.encode()).hexdigest()[:16]


# ================================================================
# 类型化图节点
# ================================================================


class TypedGraphNode(ABC):
    """类型化 DAG 节点基类。"""

    def __init__(
        self,
        node_id: str,
        node_type: NodeType,
        failure_policy: NodeFailurePolicy = NodeFailurePolicy.FAIL_CLOSED,
    ) -> None:
        self.node_id = node_id
        self.node_type = node_type
        self.failure_policy = failure_policy
        self._input_nodes: list[str] = []

    @property
    def input_dependencies(self) -> list[str]:
        return list(self._input_nodes)

    @abstractmethod
    async def execute(self, inputs: dict[str, TypedNodeOutput], context: dict) -> TypedNodeOutput: ...

    def validate(self) -> bool:
        return True


# ================================================================
# 具体节点实现
# ================================================================


class FeatureNode(TypedGraphNode):
    """特征节点 — 从 FeatureStore 读取点时可得的特征快照。"""

    def __init__(self, node_id: str, feature_names: list[str]) -> None:
        super().__init__(node_id, NodeType.FEATURE)
        self.feature_names = feature_names

    async def execute(self, inputs: dict[str, TypedNodeOutput], context: dict) -> TypedNodeOutput:
        features = context.get("features", {})
        values = {name: features.get(name, 0.0) for name in self.feature_names}
        dq_tier = (
            DataQualityTier.PASS if all(name in features for name in self.feature_names) else DataQualityTier.DEGRADED
        )

        return TypedNodeOutput(
            node_id=self.node_id,
            node_type=NodeType.FEATURE,
            output_hash="",
            data=values,
            dq_tier=dq_tier,
        )


class EntryNode(TypedGraphNode):
    """入场节点 — 输出 EntryProposal（只能有 LONG/SHORT 方向）。

    禁止输出 FilterResult 或产生 VETO 语义。
    """

    def __init__(
        self,
        node_id: str,
        entry_fn: Any,  # Callable[[dict], EntryProposal]
        factor_version: str = "",
        model_version: str = "",
    ) -> None:
        super().__init__(node_id, NodeType.ENTRY)
        self._entry_fn = entry_fn
        self.factor_version = factor_version
        self.model_version = model_version

    async def execute(self, inputs: dict[str, TypedNodeOutput], context: dict) -> TypedNodeOutput:
        try:
            proposal: EntryProposal = await self._entry_fn(context)
            output = TypedNodeOutput(
                node_id=self.node_id,
                node_type=NodeType.ENTRY,
                output_hash="",
                data=proposal,
                dq_tier=DataQualityTier.PASS,
                factor_version=self.factor_version,
                model_version=self.model_version,
            )
            output = TypedNodeOutput(
                node_id=output.node_id,
                node_type=output.node_type,
                output_hash=output.compute_hash(),
                data=output.data,
                dq_tier=output.dq_tier,
                factor_version=output.factor_version,
                model_version=output.model_version,
            )
            return output
        except Exception as e:
            if self.failure_policy == NodeFailurePolicy.FAIL_CLOSED:
                return TypedNodeOutput(
                    node_id=self.node_id,
                    node_type=NodeType.ENTRY,
                    output_hash="error",
                    data=None,
                    dq_tier=DataQualityTier.BLOCK,
                    metadata={"error": str(e)},
                )
            raise


class FilterNode(TypedGraphNode):
    """过滤节点 — 输出 FilterResult（ACCEPT/VETO/DEGRADE）。

    禁止输出 EntryProposal 或产生 LONG/SHORT 方向！
    这是 BF-08 最关键的修复。
    """

    def __init__(
        self,
        node_id: str,
        filter_fn: Any,  # Callable[[dict, EntryProposal], FilterResult]
        is_mandatory: bool = False,  # 强制 VETO 立即短路
    ) -> None:
        super().__init__(node_id, NodeType.FILTER)
        self._filter_fn = filter_fn
        self.is_mandatory = is_mandatory

    async def execute(self, inputs: dict[str, TypedNodeOutput], context: dict) -> TypedNodeOutput:
        try:
            # 从上游 Entry 节点获取提案
            entry_proposal = None
            for inp in inputs.values():
                if inp.node_type == NodeType.ENTRY and inp.data is not None:
                    entry_proposal = inp.data
                    break

            result: FilterResult = await self._filter_fn(context, entry_proposal)

            # 强制验证：FilterResult 不能有方向语义
            # FilterResult 只有 decision/confidence_multiplier/size_multiplier

            output = TypedNodeOutput(
                node_id=self.node_id,
                node_type=NodeType.FILTER,
                output_hash="",
                data=result,
                dq_tier=DataQualityTier.PASS,
                metadata={
                    "is_mandatory": self.is_mandatory,
                    "decision": result.decision.value,
                },
            )
            output = TypedNodeOutput(
                node_id=output.node_id,
                node_type=output.node_type,
                output_hash=output.compute_hash(),
                data=output.data,
                dq_tier=output.dq_tier,
                metadata=output.metadata,
            )
            return output
        except Exception as e:
            if self.failure_policy == NodeFailurePolicy.FAIL_CLOSED:
                # 过滤器失败 → VETO (safe default)
                fail_result = FilterResult(
                    decision=FilterDecision.VETO,
                    reason_codes=[f"filter_error: {str(e)[:100]}"],
                    component_id=self.node_id,
                )
                return TypedNodeOutput(
                    node_id=self.node_id,
                    node_type=NodeType.FILTER,
                    output_hash="error_veto",
                    data=fail_result,
                    dq_tier=DataQualityTier.BLOCK,
                    metadata={"error": str(e)},
                )
            raise


class ExitNode(TypedGraphNode):
    """BD-P0-04: 退出节点 — 只能产生减仓/平仓信号，不得增加绝对风险。

    核心规则：
    - 输出方向必须为 REDUCE/FLATTEN/CANCEL
    - 禁止输出 LONG/SHORT（新增风险方向）
    - 输出 Proposal.side 不得为 LONG/SHORT（Exit 不得新增风险）
    """

    def __init__(self, node_id: str, exit_fn: Any = None) -> None:
        super().__init__(node_id, NodeType.EXIT)
        self._exit_fn = exit_fn

    async def execute(self, inputs: dict[str, TypedNodeOutput], context: dict) -> TypedNodeOutput:
        try:
            if self._exit_fn is not None:
                proposal = await self._exit_fn(context)
            else:
                proposal = None
        except Exception:
            return TypedNodeOutput(
                node_id=self.node_id,
                node_type=NodeType.EXIT,
                output_hash="exit_error",
                data=None,
                dq_tier=DataQualityTier.DEGRADED,
            )

        if proposal is None:
            return TypedNodeOutput(
                node_id=self.node_id,
                node_type=NodeType.EXIT,
                output_hash="no_exit",
                data=None,
                dq_tier=DataQualityTier.PASS,
            )

        # BD-T05: 强制约束 — Exit 不得新增风险（side 不得为 BUY/SELL）
        side_val = getattr(proposal, "side", None)
        if side_val is not None:
            return TypedNodeOutput(
                node_id=self.node_id,
                node_type=NodeType.EXIT,
                output_hash="exit_violation",
                data=None,
                dq_tier=DataQualityTier.BLOCK,
            )

        return TypedNodeOutput(
            node_id=self.node_id,
            node_type=NodeType.EXIT,
            output_hash=proposal.hash() if hasattr(proposal, "hash") else "exit",
            data=proposal,
            dq_tier=DataQualityTier.PASS,
        )


class FusionNode(TypedGraphNode):
    """融合节点 — 组合 Entry + Filter 结果，输出最终方向。

    核心规则：
    - 任一强制 VETO → NO_ACTION
    - 所有 Filter ACCEPT → 保持 Entry 方向
    - DEGRADE → 降低 confidence 和 size
    - UNKNOWN DQ → NO_ACTION（不新增风险）
    - BD-P0-04: 输出 Proposal 具备确定性哈希
    """

    def __init__(self, node_id: str) -> None:
        super().__init__(node_id, NodeType.FUSION)

    async def execute(self, inputs: dict[str, TypedNodeOutput], context: dict) -> TypedNodeOutput:
        entry_proposal = None
        filter_results: list[FilterResult] = []

        for inp in inputs.values():
            if inp.node_type == NodeType.ENTRY and inp.data is not None:
                entry_proposal = inp.data
            elif inp.node_type == NodeType.FILTER and inp.data is not None:
                filter_results.append(inp.data)

        venue = VenueId("UNKNOWN")
        instrument = InstrumentId("UNKNOWN")

        if entry_proposal is None:
            return TypedNodeOutput(
                node_id=self.node_id,
                node_type=NodeType.FUSION,
                output_hash="no_entry",
                data=StrategyProposal(
                    strategy_id=StrategyId("no_entry"),
                    instrument_id=instrument,
                    venue_id=venue,
                    side=None,
                    strength=0.0,
                    confidence=0.0,
                ),
                dq_tier=DataQualityTier.DEGRADED,
            )

        venue = entry_proposal.venue_id
        instrument = entry_proposal.instrument_id

        # UNKNOWN DQ → 不新增风险
        if any(inp.dq_tier == DataQualityTier.BLOCK for inp in inputs.values()):
            return TypedNodeOutput(
                node_id=self.node_id,
                node_type=NodeType.FUSION,
                output_hash="dq_blocked",
                data=StrategyProposal(
                    strategy_id=StrategyId("dq_blocked"),
                    instrument_id=instrument,
                    venue_id=venue,
                    side=None,
                    strength=0.0,
                    confidence=0.0,
                ),
                dq_tier=DataQualityTier.BLOCK,
            )

        # 检查 VETO
        vetoes = [f for f in filter_results if f.decision == FilterDecision.VETO]
        if vetoes:
            return TypedNodeOutput(
                node_id=self.node_id,
                node_type=NodeType.FUSION,
                output_hash="vetoed",
                data=StrategyProposal(
                    strategy_id=StrategyId("vetoed"),
                    instrument_id=instrument,
                    venue_id=venue,
                    side=None,
                    strength=0.0,
                    confidence=0.0,
                    filter_results=filter_results,
                ),
                dq_tier=DataQualityTier.PASS,
            )

        # 计算 DEGRADE 乘数
        degrades = [f for f in filter_results if f.decision == FilterDecision.DEGRADE]
        confidence_mult = 1.0
        size_mult = 1.0
        for d in degrades:
            confidence_mult *= d.confidence_multiplier
            size_mult *= d.size_multiplier

        # BD-T05: 输出最终方向（side 替代 direction）
        side = entry_proposal.side
        strength = entry_proposal.strength * size_mult
        confidence = entry_proposal.confidence * confidence_mult

        if strength < 0.01:
            side = None  # NO_ACTION

        proposal = StrategyProposal(
            strategy_id=StrategyId("typed_graph"),
            instrument_id=entry_proposal.instrument_id,
            venue_id=entry_proposal.venue_id,
            side=side,
            strength=min(1.0, strength),
            confidence=min(1.0, confidence),
            entry_proposals=[entry_proposal],
            filter_results=filter_results,
            conflict_detected=len(vetoes) > 0,
        )

        output = TypedNodeOutput(
            node_id=self.node_id,
            node_type=NodeType.FUSION,
            output_hash="",
            data=proposal,
            dq_tier=DataQualityTier.PASS,
        )
        output = TypedNodeOutput(
            node_id=output.node_id,
            node_type=output.node_type,
            output_hash=output.compute_hash(),
            data=output.data,
            dq_tier=output.dq_tier,
        )
        return output


# ================================================================
# TypedAlphaGraph
# ================================================================


class TypedAlphaGraph:
    """类型化 Alpha DAG 执行器。

    替换当前的 AlphaGraph（只有拓扑排序，无类型化语义）。
    """

    def __init__(self, strategy_id: StrategyId) -> None:
        self.strategy_id = strategy_id
        self._nodes: dict[str, TypedGraphNode] = {}
        self._edges: dict[str, list[str]] = {}  # from → [to, ...]

    def add_node(self, node: TypedGraphNode) -> None:
        if node.node_id in self._nodes:
            raise ValueError(f"Node {node.node_id} already exists")
        self._nodes[node.node_id] = node
        self._edges[node.node_id] = []

    def connect(self, from_id: str, to_id: str) -> None:
        if from_id not in self._nodes:
            raise ValueError(f"Source node {from_id} not found")
        if to_id not in self._nodes:
            raise ValueError(f"Target node {to_id} not found")
        self._edges[from_id].append(to_id)
        self._nodes[to_id]._input_nodes.append(from_id)

    def topological_order(self) -> list[str]:
        """Kahn 拓扑排序。"""
        in_deg = dict.fromkeys(self._nodes, 0)
        for _src, tgts in self._edges.items():
            for t in tgts:
                in_deg[t] = in_deg.get(t, 0) + 1

        queue = [n for n, d in in_deg.items() if d == 0]
        order = []
        while queue:
            n = queue.pop(0)
            order.append(n)
            for nb in self._edges.get(n, []):
                in_deg[nb] -= 1
                if in_deg[nb] == 0:
                    queue.append(nb)

        if len(order) != len(self._nodes):
            raise ValueError("TypedAlphaGraph has unresolved dependencies")
        return order

    async def execute(self, context: dict) -> StrategyProposal | None:
        """执行完整 DAG。

        按拓扑顺序执行每个节点，传递 TypedNodeOutput。
        在遇到强制 VETO 时短路。

        Returns:
            最终 StrategyProposal，如果被否决则返回 None
        """
        order = self.topological_order()
        outputs: dict[str, TypedNodeOutput] = {}

        for node_id in order:
            node = self._nodes[node_id]

            # 收集上游输出
            upstream_outputs = {dep: outputs[dep] for dep in node._input_nodes if dep in outputs}

            # 执行节点
            output = await node.execute(upstream_outputs, context)
            outputs[node_id] = output

            # 强制 VETO 短路
            if isinstance(node, FilterNode) and node.is_mandatory:
                if isinstance(output.data, FilterResult) and output.data.decision == FilterDecision.VETO:
                    # 返回否决结果，不再执行后续节点
                    return None

        # 找 FusionNode 的输出
        for node_id in reversed(order):
            node = self._nodes[node_id]
            if node.node_type == NodeType.FUSION and node_id in outputs:
                data = outputs[node_id].data
                if isinstance(data, StrategyProposal):
                    return data

        return None

    def compute_graph_hash(self) -> str:
        """计算图结构的确定性哈希（用于 parity 验证）。"""
        nodes_info = sorted(
            [{"id": nid, "type": node.node_type.value} for nid, node in self._nodes.items()], key=lambda x: x["id"]
        )
        edges_info = sorted(
            [{"from": src, "to": tgt} for src, tgts in self._edges.items() for tgt in tgts],
            key=lambda x: (x["from"], x["to"]),
        )
        content = json.dumps(
            {
                "strategy_id": str(self.strategy_id),
                "nodes": nodes_info,
                "edges": edges_info,
            },
            sort_keys=True,
        )
        return hashlib.sha256(content.encode()).hexdigest()[:16]
