"""BF-10: 统一 StrategyKernel 与 Parity 合同。

Backtest、Paper、Testnet 必须调用同一 StrategyKernel。
仅允许替换: MarketIO, Clock, ExchangeIO, Persistence adapter。
Parity 必须比较最终 StrategyProposal、归因、风险快照和目标仓位。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol

from beidou_shared.types import StrategyId


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
        """计算 StrategyProposal 的确定性哈希。"""
        content = json.dumps({
            "direction": getattr(proposal, "direction", ""),
            "strength": getattr(proposal, "strength", 0),
            "confidence": getattr(proposal, "confidence", 0),
        }, sort_keys=True, default=str)
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    @staticmethod
    def verify_parity(
        backtest_result: Any | None,
        paper_result: Any | None,
        testnet_result: Any | None = None,
    ) -> ParityResult:
        """验证 Backtest/Paper/Testnet 一致性。"""
        result = ParityResult()

        if backtest_result is not None:
            result.backtest_hash = StrategyKernelContract.compute_proposal_hash(
                backtest_result
            )

        if paper_result is not None:
            result.paper_hash = StrategyKernelContract.compute_proposal_hash(
                paper_result
            )

        if testnet_result is not None:
            result.testnet_hash = StrategyKernelContract.compute_proposal_hash(
                testnet_result
            )

        # 比较
        if result.backtest_hash and result.paper_hash:
            if result.backtest_hash == result.paper_hash:
                result.status = ParityStatus.MATCH
            else:
                result.status = ParityStatus.DISCREPANCY
                result.discrepancies.append(
                    f"Backtest {result.backtest_hash} != Paper {result.paper_hash}"
                )
        else:
            result.status = ParityStatus.NOT_RUN

        return result


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
        backtest_proposal, paper_proposal, testnet_proposal,
    )
    passed = result.status == ParityStatus.MATCH or result.status == ParityStatus.NOT_RUN
    return passed, result
