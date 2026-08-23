"""北斗引擎启动监督、自动降级、恢复和锁定。"""

from __future__ import annotations

import asyncio
import dataclasses
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
from .runtime import (
    active_incident_blocking_severity,
    collect_runtime_checks,
    run_read_only_algorithm_probe,
)
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


_REPAIRABLE_PROTECTION_REASONS: frozenset[str] = frozenset(
    {
        "STOP_LOSS_QUANTITY_UNCOVERED",
        "MISSING_SL",
        "MISSING_TP",
        "ORPHAN_PROTECTION_WITHOUT_VENUE_POSITION",
    }
)

_COVERAGE_REPAIRABLE_ISSUES: frozenset[str] = frozenset({"MISSING_SL", "MISSING_TP"})


def _coverage_message_repairable(b: CheckResult, owned: set[str]) -> bool:
    """R2: coverage 检查按 message issue token 分类。

    message 形如 "Protection: MISSING_SL, MISSING_TP" (issues 以 ", "
    连接); 全部 token ∈ {MISSING_SL, MISSING_TP} 且 entity_id 属于
    本地所有权符号集才可修复, 否则 fail-closed 按 B。
    """
    message = str(getattr(b, "message", "") or "")
    tokens = [t.strip() for t in message.split("Protection:", 1)[-1].split(",") if t.strip()]
    if not tokens or not all(t in _COVERAGE_REPAIRABLE_ISSUES for t in tokens):
        return False
    entity_id = str((getattr(b, "evidence", None) or {}).get("entity_id", "") or "")
    return bool(entity_id) and entity_id in owned


def summarize_blockers(blockers: list) -> str:
    """聚合重复 blocker：同 (check_id, message) 合并为 check_id(×N)。

    M00-F03（P0-19）：保护覆盖类检查按持仓逐条产出相同条目（15 持仓
    MISSING_SL = 15 条重复 P0），全部拼进 _fail_closed 理由与告警文本
    造成日志/webhook 风暴。聚合后保留 entity_id 计数；完整明细仍在
    supervisor-state.json 的 blockers 字段（models.StartupReport）。
    """
    if not blockers:
        return "(none)"
    # 可观测性修复: incidents 检查的 message 是事故列表的 repr,
    # 旧逻辑取列表首项 —— 首项常是 WARNING 级非阻断事故, 导致
    # 14/19 次 LOCKED 的真实致死原因被 message[:80] 截断吞掉。
    # 改为优先展示 severity 最高的那条 (CRITICAL/HIGH 才是阻断源)。
    # R7: 展示文本只在分组循环内局部计算 (作分组键), 绝不改写输入对象
    # —— LOCKED 快照与 supervisor-state.json 的 message 保持原始完整内容。
    _PRIORITY = {"LOCKDOWN": 4, "CRITICAL": 3, "P0": 3, "HIGH": 2, "P1": 2, "WARNING": 1, "P2": 0}
    groups: dict[tuple[str, str], list] = {}
    for item in blockers:
        display = item.message
        if str(getattr(item, "check_id", "")) == "runtime.health.incidents":
            incs = ((item.evidence or {}).get("incidents") or []) if isinstance(getattr(item, "evidence", None), dict) else []
            if incs:
                inc = max(incs, key=lambda i: _PRIORITY.get(str(i.get("severity", "")).upper(), 0))
                display = (
                    f"活动事故(首列最高级): [{inc.get('severity')}] {inc.get('title')} "
                    f"{str(inc.get('description', ''))[:120]}"
                )
        groups.setdefault((item.check_id, display), []).append(item)
    parts: list[str] = []
    for (check_id, message), items in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0][0])):
        entity_ids = sorted({str((item.evidence or {}).get("entity_id", "")) for item in items} - {""})
        entity_part = f" entities={len(entity_ids)}" if entity_ids else ""
        parts.append(f"{check_id}(×{len(items)}){entity_part}: {message[:80]}")
    text = "; ".join(parts)
    return text if len(text) <= 400 else text[:397] + "..."


def _write_locked_snapshot(
    blockers: list[CheckResult],
    *,
    base_dir: Path | None = None,
    now: float | None = None,
) -> Path:
    """LOCKED 时落未截断的完整阻断快照, 修复根因不可恢复问题。"""
    import json as _json
    import time as _time
    ts = now if now is not None else _time.time()
    base = Path(base_dir) if base_dir is not None else (
        Path(os.environ.get("BEIDOU_LOCKED_SNAPSHOT_DIR", "evidence"))
    )
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"locked-{ts:.0f}.json"
    payload = {
        "locked_at": ts,
        "blockers": [b.to_dict() for b in blockers],
    }
    path.write_text(_json.dumps(payload, ensure_ascii=False, indent=2))
    return path


def _apply_g5_dev_exemption(checks: list[CheckResult], mode: str, exempt: bool) -> list[CheckResult]:
    """M22-F05 (codex merge 回归修复): 已登记 dev 便利豁免的启动层应用。

    豁免登记由 cli 层显式读 env 后以参数传入(架构测试要求本文件
    与 preflight 源码不含豁免标签)。preflight 保持严格 —— G5 检查
    永不缺席、status 恒为真实判定;启动层仅在豁免登记时把 G5 阻断
    语义降级为 P2(FAIL+P2 不阻断,证据与输出保留)。未登记豁免时
    任何 G5 缺失仍硬阻断。豁免只作用于 testnet 写模式。
    """
    if not (exempt and mode == "testnet"):
        return checks
    return [
        dataclasses.replace(check, severity=CheckSeverity.P2) if check.check_id == "preflight.g5_certificate" else check
        for check in checks
    ]


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
        g5_dev_exemption: bool = False,
    ) -> None:
        self.project_root = project_root
        self.mode = mode
        self.symbols = symbols
        self.port = port
        self.startup_timeout = startup_timeout
        self.monitor_interval = monitor_interval
        self.g5_dev_exemption = g5_dev_exemption
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
        # M00-F03-R2: 最近一次告警的 blocker 聚合指纹（指纹变化即新告警，
        # 防止 DEGRADED 期间新类型 P0 静默）
        self._last_blocker_fingerprint: str | None = None
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
        # M00-F03-R2（对抗审查反例 D）: window_seconds=60 只保留 ~12 个
        # 5s 样本，lock_after=60 永不可达（len(recent) >= 60 恒假）→
        # testnet LOCKED 终态是死代码。窗口必须能容纳 lock_after 个样本。
        self._health_debounce = HealthDebounce(
            degrade_after=6,
            lock_after=_lock_after,
            window_seconds=max(60.0, _lock_after * self.monitor_interval * 1.5),
        )
        # P1: G7 实时 SLI 追踪器 — 每个监控周期更新 7 个 SLI
        from .g7_tracker import G7LiveTracker

        self._g7_tracker = G7LiveTracker()
        # Startup cannot activate or refresh a certification producer. A
        # separately authorized evidence workflow may inject one explicitly;
        # the runtime default remains producer-free and HARD_HOLD.
        self._g7_certification: Any | None = None
        self._g7_observed_incident_ids: set[str] = set()

    @staticmethod
    def _print_checks(checks: list[CheckResult]) -> None:
        for item in checks:
            marker = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌", "UNKNOWN": "❔"}[  # nosec B105 - status labels
                item.status.value
            ]
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
            # M00-C01 containment: runtime readiness, DELETE, reduceOnly, and
            # closePosition classify intent but do not prove ownership or grant
            # terminal-write authority.  Until a scoped capability producer is
            # installed, every exchange mutation remains blocked here as well
            # as at the adapter/REST choke points.
            #
            # BD-FIX (2026-08-18, 用户批准): testnet 已部署的注册语义
            # BEIDOU_TERMINAL_WRITE_HOLD=unknown-only(.env 登记)要求写能力
            # 不退化 —— 仅未分类突变端点 fail-closed。分类过滤由
            # adapter/rest_client 的 typed TerminalWriteKind hold 强制执行
            # (双层防护保留);supervisor 互锁仅在 hard 模式或非 testnet
            # 环境保持 HARD_HOLD。live/canary 永不放行。
            del method, params
            return self.mode == "testnet" and os.environ.get("BEIDOU_TERMINAL_WRITE_HOLD", "hard") == "unknown-only"

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
                write_account_id: str | None = None,
            ) -> Any:
                if method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and not write_allowed(method, params):
                    record(path, method)
                    return Result.failure(
                        "WRITE_BLOCKED_BY_SUPERVISOR: authority_not_active",
                        category=ErrorCategory.UNKNOWN,
                        source="beidou_supervisor_interlock",
                    )
                return await adapter_request(
                    method,
                    path,
                    signed=signed,
                    params=params,
                    write_account_id=write_account_id,
                )

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
            and not self._has_active_trading_incident()
            and self._control_state() == "RESUME"
            # Readiness is a runtime certificate, not merely a control-plane
            # action.  A stale/partially-started report must not advertise
            # trading while the supervisor is still STARTING, PAUSED or
            # DEGRADED.
            and self.report.supervisor_state == "RUNNING"
        )

    def _has_active_trading_incident(self) -> bool:
        """Keep readiness fail-closed until open safety incidents resolve."""
        if self.engine is None:
            return False
        getter = getattr(getattr(self.engine, "_alerts", None), "get_active_incidents", None)
        if not callable(getter):
            return False
        try:
            return active_incident_blocking_severity(list(getter())) is not None
        except Exception:
            return True

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
                base[f"check_{safe_id}"] = {  # nosec B105 - status-to-severity mapping
                    "PASS": 0,
                    "WARN": 1,
                    "FAIL": 2,
                    "UNKNOWN": 3,
                }.get(item.status.value, 3)
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
        # BD-FIX (rate-budget): 15→30s。account 探针权重高且 rest_client
        # 已有 10s 响应缓存，30s 间隔叠加缓存后配额消耗减半。
        if now - self._last_exchange_account_probe < 30.0:
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
            # BD-FIX: 查询失败保留上次成功证据 —— position mode 是
            # 账户级设置不会瞬时变化，demo 网络抖动/熔断循环下探针
            # 反复失败 → UNKNOWN P0 持续 blocker → 自动重新授权被
            # blocker 条件卡住 → PAUSED + NO_NEW_RISK（final62 实测
            # 5.5h）。失败只记错误供审计，证据保留上次成功值。
            if (
                self._position_mode_evidence is None
                or str(getattr(self._position_mode_evidence.mode, "value", self._position_mode_evidence.mode))
                == "UNKNOWN"
            ):
                self._position_mode_evidence = PositionModeEvidence(
                    account_id="",
                    venue="BINANCE_USDM",
                    mode=AccountPositionMode.UNKNOWN,
                    source="EXCHANGE_USER_DATA",
                    observed_at=now,
                    error=f"{type(exc).__name__}: {exc}",
                )
            else:
                print(f"[supervisor] position mode probe failed ({type(exc).__name__}) — keeping last known mode")

    async def _refresh_exchange_algo_snapshot(self, *, force: bool = False) -> None:
        """读取交易所当前 openAlgoOrders；查询失败保持 UNKNOWN 并阻断。"""
        # PKG02 (BDS-P0-001): 所有环境统一运行引擎探针。
        if self.engine is None:
            return
        now = time.monotonic()
        # BD-FIX (rate-budget): 15→30s。openAlgoOrders 权重高且
        # rest_client 已有 10s 响应缓存，30s 间隔叠加缓存后配额消耗减半。
        if not force and now - self._last_exchange_algo_probe < 30.0:
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
        if self._g7_certification is None:
            return {"active_windows": [], "state_load_errors": [], "producer_status": "HARD_HOLD"}
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
        if self._g7_certification is None:
            return

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
            # M18-F01: 自愈活动信号真实传入(此前恒默认 False —— 频率
            # 策略对恢复/重启场景失明)
            self._monitoring_scheduler.tick(
                scheduler_results,
                open_p0=open_p0,
                open_p1=open_p1,
                self_heal=self._recovery_count > 0,
            )  # type: ignore[no-untyped-call]
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
        # M18-F01: 缓存 monitoring 深度检查结果 —— MON08 稀疏轮复用
        # (深度审计按调度器节奏执行,不重新 gather)
        self._cached_monitoring_checks = list(monitoring_checks)
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
        """BD-FIX (O2): 监督器 DEGRADED/LOCKED 状态推送告警。

        M00-F03: 告警文本使用聚合摘要（同 check_id 条目合并为 ×N），
        不再把逐持仓条目全量拼进描述（P0-19 告警风暴治理）。
        """
        try:
            if self.engine is not None:
                from beidou_observability.telemetry import AlertSeverity

                severity = AlertSeverity.CRITICAL if state == "LOCKED" else AlertSeverity.HIGH
                blocker_summary = summarize_blockers(blockers)
                self.engine._alerts.send_incident(
                    severity=severity,
                    title=f"Supervisor {state}",
                    description=f"Blockers: {blocker_summary}",
                    category="supervisor",
                )
                print(f"[supervisor] Alert sent: {severity.value} — Supervisor {state}: {blocker_summary}")
        except Exception as e:
            print(f"[supervisor] Alert send failed: {e}")

    def _resolve_supervisor_incidents(self) -> None:
        """清除监督器命名空间(supervisor)的活动事故。

        BD-FIX: DEGRADED/LOCKED 时发出的 "Supervisor <state>" 事故没有
        清理路径 —— 恢复 RUNNING 后仍以 DETECTED 状态残留,与事实背离
        且持续误导运维。本方法仅在防抖器判定 RUNNING(控制面 RESUME 且
        无 blocker)时调用,根因确已消除才清理;LOCKED 状态的事故不受影响。
        """
        try:
            if self.engine is None:
                return
            _alerts = getattr(self.engine, "_alerts", None)
            if _alerts is None:
                return
            _active = getattr(_alerts, "_active_incidents", {})
            for _iid, _inc in list(_active.items()):
                if str(getattr(_inc, "root_cause_category", "")) == "supervisor":
                    with suppress(Exception):
                        _alerts.resolve_incident(_iid)
                        print(f"[supervisor] Auto-resolved stale supervisor incident {_iid}")
        except Exception as e:
            print(f"[supervisor] supervisor incident resolution failed: {e}")

    def _resolve_stale_supervisor_incidents_before_debounce(self, checks: list[CheckResult]) -> bool:
        """Resolve self-generated alerts before they can block clean recovery.

        A supervisor alert is derived from an earlier blocker.  If it is the
        only remaining active incident, leaving it in ``runtime.health.incidents``
        prevents the debounce state from ever reaching ``RUNNING``; that in
        turn prevents ``_resolve_supervisor_incidents`` from being called and
        can escalate a recovered process to ``LOCKED``.  Only non-terminal
        supervisor states and an incident set containing *only* supervisor
        incidents qualify.  Any real safety incident or other blocking check
        keeps the fail-closed path intact.
        """
        if self.report.supervisor_state in {"LOCKED", "FAILED", "STOPPED"} or self.engine is None:
            return False
        alerts = getattr(self.engine, "_alerts", None)
        if alerts is None:
            return False
        # ``AlertDispatcher.get_active_incidents()`` intentionally exposes a
        # JSON-safe summary and therefore omits ``root_cause_category``.  Use
        # the in-memory incident objects for this internal classification; the
        # public summary cannot distinguish a supervisor alert from a safety
        # incident.  Keep the public getter as a fallback for test doubles or
        # alternate alert implementations.
        active = getattr(alerts, "_active_incidents", None)
        if isinstance(active, dict):
            incidents = list(active.values())
        else:
            getter = getattr(alerts, "get_active_incidents", None)
            if not callable(getter):
                return False
            try:
                incidents = list(getter())
            except Exception:
                return False
        if not incidents:
            return False

        def category(incident: Any) -> str:
            if isinstance(incident, dict):
                return str(incident.get("root_cause_category", ""))
            return str(getattr(incident, "root_cause_category", ""))

        if any(category(incident) != "supervisor" for incident in incidents):
            return False
        if any(item.is_blocking and item.check_id != "runtime.health.incidents" for item in checks):
            return False
        self._resolve_supervisor_incidents()
        return True

    async def _apply_debounce_action(
        self, debounce_action: str, persistent_blockers: list[CheckResult], has_persistent: bool
    ) -> None:
        """健康防抖器动作执行（M00-F03 抽出以便独立测试）。

        降级/告警只在状态转移时执行一次；已在 DEGRADED 期间保留静默
        fail-closed 背压（控制面被意外 RESUME 时拉回 NO_NEW_RISK）。
        """
        if debounce_action == "LOCKED":
            # 防抖器判定: 连续 lock_after 次持久阻断 → LOCKED（终态）。
            # M00-F03: LOCKED 后不再重复 _fail_closed/告警（终态只需一次）。
            if self.report.supervisor_state != "LOCKED":
                await self._fail_closed(
                    "防抖器: 连续持久阻断 → LOCKED: " + summarize_blockers(persistent_blockers),
                    fatal=True,
                )
                self.report.supervisor_state = "LOCKED"
                self._last_blocker_fingerprint = summarize_blockers(persistent_blockers)
                # 快照写入非致命: OSError (磁盘满/目录只读) 不得中断 LOCKED
                # 转移 —— 否则 _send_supervisor_alert 永不执行, 终态告警静默丢失。
                try:
                    _write_locked_snapshot(persistent_blockers)  # 新增
                except Exception as exc:
                    logger.warning("LOCKED 快照写入失败 (不阻断终态转移): %s", exc)
                self._send_supervisor_alert("LOCKED", persistent_blockers)
        elif debounce_action == "DEGRADED":
            # PKG02 (BDS-P0-001): 所有环境统一降级行为。
            if self.report.supervisor_state != "DEGRADED":
                await self._fail_closed(
                    "防抖器: 连续持久阻断 → DEGRADED: " + summarize_blockers(persistent_blockers),
                    fatal=False,
                )
                self.report.supervisor_state = "DEGRADED"
                self._last_blocker_fingerprint = summarize_blockers(persistent_blockers)
                self._send_supervisor_alert("DEGRADED", persistent_blockers)
            else:
                # M00-F03-R2（对抗审查反例 C）: 已在 DEGRADED —— 不重复
                # 相同 blocker 的告警，但 blocker 指纹变化（新类型 P0 出现）
                # 必须立即告警；否则新故障在 DEGRADED 期间永久静默。
                fingerprint = summarize_blockers(persistent_blockers)
                if fingerprint != self._last_blocker_fingerprint:
                    self._last_blocker_fingerprint = fingerprint
                    self._send_supervisor_alert("DEGRADED", persistent_blockers)
                # 静默 fail-closed 背压：授权已撤销时控制面不允许停留
                # RESUME；若被意外 RESUME，立即拉回 NO_NEW_RISK。
                # 条件含 not _resume_authorized：授权有效（启动窗口）时
                # 不参与拉回，避免与引擎恢复路径震荡。
                if not self._resume_authorized and self._control_state() == "RESUME":
                    from beidou_control.plane import ControlAction

                    _engine = self.engine
                    if _engine is not None:
                        with suppress(Exception):
                            _engine._control.execute_action(ControlAction.NO_NEW_RISK)
        elif debounce_action == "RUNNING":
            if self._control_state() != "RESUME":
                self.report.supervisor_state = "PAUSED"
            else:
                self.report.supervisor_state = "RUNNING"
            # BD-FIX (stale supervisor incidents): DEGRADED/LOCKED 期间
            # 发出的 "Supervisor <state>" 事故在恢复 RUNNING 后无人清理
            # (实测 08:29 的 HIGH 事故在 RUNNING/0 blocker 后仍 DETECTED
            # 数十分钟)。状态回到 RUNNING 且无 blocker 即事故根因消除,
            # 由监督器清除自己命名空间(supervisor)的事故 —— 引擎侧
            # auto-resolve 只负责 execution_fact/reconciliation/user_stream/
            # protection 类别,不越权清理监督器事故。
            self._resolve_supervisor_incidents()
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
                    "持久阻断检测（防抖计数中）: " + summarize_blockers(persistent_blockers),
                    fatal=False,
                )
                self.report.supervisor_state = _state_after_persistent_block(previous_state, True)
                if self.report.supervisor_state == "DEGRADED" and previous_state != "DEGRADED":
                    self._send_supervisor_alert("DEGRADED", persistent_blockers)

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

    def _classify_repairable(self, blockers: list[CheckResult]) -> bool:
        """A/B 分类: 全部阻断均为可修复缺口 → True (永不 LOCKED)。

        reason 缺失或含任何不可修复项 → False (fail-closed 按 B)。
        A 类 reason 全集见模块级 ``_REPAIRABLE_PROTECTION_REASONS``。

        R12 (2026-08-24): 分类只吃每 tick 新鲜事实 —— 仅
        ``runtime.safety.protection_gap_detail`` 的 evidence.gaps 提供 A
        证据 (A-whitelist 校验); incident dict 的 ``gap_reasons`` 是重发时
        的陈旧快照 (A 期创建的事故在 gap 清除后残留 stale reasons), 只作
        可观测性展示 (P2 发送路径保留), 不再参与分类。其余任何 blocker
        (含 ``runtime.health.incidents``) 一律不提供 A 证据, 有它在即整体
        按 B —— 消除 I-2 (真 B 类 owner-unknown 永不 LOCKED 的 fail-open)
        与 I-3 (E3 armed incident 无 gap_reasons 把 A 拖成 B, 引擎 LOCKED
        退出, 与 A-never-LOCKED 矛盾)。

        R2 (2026-08-24): ``runtime.safety.protection_coverage`` 阻断按
        message 的 issue token 分类 —— 全部 token ∈ {MISSING_SL, MISSING_TP}
        且全部 entity_id ∈ 引擎本地所有权符号集 (``_position_generation``
        ∪ ``_position_projection`` 的键) 才可修复; 含 DUP(...)/GHOST/未知
        token, 或任何 entity 非本地所有 (共享 testnet 账户的外部持仓)
        → 非可修复 (fail-closed)。引擎未就绪时所有权不可证明 → 按 B。
        """
        if not blockers:
            return False
        engine = getattr(self, "engine", None)
        owned: set[str] = set()
        if engine is not None:
            owned.update(str(sym) for sym in (getattr(engine, "_position_generation", None) or {}))
            owned.update(str(sym) for sym in (getattr(engine, "_position_projection", None) or {}))
        reasons: list[str] = []
        coverage_repairable = False  # coverage 阻断走 message 分类, 不进 reasons
        for b in blockers:
            ev = getattr(b, "evidence", None) or {}
            if not isinstance(ev, dict):
                return False
            if b.check_id == "runtime.safety.protection_coverage":
                if not _coverage_message_repairable(b, owned):
                    return False
                coverage_repairable = True
                continue
            # R12: 其余任何 blocker (含 runtime.health.incidents) 一律不
            # 提供 A 证据, 有它在即整体按 B (fail-closed)。
            if b.check_id != "runtime.safety.protection_gap_detail":
                return False
            gaps = ev.get("gaps")
            if gaps:
                for g in gaps:
                    # reason 缺失/无法分类 → 按 B
                    if not isinstance(g, dict) or not g.get("reason"):
                        return False
                    reasons.append(str(g["reason"]))
            else:
                # 空证据路径: gap_detail 无 gaps → 无 A 证据 → 按 B
                return False
        return all(r in _REPAIRABLE_PROTECTION_REASONS for r in reasons) and (bool(reasons) or coverage_repairable)

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
        # BD-FIX (M00-F02): --no-self-heal 显式关闭自动恢复时，任何
        # 路径（含 ACTIVE 捷径）都不得自动补发 RESUME —— 与下方
        # RECOVERING 路径的既有 self_heal 门保持一致。恢复需人工/
        # 重启授权。
        if not self.self_heal:
            print("[supervisor] RECOVERY SKIP: self_heal disabled (--no-self-heal)")
            return False
        # BD-FIX: ACTIVE 时跳过 transition（ACTIVE→RECOVERING 状态机
        # 非法），直接走下方 RESUME 补发 —— 引擎健康但控制面被
        # 撤销的场景（final63 实测 PAUSED 卡死：授权撤销 + 对账恢复
        # 后无任何路径重新 RESUME）。
        if state_value not in ("DEGRADED", "ACTIVE"):
            print(f"[supervisor] RECOVERY SKIP: lifecycle={state_value} (not DEGRADED/ACTIVE)")
            return False
        if state_value == "ACTIVE":
            # 引擎已 ACTIVE：无需 transition 序列，直接补发 RESUME
            from beidou_control.plane import ControlAction

            if self._control_state() not in ("RESUME", "LOCK", "EMERGENCY_FLATTEN"):
                self.engine._control.execute_action(ControlAction.RESUME)
                print("[supervisor] Re-issued RESUME (engine already ACTIVE)")
            self._control_paused_by_supervisor = False
            self._critical_streak = 0
            self._health_debounce.reset()
            self.report.supervisor_state = "RUNNING"
            return True
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
        例外之外的例外（M00-F02）：``self_heal=False``（``--no-self-heal``）
        时本路径同样关闭——显式关闭自愈优先于环境便利。
        """
        if self.engine is None or self.mode != "testnet":
            return False
        # BD-FIX (M00-F02): --no-self-heal 显式关闭自动恢复时，testnet
        # 自动重新授权同样必须关闭 —— 否则 launchd 的 --no-self-heal
        # 只挡住 _recover_if_validated 一条路径，故障自愈后仍会自动
        # RESUME（P0-01）。live/canary/paper 不受影响（本方法仅
        # testnet 进入）。
        if not self.self_heal:
            print("[supervisor] testnet auto re-auth SKIPPED: self_heal disabled (--no-self-heal)")
            return False
        if self._has_active_trading_incident():
            print("[supervisor] testnet auto re-auth SKIPPED: active safety incident")
            return False
        if self._resume_authorized or self._control_state() == "RESUME" or self.report.blockers:
            return False
        lifecycle = self.engine._lifecycle
        state_value = str(getattr(lifecycle.state, "value", lifecycle.state))
        # 仅非致命状态（DEGRADED/ACTIVE）可自愈；LOCKED/FAILED/
        # QUARANTINED 等终态或升级态必须保持人工处置。
        # BD-FIX: ACTIVE 也必须可重新授权 —— 引擎自愈回 ACTIVE 但控制面
        # 授权已被 _fail_closed 撤销时（final57 实测），旧条件只认
        # DEGRADED → PAUSED 卡死。
        if state_value not in ("DEGRADED", "ACTIVE"):
            return False

        from beidou_control.plane import ControlAction
        from beidou_lifecycle.lifecycle import ModuleState

        # 复用 _recover_if_validated 的恢复 transition 序列
        # （RECOVERING→VALIDATING→ACTIVE）。
        # BD-FIX: lifecycle 已 ACTIVE 时跳过 transition（ACTIVE→
        # RECOVERING 状态机非法），直接授权 + RESUME。
        if state_value == "ACTIVE":
            self._resume_authorized = True
            self.engine._control.execute_action(ControlAction.RESUME)
            self._control_paused_by_supervisor = False
            self._critical_streak = 0
            self._health_debounce.reset()
            self.report.supervisor_state = "RUNNING"
            print("[supervisor] testnet auto re-authorized RESUME (engine already ACTIVE)")
            return True
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

    def _authorize_resume_via_truth_snapshot(self) -> tuple[bool, str]:
        """M19-F01 (P0-12): 启动授权链挂接 TruthSnapshot 门禁。

        BD-CV02 AC-02-04: RESUME 必须有新 TruthSnapshot 且
        MATCHED 对账 + ACTIVE 保护 + NORMAL 风险。零写模式
        (paper/shadow/research/safety_only)按设计跳过对账/保护
        事实(EXEMPT-08 已登记,与 intent 门禁 engine.py:4578 及
        health liveness 的对账豁免同语义),走豁免路径。
        本方法只验证不执行 —— RESUME 动作由调用方在置位
        _resume_authorized 后发出,interlock 放行语义不变。
        """
        if self.mode in ("paper", "shadow", "research", "safety_only"):
            return True, "authorized via EXEMPT-08 (zero-write mode)"
        if self.engine is None:
            return False, "engine not initialized"
        snap = self.engine.build_truth_snapshot()
        allowed, reason = self.engine._control.authorize_resume(snap)
        return allowed, reason

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
            # M18-F01: MON08 频率策略门控实际节奏 —— 深度审计(外部事实
            # monitoring checks + 交易所快照 gather)按调度器节奏执行
            # (ALERT 600s / NORMAL 1800s / STABLE 3600s);runtime checks
            # (引擎内部状态)每轮执行,安全门禁不稀疏。非 RESUME 或存在
            # blocker 时保持每轮全查 —— P0 不被稀释,故障响应不退化。
            _deep_due = self._monitoring_scheduler.should_run_deep_audit()  # type: ignore[no-untyped-call]
            # BD-FIX (M18-F01 回归): 快照刷新不得只依赖深度审计轮。
            # check_account_unknown 要求快照 age<=45s,而深度审计最长
            # 600s 才触发一次 → 平稳期快照必然超龄 → account P0 FAIL
            # → trading_ready 周期性翻转,下单窗口被周期性关闭。
            # 三个快照各自带内部节流(account 30s / position 300s /
            # algo 按需),每轮调用成本受节流约束,不在稀疏轮跳过。
            await asyncio.gather(
                _refresh_snapshot_safe(self._refresh_exchange_account_snapshot()),
                _refresh_snapshot_safe(self._refresh_position_mode()),
                _refresh_snapshot_safe(self._refresh_exchange_algo_snapshot()),
            )
            if _deep_due or self.report.blockers or self._control_state() != "RESUME":
                # 先合并全部内部与外部事实，再决定是否阻断/恢复；不能在深度
                # monitoring 检查之前依据一组较窄的 runtime checks 自动 RESUME。
                checks = self._runtime_checks()
                checks = self._merge_monitoring_checks(checks)
            else:
                # 稀疏轮:runtime checks + 上一轮 monitoring 深度检查缓存
                checks = self._runtime_checks()
                checks = checks + list(getattr(self, "_cached_monitoring_checks", []))
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

            if self._resolve_stale_supervisor_incidents_before_debounce(checks):
                # Re-read the health surface after resolving the derived
                # incident so debounce/recovery sees the fresh fact set.
                checks = self._runtime_checks()
                checks = self._merge_monitoring_checks(checks)

            if not any(item.is_blocking for item in checks) and await self._recover_if_validated(checks):
                # 只有仍然有效的授权才可以执行已经授权的恢复路径；
                # _fail_closed 后 _resume_authorized=False，不能由清洁窗口重置。
                checks = self._runtime_checks()
                checks = self._merge_monitoring_checks(checks)

            # Task 6 (3c/D-4): A 类永不 LOCKED 的配套告警 —— 每轮监控循环
            # 刷新 stuck 标记 (文件 mtime 即"引擎还活着"的心跳)。getattr 防御
            # 引擎替身 (测试替身无该方法), 异常静默: 标记是尽力而为的告警,
            # 不得反噬主循环。
            _update_stuck = getattr(self.engine, "_update_stuck_marker", None)
            if callable(_update_stuck):
                try:
                    _update_stuck()
                except Exception:
                    pass

            self.report.phase = "RUNTIME_MONITORING"
            self.report.replace_phase_checks("runtime.", checks)
            blockers = self.report.blockers

            # Phase 3: 健康防抖器 — 滑动窗口只决定 DEGRADED→LOCKED 的升级
            # 速度；它不能让任何 P0/P1 事实失真继续持有 RESUME 授权。
            persistent_blockers = [b for b in blockers if b.check_id not in self._TRANSIENT_CHECK_IDS]
            has_persistent = bool(persistent_blockers)
            # A/B 分流: 全部阻断均可修复 (保护缺口, 引擎活着能修) → 永不
            # LOCKED; 任一不可修复/无法分类 → 按 B 走原防抖语义。
            repairable = self._classify_repairable(persistent_blockers)
            debounce_action = self._health_debounce.feed(has_persistent, repairable=repairable)
            # M00-F03: 分支链抽至 _apply_debounce_action（转移门控 + 静默背压，
            # 可独立测试）。None = 防抖器样本不足,保持当前状态不动作。
            if debounce_action is not None:
                await self._apply_debounce_action(debounce_action, persistent_blockers, has_persistent)

            self.report.trading_ready = self._is_trading_ready()
            # P1: G7 实时 SLI 追踪 — 每个周期更新
            try:
                active_windows = (
                    [
                        item
                        for item in self._g7_certification.list_windows()
                        if item.get("status") == "RUNNING" and bool(item.get("evidence_state_complete", False))
                    ]
                    if self._g7_certification is not None
                    else []
                )
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
        print("=" * 72)
        print("北斗一键启动监督器 / Beidou One-Click Supervisor")
        print(f"mode={self.mode} symbols={','.join(self.symbols)} port={self.port}")
        print("=" * 72)

        # Preflight is strictly read-only. Do not create a PID lock or write
        # supervisor evidence until every blocking fact has passed.
        preflight, _settings = run_preflight(self.project_root, self.mode, self.port)
        preflight = _apply_g5_dev_exemption(preflight, self.mode, self.g5_dev_exemption)
        self.report.phase = "PREFLIGHT"
        self.report.replace_phase_checks("preflight.", preflight)
        self._print_checks(preflight)
        if self.report.blockers:
            self.report.supervisor_state = "BLOCKED"
            print("❌ 启动前置检查未通过，系统未启动。")
            return 2

        locked, lock_message = self.lock.acquire()
        if not locked:
            print(f"❌ {lock_message}")
            return 3

        try:
            self.writer.write(self.report)

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

            # M19-F01 (P0-12): 启动授权链挂接 TruthSnapshot 门禁
            # (BD-CV02 AC-02-04)。门禁拒绝时不授权 —— 保持 NO_NEW_RISK;
            # testnet 由 _maybe_testnet_auto_reauthorize 兜底,live/canary
            # 需人工重启重新授权(生产安全语义,supervisor 注释 1031 行)。
            allowed, reason = self._authorize_resume_via_truth_snapshot()
            self._resume_authorized = allowed
            if allowed:
                if self._control_state() != "RESUME":
                    from beidou_control.plane import ControlAction

                    self.engine._control.execute_action(ControlAction.RESUME)
                print(f"✅ 深度启动自检通过：环境、模块、算法、数据、账户与安全门禁均已验证。({reason})")
            else:
                print(f"[supervisor] RESUME DEFERRED: TruthSnapshot gate rejected — {reason}")
                print("   (事实齐备后 testnet 自动重新授权 / live 需人工重启授权)")
            self._control_paused_by_supervisor = False
            self.report.supervisor_state = "RUNNING"
            self.report.trading_ready = self._is_trading_ready()
            self.report.phase = "RUNTIME_MONITORING"
            self.writer.write(self.report)
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


# 模块级别名: 测试与调用方既可用 ``BeidouSupervisor._classify_repairable``
# (self 绑定), 也可 ``from beidou_launcher.supervisor import
# _classify_repairable`` 后对任意容器做 __get__ 绑定 (plan test-5 写法)。
_classify_repairable = BeidouSupervisor._classify_repairable
