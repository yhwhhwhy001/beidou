"""G5 协议测试场景基础设施 — 场景结果、名义记账、场景基类与证据写入。

全部 G5 场景(create/query/cancel、协议组、运行时组)继承 ScenarioBase,
共享 ScenarioResult/NotionalLedger/ScenarioContext,并通过
write_scenario_evidence 落盘证据。
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from beidou_exchange.binance_usdm.rest_client import BinanceRESTClient


class ScenarioStatus(str, Enum):
    """场景执行状态;WARN 表示通过但带风险提示。"""

    @staticmethod
    def _generate_next_value_(name: str, start: int, count: int, last_values: list[str]) -> str:
        """str-Enum 成员值取成员名本身,避免字面量赋值。"""
        return name

    PASS = auto()
    FAIL = auto()
    NOT_VERIFIABLE = auto()
    WARN = auto()


@dataclass
class ScenarioResult:
    """单个场景的执行结果;artifact_hash 为证据确定性指纹。"""

    scenario_id: str
    status: ScenarioStatus
    evidence: dict
    duration: float
    error_type: str = ""
    error_message: str = ""

    def artifact_hash(self) -> str:
        """sha256 指纹:场景 id、状态值与证据的确定性序列化。"""
        canonical = json.dumps(
            {"scenario_id": self.scenario_id, "status": self.status.value, "evidence": self.evidence},
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class NotionalExceededError(Exception):
    """名义金额累计超限。"""

    def __init__(self, scenario_id: str, total: float, limit: float) -> None:
        super().__init__(f"notional {total} exceeds limit {limit} in scenario {scenario_id}")
        self.scenario_id = scenario_id
        self.total = total
        self.limit = limit


class NotionalLedger:
    """跨场景名义金额累计记账,累计超限即抛 NotionalExceededError。"""

    def __init__(self, limit_usdt: float) -> None:
        self.limit_usdt = limit_usdt
        self._total = 0.0

    @property
    def total(self) -> float:
        return self._total

    def record(self, scenario_id: str, amount_usdt: float) -> None:
        self._total += amount_usdt
        if self._total > self.limit_usdt:
            raise NotionalExceededError(scenario_id, self._total, self.limit_usdt)


@dataclass
class ScenarioContext:
    """场景执行上下文:交易所客户端、名义记账、证据目录、标的与 dry_run。"""

    client: BinanceRESTClient | None
    ledger: NotionalLedger
    evidence_dir: Path
    symbol: str
    dry_run: bool


class ScenarioBase(ABC):
    """G5 场景基类:子类声明 scenario_id 类属性并实现 async run。"""

    scenario_id: str

    @abstractmethod
    async def run(self, ctx: ScenarioContext) -> ScenarioResult:
        raise NotImplementedError

    def _fail(self, status: ScenarioStatus, error_type: str, message: str, evidence: dict) -> ScenarioResult:
        """构造失败结果辅助,供子类统一产出 FAIL/NOT_VERIFIABLE。"""
        return ScenarioResult(
            scenario_id=self.scenario_id,
            status=status,
            evidence=evidence,
            duration=0.0,
            error_type=error_type,
            error_message=message,
        )


class EvidenceWriteError(OSError):
    """证据写入失败:目录创建或文件写入的 OSError 包装。"""


def write_scenario_evidence(ctx: ScenarioContext, result: ScenarioResult) -> Path:
    """把场景结果写入 evidence_dir/{scenario_id}.json,失败抛 EvidenceWriteError。

    内容为结果全字段 + artifact_hash + written_at(UTC ISO)。
    """
    payload = {
        "scenario_id": result.scenario_id,
        "status": result.status.value,
        "evidence": result.evidence,
        "duration": result.duration,
        "error_type": result.error_type,
        "error_message": result.error_message,
        "artifact_hash": result.artifact_hash(),
        "written_at": datetime.now(timezone.utc).isoformat(),
    }
    target = ctx.evidence_dir / f"{result.scenario_id}.json"
    try:
        ctx.evidence_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        raise EvidenceWriteError(f"场景证据写入失败 {target}: {exc}") from exc
    return target
