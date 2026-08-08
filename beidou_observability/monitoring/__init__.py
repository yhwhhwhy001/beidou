"""北斗运营保障面 — 监控模块 V1.1。"""

from beidou_observability.monitoring.clock_integrity import ClockIntegrity
from beidou_observability.monitoring.contracts import *
from beidou_observability.monitoring.evidence import (
    EvidenceBundle,
    EvidenceRecord,
    from_check_result,
    verify_evidence_integrity,
)
from beidou_observability.monitoring.fact_collector import CollectedFact, FactCollector, FactDomain
from beidou_observability.monitoring.frequency_policy import (
    LEVEL_INTERVALS,
    PROMOTION_STREAK_REQUIRED,
    init_frequency_state,
    is_due,
    is_promotion_clean,
    restart_frequency_state,
    update_frequency,
)
from beidou_observability.monitoring.health_aggregator import (
    aggregate_health,
    enforce_inv006,
    enforce_inv007,
    health_summary,
)
from beidou_observability.monitoring.incident_manager import IncidentManager
from beidou_observability.monitoring.instrumentation import InstrumentationPolicy, InstrumentationRecorder, TraceEvent
from beidou_observability.monitoring.rate_limit_budget import EndpointBudget, RateLimitBudget
from beidou_observability.monitoring.repository import MonitoringRepository
from beidou_observability.monitoring.retention import RetentionPolicy, RetentionTier
from beidou_observability.monitoring.rollout import RolloutManager
from beidou_observability.monitoring.scheduler import DeepAuditScheduler
from beidou_observability.monitoring.service import MonitoringService, create_monitoring_cli
from beidou_observability.monitoring.snapshot_broker import SnapshotBroker, SnapshotFact, SnapshotSource
from beidou_observability.monitoring.storm_detector import StormDetector
from beidou_observability.monitoring.watchdog import COMPONENT_FAILURE_MATRIX, Watchdog


def _protection_order_fact(order, *, kind: str, symbol: str):
    """把引擎 ProtectionOrder 转换为监控子系统的 ProtectionFact。"""
    from beidou_observability.monitoring.checks.protection import ProtectionFact

    protection_id = str(getattr(order, "protection_id", "") or "")
    return ProtectionFact(
        protection_id=protection_id,
        position_key=symbol,
        kind=kind,
        order_id="",
        side=str(getattr(getattr(order, "side", None), "value", "") or ""),
        position_side="BOTH",
        quantity=str(getattr(order, "quantity", "") or ""),
        trigger_price=str(getattr(order, "trigger_price", "") or ""),
        status=str(getattr(getattr(order, "status", None), "value", "") or ""),
        origin_trace_id=protection_id,  # 每个保护单的 protection_id 唯一，SL/TP 不会误判为重复
        strategy_id="autopilot",
        generation=0,
    )


def _map_trace_stage(order_status: str) -> "TraceStage":
    from beidou_observability.monitoring.contracts import TraceStage

    mapping = {
        "NEW": TraceStage.INTENT_CREATED,
        "PARTIALLY_FILLED": TraceStage.EXCHANGE_ACKED,
        "PENDING_CANCEL": TraceStage.EXCHANGE_ACKED,
        "FILLED": TraceStage.FILLED,
        "CANCELED": TraceStage.TERMINAL,
        "REJECTED": TraceStage.TERMINAL,
        "EXPIRED": TraceStage.TERMINAL,
        "UNKNOWN": TraceStage.UNKNOWN,
    }
    return mapping.get(order_status, TraceStage.UNKNOWN)


def collect_monitoring_checks(
    engine=None,
    *,
    supervisor=None,
    exchange_account_snapshot=None,
    algorithm_probe=None,
    position_mode_evidence=None,
    last_loop_at=0.0,
    monitor_stall_threshold=30.0,
):
    """运行监控子系统深度检查并将结果转换为监督器 CheckResult（桥接入口）。

    覆盖：账户健康 (MON03/INV-002)、保护覆盖 (PKG-MON-04)、
    深度对账 (MON03 R1~R6)、执行质量订单轨迹 (PKG-MON-05)、
    算法探针、模块进度 (PKG-MON-06) 与监控自身健康 (PKG-MON-10)。

    engine: AutonomousEngine 引用；supervisor: BeidouSupervisor 引用（可为 None）。
    返回 list[beidou_launcher.models.CheckResult]，与监督器检查流直接兼容。
    """
    import time

    # 延迟导入避免初始化期循环依赖（beidou_launcher 已导入本包）
    from beidou_launcher.models import CheckResult, CheckSeverity, CheckStatus

    from beidou_observability.monitoring.checks.account import check_account_unknown, check_balance_sanity
    from beidou_observability.monitoring.checks.execution import OrderTraceState, check_order_trace
    from beidou_observability.monitoring.checks.modules import check_algorithm_probe, check_module_progress
    from beidou_observability.monitoring.checks.monitor_self import check_component_health, check_monitor_loop_health
    from beidou_observability.monitoring.checks.protection import (
        ExchangeEconomicPosition,
        build_protection_check,
        verify_position_protection,
    )
    from beidou_observability.monitoring.checks.reconciliation import (
        build_reconciliation_check,
        perform_reconciliation,
    )
    from beidou_observability.monitoring.contracts import (
        AccountPositionMode as MonAccountPositionMode,
        CheckSeverity as MonCheckSeverity,
        ModuleProgressContract,
    )

    def convert(result, *, name: str) -> CheckResult:
        """MonitoringCheckResult → 监督器 CheckResult。"""
        return CheckResult(
            check_id=result.check_id,
            name=name,
            status=CheckStatus(result.status.value),
            severity=CheckSeverity(result.severity.value),
            message=result.message,
            evidence={
                "entity_type": result.entity_type,
                "entity_id": result.entity_id,
                "observed_at": result.observed_at,
                "fact_age_ms": result.fact_age_ms,
                "source": result.source,
                "evidence_hash": result.evidence_hash,
                "remediation": result.remediation,
                "policy_version": result.policy_version,
            },
        )

    results: list[CheckResult] = []

    # === 1. 账户健康 (MON03/INV-002) ===
    snapshot = exchange_account_snapshot if isinstance(exchange_account_snapshot, dict) else None
    results.append(convert(check_account_unknown(snapshot), name="账户事实健康 (MON03)"))
    account_data = (snapshot or {}).get("account") if (snapshot or {}).get("ok") else None
    results.append(convert(check_balance_sanity(account_data), name="账户余额合理性"))

    # === 2. 算法探针 ===
    probe = algorithm_probe if isinstance(algorithm_probe, dict) else None
    results.append(convert(check_algorithm_probe(probe), name="只读算法探针"))

    if engine is None:
        return results

    # === 3. 模块进度 (PKG-MON-06/INV-004) ===
    try:
        last_realtime = float(getattr(engine, "_last_realtime", 0.0) or 0.0)
        last_nearline = float(getattr(engine, "_last_nearline", 0.0) or 0.0)
        last_offline = float(getattr(engine, "_last_offline", 0.0) or 0.0)
        last_recon = float(getattr(engine, "_last_recon", 0.0) or 0.0)
        contracts = [
            ModuleProgressContract(
                module_id="realtime_loop",
                criticality=MonCheckSeverity.P1,
                progress_timeout_seconds=60.0,
                last_progress_at=last_realtime,
            ),
            ModuleProgressContract(
                module_id="nearline_loop",
                criticality=MonCheckSeverity.P1,
                progress_timeout_seconds=420.0,
                last_progress_at=last_nearline,
            ),
            ModuleProgressContract(
                module_id="offline_loop",
                criticality=MonCheckSeverity.P1,
                progress_timeout_seconds=7200.0,
                last_progress_at=last_offline,
            ),
            ModuleProgressContract(
                module_id="reconciliation",
                criticality=MonCheckSeverity.P1,
                progress_timeout_seconds=120.0,
                last_progress_at=last_recon,
            ),
        ]
        for item in check_module_progress(contracts):
            results.append(convert(item, name="模块进度健康 (INV-004)"))
    except Exception:
        pass

    # === 4. 保护覆盖 (PKG-MON-04) ===
    try:
        last_account = getattr(engine, "_last_account", None) or {}
        ex_positions = last_account.get("positions", []) if isinstance(last_account, dict) else []
        exchange_positions = []
        for p in ex_positions:
            try:
                if abs(float(p.get("positionAmt", 0) or 0)) <= 1e-6:
                    continue
                exchange_positions.append(
                    ExchangeEconomicPosition(
                        symbol=str(p.get("symbol", "")),
                        position_amt=str(p.get("positionAmt", "0")),
                        entry_price=str(p.get("entryPrice", "0") or "0"),
                        position_side=str(p.get("positionSide", "BOTH") or "BOTH"),
                    )
                )
            except (TypeError, ValueError):
                continue
        mode = MonAccountPositionMode.UNKNOWN
        if position_mode_evidence is not None:
            pm = getattr(position_mode_evidence, "mode", None)
            raw_mode = getattr(pm, "value", pm)
            try:
                mode = MonAccountPositionMode(raw_mode) if raw_mode is not None else mode
            except ValueError:
                mode = MonAccountPositionMode.UNKNOWN
        local_by_symbol: dict[str, list] = {}
        protection = getattr(engine, "_protection", None)
        if protection is not None and callable(getattr(protection, "all_positions", None)):
            try:
                for pp in protection.all_positions().values():
                    symbol = str(getattr(pp, "instrument_id", "") or "")
                    facts = local_by_symbol.setdefault(symbol, [])
                    sl = getattr(pp, "stop_loss", None)
                    if sl is not None:
                        facts.append(_protection_order_fact(sl, kind="SL", symbol=symbol))
                    for tp in getattr(pp, "take_profits", []) or []:
                        facts.append(_protection_order_fact(tp, kind="TP", symbol=symbol))
            except Exception:
                pass
        for ep in exchange_positions:
            semantic = verify_position_protection(ep, local_by_symbol.get(ep.symbol, []), mode)
            results.append(convert(build_protection_check(semantic), name="持仓保护覆盖 (PKG-MON-04)"))
    except Exception:
        pass

    # === 5. 深度对账 (MON03 R1~R6) ===
    try:
        last_account = getattr(engine, "_last_account", None) or {}
        ex_positions_raw = last_account.get("positions", []) if isinstance(last_account, dict) else []
        # 过滤零仓位：Binance API 返回全部合约仓位（含零余额），
        # 不过滤会导致 hundreds 个零仓位与本地保护单对比产生大量 UNKNOWN
        ex_positions: list[dict] = []
        for p in ex_positions_raw:
            try:
                if abs(float(p.get("positionAmt", 0) or 0)) <= 1e-6:
                    continue
                ex_positions.append(p)
            except (TypeError, ValueError):
                continue
        local_positions = []
        protection = getattr(engine, "_protection", None)
        if protection is not None and callable(getattr(protection, "all_positions", None)):
            for pp in protection.all_positions().values():
                try:
                    qty = float(getattr(pp, "quantity", 0.0) or 0.0)
                    if qty == 0:
                        continue
                    side = str(getattr(getattr(pp, "side", None), "value", "") or "")
                    amt = qty if side == "BUY" else -qty
                    local_positions.append(
                        {"symbol": str(getattr(pp, "instrument_id", "") or ""), "positionAmt": str(amt)}
                    )
                except (TypeError, ValueError):
                    continue
        # 双方均有持仓事实时才运行深度对账，避免无数据时的假 PASS
        if ex_positions and local_positions:
            ledger = getattr(engine, "_ledger", None)
            local_ledger = getattr(ledger, "_entries", None) if ledger is not None else None
            recon = perform_reconciliation(ex_positions, local_positions, None, None, local_ledger)
            results.append(convert(build_reconciliation_check(recon), name="深度对账 (MON03 R1~R6)"))
    except Exception:
        pass

    # === 6. 执行质量 — 订单轨迹 (PKG-MON-05) ===
    try:
        order_trackers = getattr(engine, "_order_trackers", None)
        if isinstance(order_trackers, dict) and order_trackers:
            active_order_ids = getattr(engine, "_active_order_ids", None) or set()
            order_symbols = getattr(engine, "_order_symbols", None) or {}
            now_ts = time.time()
            traces = []
            for order_id, tracker in order_trackers.items():
                if order_id not in active_order_ids:
                    continue  # 只监控在途订单
                if callable(getattr(tracker, "is_terminal", None)) and tracker.is_terminal():
                    continue
                events = getattr(tracker, "events", None) or []
                last_ts = 0.0
                if events:
                    last_dt = events[-1][1]
                    last_ts = last_dt.timestamp() if hasattr(last_dt, "timestamp") else 0.0
                status = str(getattr(getattr(tracker, "status", None), "value", "UNKNOWN"))
                traces.append(
                    OrderTraceState(
                        correlation_id=str(getattr(tracker, "correlation_id", None) or order_id),
                        client_order_id=str(order_id),
                        symbol=str(order_symbols.get(order_id, "") or ""),
                        current_stage=_map_trace_stage(status),
                        stuck_since=last_ts if last_ts > 0 else 0.0,
                        started_at=last_ts,
                    )
                )
            for item in check_order_trace(traces):
                results.append(convert(item, name="订单执行轨迹 (PKG-MON-05)"))
    except Exception:
        pass

    # === 7. 监控自身健康 (PKG-MON-10) ===
    try:
        if last_loop_at and last_loop_at > 0:
            results.append(
                convert(
                    check_monitor_loop_health(last_loop_at, max_stall=monitor_stall_threshold),
                    name="监督循环自身健康 (PKG-MON-10)",
                )
            )
        if supervisor is not None:
            component_health = getattr(supervisor, "_monitoring_component_health", None)
            if component_health:
                for item in check_component_health(component_health):
                    results.append(convert(item, name="监控组件健康 (PKG-MON-10)"))
    except Exception:
        pass

    return results


def run_monitoring_bridge(engine: Any, supervisor_state: dict | None = None) -> list[dict[str, Any]]:
    """BD-FIX: 监控子系统桥接 — 将 2000+ 行监控管线接入运行时。

    由 supervisor 在每个监控周期调用。运行关键检查并返回结构化结果，
    兼容 supervisor 的 CheckResult 模型。

    Returns:
        list of check dicts with keys: check_id, status, severity, message, evidence
    """
    results: list[dict[str, Any]] = []
    if engine is None:
        return results

    # 1. 保护覆盖率检查
    try:
        protection = getattr(engine, "_protection", None)
        if protection is not None:
            positions = protection.all_positions()
            covered = sum(1 for pp in positions.values() if pp.stop_loss is not None)
            total = len(positions)
            ok = total == 0 or covered == total
            results.append({
                "check_id": "monitoring.protection.coverage",
                "name": "保护覆盖率 (Monitoring)",
                "status": "PASS" if ok else "FAIL",
                "severity": "P0",
                "message": f"{covered}/{total} positions protected",
                "evidence": {"covered": covered, "total": total},
            })
    except Exception as e:
        results.append({
            "check_id": "monitoring.protection.coverage",
            "status": "FAIL", "severity": "P1",
            "message": f"Protection check error: {e}",
        })

    # 2. 账本平衡检查
    try:
        ledger = getattr(engine, "_ledger", None)
        if ledger is not None:
            balanced = ledger.is_balanced()
            results.append({
                "check_id": "monitoring.ledger.balance",
                "name": "账本平衡 (Monitoring)",
                "status": "PASS" if balanced else "FAIL",
                "severity": "P0",
                "message": f"Ledger balanced={balanced}, entries={len(ledger._entries)}",
                "evidence": {"balanced": balanced, "entries": len(ledger._entries)},
            })
    except Exception as e:
        results.append({
            "check_id": "monitoring.ledger.balance",
            "status": "FAIL", "severity": "P1",
            "message": f"Ledger check error: {e}",
        })

    # 3. 对账状态检查
    try:
        recon = getattr(engine, "_recon", None)
        if recon is not None:
            result = recon.reconcile("default", "BINANCE")
            recon_ok = result.matched
            results.append({
                "check_id": "monitoring.reconciliation.status",
                "name": "对账状态 (Monitoring)",
                "status": "PASS" if recon_ok else "FAIL",
                "severity": "P0",
                "message": f"Reconciliation: {result.status.value}",
                "evidence": {"status": result.status.value, "differences": result.differences},
            })
    except Exception as e:
        results.append({
            "check_id": "monitoring.reconciliation.status",
            "status": "FAIL", "severity": "P1",
            "message": f"Reconciliation check error: {e}",
        })

    # 4. 因子健康检查
    try:
        factor_registry = getattr(engine, "_factor_registry", None)
        if factor_registry is not None:
            active = factor_registry.get_active()
            degraded = [fid for fid, rec in factor_registry._factors.items()
                       if str(getattr(rec.lifecycle, "value", rec.lifecycle)) == "DEGRADED"]
            results.append({
                "check_id": "monitoring.factors.health",
                "name": "因子健康 (Monitoring)",
                "status": "WARN" if degraded else "PASS",
                "severity": "P1",
                "message": f"Active={len(active)}, Degraded={len(degraded)}",
                "evidence": {"active": len(active), "degraded": degraded},
            })
    except Exception as e:
        results.append({
            "check_id": "monitoring.factors.health",
            "status": "FAIL", "severity": "P2",
            "message": f"Factor check error: {e}",
        })

    return results
