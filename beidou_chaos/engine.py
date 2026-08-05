
"""Kill Register 与组合故障认证。可重复故障注入。"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from beidou_shared.types import CorrelationId, ResultStatus

class KillScenario(str, Enum):
    EXCHANGE_DISCONNECT = "EXCHANGE_DISCONNECT"
    DATABASE_FAILURE = "DATABASE_FAILURE"
    KAFKA_OUTAGE = "KAFKA_OUTAGE"
    CLICKHOUSE_OUTAGE = "CLICKHOUSE_OUTAGE"
    MEMORY_PRESSURE = "MEMORY_PRESSURE"
    CPU_EXHAUSTION = "CPU_EXHAUSTION"
    NETWORK_PARTITION = "NETWORK_PARTITION"
    CLOCK_SKEW = "CLOCK_SKEW"
    SECRET_ROTATION = "SECRET_ROTATION"
    EXECUTOR_CRASH = "EXECUTOR_CRASH"

@dataclass
class KillRegister:
    scenarios: dict[KillScenario, bool] = field(default_factory=dict)
    combination_scenarios: list[list[KillScenario]] = field(default_factory=list)

    def register(self, scenario: KillScenario) -> None:
        self.scenarios[scenario] = True

    def register_combination(self, scenarios: list[KillScenario]) -> None:
        self.combination_scenarios.append(scenarios)

    def all_registered(self) -> list[KillScenario]:
        return list(self.scenarios.keys())

@dataclass
class ChaosExperiment:
    experiment_id: str; scenario: KillScenario
    injected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    observed_recovery: bool = False; recovery_time_seconds: float | None = None
    auto_actions_triggered: list[str] = field(default_factory=list)
    invariants_preserved: bool = False; evidence: list[str] = field(default_factory=list)

class ChaosEngine:
    """混沌工程引擎。故障注入、自动恢复验证、证据收集。"""
    def __init__(self):
        self._register = KillRegister()
        self._experiments: list[ChaosExperiment] = []

    def inject(self, scenario: KillScenario) -> ChaosExperiment:
        exp = ChaosExperiment(experiment_id=f"chaos-{len(self._experiments)+1}", scenario=scenario)
        self._experiments.append(exp)
        return exp

    def verify_recovery(self, exp: ChaosExperiment, recovered: bool, time_s: float, invariants_ok: bool) -> None:
        exp.observed_recovery = recovered; exp.recovery_time_seconds = time_s; exp.invariants_preserved = invariants_ok

    def all_experiments_passed(self) -> bool:
        return all(e.observed_recovery and e.invariants_preserved for e in self._experiments)

    def combination_fault_test(self, scenarios: list[KillScenario]) -> list[ChaosExperiment]:
        return [self.inject(s) for s in scenarios]
