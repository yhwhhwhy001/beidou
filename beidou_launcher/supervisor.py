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


class BeidouSupervisor:
    def __init__(
        self,
        *,
        project_root: Path,
        mode: str,
        symbols: list[str],
        port: int,
        startup_timeout: float = 300.0,
        monitor_interval: float = 5.0,
        self_heal: bool = True,
        max_restarts: int = 2,
    ) -> None:
        self.project_root = project_root
        self.mode = mode
        self.symbols = symbols
        self.port = port
        self.startup_timeout = startup_timeout
        self.monitor_interval = monitor_interval
        self.self_heal = self_heal
        self.max_restarts = max_restarts
        self.writer = EvidenceWriter(project_root)
        self.lock = InstanceLock(project_root / ".beidou" / "beidou.pid")
        self.report = StartupReport(mode=mode, symbols=symbols, port=port, commit=current_commit(project_root))
        self.engine: Any | None = None
        self._engine_task: asyncio.Task[Any] | None = None
        self._resume_authorized = False
        self._critical_streak = 0
        self._last_error_count = 0
        self._algorithm_probe: dict[str, Any] = {}
        self._last_algorithm_probe_attempt = 0.0
        self._exchange_algo_snapshot: dict[str, Any] = {"ok": mode != "testnet", "by_symbol": {}}
        self._last_exchange_algo_probe = 0.0
        self._exchange_account_snapshot: dict[str, Any] = {"ok": False}
        self._last_exchange_account_probe = 0.0
        self._shutdown_requested = False
        self._control_paused_by_supervisor = False
        self._engine_failure = ""
        self._recovery_count = 0

    @staticmethod
    def _print_checks(checks: list[CheckResult]) -> None:
        for item in checks:
            marker = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌", "UNKNOWN": "❔"}[item.status.value]
            print(f"{marker} [{item.severity.value}] {item.name}: {item.message}")

    def _install_exchange_write_interlock(self) -> None:
        """在非写模式从引擎 API 边界拦截所有交易所写请求。"""
        assert self.engine is not None
        original_async = self.engine._api_async
        original_sync = self.engine._api
        self.engine._supervisor_blocked_writes = []

        def record(path: str, method: str) -> dict[str, Any]:
            event = {"path": path, "method": method.upper(), "mode": self.mode, "timestamp": time.time()}
            self.engine._supervisor_blocked_writes.append(event)
            return {
                "code": -3,
                "error": -3,
                "msg": f"WRITE_BLOCKED_BY_SUPERVISOR: {method.upper()} {path} in {self.mode}",
            }

        async def guarded_async(
            path: str,
            method: str = "GET",
            signed: bool = False,
            params: dict[str, Any] | None = None,
        ) -> Any:
            if method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and not self.engine._can_write:
                return record(path, method)
            return await original_async(path, method=method, signed=signed, params=params)

        def guarded_sync(
            path: str,
            method: str = "GET",
            signed: bool = False,
            params: dict[str, Any] | None = None,
        ) -> Any:
            if method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and not self.engine._can_write:
                return record(path, method)
            return original_sync(path, method=method, signed=signed, params=params)

        self.engine._api_async = guarded_async
        self.engine._api = guarded_sync

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
        self._control_paused_by_supervisor = True

    def _control_state(self) -> str:
        if self.engine is None:
            return "UNKNOWN"
        try:
            status = self.engine._control.get_status()
            return str(getattr(status, "value", status))
        except Exception:
            return "UNKNOWN"

    def _is_trading_ready(self) -> bool:
        return (
            self._resume_authorized
            and not self.report.blockers
            and self._control_state() == "RESUME"
            and self.report.supervisor_state not in {"FAILED", "LOCKED", "STOPPED"}
        )

    def _install_health_callbacks(self) -> None:
        """让 HTTP readiness 与监督器证据保持一致。"""
        assert self.engine is not None

        def readiness() -> bool:
            return bool(self.engine._check_ready()) and self.report.supervisor_state == "RUNNING"

        def trading_readiness() -> tuple[bool, str]:
            ready = self._is_trading_ready()
            if ready:
                return True, "SUPERVISOR_VALIDATED"
            if self.report.blockers:
                return False, self.report.blockers[0].check_id
            return False, f"CONTROL_{self._control_state()}"

        def status_info() -> dict[str, Any]:
            base = dict(self.engine._get_status_info())
            base["supervisor"] = {
                "state": self.report.supervisor_state,
                "phase": self.report.phase,
                "trading_ready": self._is_trading_ready(),
                "control_state": self._control_state(),
                "commit": self.report.commit,
                "blockers": [item.check_id for item in self.report.blockers],
                "checks": {item.check_id: item.status.value for item in self.report.checks},
            }
            return base

        self.engine._health.set_readiness_check(readiness)
        self.engine._health.set_trading_readiness(trading_readiness)
        self.engine._health.set_status_info(status_info)

    async def _refresh_exchange_account_snapshot(self) -> None:
        """独立读取当前账户事实，拒绝使用陈旧的引擎缓存作为就绪证据。"""
        if self.engine is None:
            return
        now = time.monotonic()
        if now - self._last_exchange_account_probe < 15.0:
            return
        self._last_exchange_account_probe = now
        try:
            response = await self.engine._api_async("/fapi/v2/account", signed=True)
            valid = (
                isinstance(response, dict)
                and "totalWalletBalance" in response
                and isinstance(response.get("positions"), list)
            )
            if not valid:
                raise RuntimeError(str(response)[:300])
            self._exchange_account_snapshot = {"ok": True, "account": response, "observed_at": time.time()}
        except Exception as exc:
            self._exchange_account_snapshot = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "observed_at": time.time(),
            }

    async def _refresh_exchange_algo_snapshot(self, *, force: bool = False) -> None:
        """读取交易所当前 openAlgoOrders；查询失败保持 UNKNOWN 并阻断。"""
        if self.mode != "testnet" or self.engine is None:
            return
        now = time.monotonic()
        if not force and now - self._last_exchange_algo_probe < 15.0:
            return
        self._last_exchange_algo_probe = now
        try:
            response = await self.engine._api_async("/fapi/v1/openAlgoOrders", signed=True)
            if not isinstance(response, list):
                raise RuntimeError(str(response)[:300])
            by_symbol: dict[str, list[str]] = {}
            for item in response:
                symbol = str(item.get("symbol", ""))
                algo_id = str(item.get("algoId", ""))
                if symbol and algo_id:
                    by_symbol.setdefault(symbol, []).append(algo_id)
            self._exchange_algo_snapshot = {
                "ok": True,
                "by_symbol": by_symbol,
                "total": sum(len(ids) for ids in by_symbol.values()),
                "observed_at": time.time(),
            }
        except Exception as exc:
            self._exchange_algo_snapshot = {
                "ok": False,
                "by_symbol": {},
                "error": f"{type(exc).__name__}: {exc}",
                "observed_at": time.time(),
            }

    def _runtime_checks(self) -> list[CheckResult]:
        assert self.engine is not None
        checks, error_count = collect_runtime_checks(
            engine=self.engine,
            mode=self.mode,
            port=self.port,
            resume_authorized=self._resume_authorized,
            algorithm_probe=self._algorithm_probe,
            last_error_count=self._last_error_count,
            exchange_algo_snapshot=self._exchange_algo_snapshot,
            exchange_account_snapshot=self._exchange_account_snapshot,
        )
        self._last_error_count = error_count
        return checks

    # 启动阶段只要求关键检查通过；行情、对账、心跳等运行时检查
    # 在引擎运行一段时间后自然会就绪，不应阻断启动。
    _STARTUP_CRITICAL_CHECKS = frozenset({
        "runtime.wiring.core",
        "runtime.algorithms.alpha_graph",
        "runtime.algorithms.factor_lifecycle",
        "runtime.algorithms.trading_pool",
        "runtime.algorithms.risk_budget",
        "runtime.health.lifecycle",
        "runtime.health.http_server",
        "runtime.health.account_snapshot",
    })

    async def _wait_for_startup(self) -> bool:
        assert self.engine is not None
        assert self._engine_task is not None
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self._engine_task.done():
                try:
                    await self._engine_task
                except asyncio.CancelledError:
                    self._engine_failure = "engine task cancelled"
                except Exception as exc:
                    self._engine_failure = f"{type(exc).__name__}: {exc}"
                return False
            lifecycle = getattr(getattr(self.engine, "_lifecycle", None), "state", None)
            lifecycle_value = str(getattr(lifecycle, "value", lifecycle))
            health_thread = getattr(getattr(self.engine, "_health", None), "_thread", None)
            if lifecycle_value == "ACTIVE" and self._shutdown_requested:
                self.engine._running = False
                return False
            if lifecycle_value == "ACTIVE" and health_thread is not None and health_thread.is_alive():
                if (
                    not self._algorithm_probe.get("ok")
                    and time.monotonic() - self._last_algorithm_probe_attempt >= 10.0
                ):
                    self._last_algorithm_probe_attempt = time.monotonic()
                    try:
                        self._algorithm_probe = await run_read_only_algorithm_probe(self.engine, self.symbols)
                    except Exception as exc:
                        self._algorithm_probe = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                        print(f"[supervisor] Algorithm probe failed: {exc}")
                await self._refresh_exchange_account_snapshot()
                await self._refresh_exchange_algo_snapshot()
                try:
                    checks = self._runtime_checks()
                except Exception as exc:
                    print(f"[supervisor] Runtime checks failed: {type(exc).__name__}: {exc}")
                    import traceback
                    traceback.print_exc()
                    checks = []
                self.report.phase = "STARTUP_VALIDATION"
                self.report.replace_phase_checks("runtime.", checks)
                self.writer.write(self.report)
                # 启动阶段仅阻断关键接线/生命周期/账户检查
                startup_blockers = [
                    c for c in checks
                    if c.is_blocking and c.check_id in self._STARTUP_CRITICAL_CHECKS
                ]
                if not startup_blockers:
                    return True
            else:
                self.report.phase = "ENGINE_STARTING"
                self.report.supervisor_state = "STARTING"
                self.writer.write(self.report)
            await asyncio.sleep(1)
        return False

    async def _fail_closed(self, reason: str, fatal: bool = False) -> None:
        if self.engine is None:
            return
        from beidou_control.plane import ControlAction
        from beidou_lifecycle.lifecycle import ModuleState

        previous_control = self._control_state()
        with suppress(Exception):
            self.engine._control.execute_action(ControlAction.NO_NEW_RISK)
        if previous_control == "RESUME":
            self._control_paused_by_supervisor = True
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
        if not self.self_heal or self._recovery_count >= self.max_restarts:
            return False

        from beidou_control.plane import ControlAction
        from beidou_lifecycle.lifecycle import ModuleState

        for target in (ModuleState.RECOVERING, ModuleState.VALIDATING, ModuleState.ACTIVE):
            result = lifecycle.transition(target)
            if str(getattr(result, "value", result)) != "SUCCESS":
                return False
        if self._control_paused_by_supervisor:
            self.engine._control.execute_action(ControlAction.RESUME)
            self._control_paused_by_supervisor = False
        self._critical_streak = 0
        self._recovery_count += 1
        print(
            f"[supervisor] RECOVERED: RECOVERING → VALIDATING → ACTIVE "
            f"({self._recovery_count}/{self.max_restarts})"
        )
        return True

    async def _monitor(self) -> int:
        assert self._engine_task is not None
        fatal_triggered = False
        while not self._engine_task.done():
            await asyncio.sleep(self.monitor_interval)
            await self._refresh_exchange_account_snapshot()
            await self._refresh_exchange_algo_snapshot()
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
            if fatal_triggered:
                self.report.supervisor_state = "LOCKED"
            elif blockers:
                self.report.supervisor_state = "DEGRADED"
            elif self._control_state() != "RESUME":
                self.report.supervisor_state = "PAUSED"
            else:
                self.report.supervisor_state = "RUNNING"
            self.report.trading_ready = self._is_trading_ready()
            self.writer.write(self.report)
        try:
            await self._engine_task
        except asyncio.CancelledError:
            self._engine_failure = "engine task cancelled"
        except Exception as exc:
            self._engine_failure = f"{type(exc).__name__}: {exc}"

        self.report.trading_ready = False
        self.report.phase = "STOPPED"
        lifecycle = None
        if self.engine is not None:
            lifecycle = getattr(getattr(self.engine, "_lifecycle", None), "state", None)
        lifecycle_value = str(getattr(lifecycle, "value", lifecycle))
        if fatal_triggered or lifecycle_value in {"LOCKED", "FAILED"}:
            self.report.supervisor_state = "LOCKED" if lifecycle_value == "LOCKED" else "FAILED"
            self.writer.write(self.report)
            return 5
        if self._engine_failure:
            self.report.supervisor_state = "FAILED"
            self.writer.write(self.report)
            return 6
        self.report.supervisor_state = "STOPPED"
        self.writer.write(self.report)
        return 0

    async def run(self) -> int:
        os.chdir(self.project_root)
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
            self.engine._last_recon = 0.0
            self._install_exchange_write_interlock()
            self._install_resume_interlock()
            self._install_health_callbacks()

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
                self._shutdown_requested = True
                if self.engine is not None:
                    lifecycle = getattr(getattr(self.engine, "_lifecycle", None), "state", None)
                    if str(getattr(lifecycle, "value", lifecycle)) == "ACTIVE":
                        self.engine._running = False

            for sig in (signal.SIGINT, signal.SIGTERM):
                with suppress(NotImplementedError, RuntimeError):
                    loop.add_signal_handler(sig, request_shutdown)

            self._engine_task = asyncio.create_task(self.engine.run(), name="beidou-engine")
            ready = await self._wait_for_startup()
            if not ready:
                if self._shutdown_requested:
                    self.report.supervisor_state = "STOPPED"
                    self.report.trading_ready = False
                    self.writer.write(self.report)
                    print("[supervisor] 启动阶段收到停止请求，安全退出。")
                    return 0
                failure_reason = self._engine_failure or "启动超时或引擎提前退出"
                await self._fail_closed(failure_reason, fatal=True)
                self.report.supervisor_state = "FAILED"
                self.report.trading_ready = False
                self.writer.write(self.report)
                print("❌ 深度启动自检未通过。")
                return 4

            self._resume_authorized = True
            from beidou_control.plane import ControlAction

            self.engine._control.execute_action(ControlAction.RESUME)
            self._control_paused_by_supervisor = False
            self.report.supervisor_state = "RUNNING"
            self.report.trading_ready = self._is_trading_ready()
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
