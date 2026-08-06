"""北斗引擎启动监督、自动降级、恢复和锁定。"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from .models import CheckResult, StartupReport
from .preflight import current_commit, run_preflight
from .registry import inspect_engine_wiring
from .runtime import collect_runtime_checks, run_read_only_algorithm_probe
from .state import EvidenceWriter, InstanceLock


class StartupSupervisor:
    def __init__(
        self,
        *,
        project_root: Path,
        mode: str,
        symbols: list[str],
        port: int,
        startup_timeout: float = 300.0,
        monitor_interval: float = 5.0,
    ) -> None:
        self.project_root = project_root
        self.mode = mode
        self.symbols = symbols
        self.port = port
        self.startup_timeout = startup_timeout
        self.monitor_interval = monitor_interval
        self.writer = EvidenceWriter(project_root)
        self.lock = InstanceLock(project_root / ".beidou" / "beidou.pid")
        self.report = StartupReport(mode=mode, symbols=symbols, port=port, commit=current_commit())
        self.engine: Any | None = None
        self._engine_task: asyncio.Task[Any] | None = None
        self._resume_authorized = False
        self._critical_streak = 0
        self._last_error_count = 0
        self._algorithm_probe: dict[str, Any] = {}
        self._last_algorithm_probe_attempt = 0.0

    @staticmethod
    def _print_checks(checks: list[CheckResult]) -> None:
        for item in checks:
            marker = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌", "UNKNOWN": "❔"}[item.status.value]
            print(f"{marker} [{item.severity.value}] {item.name}: {item.message}")

    def _install_resume_interlock(self) -> None:
        """在深度启动门禁通过前阻止引擎内部自动 RESUME。"""
        from beidou_control.plane import ControlAction

        assert self.engine is not None
        control = self.engine._control
        original = control.execute_action

        def guarded_execute(action: Any, *args: Any, **kwargs: Any) -> Any:
            if action == ControlAction.RESUME and not self._resume_authorized:
                return original(ControlAction.NO_NEW_RISK)
            return original(action, *args, **kwargs)

        control.execute_action = guarded_execute
        control.execute_action(ControlAction.NO_NEW_RISK)

    def _runtime_checks(self) -> list[CheckResult]:
        assert self.engine is not None
        checks, error_count = collect_runtime_checks(
            engine=self.engine,
            mode=self.mode,
            port=self.port,
            resume_authorized=self._resume_authorized,
            algorithm_probe=self._algorithm_probe,
            last_error_count=self._last_error_count,
        )
        self._last_error_count = error_count
        return checks

    async def _wait_for_startup(self) -> bool:
        assert self.engine is not None
        assert self._engine_task is not None
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self._engine_task.done():
                with suppress(Exception):
                    await self._engine_task
                return False
            lifecycle = getattr(getattr(self.engine, "_lifecycle", None), "state", None)
            lifecycle_value = str(getattr(lifecycle, "value", lifecycle))
            health_thread = getattr(getattr(self.engine, "_health", None), "_thread", None)
            if lifecycle_value == "ACTIVE" and health_thread is not None and health_thread.is_alive():
                if (
                    not self._algorithm_probe.get("ok")
                    and time.monotonic() - self._last_algorithm_probe_attempt >= 10.0
                ):
                    self._last_algorithm_probe_attempt = time.monotonic()
                    self._algorithm_probe = await run_read_only_algorithm_probe(self.engine, self.symbols)
                checks = self._runtime_checks()
                self.report.phase = "STARTUP_VALIDATION"
                self.report.replace_phase_checks("runtime.", checks)
                self.writer.write(self.report)
                if not self.report.blockers:
                    return True
            await asyncio.sleep(1)
        return False

    async def _fail_closed(self, reason: str, fatal: bool = False) -> None:
        if self.engine is None:
            return
        from beidou_control.plane import ControlAction
        from beidou_lifecycle.lifecycle import ModuleState

        with suppress(Exception):
            self.engine._control.execute_action(ControlAction.NO_NEW_RISK)
        lifecycle = self.engine._lifecycle
        if fatal:
            with suppress(Exception):
                lifecycle.transition(ModuleState.LOCKED)
            self.engine._running = False
        elif str(getattr(lifecycle.state, "value", lifecycle.state)) == "ACTIVE":
            with suppress(Exception):
                lifecycle.transition(ModuleState.DEGRADED)
        print(f"[supervisor] FAIL-CLOSED: {reason}; fatal={fatal}")

    async def _recover_if_validated(self, checks: list[CheckResult]) -> bool:
        """底层异常消失后，严格经过 RECOVERING→VALIDATING→ACTIVE。"""
        if self.engine is None:
            return False
        lifecycle = self.engine._lifecycle
        state_value = str(getattr(lifecycle.state, "value", lifecycle.state))
        non_lifecycle_blockers = [
            item for item in checks if item.is_blocking and item.check_id != "runtime.health.lifecycle"
        ]
        if state_value != "DEGRADED" or non_lifecycle_blockers:
            return False

        from beidou_control.plane import ControlAction
        from beidou_lifecycle.lifecycle import ModuleState

        for target in (ModuleState.RECOVERING, ModuleState.VALIDATING, ModuleState.ACTIVE):
            result = lifecycle.transition(target)
            if str(getattr(result, "value", result)) != "SUCCESS":
                return False
        self.engine._control.execute_action(ControlAction.RESUME)
        self._critical_streak = 0
        print("[supervisor] RECOVERED: RECOVERING → VALIDATING → ACTIVE")
        return True

    async def _monitor(self) -> int:
        assert self._engine_task is not None
        fatal_triggered = False
        while not self._engine_task.done():
            await asyncio.sleep(self.monitor_interval)
            checks = self._runtime_checks()
            if await self._recover_if_validated(checks):
                checks = self._runtime_checks()
            self.report.phase = "RUNTIME_MONITORING"
            self.report.replace_phase_checks("runtime.", checks)
            blockers = self.report.blockers
            if blockers:
                self._critical_streak += 1
                fatal_triggered = self._critical_streak >= 3
                await self._fail_closed(
                    "; ".join(f"{item.check_id}:{item.message}" for item in blockers),
                    fatal=fatal_triggered,
                )
            else:
                self._critical_streak = 0
            self.report.trading_ready = not blockers and self._resume_authorized
            self.report.supervisor_state = (
                "LOCKED" if fatal_triggered else ("RUNNING" if not blockers else "DEGRADED")
            )
            self.writer.write(self.report)
        with suppress(asyncio.CancelledError):
            await self._engine_task
        return 5 if fatal_triggered else 0

    async def run(self) -> int:
        os.environ["BEIDOU_ENV"] = self.mode
        locked, lock_message = self.lock.acquire()
        if not locked:
            print(f"❌ {lock_message}")
            return 3

        try:
            print("=" * 72)
            print("北斗一键启动监督器 / Beidou One-Click Supervisor")
            print(f"mode={self.mode} symbols={','.join(self.symbols)} port={self.port}")
            print("=" * 72)

            preflight, _settings = run_preflight(self.project_root, self.mode, self.port)
            self.report.phase = "PREFLIGHT"
            self.report.replace_phase_checks("preflight.", preflight)
            self._print_checks(preflight)
            self.writer.write(self.report)
            if self.report.blockers:
                self.report.supervisor_state = "BLOCKED"
                self.writer.write(self.report)
                print("❌ 启动前置检查未通过，系统未启动。")
                return 2

            from beidou_core.engine import AutonomousEngine

            self.engine = AutonomousEngine(symbols=self.symbols, mode=self.mode)
            self.engine._health._port = self.port
            self.engine._last_realtime = 0.0
            self._install_resume_interlock()

            wiring = inspect_engine_wiring(self.engine, self.mode)
            self.report.phase = "CONSTRUCTION_VALIDATION"
            self.report.replace_phase_checks("runtime.", wiring)
            self._print_checks(wiring)
            self.writer.write(self.report)
            if self.report.blockers:
                self.report.supervisor_state = "BLOCKED"
                self.writer.write(self.report)
                print("❌ 模块或算法接线不完整，系统未进入运行循环。")
                return 2

            loop = asyncio.get_running_loop()

            def request_shutdown() -> None:
                if self.engine is not None:
                    self.engine._running = False

            for sig in (signal.SIGINT, signal.SIGTERM):
                with suppress(NotImplementedError, RuntimeError):
                    loop.add_signal_handler(sig, request_shutdown)

            self._engine_task = asyncio.create_task(self.engine.run(), name="beidou-engine")
            ready = await self._wait_for_startup()
            if not ready:
                await self._fail_closed("启动超时或引擎提前退出", fatal=True)
                self.report.supervisor_state = "FAILED"
                self.report.trading_ready = False
                self.writer.write(self.report)
                print("❌ 深度启动自检未通过。")
                return 4

            self._resume_authorized = True
            from beidou_control.plane import ControlAction

            self.engine._control.execute_action(ControlAction.RESUME)
            self.report.trading_ready = True
            self.report.supervisor_state = "RUNNING"
            self.report.phase = "RUNTIME_MONITORING"
            self.writer.write(self.report)
            print("✅ 深度启动自检通过：环境、模块、算法、数据、账户与安全门禁均已验证。")
            print(f"✅ 状态: http://127.0.0.1:{self.port}/status")
            print(f"✅ 证据: {self.writer.state_path}")
            return await self._monitor()
        finally:
            if self.engine is not None:
                self.engine._running = False
            if self._engine_task is not None and not self._engine_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(self._engine_task), timeout=30.0)
                except TimeoutError:
                    self._engine_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await self._engine_task
            self.lock.release()
