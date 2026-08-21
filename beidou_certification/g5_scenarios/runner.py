"""G5 场景注册表、执行器与证书汇总。"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Callable

from beidou_certification.g5_scenarios.base import (
    NotionalExceededError,
    NotionalLedger,
    ScenarioBase,
    ScenarioContext,
    ScenarioResult,
    ScenarioStatus,
)

RESTART_GROUP: set[str] = {"process_restart", "database_restart", "user_stream_reconnect"}
SCENARIO_REGISTRY: dict[str, type[ScenarioBase]] = {}


def summarize(results: dict[str, ScenarioResult]) -> dict:
    counts = {"total": len(results), "pass": 0, "warn": 0, "fail": 0, "not_verifiable": 0}  # nosec B105 - result labels
    for r in results.values():
        key = {"PASS": "pass", "WARN": "warn", "FAIL": "fail", "NOT_VERIFIABLE": "not_verifiable"}[  # nosec B105 - result labels
            r.status.value
        ]
        counts[key] += 1
    return counts


def certificate_status(summary: dict, restart_skipped: list[str]) -> str:
    if summary["fail"]:
        return "FAIL"
    if summary["not_verifiable"] or restart_skipped:
        return "NOT_VERIFIABLE"
    return "PASS"


def aggregate_evidence_hash(results: dict[str, ScenarioResult]) -> str:
    parts = "".join(f"{sid}:{results[sid].artifact_hash()}" for sid in sorted(results))
    return hashlib.sha256(parts.encode()).hexdigest()


class G5Runner:
    """按注册顺序执行场景并汇总证书。"""

    def __init__(
        self,
        *,
        plan_path: Path,
        commit: str,
        testnet_url: str,
        evidence_dir: Path,
        ledger: NotionalLedger,
        symbol: str,
        make_context: Callable[[], ScenarioContext],
    ) -> None:
        self.plan_path = plan_path
        self.commit = commit
        self.testnet_url = testnet_url
        self.evidence_dir = evidence_dir
        self.ledger = ledger
        self.symbol = symbol
        self.make_context = make_context
        self.restart_skipped: list[str] = []

    def _run_one(self, scenario_cls: type[ScenarioBase], ctx: ScenarioContext) -> ScenarioResult:
        """执行单个场景;名义超限按 fail-fast 记 FAIL 并阻断后续场景,
        其他异常记 FAIL(error_type=类型名)不阻断后续场景。"""
        try:
            scenario = scenario_cls()
            return asyncio.run(scenario.run(ctx))
        except NotionalExceededError as exc:
            return ScenarioResult(
                scenario_id=scenario_cls.scenario_id,
                status=ScenarioStatus.FAIL,
                evidence={},
                duration=0.0,
                error_type="NOTIONAL_EXCEEDED",
                error_message=str(exc),
            )
        except Exception as exc:
            return ScenarioResult(
                scenario_id=scenario_cls.scenario_id,
                status=ScenarioStatus.FAIL,
                evidence={},
                duration=0.0,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )

    def run_selected(self, *, only: str | None = None, skip_restart: bool = False) -> dict[str, ScenarioResult]:
        self.restart_skipped = []  # 每次运行重置,避免残留跳过记录污染证书状态
        results: dict[str, ScenarioResult] = {}
        for sid, cls in SCENARIO_REGISTRY.items():
            if only and sid != only:
                continue
            if skip_restart and sid in RESTART_GROUP:
                self.restart_skipped.append(sid)
                continue
            result = self._run_one(cls, self.make_context())
            results[sid] = result
            if result.error_type == "NOTIONAL_EXCEEDED":
                break  # 资金保护优先:超限即停止后续场景
        return results

    def build_certificate(
        self, results: dict[str, ScenarioResult], *, started_at: str, ended_at: str, account_access: dict | None = None
    ) -> dict:
        summary = summarize(results)
        status = certificate_status(summary, self.restart_skipped)
        scenarios = {
            sid: {"status": r.status.value, "artifact_hash": r.artifact_hash(), "duration": r.duration}
            for sid, r in results.items()
        }
        return {
            "gate": "G5",
            "certification_mode": "FULL",
            "status": status,
            "commit": self.commit,
            "environment": "BINANCE_USDM_TESTNET_ONLY",
            "testnet_url": self.testnet_url,
            "mainnet_prohibited": True,
            "is_simulated": False,
            "started_at": started_at,
            "ended_at": ended_at,
            "evidence_hash": aggregate_evidence_hash(results),
            "max_notional_usdt": self.ledger.limit_usdt,
            "scenarios": scenarios,
            "summary": {**summary, "p0": 0},
            # 未测量时占位默认必须是保守声明(不肯定性声称可交易/有余额),
            # 防止 partial probe 生成 PASS 证书
            "account_access": account_access
            if account_access is not None
            else {"can_trade": False, "can_withdraw": None, "has_balance": False},
            "blockers": [],
            "p0_failures": [],
        }
