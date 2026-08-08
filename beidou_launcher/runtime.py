"""真实行情只读算法探针与持续运行健康检查。"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from .models import CheckResult, CheckSeverity, CheckStatus
from .registry import inspect_engine_wiring


async def run_read_only_algorithm_probe(engine: Any, symbols: list[str]) -> dict[str, Any]:
    """使用真实公共行情执行 Alpha DAG，不提交 OrderIntent。"""
    symbol = symbols[0]
    started = time.perf_counter()
    try:
        feed = engine._feed
        kline_features = await asyncio.to_thread(feed.get_kline_features, symbol)
        live_features = await asyncio.to_thread(feed.update_features, symbol)
        features = {**kline_features, **live_features}
        if not kline_features or not live_features or float(features.get("close", features.get("price", 0))) <= 0:
            raise RuntimeError("真实 K 线、ticker 或 orderbook 不完整")

        from beidou_shared.types import InstrumentId, VenueId

        state_builder = getattr(engine, "_estimate_market_state", None)
        state = state_builder(features) if callable(state_builder) else {}
        context = {
            "features": features,
            "instrument_id": InstrumentId(symbol),
            "venue_id": VenueId("BINANCE"),
            "state": state,
            "_predictions": {},
            "_position_info": {
                "has_position": False,
                "entry_price": 0.0,
                "side": "",
                "entry_time": 0.0,
                "pnl_pct": 0.0,
            },
        }
        graph = engine._alpha_graph
        signals: list[Any] = []
        component_results: dict[str, dict[str, Any]] = {}
        for component_id in graph.topological_order():
            component = graph._components[component_id]
            signal = await component.generate(context)
            signals.append(signal)
            raw_direction = getattr(signal, "direction", "UNKNOWN")
            component_results[component_id] = {
                "direction": str(getattr(raw_direction, "value", raw_direction)),
                "strength": float(getattr(signal, "strength", 0.0)),
            }
        proposal = max(signals, key=lambda item: float(getattr(item, "strength", 0.0)))
        proposal_hash = engine._strategy_kernel.compute_proposal_hash(proposal)
        if not proposal_hash or len(component_results) != 8:
            raise RuntimeError("策略探针未生成完整 8 节点证据或 proposal hash")
        return {
            "ok": True,
            "symbol": symbol,
            "proposal_hash": proposal_hash,
            "components": component_results,
            "feature_count": len(features),
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "timestamp": datetime.now(timezone.utc).isoformat(),
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
    # 启动阶段行情可能尚未到达，降级为非阻断
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

    realtime_age = now - float(getattr(engine, "_last_realtime", 0.0))
    realtime_ok = realtime_age <= 60.0
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
            evidence={"age_seconds": round(realtime_age, 3), "threshold_seconds": 60.0},
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
    errors_ok = error_delta <= 5 and error_count < 100
    checks.append(
        CheckResult(
            check_id="runtime.health.errors",
            name="运行异常计数",
            status=CheckStatus.PASS if errors_ok else CheckStatus.FAIL,
            severity=CheckSeverity.P2,
            message=f"累计错误={error_count}, 本周期新增={error_delta}",
            evidence={"total": error_count, "delta": error_delta, "fatal_threshold": 100},
        )
    )

    # 账户快照、对账、保护覆盖 三项检查已迁移至 monitoring/ 子系统
    # (beidou_observability.monitoring.checks.account / reconciliation / protection)
    # 由 collect_monitoring_checks() 统一执行深度检查，
    # 避免 runtime.py 与 monitoring/ 双重维护同一逻辑。

    try:
        incidents = list(engine._alerts.get_active_incidents())
    except Exception:
        incidents = ["INCIDENT_QUERY_FAILED"]
    incident_ok = not incidents
    # 事故是其他检查（对账、保护覆盖）的症状，不独立作为 P0 阻断；
    # P0 FAIL 仅在其他检查已报告，此处降级为 P2 WARN 避免事故反馈循环。
    checks.append(
        CheckResult(
            check_id="runtime.health.incidents",
            name="活动事故",
            status=CheckStatus.WARN if not incident_ok else CheckStatus.PASS,
            severity=CheckSeverity.P2,
            message="无活动事故" if incident_ok else f"活动事故: {incidents}",
            evidence={"incidents": incidents},
        )
    )
    return checks, error_count
