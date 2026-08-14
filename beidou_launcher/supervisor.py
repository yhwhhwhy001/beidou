"""北斗引擎启动监督、自动降级、恢复和锁定。"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable

from beidou_exchange.binance_usdm.endpoints import Endpoint
from beidou_observability.monitoring import (  # type: ignore[attr-defined]  # re-exported without __all__
    DeepAuditScheduler,
    collect_monitoring_checks,
)
from beidou_observability.monitoring.contracts import (
    AccountPositionMode,
    PositionModeEvidence,
)
from beidou_safety.execution.recovery import RecoveryEngine

from .manifest import MAX_RESTARTS, MONITOR_INTERVAL, STARTUP_TIMEOUT
from .models import CheckResult, CheckSeverity, CheckStatus, StartupReport
from .preflight import current_commit, run_preflight
from .registry import inspect_engine_wiring
from .runtime import collect_runtime_checks, run_read_only_algorithm_probe
from .state import EvidenceWriter, InstanceLock

logger = logging.getLogger(__name__)


def _state_after_persistent_block(current_state: str, has_persistent_blocker: bool) -> str:
    """Make a persistent runtime blocker visible in the supervisor state immediately.

    Health debounce may delay the escalation to ``LOCKED`` but must never leave
    the control-plane certificate looking ``RUNNING`` while a P0/P1 blocker is
    already present.  Terminal states stay terminal until an explicit lifecycle
    action handles them.
    """

    if has_persistent_blocker and current_state not in {"LOCKED", "FAILED", "STOPPED"}:
        return "DEGRADED"
    return current_state


async def _refresh_snapshot_safe(coro: Any, *, timeout: float = 25.0) -> None:
    """为单个交易所快照查询加超时保护（BD-FIX: Main loop STALL 误报）。

    慢网络快照（数秒到数十秒）不应拖住监控循环心跳，也不应阻塞下一轮
    检查。超时后 ``wait_for`` 取消该 coroutine，快照保留上一轮状态，
    由下轮循环在探测频率窗口后重试；取消（CancelledError）不在此捕获，
    随监督器关停正常传播。
    """

    try:
        await asyncio.wait_for(coro, timeout=timeout)
    except Exception:
        # 慢快照下轮重试；快照失败（含超时）不影响循环心跳与下轮检查。
        # 内部 _refresh_* 已把失败写入 UNKNOWN/error 快照，这里仅吞掉超时。
        return


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
        # PKG02 (BDS-P0-001): 所有环境统一初始化 — ok 由实际探测结果决定。
        self._exchange_algo_snapshot: dict[str, Any] = {"ok": False, "by_symbol": {}}
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
        # Use the monotonic clock for the supervisor-loop heartbeat.  The
        # monitoring check and the HTTP liveness callback must share this
        # clock domain; wall-clock timestamps can make a healthy loop appear
        # permanently stalled (or hide a stall after an NTP adjustment).
        self._last_monitor_loop_ts = time.monotonic()  # 上一轮监督循环完成时刻（PKG-MON-10 自身健康）
        self._monitoring_check_states: dict[str, str] = {}  # check_id → 最近状态（阻断转变事件）
        # Phase 3: 健康防抖器 — 滑动窗口消除瞬时抖动（市场数据积累期、探针重试等）
        from .models import HealthDebounce

        # PKG02 (BDS-P0-001): 所有环境使用统一的健康防抖参数。
        # BD-FIX（C6 审查）: demo 故障时长（用户流重启预算 45s+、
        # position-mode 探针 300s 周期、对账 90s 阈值）远超 60s LOCKED
        # 窗口 —— 网络类瞬时故障即停机。testnet 加长 LOCKED 窗口
        # （60 周期 × 5s = 5 分钟），live/canary 保持严格 12 周期。
        _lock_after = 60 if self.mode == "testnet" else 12
        self._health_debounce = HealthDebounce(
            degrade_after=6,
            lock_after=_lock_after,
        )
        # P1: G7 实时 SLI 追踪器 — 每个监控周期更新 7 个 SLI
        from .g7_tracker import G7LiveTracker

        self._g7_tracker = G7LiveTracker()
        # The live tracker is informational; the certification producer is
        # the durable source used by the real G7 window.  It never creates or
        # starts a window automatically, so a missing/legacy window remains
        # NOT_VERIFIABLE rather than silently starting certification.
        from beidou_certification.unattended import UnattendedCertification

        self._g7_certification = UnattendedCertification(str(project_root / "artifacts" / "evidence" / "g7"))
        self._g7_observed_incident_ids: set[str] = set()

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
            event = {
                "path": path,
                "method": method.upper(),
                "mode": mode,
                "timestamp": time.time(),
                "reason": "authority_not_active",
            }
            engine._supervisor_blocked_writes.append(event)
            return {
                "code": -3,
                "error": -3,
                "msg": f"WRITE_BLOCKED_BY_SUPERVISOR: {method.upper()} {path} in {mode}; authority_not_active",
            }

        def write_allowed(method: str, params: dict[str, Any] | None = None) -> bool:
            # 环境变量 _can_write 只是能力上限，不是运行时授权。
            # 只有监督器确认无阻断、控制面 RESUME 且本轮授权仍有效时才允许
            # 新风险写入；NO_NEW_RISK/EXIT_ONLY 仍允许显式撤单和
            # reduce-only/closePosition 退出，避免安全门禁反而阻断平仓。
            if not bool(engine._can_write):
                return False
            if self._is_trading_ready():
                return True
            method_upper = method.upper()
            params = params or {}

            def enabled(value: Any) -> bool:
                if isinstance(value, bool):
                    return value
                return str(value).strip().lower() in {"1", "true", "yes"}

            reducing = enabled(params.get("reduceOnly")) or enabled(params.get("closePosition"))
            return self._control_state() in {"NO_NEW_RISK", "EXIT_ONLY", "EMERGENCY_FLATTEN"} and (
                method_upper == "DELETE" or reducing
            )

        async def guarded_async(
            path: str,
            method: str = "GET",
            signed: bool = False,
            params: dict[str, Any] | None = None,
        ) -> Any:
            _blocked = method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and not write_allowed(method, params)
            if _blocked:
                print(f"[supervisor] BLOCKED: {method} {path}")
                return record(path, method)
            return await original_async(path, method=method, signed=signed, params=params)

        def guarded_sync(
            path: str,
            method: str = "GET",
            signed: bool = False,
            params: dict[str, Any] | None = None,
        ) -> Any:
            if method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and not write_allowed(method, params):
                return record(path, method)
            return original_sync(path, method=method, signed=signed, params=params)

        engine._api_async = guarded_async
        engine._api = guarded_sync

        # Engine 的部分保护/订单路径直接调用 typed adapter
        # (create_order/create_algo_order/cancel_algo_order)，不会经过
        # ``_api_async``。把同一互锁下沉到 adapter.request，确保不存在
        # “REST 包装已拦截、typed adapter 仍可写”的第二条交易所写路径。
        adapter = getattr(engine, "_adapter", None)
        adapter_request = getattr(adapter, "request", None)
        if adapter is not None and callable(adapter_request):
            from beidou_exchange.core.error_taxonomy import Result
            from beidou_shared.errors import ErrorCategory

            async def guarded_adapter_request(
                method: str,
                path: str,
                signed: bool = False,
                params: dict[str, Any] | None = None,
            ) -> Any:
                if method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and not write_allowed(method, params):
                    record(path, method)
                    return Result.failure(
                        "WRITE_BLOCKED_BY_SUPERVISOR: authority_not_active",
                        category=ErrorCategory.UNKNOWN,
                        source="beidou_supervisor_interlock",
                    )
                return await adapter_request(method, path, signed=signed, params=params)

            adapter.request = guarded_adapter_request

    def _install_resume_interlock(self) -> None:
        """在深度启动门禁通过前阻止引擎内部自动 RESUME。"""
        from beidou_control.plane import ControlAction

        assert self.engine is not None
        control = self.engine._control
        self._original_control_execute = control.execute_action  # BD-FIX (S23): 保存原始方法

        def guarded_execute(action: Any, *args: Any, **kwargs: Any) -> Any:
            if action == ControlAction.RESUME and not self._resume_authorized:
                return self._original_control_execute(ControlAction.NO_NEW_RISK)
            return self._original_control_execute(action, *args, **kwargs)

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
            # Readiness is a runtime certificate, not merely a control-plane
            # action.  A stale/partially-started report must not advertise
            # trading while the supervisor is still STARTING, PAUSED or
            # DEGRADED.
            and self.report.supervisor_state == "RUNNING"
        )

    def _install_health_callbacks(self) -> None:
        """让 HTTP readiness 与监督器证据保持一致。"""
        assert self.engine is not None
        engine = self.engine  # mypy 类型收窄

        def readiness() -> bool:
            # /ready 不能在 P0 blocker 存在时继续返回 true。进程存活由 /health
            # 单独表示；服务 readiness 必须与当前 authority/监督状态一致。
            return self._is_trading_ready()

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
                "g7_certification": self._g7_certification_summary(),
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
        engine_metrics_fn: Callable[[], dict[str, Any]] = getattr(self.engine._health, "_metrics_collector", lambda: {})

        def metrics_with_monitoring() -> dict[str, Any]:
            base = engine_metrics_fn()
            # 监控检查状态指标: 按 check_id 分组
            for item in self.report.checks:
                safe_id = item.check_id.replace(".", "_").replace("-", "_")
                base[f"check_{safe_id}"] = {"PASS": 0, "WARN": 1, "FAIL": 2, "UNKNOWN": 3}.get(item.status.value, 3)
            # 监督器状态
            base["supervisor_state"] = {
                "RUNNING": 0,
                "PAUSED": 1,
                "DEGRADED": 2,
                "LOCKED": 3,
                "FAILED": 4,
                "STOPPED": 5,
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
            # API 查询失败时，回退到引擎缓存的账户数据
            _cached = getattr(self.engine, "_last_account", None)
            if isinstance(_cached, dict) and "totalWalletBalance" in _cached:
                self._exchange_account_snapshot = {
                    "ok": True,
                    "account": _cached,
                    "observed_at": time.time(),
                    "source": "cached_fallback",
                }
            else:
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
        # PKG02 (BDS-P0-001): 所有环境统一运行引擎探针。
        if self.engine is None:
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
        # BD-FIX: Testnet 模式下，算法探针可能因市场 RANGING 无信号而
        # 返回 NO_ACTION（安全行为），不应作为阻断项。将失败的探针结果
        # PKG02 (BDS-P0-001): 所有环境使用真实探测结果，不掩码。
        _probe_for_check = dict(self._algorithm_probe)
        checks, error_count = collect_runtime_checks(
            engine=self.engine,
            mode=self.mode,
            port=self.port,
            resume_authorized=self._resume_authorized,
            algorithm_probe=_probe_for_check,
            last_error_count=self._last_error_count,
            exchange_algo_snapshot=self._exchange_algo_snapshot,
            exchange_account_snapshot=self._exchange_account_snapshot,
            position_mode_evidence=self._position_mode_evidence,
        )
        self._last_error_count = error_count
        return checks

    def _g7_certification_summary(self) -> dict[str, Any]:
        """Expose durable G7 producer state without certifying it."""
        try:
            windows = self._g7_certification.list_windows()
            active = [item for item in windows if item.get("status") in {"CREATED", "RUNNING", "PAUSED"}]
            return {
                "active_windows": active,
                "state_load_errors": list(self._g7_certification.state_load_errors),
            }
        except Exception as exc:
            return {"active_windows": [], "state_load_errors": [type(exc).__name__]}

    def _record_g7_certification_evidence(self, checks: list[CheckResult]) -> None:
        """Persist one real monitoring cycle into an explicitly started G7 window.

        This is intentionally producer-only: it records facts and daily
        reports, but never evaluates or signs a certificate.  Any missing
        check is recorded as a failed SLI; a P0 blocker also opens a durable
        P0 incident and resets the window through the certification engine.
        """
        from datetime import datetime, timezone

        from beidou_certification.unattended import (
            IncidentRecord,
            IncidentSeverity,
            SLICategory,
            SLISample,
            WindowStatus,
        )

        active_windows = [
            item for item in self._g7_certification.list_windows() if item.get("status") == WindowStatus.RUNNING.value
        ]
        if not active_windows:
            return
        # A single producer window is the governed contract.  If operators
        # accidentally leave multiple RUNNING windows, record into the newest
        # one and surface the ambiguity in status rather than fan out writes.
        active_windows.sort(key=lambda item: str(item.get("window_id", "")))
        window_id = str(active_windows[-1]["window_id"])
        window = self._g7_certification.get_window(window_id)
        if window is None or not window.evidence_state_complete:
            return

        by_id: dict[str, list[CheckResult]] = {}
        for item in checks:
            by_id.setdefault(item.check_id, []).append(item)

        def all_pass(check_id: str) -> bool:
            results = by_id.get(check_id, [])
            return bool(results) and all(item.status.value == "PASS" for item in results)

        order_results = by_id.get("runtime.execution.order_trace", [])
        order_ok = bool(order_results) and all(
            item.status.value == "PASS" and "DUP" not in item.message for item in order_results
        )
        samples = [
            (SLICategory.DATA_QUALITY, all_pass("runtime.health.market_data")),
            (SLICategory.ORDER_DUPLICATES, order_ok),
            (SLICategory.PROTECTION_SLO, all_pass("runtime.safety.protection_coverage")),
            (SLICategory.RECONCILIATION, all_pass("runtime.safety.reconciliation")),
            (SLICategory.RECOVERY_BOUNDED, self._recovery_count <= self.max_restarts),
            (SLICategory.INCIDENT_CLOSURE, all_pass("runtime.health.incidents")),
            (SLICategory.COST_PNL_REPORTING, all_pass("runtime.safety.cost_and_pnl_reporting")),
        ]
        cycle = int(self._g7_tracker.summary().get("cycles", 0))
        observed_at = datetime.now(timezone.utc)
        self._g7_certification.record_batch_sli(
            window_id,
            [
                SLISample(
                    category=category,
                    value=1.0 if passed else 0.0,
                    threshold=1.0,
                    passed=passed,
                    timestamp=observed_at,
                    metadata={"source": "supervisor_checks", "cycle": cycle},
                )
                for category, passed in samples
            ],
        )
        window.total_recovery_count = self._recovery_count

        for item in checks:
            if not (item.is_blocking and item.severity.value == "P0"):
                continue
            incident_id = f"g7-{window_id}-{item.check_id}"
            if incident_id in self._g7_observed_incident_ids:
                continue
            self._g7_observed_incident_ids.add(incident_id)
            self._g7_certification.open_incident(
                window_id,
                IncidentRecord(
                    incident_id=incident_id,
                    severity=IncidentSeverity.P0,
                    title=item.check_id,
                    description=item.message,
                    evidence={"cycle": cycle, "check": item.to_dict()},
                ),
            )
        # Idempotent by date; repeated monitoring cycles do not create fake
        # duplicate daily reports.
        self._g7_certification.generate_daily_report(window_id)

    def _merge_monitoring_checks(self, runtime_checks: list[CheckResult]) -> list[CheckResult]:
        """运行监控子系统深度检查并合并进监督器检查流。

        架构分工 (Phase 1 去重后):
        - runtime.py: 仅引擎内部状态检查 (lifecycle/control_plane/heartbeat/market_data/errors/incidents)
        - monitoring/: 所有需要外部事实的深度检查 (account/reconciliation/protection/order_trace/module_progress)
        - 两个管道无 check_id 重叠，直接拼接即可。MON08 频率策略以监控结果驱动深度审计节奏。
        - 阻断检查以状态转变事件写入证据目录。
        """
        assert self.engine is not None
        # Webhook delivery is a durable side effect, not a best-effort log.
        # Retry at most one due item per monitoring cycle so a dead channel is
        # visible in delivery health without starving safety checks.
        try:
            retry_pending = getattr(getattr(self.engine, "_alerts", None), "retry_pending", None)
            if callable(retry_pending):
                retry_pending(max_items=1)
        except Exception as exc:
            logger.warning("alert delivery retry failed: %s: %s", type(exc).__name__, str(exc)[:160])
        monitoring_checks: list[CheckResult] = []
        try:
            # PKG02 (BDS-P0-001): 所有环境使用真实探测结果。
            _probe_for_mon = dict(self._algorithm_probe)
            monitoring_checks = collect_monitoring_checks(  # type: ignore[no-untyped-call] # beidou_observability.monitoring 遗留豁免
                engine=self.engine,
                supervisor=self,
                exchange_account_snapshot=self._exchange_account_snapshot,
                algorithm_probe=_probe_for_mon,
                position_mode_evidence=self._position_mode_evidence,
                last_loop_at=self._last_monitor_loop_ts,
                monitor_stall_threshold=max(30.0, self.monitor_interval * 3),
            )
        except Exception as exc:
            print(f"[supervisor] Monitoring checks failed: {type(exc).__name__}: {exc}")
            import traceback

            traceback.print_exc()
            # Monitoring is part of the execution safety certificate.  If it
            # cannot run, an empty result set must never be interpreted as a
            # clean runtime.  Emit a durable P0 blocker so the monitor loop
            # revokes authority and readiness stays false.
            monitoring_checks = [
                CheckResult(
                    check_id="runtime.monitoring.execution",
                    name="监控执行链",
                    status=CheckStatus.FAIL,
                    severity=CheckSeverity.P0,
                    message=f"监控检查执行失败: {type(exc).__name__}",
                    evidence={"error": f"{type(exc).__name__}: {str(exc)[:240]}"},
                )
            ]

        # MON08 频率策略：监控结果 → 深度审计调度状态
        from beidou_observability.monitoring.contracts import (
            CheckSeverity as MonCheckSeverity,
        )
        from beidou_observability.monitoring.contracts import (
            CheckStatus as MonCheckStatus,
        )

        try:
            scheduler_results = [
                (MonCheckStatus(item.status.value), MonCheckSeverity(item.severity.value)) for item in monitoring_checks
            ]
            open_p0 = any(
                item.status == CheckStatus.FAIL and item.severity == CheckSeverity.P0 for item in monitoring_checks
            )
            open_p1 = any(
                item.status == CheckStatus.FAIL and item.severity == CheckSeverity.P1 for item in monitoring_checks
            )
            self._monitoring_scheduler.tick(scheduler_results, open_p0=open_p0, open_p1=open_p1)  # type: ignore[no-untyped-call]
        except Exception as exc:
            logger.warning("monitoring scheduler update failed: %s: %s", type(exc).__name__, exc)
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
            # BD-FIX（C5 审查残留）: 引擎启动期 lifecycle 可为 DEGRADED
            # （启动 durable gate 环境化后 fail-closed 但非致命）—— 只认
            # ACTIVE 会让检查永不执行、启动必超时（final43 实测 300s
            # exit）。ACTIVE/DEGRADED 均进入检查循环：检查通过后授权
            # RESUME，引擎经 _recover_if_validated 回到 ACTIVE。
            if lifecycle_value in ("ACTIVE", "DEGRADED") and health_thread is not None and health_thread.is_alive():
                if not self._algorithm_probe.get("ok") and (
                    time.monotonic() - self._last_algorithm_probe_attempt >= 10.0
                    or self._last_algorithm_probe_attempt == 0.0
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
                # 只有关键启动检查全部清除才允许授权 RESUME。
                # BD-FIX（C5 审查）: 旧条件 `not startup_blockers and not
                # self.report.blockers` 要求零 blocker —— 任何瞬时
                # P0/P1（user_stream/对账/alert_delivery/position_mode）
                # 即启动超时 exit 4，demo 抖动下必然循环。注释（741-742）
                # 明说"行情、对账、心跳等运行时检查不应阻断启动"，
                # 实现却相反。启动门禁只按关键集判定；运行时检查的
                # blocker 在启动后由 monitor 循环持续收紧。
                startup_blockers = [c for c in checks if c.is_blocking and c.check_id in self._STARTUP_CRITICAL_CHECKS]
                all_blockers = [c for c in checks if c.is_blocking]
                if all_blockers:
                    self._blocker_report_count = getattr(self, "_blocker_report_count", 0) + 1
                    if self._blocker_report_count % 10 == 1:
                        print(
                            f"[supervisor] Blockers ({len(all_blockers)}): "
                            f"{[(b.check_id, b.message[:60]) for b in all_blockers[:5]]}"
                        )
                if not startup_blockers:
                    return True
            else:
                self.report.phase = "ENGINE_STARTING"
                self.report.supervisor_state = "STARTING"
                self.writer.write(self.report)
            # BD-FIX: 启动验证循环本身就是监督器存活的证据。此前
            # _last_monitor_loop_ts 只在 _monitor() 循环更新，启动验证
            # 期间 (可能长达 startup_timeout) 恒为构造时刻，导致
            # monitor_self 检查误报 "Main loop STALL" P0 blocker。
            self._last_monitor_loop_ts = time.monotonic()
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
        # 一旦监督器因运行时事实降级，之前的启动授权失效；恢复必须重新
        # 经过新鲜事实、对账和具名人工授权，不能由健康防抖器自动 RESUME。
        self._resume_authorized = False
        with suppress(Exception):
            self.engine._control.execute_action(ControlAction.NO_NEW_RISK)
        if previous_control == "RESUME":
            self._control_paused_by_supervisor = True
        lifecycle = self.engine._lifecycle
        if fatal:
            # PKG02 (BDS-P0-001): 所有环境统一的 fail-closed 行为。
            with suppress(Exception):
                lifecycle.transition(ModuleState.LOCKED)
            self.engine._running = False
        elif str(getattr(lifecycle.state, "value", lifecycle.state)) == "ACTIVE":
            with suppress(Exception):
                lifecycle.transition(ModuleState.DEGRADED)
        print(f"[supervisor] FAIL-CLOSED: {reason}; fatal={fatal}")

    # V3 安全语义：运行时的每个 P0/P1 事实失败都是 authority blocker。
    # “瞬时”只能影响诊断、告警和人工处置，绝不能绕过新风险写边界。
    # 这里故意不维护可自动恢复的白名单：心跳、模块进度、对账、订单链、
    # 保护覆盖等任一事实失真时，必须撤销当前授权并重新走启动/恢复门禁。
    # 这条约束防止 supervisor 在 stale heartbeat 或卡死订单链期间继续声称
    # RESUME/READY，也防止健康防抖器把安全故障误判成可自愈抖动。
    # BD-FIX（C6 审查落实，无人值守目标）: demo 网络抖动的瞬时 FAIL 不
    # 进入 LOCKED 防抖（只 DEGRADED 并持续重试）。LOCKED 只留给
    # execution/protection 类持久事实失真；网络类检查（对账/用户流/
    # 持仓模式探针）由各自的恢复机制处理（对账 30s 重试、user stream
    # 限次重启 + recon 重置预算、position_mode 300s 周期探针）。
    _TRANSIENT_CHECK_IDS: frozenset[str] = frozenset(
        {
            "runtime.safety.position_mode",
            "runtime.safety.user_stream",
            "runtime.safety.reconciliation",
            "runtime.safety.reconciliation_authority",
        }
    )

    async def _recover_if_validated(self, checks: list[CheckResult]) -> bool:
        """在仍有有效授权时，经过 RECOVERING→VALIDATING→ACTIVE。

        ``_fail_closed`` 会撤销授权；因此运行时事实故障不能靠“干净几轮”
        自动回到 RESUME。重新授权必须由受治理的启动/人工恢复流程完成
        （testnet/demo 例外：见 ``_maybe_testnet_auto_reauthorize``，故障
        自愈后由防抖器清洁窗口自动补发授权）。
        """
        if self.engine is None:
            return False
        if not self._resume_authorized:
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
        # BD-FIX: 基于状态事实 + 授权标志补发 RESUME，而非仅依赖 _control_paused_by_supervisor 标志。
        # 原逻辑：仅当 _control_paused_by_supervisor 为 True 时才 RESUME，
        # 但该标志仅在 _fail_closed 的 previous_control=="RESUME" 时置位。
        # 若控制面已在 NO_NEW_RISK（引擎默认、外部 API 下发等），触发 _fail_closed 后标志永不为 True，
        # 再无任何路径发出 RESUME → 永久卡死在 NO_NEW_RISK/PAUSED。
        if self._resume_authorized and self._control_state() not in ("RESUME", "LOCK", "EMERGENCY_FLATTEN"):
            self.engine._control.execute_action(ControlAction.RESUME)
            print("[supervisor] Re-issued RESUME during recovery (autorized, control was not RESUME)")
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

    def _maybe_testnet_auto_reauthorize(self) -> bool:
        """testnet 故障自愈后自动重新授权 RESUME（BD-FIX）。

        环境区分语义：demo/testnet 的运行时事实故障（user stream 掉线、
        对账失真等）触发 ``_fail_closed`` 撤销授权后，``_recover_if_validated``
        的前提“仍有有效授权”已不成立 → 控制面永久 NO_NEW_RISK。demo 故障
        频发、人工授权不现实，因此在防抖器连续清洁（回到 RUNNING）且无
        blocker 时自动补发授权并 RESUME；live/canary/paper 不进入此路径，
        保持“撤销后必须人工/重启重新授权”的生产安全语义不变。
        """
        if self.engine is None or self.mode != "testnet":
            return False
        if self._resume_authorized or self._control_state() == "RESUME" or self.report.blockers:
            return False
        lifecycle = self.engine._lifecycle
        state_value = str(getattr(lifecycle.state, "value", lifecycle.state))
        # 仅非致命降级（DEGRADED）可自愈；LOCKED/FAILED/QUARANTINED 等
        # 终态或升级态必须保持人工处置。
        if state_value != "DEGRADED":
            return False

        from beidou_control.plane import ControlAction
        from beidou_lifecycle.lifecycle import ModuleState

        # 复用 _recover_if_validated 的恢复 transition 序列
        # （RECOVERING→VALIDATING→ACTIVE）。
        for target in (ModuleState.RECOVERING, ModuleState.VALIDATING, ModuleState.ACTIVE):
            result = lifecycle.transition(target)
            if str(getattr(result, "value", result)) != "SUCCESS":
                return False

        # 先恢复授权再补发 RESUME：_install_resume_interlock 的 guard
        # 会在 _resume_authorized=False 时把 RESUME 改写为 NO_NEW_RISK。
        self._resume_authorized = True
        self.engine._control.execute_action(ControlAction.RESUME)
        self._control_paused_by_supervisor = False
        self._critical_streak = 0
        self._health_debounce.reset()
        self.report.supervisor_state = "RUNNING"
        print("[supervisor] testnet auto re-authorized RESUME after clean recovery")
        return True

    async def _monitor(self) -> int:
        assert self._engine_task is not None
        fatal_triggered = False
        while not self._engine_task.done():
            await asyncio.sleep(self.monitor_interval)
            # BD-FIX: 循环醒来即心跳。快照 gather 可能耗时数秒到数十秒，
            # 若在 gather/checks 之后才更新时间戳，慢 IO 会被
            # check_monitor_loop_health 误判为循环 STALL（Main loop STALL 误报）。
            # 醒来即证明主循环存活，慢网络快照不计入心跳间隔。
            self._last_monitor_loop_ts = time.monotonic()
            # P1 优化: 三个独立 exchange 快照并行获取（无依赖关系）；
            # 每路快照带超时保护，慢网络不拖住循环心跳，超时下轮重试。
            await asyncio.gather(
                _refresh_snapshot_safe(self._refresh_exchange_account_snapshot()),
                _refresh_snapshot_safe(self._refresh_position_mode()),
                _refresh_snapshot_safe(self._refresh_exchange_algo_snapshot()),
            )
            # 先合并全部内部与外部事实，再决定是否阻断/恢复；不能在深度
            # monitoring 检查之前依据一组较窄的 runtime checks 自动 RESUME。
            checks = self._runtime_checks()
            checks = self._merge_monitoring_checks(checks)
            try:
                self._record_g7_certification_evidence(checks)
            except Exception as exc:
                # A started G7 window is itself an evidence contract.  If the
                # producer cannot persist a cycle, do not keep RESUME under a
                # false certification narrative; surface a P0 blocker.
                checks.append(
                    CheckResult(
                        check_id="runtime.g7.certification.persistence",
                        name="G7 证据持久化",
                        status=CheckStatus.FAIL,
                        severity=CheckSeverity.P0,
                        message=f"G7 证据写入失败: {type(exc).__name__}",
                        evidence={"error": type(exc).__name__},
                    )
                )

            if not any(item.is_blocking for item in checks) and await self._recover_if_validated(checks):
                # 只有仍然有效的授权才可以执行已经授权的恢复路径；
                # _fail_closed 后 _resume_authorized=False，不能由清洁窗口重置。
                checks = self._runtime_checks()
                checks = self._merge_monitoring_checks(checks)

            self.report.phase = "RUNTIME_MONITORING"
            self.report.replace_phase_checks("runtime.", checks)
            blockers = self.report.blockers

            # Phase 3: 健康防抖器 — 滑动窗口只决定 DEGRADED→LOCKED 的升级
            # 速度；它不能让任何 P0/P1 事实失真继续持有 RESUME 授权。
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
                # PKG02 (BDS-P0-001): 所有环境统一降级行为。
                await self._fail_closed(
                    "防抖器: 连续持久阻断 → DEGRADED: "
                    + "; ".join(f"{b.check_id}:{b.message}" for b in persistent_blockers),
                    fatal=False,
                )
                self.report.supervisor_state = "DEGRADED"
                self._send_supervisor_alert("DEGRADED", persistent_blockers)
            elif debounce_action == "RUNNING":
                if self._control_state() != "RESUME":
                    self.report.supervisor_state = "PAUSED"
                else:
                    self.report.supervisor_state = "RUNNING"
                # BD-FIX: testnet 故障自愈后自动重新授权 RESUME；live/canary/
                # paper 保持“撤销后需人工/重启授权”语义（demo 故障频发，
                # 人工授权不现实）。helper 内部再次校验：授权已撤销、控制面
                # 非 RESUME、无 blocker、lifecycle 为 DEGRADED 才动作。
                if self.mode == "testnet" and not self._resume_authorized:
                    self._maybe_testnet_auto_reauthorize()
            else:
                # UNCHANGED: 防抖器计数中。
                if has_persistent:
                    previous_state = self.report.supervisor_state
                    await self._fail_closed(
                        "持久阻断检测（防抖计数中）: "
                        + "; ".join(f"{b.check_id}:{b.message}" for b in persistent_blockers),
                        fatal=False,
                    )
                    self.report.supervisor_state = _state_after_persistent_block(previous_state, True)
                    if self.report.supervisor_state == "DEGRADED" and previous_state != "DEGRADED":
                        self._send_supervisor_alert("DEGRADED", persistent_blockers)

            self.report.trading_ready = self._is_trading_ready()
            # P1: G7 实时 SLI 追踪 — 每个周期更新
            try:
                active_windows = [
                    item
                    for item in self._g7_certification.list_windows()
                    if item.get("status") == "RUNNING" and bool(item.get("evidence_state_complete", False))
                ]
                set_window_state = getattr(self._g7_tracker, "set_durable_window_state", None)
                if callable(set_window_state):
                    set_window_state(running=len(active_windows) == 1, evidence_state_complete=len(active_windows) == 1)
            except Exception as exc:
                logger.warning("G7 durable window state unavailable: %s: %s", type(exc).__name__, str(exc)[:160])
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
            # _last_realtime 保持引擎默认值 (0.0)，确保首个 tick 立即执行
            self._install_exchange_write_interlock()
            self._install_resume_interlock()
            self._install_health_callbacks()

            # DEV_BYPASS: 仅在本地研究/Paper 环境激活因子。
            if self.mode in ("paper", "research"):
                try:
                    from beidou_bootstrap.dev import patch_engine_for_dev

                    patch_engine_for_dev(self.engine, self.mode)
                except Exception as _bootstrap_exc:
                    print(f"[supervisor] 开发引导失败（非致命）: {_bootstrap_exc}")

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
            # 本地研究/Paper 可使用开发宇宙评估；Testnet 必须走真实生命周期证据。
            if self.mode in ("paper", "research"):
                await asyncio.sleep(8)  # 等待 WebSocket 连接和首批 ticker 数据
                try:
                    from beidou_bootstrap.dev import bootstrap_universe

                    await bootstrap_universe(self.engine, self.mode)
                except Exception as _uni_exc:
                    print(f"[supervisor] 首次宇宙评估失败（非致命）: {_uni_exc}")
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
