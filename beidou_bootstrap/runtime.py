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


def collect_runtime_checks(
    *,
    engine: Any,
    mode: str,
    port: int,
    resume_authorized: bool,
    algorithm_probe: dict[str, Any],
    last_error_count: int,
) -> tuple[list[CheckResult], int]:
    checks = inspect_engine_wiring(engine, mode)
    now = time.time()

    lifecycle = getattr(getattr(engine, "_lifecycle", None), "state", None)
    lifecycle_value = str(getattr(lifecycle, "value", lifecycle))
    lifecycle_ok = lifecycle_value == "ACTIVE"
    checks.append(
        CheckResult(
            check_id="runtime.health.lifecycle",
            name="引擎生命周期",
            status=CheckStatus.PASS if lifecycle_ok else CheckStatus.FAIL,
            severity=CheckSeverity.P0,
            message=f"生命周期={lifecycle_value}",
            evidence={"state": lifecycle_value},
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
    checks.append(
        CheckResult(
            check_id="runtime.health.market_data",
            name="行情数据健康",
            status=CheckStatus.PASS if feed_healthy else CheckStatus.FAIL,
            severity=CheckSeverity.P0,
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
    realtime_ok = realtime_age <= 30.0
    checks.append(
        CheckResult(
            check_id="runtime.health.realtime_heartbeat",
            name="实时循环心跳",
            status=CheckStatus.PASS if realtime_ok else CheckStatus.FAIL,
            severity=CheckSeverity.P0,
            message=f"最近实时 tick {realtime_age:.1f}s 前",
            evidence={"age_seconds": round(realtime_age, 3), "threshold_seconds": 30.0},
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
            severity=CheckSeverity.P0,
            message=f"累计错误={error_count}, 本周期新增={error_delta}",
            evidence={"total": error_count, "delta": error_delta, "fatal_threshold": 100},
        )
    )

    account = getattr(engine, "_last_account", {})
    account_ok = isinstance(account, dict) and bool(account.get("totalWalletBalance"))
    checks.append(
        CheckResult(
            check_id="runtime.health.account_snapshot",
            name="账户事实快照",
            status=CheckStatus.PASS if account_ok else CheckStatus.FAIL,
            severity=CheckSeverity.P0,
            message="账户快照可用" if account_ok else "账户状态 UNKNOWN，禁止增加风险",
            evidence={"has_wallet_balance": account_ok},
        )
    )

    if mode == "testnet" and isinstance(account, dict):
        open_symbols = {
            str(position.get("symbol", ""))
            for position in account.get("positions", [])
            if abs(float(position.get("positionAmt", 0) or 0)) > 0
        }
        protection = getattr(engine, "_protection", None)
        try:
            protected_symbols = (
                {str(item.instrument_id) for item in protection.all_positions().values()}
                if protection is not None
                else set()
            )
        except Exception:
            protected_symbols = set()
        missing_protection = sorted(open_symbols - protected_symbols)
        coverage_ok = not missing_protection
        checks.append(
            CheckResult(
                check_id="runtime.safety.protection_coverage",
                name="持仓保护覆盖",
                status=CheckStatus.PASS if coverage_ok else CheckStatus.FAIL,
                severity=CheckSeverity.P0,
                message=(
                    f"全部 {len(open_symbols)} 个持仓标的均有保护"
                    if coverage_ok
                    else f"存在未保护持仓标的: {missing_protection}"
                ),
                evidence={
                    "open_symbols": sorted(open_symbols),
                    "protected_symbols": sorted(protected_symbols),
                    "missing": missing_protection,
                },
            )
        )

    try:
        incidents = list(engine._alerts.get_active_incidents())
    except Exception:
        incidents = ["INCIDENT_QUERY_FAILED"]
    incident_ok = not incidents
    checks.append(
        CheckResult(
            check_id="runtime.health.incidents",
            name="活动事故",
            status=CheckStatus.PASS if incident_ok else CheckStatus.FAIL,
            severity=CheckSeverity.P0,
            message="无活动事故" if incident_ok else f"活动事故: {incidents}",
            evidence={"incidents": incidents},
        )
    )
    return checks, error_count
