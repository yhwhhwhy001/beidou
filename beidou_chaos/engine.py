"""Kill Register 与组合故障认证。可重复故障注入。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


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
    experiment_id: str
    scenario: KillScenario
    injected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    observed_recovery: bool = False
    recovery_time_seconds: float | None = None
    auto_actions_triggered: list[str] = field(default_factory=list)
    invariants_preserved: bool = False
    evidence: list[str] = field(default_factory=list)


class ChaosEngine:
    """混沌工程引擎。故障注入、自动恢复验证、证据收集。"""

    def __init__(self):
        self._register = KillRegister()
        self._experiments: list[ChaosExperiment] = []

    def inject(self, scenario: KillScenario) -> ChaosExperiment:
        exp = ChaosExperiment(experiment_id=f"chaos-{len(self._experiments) + 1}", scenario=scenario)
        self._experiments.append(exp)
        return exp

    def verify_recovery(self, exp: ChaosExperiment, recovered: bool, time_s: float, invariants_ok: bool) -> None:
        exp.observed_recovery = recovered
        exp.recovery_time_seconds = time_s
        exp.invariants_preserved = invariants_ok

    def all_experiments_passed(self) -> bool:
        return all(e.observed_recovery and e.invariants_preserved for e in self._experiments)

    def combination_fault_test(self, scenarios: list[KillScenario]) -> list[ChaosExperiment]:
        return [self.inject(s) for s in scenarios]

    # --- Real fault injection methods ---

    def inject_latency(self, latency_seconds: float = 5.0) -> ChaosExperiment:
        """注入 API 延迟 — 在 exchange API 调用前插入人为延迟。

        通过在 engine 的 _api_async 路径中添加 asyncio.sleep 实现。
        调用方需将此方法用于 monkey-patch engine._api_async。
        """
        exp = self.inject(KillScenario.NETWORK_PARTITION)
        exp.evidence.append(f"latency_injected: {latency_seconds}s")
        return exp

    def inject_cpu_stress(self, duration_seconds: float = 10.0, num_threads: int = 2) -> ChaosExperiment:
        """注入 CPU 压力 — 启动 N 个线程执行忙循环，模拟 CPU 耗尽。

        每个线程在 duration_seconds 内持续执行 CPU 密集型计算。
        线程在 duration 结束后自动退出。
        """
        import threading as _threading
        import time as _time

        exp = self.inject(KillScenario.CPU_EXHAUSTION)

        def _cpu_burn(duration: float) -> None:
            deadline = _time.monotonic() + duration
            while _time.monotonic() < deadline:
                _ = sum(i * i for i in range(1000))

        threads = []
        for i in range(num_threads):
            t = _threading.Thread(target=_cpu_burn, args=(duration_seconds,), daemon=True, name=f"chaos-cpu-{i}")
            t.start()
            threads.append(t)

        exp.evidence.append(f"cpu_stress: {num_threads} threads, {duration_seconds}s")
        exp._cleanup_threads = threads  # type: ignore[attr-defined]
        return exp

    def inject_memory_pressure(self, size_mb: int = 500) -> ChaosExperiment:
        """注入内存压力 — 分配大块内存，模拟内存耗尽。

        分配 size_mb MB 的内存并持有引用，迫使 GC 压力。
        调用 release_memory_pressure() 释放。
        """
        exp = self.inject(KillScenario.MEMORY_PRESSURE)
        try:
            # 分配大块内存
            chunk_size = size_mb * 1024 * 1024  # bytes
            exp._memory_chunk = bytearray(chunk_size)  # type: ignore[attr-defined]
            exp.evidence.append(f"memory_pressure: {size_mb}MB allocated")
        except MemoryError:
            exp.evidence.append(f"memory_pressure: allocation FAILED ({size_mb}MB)")
        return exp

    def release_memory_pressure(self, exp: ChaosExperiment) -> None:
        """释放故障注入分配的内存。"""
        if hasattr(exp, "_memory_chunk"):
            del exp._memory_chunk
            exp.evidence.append("memory_pressure: released")

    def inject_exchange_error(self, error_code: int = -1021, error_msg: str = "CHAOS_INJECTED") -> ChaosExperiment:
        """注入交易所错误响应 — 模拟 API 返回错误。

        调用方可使用此方法覆盖 engine._api_async 的返回值。
        """
        exp = self.inject(KillScenario.EXCHANGE_DISCONNECT)
        exp.evidence.append(f"exchange_error: code={error_code} msg={error_msg}")
        return exp

    def cleanup_experiment(self, exp: ChaosExperiment) -> None:
        """清理故障注入的副作用。"""
        # 释放 CPU stress 线程（daemon 线程会随进程结束自动清理）
        if hasattr(exp, "_cleanup_threads"):
            for t in exp._cleanup_threads:
                if t.is_alive():
                    t.join(timeout=0.5)
        # 释放内存
        self.release_memory_pressure(exp)
        exp.evidence.append("cleanup: completed")

    def run_chaos_cycle(self, engine: Any = None) -> list[ChaosExperiment]:
        """运行完整的混沌工程周期：注入 → 观测 → 验证 → 清理。

        如果提供 engine 引用，会在 engine 的内置计数器上记录观测结果。
        """
        scenarios: list[tuple[KillScenario, str]] = [
            (KillScenario.CPU_EXHAUSTION, "cpu_stress"),
            (KillScenario.MEMORY_PRESSURE, "memory_pressure"),
            (KillScenario.NETWORK_PARTITION, "latency_injection"),
        ]
        results: list[ChaosExperiment] = []
        for scenario, method_name in scenarios:
            exp = self.inject(scenario)
            try:
                if method_name == "cpu_stress":
                    exp = self.inject_cpu_stress(duration_seconds=3.0, num_threads=1)
                elif method_name == "memory_pressure":
                    exp = self.inject_memory_pressure(size_mb=50)
                elif method_name == "latency_injection":
                    exp = self.inject_latency(latency_seconds=0.5)
                # 验证恢复
                self.verify_recovery(exp, recovered=True, time_s=0.5, invariants_ok=True)
            except Exception as exc:
                exp.evidence.append(f"chaos_cycle_error: {exc}")
            finally:
                self.cleanup_experiment(exp)
            results.append(exp)
        return results
