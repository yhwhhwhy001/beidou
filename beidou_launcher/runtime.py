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
    checks: list[CheckResult], evidence: Any, now: float,
) -> None:
    """MON00A: Position Mode 运行时检查。

    - UNKNOWN → P0 FAIL (MON00A-05)
    - 无 evidence → P1 WARN (启动阶段)
    - ONE_WAY/HEDGE → PASS
    """
    if evidence is None:
        checks.append(CheckResult(
            check_id="runtime.safety.position_mode",
            name="账户持仓模式",
            status=CheckStatus.WARN, severity=CheckSeverity.P1,
            message="Position Mode 尚未查询，等待交易所响应",
            evidence={"mode": "UNKNOWN", "reason": "NO_EVIDENCE_YET"},
        ))
        return

    mode = getattr(evidence, "mode", None)
    if mode is None or str(mode) == "AccountPositionMode.UNKNOWN" or (
        hasattr(mode, "value") and mode.value == "UNKNOWN"
    ):
        checks.append(CheckResult(
            check_id="runtime.safety.position_mode",
            name="账户持仓模式",
            status=CheckStatus.FAIL, severity=CheckSeverity.P0,
            message=f"Position Mode UNKNOWN: {getattr(evidence, 'error', 'API_FAILURE')} — 阻止新增风险",
            evidence={"mode": "UNKNOWN", "error": getattr(evidence, "error", None)},
        ))
        return

    mode_str = mode.value if hasattr(mode, "value") else str(mode)
    checks.append(CheckResult(
        check_id="runtime.safety.position_mode",
        name="账户持仓模式",
        status=CheckStatus.PASS, severity=CheckSeverity.P0,
        message=f"Position Mode: {mode_str}",
        evidence={
            "mode": mode_str,
            "source": getattr(evidence, "source", ""),
            "source_timestamp": getattr(evidence, "source_timestamp", None),
            "observed_at": getattr(evidence, "observed_at", 0.0),
        },
    ))


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
        raw_control_state = control.get_status()
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
                f"已拦截 {len(blocked_writes)} 次非授权交易所写请求"
                if blocked_writes
                else "未发现非授权交易所写请求"
            ),
            evidence={"blocked_count": len(blocked_writes), "recent": blocked_writes[-20:]},
        )
    )

    feed = getattr(engine, "_feed", None)
    try:
        feed_internal_healthy = bool(feed.is_healthy())
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

    account_snapshot = exchange_account_snapshot or {"ok": False, "error": "NO_SNAPSHOT"}
    account_observed_at = float(account_snapshot.get("observed_at", 0.0) or 0.0)
    account_age = now - account_observed_at if account_observed_at > 0 else float("inf")
    account = account_snapshot.get("account", {})
    account_ok = (
        bool(account_snapshot.get("ok"))
        and account_age <= 45.0
        and isinstance(account, dict)
        and "totalWalletBalance" in account
        and isinstance(account.get("positions"), list)
    )
    checks.append(
        CheckResult(
            check_id="runtime.health.account_snapshot",
            name="账户事实快照",
            status=CheckStatus.PASS if account_ok else CheckStatus.FAIL,
            severity=CheckSeverity.P0,
            message=(
                f"独立账户快照可用，年龄={account_age:.1f}s"
                if account_ok
                else f"账户状态 UNKNOWN/STALE: {account_snapshot.get('error', 'INVALID_SCHEMA')}"
            ),
            evidence={
                "ok": account_ok,
                "age_seconds": round(account_age, 3) if account_age != float("inf") else None,
                "observed_at": account_observed_at or None,
                "error": account_snapshot.get("error"),
            },
        )
    )

    recon_age = now - float(getattr(engine, "_last_recon", 0.0))
    recon_status = "UNKNOWN"
    recon_differences: list[str] = []
    recon_snapshot_age = float("inf")
    try:
        reconciliation = engine._recon.reconcile("default", "BINANCE")
        raw_status = getattr(reconciliation, "status", "UNKNOWN")
        recon_status = str(getattr(raw_status, "value", raw_status))
        recon_differences = list(getattr(reconciliation, "differences", []))
        exchange_facts = getattr(reconciliation, "exchange_facts", None)
        system_facts = getattr(reconciliation, "system_facts", None)
        timestamps = [
            getattr(facts, "timestamp", None)
            for facts in (exchange_facts, system_facts)
            if facts is not None
        ]
        valid_timestamps = [item.timestamp() for item in timestamps if item is not None]
        if valid_timestamps:
            recon_snapshot_age = now - min(valid_timestamps)
    except Exception as exc:
        recon_differences = [f"RECONCILIATION_CHECK_ERROR: {type(exc).__name__}: {exc}"]
    recon_ok = recon_status == "MATCHED" and recon_age <= 120.0 and recon_snapshot_age <= 120.0
    # 对账宽限期：nearline 下单后 30s 内，订单可能尚未成交，
    # MISMATCHED 属于正常竞态，降级为 P2 WARN 避免误触发 DEGRADED。
    _last_order_placed_at = float(getattr(engine, "_last_order_placed_at", 0.0) or 0.0)
    _recon_grace_active = _last_order_placed_at > 0.0 and (now - _last_order_placed_at) <= 30.0
    # 启动阶段（resume_authorized=False）系统尚未同步交易所数据，
    # 对账不匹配属于正常现象，降级为 P2 WARN；运行时恢复 P0 阻断。
    if resume_authorized:
        if _recon_grace_active and not recon_ok:
            recon_severity = CheckSeverity.P2
            recon_status_check = CheckStatus.WARN
        else:
            recon_severity = CheckSeverity.P0
            recon_status_check = CheckStatus.PASS if recon_ok else CheckStatus.FAIL
    else:
        recon_severity = CheckSeverity.P2
        recon_status_check = CheckStatus.PASS if recon_ok else CheckStatus.WARN
    checks.append(
        CheckResult(
            check_id="runtime.safety.reconciliation",
            name="账户与订单对账",
            status=recon_status_check,
            severity=recon_severity,
            message=(
                f"对账状态=MATCHED，事实年龄={recon_snapshot_age:.1f}s"
                if recon_ok
                else f"对账未通过: status={recon_status}, differences={recon_differences[:3]}"
            ),
            evidence={
                "status": recon_status,
                "differences": recon_differences,
                "engine_heartbeat_age_seconds": round(recon_age, 3),
                "fact_snapshot_age_seconds": (
                    round(recon_snapshot_age, 3) if recon_snapshot_age != float("inf") else None
                ),
                "threshold_seconds": 120.0,
                "grace_period_active": _recon_grace_active,
                "last_order_placed_age_seconds": (
                    round(now - _last_order_placed_at, 3) if _last_order_placed_at > 0 else None
                ),
            },
        )
    )

    if mode == "testnet" and account_ok:
        positions = account.get("positions", [])
        open_symbols = {
            str(position.get("symbol", ""))
            for position in positions
            if abs(float(position.get("positionAmt", 0) or 0)) > 0
        }
        protection = getattr(engine, "_protection", None)
        protection_evidence: dict[str, list[dict[str, Any]]] = {}
        expected_by_symbol: dict[str, int] = {}
        try:
            local_positions = protection.all_positions() if protection is not None else {}
            for position_id, protected_position in local_positions.items():
                symbol = str(protected_position.instrument_id)

                def is_active(order: Any) -> bool:
                    if order is None:
                        return False
                    check = getattr(order, "is_active", None)
                    return bool(check()) if callable(check) else True

                expected_orders = int(is_active(protected_position.stop_loss)) + sum(
                    1 for order in protected_position.take_profits if is_active(order)
                )
                expected_by_symbol[symbol] = expected_by_symbol.get(symbol, 0) + expected_orders
                protection_evidence.setdefault(symbol, []).append(
                    {"position_id": str(position_id), "expected_orders": expected_orders}
                )
        except Exception as exc:
            protection_evidence = {"_error": [{"message": f"{type(exc).__name__}: {exc}"}]}
            expected_by_symbol = {}

        snapshot = exchange_algo_snapshot or {"ok": False, "by_symbol": {}, "error": "NO_SNAPSHOT"}
        snapshot_observed_at = float(snapshot.get("observed_at", 0.0) or 0.0)
        snapshot_age = now - snapshot_observed_at if snapshot_observed_at > 0 else float("inf")
        snapshot_ok = bool(snapshot.get("ok")) and snapshot_age <= 45.0
        server_by_symbol = snapshot.get("by_symbol", {}) if isinstance(snapshot.get("by_symbol", {}), dict) else {}
        exchange_protected_symbols: set[str] = set()
        for symbol in open_symbols:
            expected_orders = expected_by_symbol.get(symbol, 0)
            server_algo_ids = {str(item) for item in server_by_symbol.get(symbol, [])}
            server_order_count = len(server_algo_ids)
            fully_placed = snapshot_ok and expected_orders > 0 and server_order_count >= expected_orders
            protection_evidence.setdefault(symbol, []).append(
                {
                    "source": "exchange_openAlgoOrders",
                    "expected_orders": expected_orders,
                    "server_order_count": server_order_count,
                    "server_algo_ids": sorted(server_algo_ids),
                    "fully_placed": fully_placed,
                }
            )
            if fully_placed:
                exchange_protected_symbols.add(symbol)
        missing_protection = sorted(open_symbols - exchange_protected_symbols)
        all_protected = not missing_protection
        none_protected = not exchange_protected_symbols
        # 保护单宽限期：nearline 批量重建保护单时，cancel+re-place 存在窗口期。
        # 最近下单后 45s 内，protection_coverage FAIL 降级为 P2 WARN。
        _protection_grace_active = _last_order_placed_at > 0.0 and (now - _last_order_placed_at) <= 45.0
        # 部分保护：有持仓已覆盖但部分缺失 → WARN 降级，不阻断全系统
        # 启动阶段（resume_authorized=False）保护单尚未下发完毕，
        # FAIL 降级为 P2 WARN，与对账检查保持一致；运行时恢复 P0 阻断。
        if not snapshot_ok:
            coverage_ok = False
            if resume_authorized and not _protection_grace_active:
                coverage_status = CheckStatus.FAIL
                coverage_severity = CheckSeverity.P0
            else:
                coverage_status = CheckStatus.WARN
                coverage_severity = CheckSeverity.P2
            coverage_message = f"交易所保护事实查询失败: {snapshot.get('error', 'UNKNOWN')}"
        elif all_protected:
            coverage_ok = True
            coverage_status = CheckStatus.PASS
            coverage_severity = CheckSeverity.P0
            coverage_message = f"全部 {len(open_symbols)} 个持仓标的均有当前交易所保护单"
        elif none_protected:
            coverage_ok = False
            if resume_authorized and not _protection_grace_active:
                coverage_status = CheckStatus.FAIL
                coverage_severity = CheckSeverity.P0
            else:
                coverage_status = CheckStatus.WARN
                coverage_severity = CheckSeverity.P2
            coverage_message = f"全部 {len(open_symbols)} 个持仓标的均无保护单落地"
        else:
            coverage_ok = True
            coverage_status = CheckStatus.WARN
            coverage_severity = CheckSeverity.P1
            coverage_message = f"部分持仓标的保护单未落地: {missing_protection}（已保护: {sorted(exchange_protected_symbols)}）"
        checks.append(
            CheckResult(
                check_id="runtime.safety.protection_coverage",
                name="持仓保护覆盖",
                status=coverage_status,
                severity=coverage_severity,
                message=coverage_message,
                evidence={
                    "open_symbols": sorted(open_symbols),
                    "exchange_protected_symbols": sorted(exchange_protected_symbols),
                    "missing": missing_protection,
                    "exchange_snapshot": snapshot,
                    "exchange_snapshot_age_seconds": (
                        round(snapshot_age, 3) if snapshot_age != float("inf") else None
                    ),
                    "positions": protection_evidence,
                    "grace_period_active": _protection_grace_active,
                    "last_order_placed_age_seconds": (
                        round(now - _last_order_placed_at, 3) if _last_order_placed_at > 0 else None
                    ),
                },
            )
        )

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
