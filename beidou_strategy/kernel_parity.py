"""BF-10: 统一 StrategyKernel 与 Parity 合同。

Backtest、Paper、Testnet 必须调用同一 StrategyKernel。
仅允许替换: MarketIO, Clock, ExchangeIO, Persistence adapter。
Parity 必须比较最终 StrategyProposal、归因、风险快照和目标仓位。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from typing import Any


class KernelMode(str, Enum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    TESTNET = "TESTNET"


class ParityStatus(str, Enum):
    MATCH = "MATCH"
    DISCREPANCY = "DISCREPANCY"
    NOT_RUN = "NOT_RUN"


@dataclass
class ParityResult:
    """策略内核一致性验证结果。"""

    backtest_hash: str = ""
    paper_hash: str = ""
    testnet_hash: str = ""
    status: ParityStatus = ParityStatus.NOT_RUN
    discrepancies: list[str] = field(default_factory=list)
    allowed_discrepancies: list[str] = field(default_factory=list)  # 仅成交模拟差异
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class StrategyKernelContract:
    """策略内核一致性合同。

    不变量：
    1. 同一冻结输入的 Backtest 与 Paper StrategyProposal hash 一致
    2. 仅允许成交模拟导致的执行差异
    3. 任何 factor/feature/strategy hash 不一致触发失败
    4. Research Lab 不启动 Autopilot
    """

    @staticmethod
    def compute_proposal_hash(proposal: Any) -> str:
        """计算包含策略归因和风险相关字段的确定性哈希。"""
        if proposal is None:
            return ""
        try:
            canonical = StrategyKernelContract._canonicalize(proposal)
            content = json.dumps(canonical, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError, OverflowError):
            # A non-finite or otherwise non-serializable proposal cannot be
            # used as an execution/parity identity.
            return ""
        # BD-FIX: NO_ACTION/VETO 提案也可能因 _canonicalize 产生的 dict
        # 在 json.dumps 后为空对象 "{}" 导致 hash 计算为空。对有效 JSON
        # 但空内容的提案，基于原始 proposal 的字符串表示计算 fallback hash。
        if not content or content == "{}":
            try:
                content = json.dumps(
                    {"_fallback": str(proposal)},
                    sort_keys=True, separators=(",", ":"), allow_nan=False,
                )
            except (TypeError, ValueError, OverflowError):
                return ""
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    @staticmethod
    def _canonicalize(value: Any) -> Any:
        """Convert proposals to stable JSON without wall-clock fields."""

        if isinstance(value, Enum):
            return value.value
        if is_dataclass(value):
            return {
                item.name: StrategyKernelContract._canonicalize(getattr(value, item.name))
                for item in fields(value)
                if item.name not in {"timestamp", "ingest_time"}
            }
        if isinstance(value, dict):
            return {
                str(key): StrategyKernelContract._canonicalize(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(value, (list, tuple)):
            return [StrategyKernelContract._canonicalize(item) for item in value]
        if isinstance(value, float):
            if not isfinite(value):
                raise ValueError("non-finite proposal value")
            return value
        if isinstance(value, (str, int, bool)) or value is None:
            return value
        if hasattr(value, "__dict__"):
            return StrategyKernelContract._canonicalize(vars(value))
        return str(value)

    @staticmethod
    def verify_parity(
        backtest_result: Any | None,
        paper_result: Any | None,
        testnet_result: Any | None = None,
    ) -> ParityResult:
        """验证 Backtest/Paper/Testnet 一致性。"""
        result = ParityResult()

        if backtest_result is not None:
            result.backtest_hash = StrategyKernelContract.compute_proposal_hash(backtest_result)

        if paper_result is not None:
            result.paper_hash = StrategyKernelContract.compute_proposal_hash(paper_result)

        if testnet_result is not None:
            result.testnet_hash = StrategyKernelContract.compute_proposal_hash(testnet_result)

        # A missing environment is not a pass.  If Testnet is supplied it must
        # match the same frozen proposal as Backtest and Paper.
        if not result.backtest_hash or not result.paper_hash:
            result.status = ParityStatus.NOT_RUN
            result.discrepancies.append("BACKTEST_AND_PAPER_REQUIRED")
            return result

        hashes = [("Backtest", result.backtest_hash), ("Paper", result.paper_hash)]
        if result.testnet_hash:
            hashes.append(("Testnet", result.testnet_hash))
        expected = hashes[0][1]
        mismatches = [(name, value) for name, value in hashes[1:] if value != expected]
        if mismatches:
            result.status = ParityStatus.DISCREPANCY
            for name, value in mismatches:
                result.discrepancies.append(f"{hashes[0][0]} {expected} != {name} {value}")
        else:
            result.status = ParityStatus.MATCH

        return result


class StrategyKernel:
    """BD-T05: 统一策略内核 — Backtest/Paper/Shadow/Testnet 同一入口。

    封装 AlphaGraph DAG (当前) 和 TypedAlphaGraph (新增) 的执行。
    所有模式使用相同内核；仅 MarketIO/Clock/ExchangeIO adapter 可替换。
    """

    def __init__(self, mode: str = "PAPER") -> None:
        self.mode = mode
        self._alpha_graph = None  # 旧 DAG
        self._typed_graph = None  # 新 DAG (BD-T05)

    def set_alpha_graph(self, graph) -> None:
        self._alpha_graph = graph

    def set_typed_graph(self, graph) -> None:
        self._typed_graph = graph

    async def evaluate(self, context: dict) -> dict | None:
        """执行策略评估。

        TypedAlphaGraph 一旦接线就是唯一可执行边界。旧 AlphaGraph 只可
        作为诊断/兼容对象保留，不能在 typed graph 返回 ``None``（例如
        强制 VETO）时偷偷回退并产生另一份策略结果。
        """
        if self._typed_graph is not None:
            _detailed_fn = getattr(self._typed_graph, "_execute_detailed", None)
            if callable(_detailed_fn):
                _detailed = await _detailed_fn(context)
                _proposal = _detailed.get("proposal")
                _component_outputs = _detailed.get("component_outputs", {})
                # 诊断：打印拓扑顺序
                _order = self._typed_graph.topological_order()
                _types = {nid: self._typed_graph._nodes[nid].node_type.value for nid in _order if nid in self._typed_graph._nodes}
                print(f"[kernel] DAG order: {list(zip(_order, [_types.get(n,'?') for n in _order]))}")
                print(f"[kernel] component outputs: {list(_component_outputs.keys())}")
            else:
                _proposal = await self._typed_graph.execute(context)
                _component_outputs = {}
            graph_hash = ""
            graph_hash_fn = getattr(self._typed_graph, "compute_graph_hash", None)
            if callable(graph_hash_fn):
                graph_hash = str(graph_hash_fn())
            return {
                "proposal": _proposal,
                "component_outputs": _component_outputs,
                "kernel": "typed_graph",
                "mode": self.mode,
                "graph_hash": graph_hash,
                "blocked_by": "typed_graph_no_proposal" if _proposal is None else "",
            }
        if self._alpha_graph is not None:
            signals = await self._alpha_graph.generate(context)
            return {
                "signals": signals,
                "kernel": "alpha_graph",
                "mode": self.mode,
            }
        return None

    @staticmethod
    def compute_proposal_hash(proposal: Any) -> str:
        """Return the canonical hash of the final proposal/no-action evidence."""

        return StrategyKernelContract.compute_proposal_hash(proposal)

    def proposal_hash(self, proposal: Any) -> str:
        """BD-P0-04: hash the result, not merely the input key set."""

        return self.compute_proposal_hash(proposal)


def parity_check(
    backtest_proposal=None,
    paper_proposal=None,
    testnet_proposal=None,
) -> tuple[bool, ParityResult]:
    """便捷函数：执行 parity 检查。

    Returns:
        (passed, ParityResult)
    """
    result = StrategyKernelContract.verify_parity(
        backtest_proposal,
        paper_proposal,
        testnet_proposal,
    )
    passed = result.status == ParityStatus.MATCH
    return passed, result
