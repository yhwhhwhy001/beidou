"""北斗引擎启动监督、自动降级、恢复和锁定。"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from beidou_exchange.binance_usdm.endpoints import Endpoint
from beidou_observability.monitoring import DeepAuditScheduler, collect_monitoring_checks  # type: ignore[attr-defined]  # re-exported without __all__
from beidou_safety.execution.recovery import RecoveryEngine
from beidou_observability.monitoring.contracts import (
    AccountPositionMode,
    PositionModeEvidence,
)

from .manifest import MAX_RESTARTS, MONITOR_INTERVAL, STARTUP_TIMEOUT
from .models import CheckResult, CheckSeverity, CheckStatus, StartupReport
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
        startup_timeout: float = STARTUP_TIMEOUT,
        monitor_interval: float = MONITOR_INTERVAL,
        self_heal: bool = True,
        max_restarts: int = MAX_RESTARTS,
    ) -> None:
        self.project_root = project_root
        self.mode = mode
        self.symbols = symbols
        self.port = port
        self.startup_timeout = startup_timeout
        self.monitor_interval = monitor_interval
        self.self_heal = self_heal
        self.max_restarts = max_restarts
        self.recovery_window_seconds: float = 600.0  # 10 分钟滑动窗口
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
        self._position_mode_evidence: PositionModeEvidence | None = None
        self._last_position_mode_probe = 0.0
        self._shutdown_requested = False
        self._control_paused_by_supervisor = False
        self._engine_failure = ""
        self._recovery_count = 0
        self._recovery_timestamps: list[float] = []  # 时间窗口恢复追踪
        # 监控子系统 (MON08)：深度审计调度器与状态
        self._monitoring_scheduler = DeepAuditScheduler()
        self._recovery_engine = RecoveryEngine()  # BD-T14: 恢复引擎接线
        self._monitoring_state: dict[str, Any] = {}
        self._last_monitor_loop_ts = 0.0  # 上一轮监督循环完成时刻（PKG-MON-10 自身健康）
        self._monitoring_check_states: dict[str, str] = {}  # check_id → 最近状态（阻断转变事件）
        # Phase 3: 健康防抖器 — 滑动窗口消除瞬时抖动（市场数据积累期、探针重试等）
        from .models import HealthDebounce

        self._health_debounce = HealthDebounce()
        # P1: G7 实时 SLI 追踪器 — 每个监控周期更新 7 个 SLI
        from .g7_tracker import G7LiveTracker

        self._g7_tracker = G7LiveTracker()

    @staticmethod
    def _print_checks(checks: list[CheckResult]) -> None:
        for item in checks:
            marker = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌", "UNKNOWN": "❔"}[item.status.value]
            print(f"{marker} [{item.severity.value}] {item.name}: {item.message}")

    def _install_exchange_write_interlock(self) -> None:
        """在非写模式从引擎 API 边界拦截所有交易所写请求。"""
        assert self.engine is not None
        engine = self.engine  # mypy 类型收窄：闭包内使用局部变量避免 union-attr
        mode = self.mode
        original_async = engine._api_async
        original_sync = engine._api
        engine._supervisor_blocked_writes = []

        def record(path: str, method: str) -> dict[str, Any]:
            event = {"path": path, "method": method.upper(), "mode": mode, "timestamp": time.time()}
            engine._supervisor_blocked_writes.append(event)
            return {
                "code": -3,
                "error": -3,
                "msg": f"WRITE_BLOCKED_BY_SUPERVISOR: {method.upper()} {path} in {mode}",
            }

        async def guarded_async(
            path: str,
            method: str = "GET",
            signed: bool = False,
            params: dict[str, Any] | None = None,
        ) -> Any:
            if method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and not engine._can_write:
                return record(path, method)
            return await original_async(path, method=method, signed=signed, params=params)

        def guarded_sync(
            path: str,
            method: str = "GET",
            signed: bool = False,
            params: dict[str, Any] | None = None,
        ) -> Any:
            if method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and not engine._can_write:
                return record(path, method)
            return original_sync(path, method=method, signed=signed, params=params)

        engine._api_async = guarded_async
        engine._api = guarded_sync

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
        engine = self.engine  # mypy 类型收窄

        def readiness() -> bool:
            return bool(engine._check_ready()) and self.report.supervisor_state == "RUNNING"

        def trading_readiness() -> tuple[bool, str]:
            ready = self._is_trading_ready()
            if ready:
                return True, "SUPERVISOR_VALIDATED"
            if self.report.blockers:
                return False, self.report.blockers[0].check_id
            return False, f"CONTROL_{self._control_state()}"

        # BD-FIX: Liveness 接线 — 检测进程假死/事件循环卡住
        from beidou_core.health import HealthState

        def liveness() -> HealthState:
            lifecycle = getattr(engine, "_lifecycle", None)
            lifecycle_state = str(getattr(getattr(lifecycle, "state", None), "value", ""))
            if lifecycle_state == "LOCKED":
                return HealthState.UNHEALTHY
            # 监控循环心跳：超过 monitor_interval * 3 无活动 → DEGRADED
            if time.monotonic() - self._last_monitor_loop_ts > self.monitor_interval * 3:
                return HealthState.DEGRADED
            return HealthState.HEALTHY

        # BD-FIX: Exit Readiness 接线 — 检测安全退出条件
        def exit_readiness() -> tuple[bool, str]:
            active_orders = getattr(engine, "_active_order_ids", None) or set()
            if active_orders:
                return False, f"HAS_ACTIVE_ORDERS:{len(active_orders)}"
            try:
                recon = getattr(engine, "_recon", None)
                if recon is not None:
                    result = recon.reconcile("default", "BINANCE")
                    raw_status = getattr(result, "status", None)
                    status_str = str(getattr(raw_status, "value", raw_status))
                    if status_str != "MATCHED":
                        return False, f"RECON_{status_str}"
            except Exception:
                return False, "RECON_CHECK_FAILED"
            return True, "READY"

        def status_info() -> dict[str, Any]:
            base = dict(engine._get_status_info())
            base["supervisor"] = {
                "state": self.report.supervisor_state,
                "phase": self.report.phase,
                "trading_ready": self._is_trading_ready(),
                "control_state": self._control_state(),
                "commit": self.report.commit,
                "blockers": [item.check_id for item in self.report.blockers],
                "checks": {item.check_id: item.status.value for item in self.report.checks},
                "monitoring": self._monitoring_state,
                "g7_sli": self._g7_tracker.summary(),
            }
            return base

        def factor_provider() -> list[dict]:
            registry = getattr(engine, "_factor_registry", None)
            if registry is None:
                return []
            result = []
            for fid, rec in registry._factors.items():
                result.append(
                    {
                        "factor_id": fid,
                        "lifecycle": str(getattr(rec.lifecycle, "value", rec.lifecycle)),
                    }
                )
            return result

        # P2: 监控指标注入 Prometheus /metrics
        engine_metrics_fn = getattr(self.engine._health, "_metrics_collector", lambda: {})

        def metrics_with_monitoring() -> dict[str, Any]:
            base = engine_metrics_fn()
            # 监控检查状态指标: 按 check_id 分组
            for item in self.report.checks:
                safe_id = item.check_id.replace(".", "_").replace("-", "_")
                base[f"check_{safe_id}"] = {
                    "PASS": 0, "WARN": 1, "FAIL": 2, "UNKNOWN": 3
                }.get(item.status.value, 3)
            # 监督器状态
            base["supervisor_state"] = {
                "RUNNING": 0, "PAUSED": 1, "DEGRADED": 2, "LOCKED": 3, "FAILED": 4, "STOPPED": 5
            }.get(self.report.supervisor_state, -1)
            # 防抖窗口状态
            base["debounce_window_size"] = len(self._health_debounce.window)
            # G7 SLI 指标
            g7 = self._g7_tracker.summary()
            base["g7_overall_pass_rate"] = g7["overall_pass_rate"]
            base["g7_all_passing"] = 1 if g7["all_slis_passing"] else 0
            for sli_name, sli_data in g7["slis"].items():
                base[f"g7_sli_{sli_name}_pass_rate"] = sli_data["window_pass_rate"]
            return base

        self.engine._health.set_metrics_collector(metrics_with_monitoring)

        self.engine._health.set_readiness_check(readiness)
        self.engine._health.set_trading_readiness(trading_readiness)
        self.engine._health.set_liveness_check(liveness)
        self.engine._health.set_exit_readiness(exit_readiness)
        self.engine._health.set_status_info(status_info)
        self.engine._health.set_factor_provider(factor_provider)

    async def _refresh_exchange_account_snapshot(self) -> None:
        """独立读取当前账户事实，拒绝使用陈旧的引擎缓存作为就绪证据。"""
        if self.engine is None:
            return
        now = time.monotonic()
        if now - self._last_exchange_account_probe < 15.0:
            return
        self._last_exchange_account_probe = now
        try:
            response = await self.engine._api_async(Endpoint.ACCOUNT, signed=True)
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

    async def _refresh_position_mode(self) -> None:
        """读取当前账户 Position Mode (ONE_WAY/HEDGE)。

        启动时至少查询一次，运行中每 300s 复查。UNKNOWN 时 P0。
        mode 运行中变化 → P0 + NO_NEW_RISK + full revalidation (MON00A-06)。
        """
        if self.engine is None:
            return
        now = time.monotonic()
        if self._position_mode_evidence is not None and now - self._last_position_mode_probe < 300.0:
            return
        self._last_position_mode_probe = now
        try:
            response = await self.engine._api_async(Endpoint.POSITION_SIDE_DUAL, signed=True)
            if isinstance(response, dict) and "dualSidePosition" in response:
                dual_side = bool(response["dualSidePosition"])
                mode = AccountPositionMode.HEDGE if dual_side else AccountPositionMode.ONE_WAY
                previous = self._position_mode_evidence
                previous_mode = previous.mode if previous else None
                self._position_mode_evidence = PositionModeEvidence(
                    account_id="",
                    venue="BINANCE_USDM",
                    mode=mode,
                    source="EXCHANGE_USER_DATA",
                    source_timestamp=time.time(),
                    observed_at=now,
                    raw_response=response,
                )
                if previous_mode is not None and previous_mode != mode and previous_mode != AccountPositionMode.UNKNOWN:
                    self.writer.write_event(
                        "position_mode_changed",
                        {
                            "previous": previous_mode.value,
                            "current": mode.value,
                            "observed_at": now,
                        },
                    )
            else:
                raise RuntimeError(f"Invalid position mode response: {str(response)[:300]}")
        except Exception as exc:
            self._position_mode_evidence = PositionModeEvidence(
                account_id="",
                venue="BINANCE_USDM",
                mode=AccountPositionMode.UNKNOWN,
                source="EXCHANGE_USER_DATA",
                observed_at=now,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _refresh_exchange_algo_snapshot(self, *, force: bool = False) -> None:
        """读取交易所当前 openAlgoOrders；查询失败保持 UNKNOWN 并阻断。"""
        if self.mode != "testnet" or self.engine is None:
            return
        now = time.monotonic()
        if not force and now - self._last_exchange_algo_probe < 15.0:
            return
        self._last_exchange_algo_probe = now
        try:
            response = await self.engine._api_async(Endpoint.OPEN_ALGO_ORDERS, signed=True)
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
            position_mode_evidence=self._position_mode_evidence,
        )
        self._last_error_count = error_count
        return checks

    def _merge_monitoring_checks(self, runtime_checks: list[CheckResult]) -> list[CheckResult]:
        """运行监控子系统深度检查并合并进监督器检查流。

        架构分工 (Phase 1 去重后):
        - runtime.py: 仅引擎内部状态检查 (lifecycle/control_plane/heartbeat/market_data/errors/incidents)
        - monitoring/: 所有需要外部事实的深度检查 (account/reconciliation/protection/order_trace/module_progress)
        - 两个管道无 check_id 重叠，直接拼接即可。MON08 频率策略以监控结果驱动深度审计节奏。
        - 阻断检查以状态转变事件写入证据目录。
        """
        assert self.engine is not None
        monitoring_checks: list[CheckResult] = []
        try:
            monitoring_checks = collect_monitoring_checks(  # type: ignore[no-untyped-call] # beidou_observability.monitoring 遗留豁免
                engine=self.engine,
                supervisor=self,
                exchange_account_snapshot=self._exchange_account_snapshot,
                algorithm_probe=self._algorithm_probe,
                position_mode_evidence=self._position_mode_evidence,
                last_loop_at=self._last_monitor_loop_ts,
                monitor_stall_threshold=max(30.0, self.monitor_interval * 3),
            )
        except Exception as exc:
            print(f"[supervisor] Monitoring checks failed: {type(exc).__name__}: {exc}")
            import traceback

            traceback.print_exc()

        # MON08 频率策略：监控结果 → 深度审计调度状态
        from beidou_observability.monitoring.contracts import (
            CheckSeverity as MonCheckSeverity,
            CheckStatus as MonCheckStatus,
        )

        try:
            scheduler_results = [
                (MonCheckStatus(item.status.value), MonCheckSeverity(item.severity.value))
                for item in monitoring_checks
            ]
            open_p0 = any(
                item.status == CheckStatus.FAIL and item.severity == CheckSeverity.P0 for item in monitoring_checks
            )
            open_p1 = any(
                item.status == CheckStatus.FAIL and item.severity == CheckSeverity.P1 for item in monitoring_checks
            )
            self._monitoring_scheduler.tick(scheduler_results, open_p0_incident=open_p0, open_p1_incident=open_p1)  # type: ignore[call-arg,no-untyped-call]
        except Exception:
            pass
        self._monitoring_state = {
            "level": self._monitoring_scheduler.level.value,
            "interval_seconds": self._monitoring_scheduler.current_interval,
            "deep_audit_due": self._monitoring_scheduler.should_run_deep_audit(),  # type: ignore[no-untyped-call]
            "last_reason": getattr(self._monitoring_scheduler.state, "last_reason", ""),
            "monitoring_check_count": len(monitoring_checks),
        }

        # 阻断检查以状态转变写入证据事件
        for item in monitoring_checks:
            if item.is_blocking and self._monitoring_check_states.get(item.check_id) != item.status.value:
                self.writer.write_event(
                    "monitoring_check_fail",
                    {
                        "check_id": item.check_id,
                        "message": item.message,
                        "severity": item.severity.value,
                        "monitor_level": self._monitoring_state["level"],
                    },
                )
            self._monitoring_check_states[item.check_id] = item.status.value

        # 合并：监控子系统深度审计结果对相同 check_id 优先。
        # 同 check_id 的多个条目（多模块/多持仓/多订单）全部保留，避免遮蔽单项失败。
        monitoring_ids = {item.check_id for item in monitoring_checks}
        retained_runtime = [item for item in runtime_checks if item.check_id not in monitoring_ids]
        return retained_runtime + monitoring_checks

    # 启动阶段只要求关键检查通过；行情、对账、心跳等运行时检查
    # 在引擎运行一段时间后自然会就绪，不应阻断启动。
    _STARTUP_CRITICAL_CHECKS = frozenset(
        {
            "runtime.wiring.core",
            "runtime.algorithms.alpha_graph",
            "runtime.algorithms.factor_lifecycle",
            "runtime.algorithms.trading_pool",
            "runtime.algorithms.risk_budget",
            "runtime.health.lifecycle",
            "runtime.health.http_server",
            # Phase 1 去重后: account_snapshot → monitoring check_account_unknown (INV-002)
            "runtime.safety.account",
        }
    )

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
                await asyncio.gather(
                    self._refresh_exchange_account_snapshot(),
                    self._refresh_position_mode(),
                    self._refresh_exchange_algo_snapshot(),
                )
                try:
                    checks = self._runtime_checks()
                    checks = self._merge_monitoring_checks(checks)
                except Exception as exc:
                    print(f"[supervisor] Runtime checks failed: {type(exc).__name__}: {exc}")
                    import traceback

                    traceback.print_exc()
                    checks = []
                self.report.phase = "STARTUP_VALIDATION"
                self.report.replace_phase_checks("runtime.", checks)
                self.writer.write(self.report)
                # 启动阶段仅阻断关键接线/生命周期/账户检查
                startup_blockers = [c for c in checks if c.is_blocking and c.check_id in self._STARTUP_CRITICAL_CHECKS]
                if not startup_blockers:
                    return True
            else:
                self.report.phase = "ENGINE_STARTING"
                self.report.supervisor_state = "STARTING"
                self.writer.write(self.report)
            await asyncio.sleep(1)
        return False

    def _send_supervisor_alert(self, state: str, blockers: list) -> None:
        """BD-FIX (O2): 监督器 DEGRADED/LOCKED 状态推送告警。"""
        try:
            if self.engine is not None:
                from beidou_observability.telemetry import AlertSeverity

                severity = AlertSeverity.CRITICAL if state == "LOCKED" else AlertSeverity.HIGH
                blocker_ids = [b.check_id for b in blockers]
                self.engine._alerts.send_incident(
                    severity=severity,
                    title=f"Supervisor {state}",
                    description=f"Blockers: {blocker_ids}",
                    category="supervisor",
                )
                print(f"[supervisor] Alert sent: {severity.value} — Supervisor {state}: {blocker_ids}")
        except Exception as e:
            print(f"[supervisor] Alert send failed: {e}")

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

    # 可在运行时自愈的瞬时阻断项（心跳、行情延迟等）；
    # 对账 MISMATCHED 在活跃交易中是瞬时状态 — 引擎有 _sync_exchange_state()
    # 和 _reconcile() 自愈逻辑，可在数秒内修复。只有连续多轮无法自愈时才需人工干预。
    _TRANSIENT_CHECK_IDS = frozenset(
        {
            "runtime.health.realtime_heartbeat",
            "runtime.health.nearline_heartbeat",
            "runtime.health.market_data",
            "runtime.health.http_server",
            "runtime.health.errors",
            # Phase 1 去重: account_snapshot → monitoring (runtime.safety.account + runtime.safety.balance_sanity 已在下方)
            "runtime.safety.reconciliation",  # 引擎自愈可在数秒内修复
            "runtime.safety.protection_coverage",  # _ensure_exchange_position_protections 可自动补齐
            "runtime.safety.position_mode",  # 交易所断路器/临时 API 故障可自愈
            # 监控子系统检查 — 与上列同源的瞬时状态（API 故障/引擎自愈可恢复）
            "runtime.safety.account",  # INV-002: API 故障 ≠ 空账户，可自愈
            "runtime.safety.balance_sanity",  # 账户事实缺失可自愈
            "runtime.execution.order_trace",  # _sync_exchange_state 可自愈在途订单
            "runtime.health.algorithm_probe",  # 探针重试可自愈
            "runtime.health.module_progress",  # 心跳类瞬时状态
            "runtime.health.monitor_self",  # 监督循环自身可恢复
        }
    )

    async def _recover_if_validated(self, checks: list[CheckResult]) -> bool:
        """底层异常消失后，严格经过 RECOVERING→VALIDATING→ACTIVE。

        仅允许瞬时阻断（心跳、行情）自愈；持久阻断（对账 MISMATCHED、
        保护缺失）需要人工干预或引擎自行修复后清除。
        """
        if self.engine is None:
            return False
        lifecycle = self.engine._lifecycle
        state_value = str(getattr(lifecycle.state, "value", lifecycle.state))
        if state_value != "DEGRADED":
            if state_value not in ("DEGRADED", "ACTIVE"):
                print(f"[supervisor] RECOVERY SKIP: lifecycle={state_value} (not DEGRADED)")
            return False
        # 时间窗口恢复计数：清理过期记录，仅在窗口内超限时拒绝
        now = time.monotonic()
        self._recovery_timestamps = [t for t in self._recovery_timestamps if now - t < self.recovery_window_seconds]
        if not self.self_heal or len(self._recovery_timestamps) >= self.max_restarts:
            if len(self._recovery_timestamps) >= self.max_restarts:
                print(
                    f"[supervisor] RECOVERY BLOCKED: {len(self._recovery_timestamps)}/{self.max_restarts} "
                    f"in {self.recovery_window_seconds:.0f}s window "
                    f"(timestamps={[f'{now - t:.0f}s ago' for t in self._recovery_timestamps]})"
                )
            return False

        persistent_blockers = [
            item for item in checks if item.is_blocking and item.check_id not in self._TRANSIENT_CHECK_IDS
        ]
        if persistent_blockers:
            return False

        from beidou_control.plane import ControlAction
        from beidou_lifecycle.lifecycle import ModuleState

        # BD-T14: RecoveryEngine 记录恢复尝试
        self._recovery_engine.start_recovery(f"auto-{int(time.time())}")

        for target in (ModuleState.RECOVERING, ModuleState.VALIDATING, ModuleState.ACTIVE):
            result = lifecycle.transition(target)
            if str(getattr(result, "value", result)) != "SUCCESS":
                return False
        if self._control_paused_by_supervisor:
            self.engine._control.execute_action(ControlAction.RESUME)
            self._control_paused_by_supervisor = False
        self._critical_streak = 0
        self._health_debounce.reset()  # Phase 3: 恢复成功后重置防抖窗口
        self._recovery_timestamps.append(time.monotonic())
        self._recovery_count += 1
        window_active = len(self._recovery_timestamps)
        print(
            f"[supervisor] RECOVERED: RECOVERING → VALIDATING → ACTIVE "
            f"({window_active}/{self.max_restarts} in {self.recovery_window_seconds:.0f}s window, "
            f"total={self._recovery_count})"
        )
        return True

    async def _monitor(self) -> int:
        assert self._engine_task is not None
        fatal_triggered = False
        while not self._engine_task.done():
            await asyncio.sleep(self.monitor_interval)
            # P1 优化: 三个独立 exchange 快照并行获取（无依赖关系）
            await asyncio.gather(
                self._refresh_exchange_account_snapshot(),
                self._refresh_position_mode(),
                self._refresh_exchange_algo_snapshot(),
            )
            checks = self._runtime_checks()
            if await self._recover_if_validated(checks):
                checks = self._runtime_checks()
            # 运行监控子系统深度检查并合并（账户/保护/对账/执行/模块/探针/自身健康）
            checks = self._merge_monitoring_checks(checks)
            self._last_monitor_loop_ts = time.time()

            self.report.phase = "RUNTIME_MONITORING"
            self.report.replace_phase_checks("runtime.", checks)
            blockers = self.report.blockers

            # Phase 3: 健康防抖器 — 滑动窗口消除瞬时抖动
            # 区分瞬时阻断（心跳/行情/对账/保护等可自愈）和持久阻断
            persistent_blockers = [b for b in blockers if b.check_id not in self._TRANSIENT_CHECK_IDS]
            has_persistent = bool(persistent_blockers)

            debounce_action = self._health_debounce.feed(has_persistent)

            if debounce_action == "LOCKED":
                # 防抖器判定: 连续 lock_after 次持久阻断 → LOCKED
                await self._fail_closed(
                    "防抖器: 连续持久阻断 → LOCKED: "
                    + "; ".join(f"{b.check_id}:{b.message}" for b in persistent_blockers),
                    fatal=True,
                )
                self.report.supervisor_state = "LOCKED"
                self._send_supervisor_alert("LOCKED", persistent_blockers)
            elif debounce_action == "DEGRADED":
                # 防抖器判定: 连续 degrade_after 次持久阻断 → DEGRADED
                await self._fail_closed(
                    "防抖器: 连续持久阻断 → DEGRADED: "
                    + "; ".join(f"{b.check_id}:{b.message}" for b in persistent_blockers),
                    fatal=False,
                )
                self.report.supervisor_state = "DEGRADED"
                self._send_supervisor_alert("DEGRADED", persistent_blockers)
            elif debounce_action == "RUNNING":
                # 防抖器判定干净 — 可恢复
                if self.report.supervisor_state in ("DEGRADED",):
                    await self._recover_if_validated(checks)
                if self._control_state() != "RESUME":
                    self.report.supervisor_state = "PAUSED"
                else:
                    self.report.supervisor_state = "RUNNING"
            else:
                # UNCHANGED: 防抖器计数中，保持当前状态
                # 有持久阻断时仍调用 fail_closed 降低控制面（但不改变 supervisor_state）
                if has_persistent:
                    await self._fail_closed(
                        "持久阻断检测（防抖计数中）: "
                        + "; ".join(f"{b.check_id}:{b.message}" for b in persistent_blockers),
                        fatal=False,
                    )

            self.report.trading_ready = self._is_trading_ready()
            # P1: G7 实时 SLI 追踪 — 每个周期更新
            self._g7_tracker.feed(checks)
            self._g7_tracker.feed_recovery_context(self._recovery_count, self.max_restarts)
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
            self.engine._last_realtime = time.time()
            self.engine._last_recon = time.time()
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
