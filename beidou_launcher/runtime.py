"""真实行情只读算法探针与持续运行健康检查。"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from .models import CheckResult, CheckSeverity, CheckStatus
from .registry import inspect_engine_wiring


def active_incident_blocking_severity(incidents: list[Any]) -> CheckSeverity | None:
    """Return the runtime check severity required by open safety incidents.

    ``AlertDispatcher.get_active_incidents`` omits resolved incidents, but
    callers may provide dictionaries or lightweight test doubles.  Treat an
    incident as active unless its status explicitly says RESOLVED/CLOSED.
    HIGH pauses trading; CRITICAL/LOCKDOWN are P0 authority blockers.
    """

    p1_severities = {"HIGH", "P1"}
    p0_severities = {"CRITICAL", "LOCKDOWN", "P0"}
    p1_found = False
    for incident in incidents:
        if isinstance(incident, dict):
            raw_status = incident.get("status", "")
            raw_severity = incident.get("severity", "")
        else:
            raw_status = getattr(incident, "status", "")
            raw_severity = getattr(incident, "severity", "")
        status = str(getattr(raw_status, "value", raw_status)).upper()
        severity = str(getattr(raw_severity, "value", raw_severity)).upper()
        if status in {"RESOLVED", "CLOSED"}:
            continue
        if severity in p0_severities:
            return CheckSeverity.P0
        if severity in p1_severities:
            p1_found = True
    return CheckSeverity.P1 if p1_found else None


def has_active_trading_incident(engine: Any) -> bool:
    """Return whether active incident state must revoke new-risk authority.

    A missing incident boundary is tolerated only for minimal test doubles;
    once the production boundary exists, query errors fail closed.
    """

    getter = getattr(getattr(engine, "_alerts", None), "get_active_incidents", None)
    if not callable(getter):
        return False
    try:
        incidents = list(getter())
    except Exception:
        return True
    return active_incident_blocking_severity(incidents) is not None


def build_decision_trace(probe: dict[str, Any]) -> dict[str, Any]:
    """Build a read-only market-to-kernel trace for later PnL joining."""
    from beidou_reporting.pnl_attribution import DecisionTrace

    return DecisionTrace.from_probe(probe).to_dict()


async def run_read_only_algorithm_probe(engine: Any, symbols: list[str]) -> dict[str, Any]:
    """使用真实公共行情执行唯一 StrategyKernel，不提交 OrderIntent。

    ``_alpha_graph`` 仍可出现在诊断注册表中，但不能成为运行时探针或
    下单前决策的第二条执行路径。探针必须证明 typed graph 已执行，即使
    最终结果是安全的 NO_ACTION/VETO。
    """
    symbol = symbols[0]
    started = time.perf_counter()
    try:
        feed = engine._feed
        kline_features = await feed.async_get_kline_features(symbol)
        live_features = await feed.async_update_features(symbol)
        features = {**kline_features, **live_features}
        if not kline_features or not live_features or float(features.get("close", features.get("price", 0))) <= 0:
            missing = []
            if not kline_features:
                missing.append("K线")
            if not live_features:
                missing.append("ticker/orderbook")
            if float(features.get("close", features.get("price", 0))) <= 0:
                missing.append("close/price=0")
            raise RuntimeError(f"真实{'/'.join(missing)}不完整")

        from beidou_shared.types import InstrumentId, VenueId

        state_builder = getattr(engine, "_estimate_market_state", None)
        state = state_builder(features) if callable(state_builder) else {}
        canonical_state = getattr(engine, "_last_market_state", None)
        state_to_dict = getattr(canonical_state, "to_dict", None)
        trace_state = state_to_dict() if callable(state_to_dict) else state
        context = {
            "features": features,
            "instrument_id": InstrumentId(symbol),
            "venue_id": VenueId("BINANCE"),
            "state": state,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "market_state_hash": getattr(canonical_state, "state_hash", ""),
            "_predictions": {},
            "_position_info": {
                "has_position": False,
                "entry_price": 0.0,
                "side": "",
                "entry_time": 0.0,
                "pnl_pct": 0.0,
            },
        }
        v3_shadow: dict[str, Any]
        try:
            from beidou_strategy.alpha.pipeline import AlphaV3ShadowEngine

            shadow_result = AlphaV3ShadowEngine(getattr(engine, "_alpha_v3_calibration_registry", None)).evaluate(
                context
            )
            v3_shadow = shadow_result.to_dict()
            v3_shadow.update(
                {
                    "schema_version": str(shadow_result.ensemble_forecast.schema_version),
                    "model_version": str(shadow_result.ensemble_forecast.model_version),
                    "policy_version": shadow_result.ensemble_forecast.policy_version,
                }
            )
        except Exception as exc:
            v3_shadow = {
                "status": "NOT_VERIFIABLE",
                "failure_reasons": [f"V3_SHADOW_ERROR:{type(exc).__name__}"],
                "error": str(exc),
            }
        kernel = getattr(engine, "_strategy_kernel", None)
        evaluate = getattr(kernel, "evaluate", None)
        if not callable(evaluate):
            raise RuntimeError("StrategyKernel.evaluate 未接线")
        kernel_result = await evaluate(context)
        if not isinstance(kernel_result, dict) or kernel_result.get("kernel") != "typed_graph":
            raise RuntimeError("策略探针未通过唯一 typed StrategyKernel")
        compute_hash = getattr(kernel, "compute_proposal_hash", None)
        if not callable(compute_hash):
            raise RuntimeError("StrategyKernel.compute_proposal_hash 未接线")

        proposal = kernel_result.get("proposal")
        graph_hash = str(kernel_result.get("graph_hash", ""))
        if not graph_hash:
            raise RuntimeError("typed graph 缺少 graph hash")

        # VETO/NO_ACTION 也是一次成功的、可审计的策略执行；用稳定的
        # no-action 证据代替空 hash，避免把安全阻断误报成探针故障。
        proposal_evidence: Any = proposal
        if proposal_evidence is None:
            proposal_evidence = {
                "status": "NO_ACTION",
                "reason": str(kernel_result.get("blocked_by", "typed_graph_no_proposal")),
                "instrument_id": str(context["instrument_id"]),
                "venue_id": str(context["venue_id"]),
                "graph_hash": graph_hash,
            }
        proposal_hash = compute_hash(proposal_evidence)
        if not proposal_hash:
            raise RuntimeError("策略探针未生成 proposal hash")

        component_results: dict[str, dict[str, Any]] = {}
        typed_graph = getattr(kernel, "_typed_graph", None)
        order_fn = getattr(typed_graph, "topological_order", None)
        if callable(order_fn):
            nodes = getattr(typed_graph, "_nodes", {})
            for component_id in order_fn():
                node = nodes.get(component_id)
                node_type = getattr(getattr(node, "node_type", None), "value", "UNKNOWN")
                component_results[component_id] = {"node_type": str(node_type)}
        if not component_results:
            raise RuntimeError("typed graph 未提供节点执行证据")
        probe_timestamp = str(context["timestamp"])
        decision_trace = build_decision_trace(
            {
                "symbol": symbol,
                "features": features,
                "state": trace_state,
                "graph_hash": graph_hash,
                "proposal_hash": proposal_hash,
                "context_hash": str(kernel_result.get("context_hash", "")),
                "timestamp": probe_timestamp,
                "market_data_hash": v3_shadow.get("benchmark_snapshot_hash"),
                "benchmark_snapshot_hash": v3_shadow.get("benchmark_snapshot_hash"),
                "alpha_forecast_hash": v3_shadow.get("alpha_forecast_hash"),
                "ensemble_forecast_hash": v3_shadow.get("ensemble_forecast_hash"),
                "exposure_target_hash": v3_shadow.get("exposure_target_hash"),
                "portfolio_target_hash": v3_shadow.get("portfolio_target_hash"),
                "schema_version": v3_shadow.get("schema_version"),
                "model_version": v3_shadow.get("model_version"),
                "policy_version": v3_shadow.get("policy_version"),
            }
        )
        return {
            "ok": True,
            "symbol": symbol,
            "proposal_hash": proposal_hash,
            "kernel": "typed_graph",
            "graph_hash": graph_hash,
            "blocked_by": kernel_result.get("blocked_by", ""),
            "components": component_results,
            "v3_shadow": v3_shadow,
            "v3_shadow_status": v3_shadow.get("status", "NOT_VERIFIABLE"),
            "decision_trace": decision_trace,
            "feature_count": len(features),
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "timestamp": probe_timestamp,
        }
    except Exception as exc:
        return {
            "ok": False,
            "symbol": symbol,
            "error": f"{type(exc).__name__}: {exc}",
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


def _append_position_mode_check(
    checks: list[CheckResult],
    evidence: Any,
    now: float,
) -> None:
    """MON00A: Position Mode 运行时检查。

    - UNKNOWN → P0 FAIL (MON00A-05)
    - 无 evidence → P1 WARN (启动阶段)
    - ONE_WAY/HEDGE → PASS
    """
    if evidence is None:
        checks.append(
            CheckResult(
                check_id="runtime.safety.position_mode",
                name="账户持仓模式",
                status=CheckStatus.WARN,
                severity=CheckSeverity.P1,
                message="Position Mode 尚未查询，等待交易所响应",
                evidence={"mode": "UNKNOWN", "reason": "NO_EVIDENCE_YET"},
            )
        )
        return

    mode = getattr(evidence, "mode", None)
    if (
        mode is None
        or str(mode) == "AccountPositionMode.UNKNOWN"
        or (hasattr(mode, "value") and mode.value == "UNKNOWN")
    ):
        checks.append(
            CheckResult(
                check_id="runtime.safety.position_mode",
                name="账户持仓模式",
                status=CheckStatus.FAIL,
                severity=CheckSeverity.P0,
                message=f"Position Mode UNKNOWN: {getattr(evidence, 'error', 'API_FAILURE')} — 阻止新增风险",
                evidence={"mode": "UNKNOWN", "error": getattr(evidence, "error", None)},
            )
        )
        return

    mode_str = mode.value if hasattr(mode, "value") else str(mode)
    checks.append(
        CheckResult(
            check_id="runtime.safety.position_mode",
            name="账户持仓模式",
            status=CheckStatus.PASS,
            severity=CheckSeverity.P0,
            message=f"Position Mode: {mode_str}",
            evidence={
                "mode": mode_str,
                "source": getattr(evidence, "source", ""),
                "source_timestamp": getattr(evidence, "source_timestamp", None),
                "observed_at": getattr(evidence, "observed_at", 0.0),
            },
        )
    )


def collect_runtime_checks(
    *,
    engine: Any,
    mode: str,
    port: int,
    resume_authorized: bool,
    algorithm_probe: dict[str, Any],
    last_error_count: int,
    exchange_algo_snapshot: dict[str, Any] | None = None,
    exchange_account_snapshot: dict[str, Any] | None = None,
    position_mode_evidence: Any = None,
) -> tuple[list[CheckResult], int]:
    checks = inspect_engine_wiring(engine, mode)
    now = time.time()

    # Position Mode 检查 (PKG-MON-00A)
    _append_position_mode_check(checks, position_mode_evidence, now)

    lifecycle = getattr(getattr(engine, "_lifecycle", None), "state", None)
    lifecycle_value = str(getattr(lifecycle, "value", lifecycle))
    lifecycle_ok = lifecycle_value == "ACTIVE"
    # DEGRADED 是监督器主动设置的受控安全状态，不应自锁；
    # 仅 LOCKED/FAILED 视为阻断。
    if lifecycle_value in ("LOCKED", "FAILED"):
        lc_status = CheckStatus.FAIL
        lc_severity = CheckSeverity.P0
    elif lifecycle_value == "DEGRADED":
        lc_status = CheckStatus.WARN
        lc_severity = CheckSeverity.P1
    else:
        lc_status = CheckStatus.PASS if lifecycle_ok else CheckStatus.FAIL
        lc_severity = CheckSeverity.P0
    checks.append(
        CheckResult(
            check_id="runtime.health.lifecycle",
            name="引擎生命周期",
            status=lc_status,
            severity=lc_severity,
            message=f"生命周期={lifecycle_value}",
            evidence={"state": lifecycle_value},
        )
    )

    control = getattr(engine, "_control", None)
    try:
        raw_control_state = control.get_status() if control is not None else "UNKNOWN"
        control_state = str(getattr(raw_control_state, "value", raw_control_state))
    except Exception:
        control_state = "UNKNOWN"
    if control_state == "RESUME":
        control_status = CheckStatus.PASS
        control_severity = CheckSeverity.P1
        control_message = "控制面允许正常运行"
    elif control_state in {"NO_NEW_RISK", "EXIT_ONLY"}:
        control_status = CheckStatus.WARN
        control_severity = CheckSeverity.P1
        control_message = f"控制面处于安全暂停状态: {control_state}"
    else:
        control_status = CheckStatus.FAIL
        control_severity = CheckSeverity.P0
        control_message = f"控制面异常或锁定: {control_state}"
    checks.append(
        CheckResult(
            check_id="runtime.safety.control_plane",
            name="控制面状态",
            status=control_status,
            severity=control_severity,
            message=control_message,
            evidence={"state": control_state, "resume_authorized": resume_authorized},
        )
    )

    blocked_writes = list(getattr(engine, "_supervisor_blocked_writes", []))
    checks.append(
        CheckResult(
            check_id="runtime.safety.write_interlock",
            name="零写模式互锁",
            status=CheckStatus.WARN if blocked_writes else CheckStatus.PASS,
            severity=CheckSeverity.P1,
            message=(
                f"已拦截 {len(blocked_writes)} 次非授权交易所写请求" if blocked_writes else "未发现非授权交易所写请求"
            ),
            evidence={"blocked_count": len(blocked_writes), "recent": blocked_writes[-20:]},
        )
    )

    feed = getattr(engine, "_feed", None)
    try:
        feed_internal_healthy = bool(feed.is_healthy()) if feed is not None else False
        ticker_symbols = set(getattr(feed, "_last_ticker", {}).keys())
        orderbook_symbols = set(getattr(feed, "_last_orderbook", {}).keys())
        observed_symbols = sorted(ticker_symbols & orderbook_symbols)
    except Exception:
        feed_internal_healthy = False
        observed_symbols = []
    tick_count = int(getattr(engine, "_tick_count", 0))
    feed_healthy = feed_internal_healthy and tick_count > 0 and bool(observed_symbols)
    # PKG02 (BDS-P0-001): 所有环境统一行情数据健康检查。
    if resume_authorized:
        market_severity = CheckSeverity.P0
        market_status = CheckStatus.PASS if feed_healthy else CheckStatus.FAIL
    else:
        market_severity = CheckSeverity.P2
        market_status = CheckStatus.PASS if feed_healthy else CheckStatus.WARN
    checks.append(
        CheckResult(
            check_id="runtime.health.market_data",
            name="行情数据健康",
            status=market_status,
            severity=market_severity,
            message=(
                f"已观测 {len(observed_symbols)} 个标的的 ticker+orderbook"
                if feed_healthy
                else "尚未取得真实 ticker+orderbook，不能仅凭错误计数声明健康"
            ),
            evidence={
                "internal_healthy": feed_internal_healthy,
                "tick_count": tick_count,
                "observed_symbols": observed_symbols,
            },
        )
    )

    health_server = getattr(engine, "_health", None)
    health_thread = getattr(health_server, "_thread", None)
    health_alive = bool(health_thread and health_thread.is_alive())
    checks.append(
        CheckResult(
            check_id="runtime.health.http_server",
            name="健康服务线程",
            status=CheckStatus.PASS if health_alive else CheckStatus.FAIL,
            severity=CheckSeverity.P1,
            message=f"健康服务端口 {port} 正常" if health_alive else "健康服务线程未运行",
            evidence={"port": port, "thread_alive": health_alive},
        )
    )

    last_realtime_mono = getattr(engine, "_last_realtime_mono", None)
    if isinstance(last_realtime_mono, (int, float)):
        realtime_age = max(0.0, time.monotonic() - float(last_realtime_mono))
        heartbeat_clock = "monotonic"
    else:
        # Compatibility for diagnostic/test shells created without the
        # monotonic field; real engine instances always provide it.
        realtime_age = max(0.0, now - float(getattr(engine, "_last_realtime", 0.0)))
        heartbeat_clock = "wall_clock_compatibility"
    # BD-FIX: demo-fapi 慢网络 + 因子组件负载下循环 60-70s 一圈；
    # 90s 阈值保留新鲜度语义且消除边界摩擦（2026-08-13 验收校准）
    realtime_ok = realtime_age <= 90.0
    # 启动阶段（_last_realtime 被 supervisor 重置为 0），允许等待首个 tick
    if resume_authorized:
        rt_severity = CheckSeverity.P0
        rt_status = CheckStatus.PASS if realtime_ok else CheckStatus.FAIL
    else:
        rt_severity = CheckSeverity.P1
        rt_status = CheckStatus.PASS if realtime_ok else CheckStatus.WARN
    checks.append(
        CheckResult(
            check_id="runtime.health.realtime_heartbeat",
            name="实时循环心跳",
            status=rt_status,
            severity=rt_severity,
            message=f"最近实时 tick {realtime_age:.1f}s 前",
            evidence={
                "age_seconds": round(realtime_age, 3),
                "threshold_seconds": 90.0,
                "clock": heartbeat_clock,
            },
        )
    )

    # A recent timestamp alone is not proof that the executor is alive: the
    # timestamp can survive a stalled task or be initialized before startup.
    # Once RESUME has been authorized, a stopped writable engine is a P0
    # authority failure and must revoke readiness immediately.
    engine_running = bool(getattr(engine, "_running", False))
    loop_status = (
        CheckStatus.PASS if engine_running else (CheckStatus.WARN if not resume_authorized else CheckStatus.FAIL)
    )
    loop_severity = CheckSeverity.P1 if not resume_authorized else CheckSeverity.P0
    checks.append(
        CheckResult(
            check_id="runtime.health.engine_loop",
            name="引擎实时循环状态",
            status=loop_status,
            severity=loop_severity,
            message="引擎实时循环运行中" if engine_running else "引擎实时循环未运行，不能保持交易授权",
            evidence={"running": engine_running, "resume_authorized": resume_authorized},
        )
    )

    # The engine's three-way reconciliation result is the authoritative
    # position/order fact chain.  Do not infer readiness from a fresh account
    # snapshot or from the monitoring projection below: a process can have a
    # healthy REST call while its durable system, venue and user-stream facts
    # disagree (or while the event stream is missing).  The supervisor uses
    # this check during startup and every runtime cycle, so the old
    # ``/health=HEALTHY + RESUME`` false certificate cannot recur.
    #
    # BD-FIX: 零写模式 (paper/shadow/research) 的引擎在 _reconcile() 中
    # 按设计跳过三方对账 (模拟订单与交易所订单必然不一致)，因此
    # _last_reconciliation_result 恒为 None。旧逻辑让启动验证永远卡在
    # "三方对账尚未产生结果" P0 blocker，直到 startup_timeout 失败退出。
    # 零写模式没有交易写能力，对账是写环境保护，按 user_stream 检查的
    # 同一模式豁免 (PASS/P1)，不得阻塞零写模式启动。
    zero_write_mode = str(mode).lower() in ("paper", "shadow", "research", "safety_only")
    reconciliation = getattr(engine, "_last_reconciliation_result", None)
    reconciliation_status = str(
        getattr(getattr(reconciliation, "status", None), "value", getattr(reconciliation, "status", "UNKNOWN"))
    ).upper()
    checked_at = getattr(reconciliation, "checked_at", None)
    reconciliation_age: float | None = None
    if checked_at is not None:
        try:
            if getattr(checked_at, "tzinfo", None) is None:
                checked_at = checked_at.replace(tzinfo=timezone.utc)
            reconciliation_age = max(0.0, time.time() - float(checked_at.timestamp()))
        except (AttributeError, TypeError, ValueError, OverflowError):
            reconciliation_age = None
    # PKG02 (BDS-P0-001): 所有环境统一对账标准和严重级别。
    # BD-FIX: demo-fapi 慢网络 + 因子组件负载下循环 60-70s 一圈；
    # 90s 阈值保留新鲜度语义且消除边界摩擦（2026-08-13 验收校准）
    _max_age = 90.0
    if zero_write_mode:
        # BD-FIX: 零写模式 (paper/shadow/research) 的引擎在 _reconcile() 中
        # 按设计跳过三方对账 (模拟订单与交易所订单必然不一致)，因此
        # _last_reconciliation_result 恒为 None。旧逻辑让启动验证永远卡在
        # "三方对账尚未产生结果" P0 blocker，直到 startup_timeout 失败退出。
        # 零写模式没有交易写能力，对账是写环境保护，按 user_stream 检查的
        # 同一模式豁免 (PASS/P1)，不得阻塞零写模式启动。
        checks.append(
            CheckResult(
                check_id="runtime.safety.reconciliation_authority",
                name="权威三方对账事实",
                status=CheckStatus.PASS,
                severity=CheckSeverity.P1,
                message="写能力关闭；三方对账不参与本模式授权",
                evidence={
                    "status": reconciliation_status,
                    "matched": False,
                    "age_seconds": None,
                    "threshold_seconds": 90.0,
                    "differences": ["RECONCILIATION_EXEMPT_ZERO_WRITE_MODE"],
                    "source": "engine._last_reconciliation_result",
                },
            )
        )
    elif reconciliation is None:
        checks.append(
            CheckResult(
                check_id="runtime.safety.reconciliation_authority",
                name="权威三方对账事实",
                status=CheckStatus.FAIL,
                severity=CheckSeverity.P0,
                message="三方对账尚未产生结果；账户/订单事实 UNKNOWN",
                evidence={
                    "status": reconciliation_status,
                    "matched": False,
                    "age_seconds": None,
                    "threshold_seconds": 90.0,
                    "differences": ["NO_RECONCILIATION_RESULT"],
                    "source": "engine._last_reconciliation_result",
                },
            )
        )
    else:
        reconciliation_fresh = reconciliation_age is not None and reconciliation_age <= _max_age
        reconciliation_ok = (
            bool(getattr(reconciliation, "matched", False))
            and reconciliation_status == "MATCHED"
            and reconciliation_fresh
        )
        if reconciliation_ok:
            recon_status = CheckStatus.PASS
            recon_message = f"三方对账 MATCHED，事实年龄 {reconciliation_age:.1f}s"
        else:
            recon_status = CheckStatus.FAIL
            recon_message = (
                f"三方对账不可授权: status={reconciliation_status}, "
                f"matched={bool(getattr(reconciliation, 'matched', False))}, "
                f"age={reconciliation_age if reconciliation_age is not None else 'UNKNOWN'}s"
            )
        checks.append(
            CheckResult(
                check_id="runtime.safety.reconciliation_authority",
                name="权威三方对账事实",
                status=recon_status,
                severity=CheckSeverity.P0,
                message=recon_message,
                evidence={
                    "status": reconciliation_status,
                    "matched": bool(getattr(reconciliation, "matched", False)) if reconciliation else False,
                    "age_seconds": round(reconciliation_age, 3) if reconciliation_age is not None else None,
                    "threshold_seconds": 90.0,
                    "differences": list(getattr(reconciliation, "differences", []) or [])[:20]
                    if reconciliation is not None
                    else ["NO_RECONCILIATION_RESULT"],
                    "source": "engine._last_reconciliation_result",
                },
            )
        )

    # A durable projector or REST snapshot cannot prove that this process is
    # still receiving venue user-data events.  Writable engines therefore
    # require an explicit transport status plus a recent complete event fact;
    # a missing runtime boundary is a P0 UNKNOWN even before RESUME.
    can_write = bool(getattr(engine, "_can_write", False))
    user_stream_ready = False
    user_stream_evidence: dict[str, Any] = {"required": can_write}
    readiness_fn = getattr(engine, "_user_stream_readiness", None)
    if callable(readiness_fn):
        try:
            user_stream_ready, user_stream_evidence = readiness_fn()
        except Exception as exc:
            user_stream_evidence = {"required": can_write, "error": type(exc).__name__}
    elif can_write:
        user_stream_evidence = {"required": True, "status": "UNKNOWN", "reason": "runtime_boundary_missing"}
    else:
        user_stream_ready = True
    if can_write:
        user_stream_status = CheckStatus.PASS if user_stream_ready else CheckStatus.FAIL
        user_stream_severity = CheckSeverity.P0
        user_stream_message = (
            "用户数据流已连接且有新鲜完整事实"
            if user_stream_ready
            else "用户数据流未形成当前进程的可验证新鲜事实，禁止交易授权"
        )
    else:
        user_stream_status = CheckStatus.PASS
        user_stream_severity = CheckSeverity.P1
        user_stream_message = "写能力关闭；用户数据流不参与本模式授权"
    checks.append(
        CheckResult(
            check_id="runtime.safety.user_stream",
            name="用户数据流事实",
            status=user_stream_status,
            severity=user_stream_severity,
            message=user_stream_message,
            evidence=user_stream_evidence,
        )
    )

    nearline_age = now - float(getattr(engine, "_last_nearline", 0.0))
    if not resume_authorized:
        nearline_ok = bool(algorithm_probe.get("ok"))
        nearline_message = "真实行情只读 Alpha DAG 探针通过" if nearline_ok else "真实行情只读 Alpha DAG 探针尚未通过"
        nearline_evidence = dict(algorithm_probe)
    else:
        nearline_ok = nearline_age <= 420.0
        nearline_message = f"最近 nearline tick {nearline_age:.1f}s 前"
        nearline_evidence = {"age_seconds": round(nearline_age, 3), "threshold_seconds": 420.0}
    checks.append(
        CheckResult(
            check_id="runtime.health.nearline_heartbeat",
            name="近线策略循环与算法执行",
            status=CheckStatus.PASS if nearline_ok else CheckStatus.FAIL,
            severity=CheckSeverity.P0,
            message=nearline_message,
            evidence=nearline_evidence,
        )
    )

    error_count = int(getattr(engine, "_error_count", 0))
    error_delta = error_count - last_error_count
    # BD-FIX: 只看本周期新增 —— 累计硬阈值（<100）让 demo 抖动期间
    # 的历史错误（final56 累计 420）永久 FAIL（累计值从不回落）。
    # 爆发检测的核心信号是本周期新增速率；累计值只作 evidence。
    errors_ok = error_delta <= 5
    checks.append(
        CheckResult(
            check_id="runtime.health.errors",
            name="运行异常计数",
            status=CheckStatus.PASS if errors_ok else CheckStatus.FAIL,
            severity=CheckSeverity.P2,
            message=f"累计错误={error_count}, 本周期新增={error_delta}",
            evidence={"total": error_count, "delta": error_delta},
        )
    )

    # 账户快照、对账、保护覆盖 三项检查已迁移至 monitoring/ 子系统
    # (beidou_observability.monitoring.checks.account / reconciliation / protection)
    # 由 collect_monitoring_checks() 统一执行深度检查，
    # 避免 runtime.py 与 monitoring/ 双重维护同一逻辑。

    incident_query_failed = False
    try:
        incidents = list(engine._alerts.get_active_incidents())
    except Exception:
        incidents = ["INCIDENT_QUERY_FAILED"]
        incident_query_failed = True
    incident_severity = CheckSeverity.P0 if incident_query_failed else active_incident_blocking_severity(incidents)
    incident_ok = not incidents
    incident_blocking = incident_severity is not None
    # HIGH/CRITICAL/LOCKDOWN incidents represent unresolved safety authority
    # loss, even when the originating check has not yet been re-collected in
    # this monitoring cycle.  Keeping these as P2 warnings allowed testnet
    # auto-recovery to RESUME while reconciliation incidents were still open.
    checks.append(
        CheckResult(
            check_id="runtime.health.incidents",
            name="活动事故",
            status=CheckStatus.FAIL
            if incident_blocking
            else (CheckStatus.WARN if not incident_ok else CheckStatus.PASS),
            severity=incident_severity or CheckSeverity.P2,
            message="无活动事故" if incident_ok else f"活动事故: {incidents}",
            evidence={"incidents": incidents},
        )
    )

    # gap reason 贯通 (可观测性修复): 保护缺口明细单独成检查, 供 supervisor
    # A/B 分流区分"引擎可修复的覆盖缺口"与"真故障"。有缺口时必须是 FAIL
    # 而非 WARN —— supervisor 只把 FAIL/UNKNOWN 的 P0/P1 检查收进 blockers
    # 列表并进入防抖;WARN 永远进不了 blockers, 分流机制会静默失效。
    # severity 保持 P1 (报告型证据; 资格门已由 protection 覆盖检查另行阻断)。
    _gap_detail = getattr(engine, "_last_protection_gap_detail", None) or []
    _repairable = bool(_gap_detail) and all(
        str(g.get("reason", ""))
        in {"STOP_LOSS_QUANTITY_UNCOVERED", "MISSING_SL", "MISSING_TP", "ORPHAN_PROTECTION_WITHOUT_VENUE_POSITION"}
        for g in _gap_detail
    )
    checks.append(
        CheckResult(
            check_id="runtime.safety.protection_gap_detail",
            name="保护缺口明细",
            status=CheckStatus.FAIL if _gap_detail else CheckStatus.PASS,
            severity=CheckSeverity.P1,
            message=(f"保护缺口: {_gap_detail}" if _gap_detail else "无保护缺口"),
            evidence={"gaps": _gap_detail, "repairable": _repairable},
        )
    )

    # Webhook delivery is part of the unattended safety certificate when the
    # dispatcher exposes durable delivery state.  Older test doubles without
    # this method are left untouched; the production dispatcher must not hide
    # CRITICAL/LOCKDOWN delivery UNKNOWN or dead-letter states.
    delivery_getter = getattr(getattr(engine, "_alerts", None), "get_delivery_health", None)
    if callable(delivery_getter):
        try:
            delivery = delivery_getter()
            critical_pending = int(delivery.get("critical_pending", 0))
            dead_letter = int(delivery.get("dead_letter", 0))
            unknown = int(delivery.get("unknown", 0))
            pending = int(delivery.get("pending", 0))
            configured = bool(delivery.get("configured", False))
            # BD-FIX: dead_letter 不再 P0 阻断 —— 死信现在可重试
            # （retry_pending 重置预算后重投，幂等键在），重试间隙
            # 的残留计数不应触发 LOCKED（C4 审查：旧逻辑死信永久
            # P0 → LOCKED → 跨重启崩溃循环）。critical_pending/unknown
            # 仍保持 P0（交付确实不可证明）。
            if critical_pending or unknown:
                # PKG02: 所有环境统一交付状态检查标准。
                delivery_status = CheckStatus.FAIL
                delivery_severity = CheckSeverity.P0
                delivery_message = (
                    f"告警送达不可证明: critical_pending={critical_pending}, "
                    f"dead_letter={dead_letter}, unknown={unknown}"
                )
            elif pending or dead_letter:
                delivery_status = CheckStatus.WARN
                delivery_severity = CheckSeverity.P1
                delivery_message = f"告警仍在重试队列: pending={pending}, dead_letter={dead_letter}（死信可重试）"
            elif not configured:
                delivery_status = CheckStatus.WARN
                delivery_severity = CheckSeverity.P1
                delivery_message = "未配置外部 webhook；仅本地告警文件可用"
            else:
                delivery_status = CheckStatus.PASS
                delivery_severity = CheckSeverity.P1
                delivery_message = "告警 webhook 当前无待投递事实"
            checks.append(
                CheckResult(
                    check_id="runtime.health.alert_delivery",
                    name="告警送达状态",
                    status=delivery_status,
                    severity=delivery_severity,
                    message=delivery_message,
                    evidence=delivery,
                )
            )
        except Exception as exc:
            checks.append(
                CheckResult(
                    check_id="runtime.health.alert_delivery",
                    name="告警送达状态",
                    status=CheckStatus.FAIL,
                    severity=CheckSeverity.P0,
                    message=f"告警送达状态查询失败: {type(exc).__name__}",
                    evidence={"error": type(exc).__name__},
                )
            )
    return checks, error_count
