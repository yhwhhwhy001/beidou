"""北斗运营保障面 — 监控模块 V1.1。"""

import os
import time
from datetime import datetime, timezone

from beidou_observability.monitoring.clock_integrity import ClockIntegrity
from beidou_observability.monitoring.contracts import *
from beidou_observability.monitoring.contracts import TraceStage
from beidou_observability.monitoring.evidence import (
    EvidenceBundle,
    EvidenceRecord,
    from_check_result,
    verify_evidence_integrity,
)
from beidou_observability.monitoring.fact_bus import get_fact_bus  # P1-048: 操作事实总线
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
from beidou_observability.monitoring.instrumentation import InstrumentationPolicy, InstrumentationRecorder, TraceEvent
from beidou_observability.monitoring.rate_limit_budget import EndpointBudget, RateLimitBudget
from beidou_observability.monitoring.repository import MonitoringRepository
from beidou_observability.monitoring.retention import RetentionPolicy, RetentionTier
from beidou_observability.monitoring.rollout import RolloutManager
from beidou_observability.monitoring.scheduler import DeepAuditScheduler
from beidou_observability.monitoring.service import MonitoringService, create_monitoring_cli
from beidou_observability.monitoring.snapshot_broker import SnapshotBroker, SnapshotFact, SnapshotSource
from beidou_observability.monitoring.watchdog import COMPONENT_FAILURE_MATRIX, Watchdog


def _protection_order_fact(order, *, kind: str, symbol: str):
    """把引擎 ProtectionOrder 转换为监控子系统的 ProtectionFact。"""
    from beidou_observability.monitoring.checks.protection import ProtectionFact

    protection_id = str(getattr(order, "protection_id", "") or "")
    return ProtectionFact(
        protection_id=protection_id,
        position_key=symbol,
        kind=kind,
        # A local ProtectionOrder is only a desired definition until the
        # venue returns an algo/order id.  Monitoring must never treat a
        # CREATED/PENDING object as exchange coverage.
        order_id=str(getattr(order, "exchange_order_id", "") or ""),
        side=str(getattr(getattr(order, "side", None), "value", "") or ""),
        position_side="BOTH",
        quantity=str(getattr(order, "quantity", "") or ""),
        trigger_price=str(getattr(order, "trigger_price", "") or ""),
        status=str(getattr(getattr(order, "status", None), "value", "") or ""),
        origin_trace_id=protection_id,  # 每个保护单的 protection_id 唯一，SL/TP 不会误判为重复
        strategy_id="autopilot",
        generation=0,
    )


def _map_trace_stage(order_status: str) -> TraceStage:
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


def _factor_state_snapshots(factor_registry) -> list:
    """BD-FIX: FactorRegistry → FactorState 快照列表。

    监控检查函数契约要求可迭代的 FactorState 快照（fs.value/fs.lifecycle/
    fs.last_evaluation 属性访问），此前直接传入 FactorRegistry 对象导致
    ``'FactorRegistry' object is not iterable``。
    """
    from beidou_observability.monitoring.checks.factors import FactorState

    states: list = []
    records = getattr(factor_registry, "_factors", None)
    if not isinstance(records, dict):
        return states
    for fid, record in records.items():
        try:
            lifecycle = str(getattr(getattr(record, "lifecycle", None), "value", "UNKNOWN"))
            performance = getattr(record, "performance", None) or []
            value = 0.0
            last_evaluation = 0.0
            if performance:
                latest = performance[-1]
                value = float(getattr(latest, "ic_mean", 0.0) or 0.0)
                last_evaluation = _performance_timestamp(getattr(latest, "timestamp", None))
            states.append(
                FactorState(
                    factor_id=str(fid),
                    value=value,
                    lifecycle=lifecycle,
                    last_evaluation=last_evaluation,
                )
            )
        except (TypeError, ValueError, AttributeError):
            continue
    return states


def _performance_timestamp(value) -> float:
    """Return a performance timestamp without inventing freshness."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return 0.0
        try:
            return float(raw)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return 0.0
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
    return 0.0


def _strategy_snapshots(engine) -> list[dict]:
    """BD-FIX: 引擎 → 策略健康快照 dict 列表。

    监控检查函数契约要求可迭代的策略快照 dict（s.get("strategy_id")/
    s.get("signal_silence_seconds")/s.get("risk_budget_drift_pct")/
    s.get("stale_factor_count")），此前直接传入 AutonomousEngine /
    StrategyRiskManager 对象导致 ``object is not iterable``。
    """
    if engine is None:
        return []
    strategy_id = str(getattr(engine, "_autopilot_strategy_id", "autopilot"))
    last_nearline = float(getattr(engine, "_last_nearline", 0.0) or 0.0)
    silence = max(0.0, time.time() - last_nearline) if last_nearline > 0 else 0.0
    drift = 0.0
    stale = 0

    def _has_authorized_active_evidence(record) -> bool:
        check = getattr(record, "has_authorized_active_evidence", None)
        return bool(callable(check) and check())

    try:
        risk = getattr(engine, "_strategy_risk", None)
        if risk is not None:
            sid = getattr(engine, "_autopilot_strategy_id", None)
            budget = risk.get_budget(sid)
            state = risk.get_state(sid)
            if budget is not None and state is not None:
                max_dd = float(getattr(budget, "max_drawdown_pct", 0.0) or 0.0)
                current_dd = float(getattr(state, "current_drawdown_pct", 0.0) or 0.0)
                drift = max(0.0, current_dd - max_dd)
    except Exception as exc:
        import logging

        logging.getLogger("beidou.monitoring").debug("strategy risk snapshot skipped: %s", type(exc).__name__)
    try:
        registry = getattr(engine, "_factor_registry", None)
        records = getattr(registry, "_factors", None)
        if isinstance(records, dict):
            # Count only dependencies of the executable strategy.  IDEA and
            # PAPER_TRADING records are research inventory, not stale runtime
            # dependencies.  The typed graph is authoritative because it is
            # rebuilt from evidence-authorized factors in writable modes.
            graph = getattr(engine, "_typed_graph", None)
            graph_nodes = getattr(graph, "_nodes", None)
            component_registry = getattr(engine, "_factor_component_registry", None)
            if isinstance(graph_nodes, dict):
                for factor_id in graph_nodes:
                    factor_id = str(factor_id)
                    # TypedAlphaGraph also contains structural nodes (for
                    # example ``typed_fusion_v1``) that are not factor
                    # dependencies and have no FactorRecord.
                    if isinstance(component_registry, dict):
                        if factor_id not in component_registry:
                            continue
                    elif factor_id not in records:
                        continue
                    record = records.get(factor_id)
                    if record is None or not _has_authorized_active_evidence(record):
                        stale += 1
            else:
                # Lightweight diagnostic engines may not expose the typed
                # graph.  Keep the fallback scoped to registered ACTIVE
                # components; CHALLENGER is intentionally non-production.
                if isinstance(component_registry, dict):
                    for factor_id, record in records.items():
                        if str(factor_id) not in component_registry:
                            continue
                        lifecycle = str(getattr(getattr(record, "lifecycle", None), "value", ""))
                        if lifecycle == "ACTIVE" and not _has_authorized_active_evidence(record):
                            stale += 1
                else:
                    # Compatibility path for the older bridge fixture
                    # contract, which has no execution projection at all.
                    for record in records.values():
                        if not _has_authorized_active_evidence(record):
                            stale += 1
    except Exception as exc:
        import logging

        logging.getLogger("beidou.monitoring").debug("factor staleness snapshot skipped: %s", type(exc).__name__)
    return [
        {
            "strategy_id": strategy_id,
            "signal_silence_seconds": silence,
            "risk_budget_drift_pct": drift,
            "stale_factor_count": stale,
        }
    ]


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
    # 延迟导入避免初始化期循环依赖（beidou_launcher 已导入本包）
    from beidou_launcher.models import CheckResult, CheckSeverity, CheckStatus
    from beidou_observability.monitoring.checks.account import (
        check_account_permissions,
        check_account_unknown,
        check_balance_sanity,
    )
    from beidou_observability.monitoring.checks.execution import OrderTraceState, check_order_trace
    from beidou_observability.monitoring.checks.factors import check_factors
    from beidou_observability.monitoring.checks.factors import check_strategies as check_factor_strategies
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
    from beidou_observability.monitoring.checks.strategies import (
        check_strategy_risk_drift,
        check_strategy_signal_silence,
        check_strategy_version_drift,
    )
    from beidou_observability.monitoring.contracts import AccountPositionMode as MonAccountPositionMode
    from beidou_observability.monitoring.contracts import CheckSeverity as MonCheckSeverity
    from beidou_observability.monitoring.contracts import ModuleProgressContract

    def convert(result, *, name: str) -> CheckResult:
        """MonitoringCheckResult → 监督器 CheckResult。"""
        evidence_hash = str(getattr(result, "evidence_hash", "") or "")
        if not evidence_hash:
            try:
                evidence_hash = str(result.compute_evidence_hash())
            except (AttributeError, TypeError, ValueError):
                evidence_hash = ""
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
                "evidence_hash": evidence_hash,
                "correlation_id": result.correlation_id,
                "remediation": result.remediation,
                "policy_version": result.policy_version,
            },
        )

    def _convert_multi(result_list, *, name: str):
        """BD-FIX: 检查函数返回 list[MonitoringCheckResult] 时逐个转换。

        旧代码把 list 当单个结果 convert → 'list' object has no attribute
        'check_id' → MONITORING_CHECK_ERROR，且掩蔽真实 P0 因子失败
        （I5 审查：5 个检查永久 FAIL）。
        """
        out = []
        for item in result_list or []:
            out.append(convert(item, name=name))
        return out

    results: list[CheckResult] = []

    def failure(check_id: str, name: str, severity: CheckSeverity, exc: Exception) -> CheckResult:
        """Convert a monitoring implementation failure into a blocking fact.

        A missing monitoring result is not a PASS.  Returning an explicit
        failure keeps the supervisor fail-closed when a collector or semantic
        verifier cannot execute.
        """

        return CheckResult(
            check_id=check_id,
            name=name,
            status=CheckStatus.FAIL,
            severity=severity,
            message=f"MONITORING_CHECK_ERROR:{type(exc).__name__}:{exc}"[:500],
            evidence={"error_type": type(exc).__name__},
        )

    # === 1. 账户健康 (MON03/INV-002) ===
    snapshot = exchange_account_snapshot if isinstance(exchange_account_snapshot, dict) else None
    results.append(convert(check_account_unknown(snapshot), name="账户事实健康 (MON03)"))
    account_data = (snapshot or {}).get("account") if (snapshot or {}).get("ok") else None
    results.append(convert(check_balance_sanity(account_data), name="账户余额合理性"))
    # BD-FIX: Testnet 模式下提款权限由交易所默认开启（测试资金），
    # 不应作为阻断项。仅非 testnet 的可写环境要求 account_permissions 检查。
    _can_write = bool(getattr(engine, "_can_write", False))
    _env_mode = getattr(engine, "_env_mode", None)
    _is_testnet = _env_mode is not None and getattr(_env_mode, "value", "") == "testnet"
    _require_permissions = _can_write and not _is_testnet
    results.append(
        convert(
            check_account_permissions(snapshot, required=_require_permissions),
            name="账户权限事实 (R9)",
        )
    )

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
                # BD-FIX: demo-fapi 慢网络 + 因子组件负载下循环 60-70s 一圈；
                # 90s 阈值保留新鲜度语义且消除边界摩擦（2026-08-13 验收校准）
                progress_timeout_seconds=90.0,
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
    except Exception as exc:
        results.append(failure("runtime.monitoring.module_progress", "模块进度健康 (INV-004)", CheckSeverity.P1, exc))

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
            except Exception as exc:
                raise RuntimeError("local protection projection unavailable") from exc
        if exchange_positions:
            for ep in exchange_positions:
                semantic = verify_position_protection(ep, local_by_symbol.get(ep.symbol, []), mode)
                check = build_protection_check(semantic)
                results.append(convert(check, name="持仓保护覆盖 (PKG-MON-04)"))
        else:
            results.append(
                CheckResult(
                    check_id="runtime.safety.protection_coverage",
                    name="持仓保护覆盖 (PKG-MON-04)",
                    status=CheckStatus.PASS,
                    severity=CheckSeverity.P1,
                    message="无持仓，保护覆盖不适用",
                )
            )
    except Exception as exc:
        print(f"[monitor] protection_coverage EXCEPTION: {type(exc).__name__}: {exc}", flush=True)
        results.append(
            failure("runtime.safety.protection_coverage", "持仓保护覆盖 (PKG-MON-04)", CheckSeverity.P0, exc)
        )

    # === 5. 深度对账 (MON03 R1~R6) ===
    try:
        # Prefer the supervisor's fresh independent account snapshot.  A
        # process-local ``_last_account`` cache must not be the only source
        # that makes a reconciliation check appear complete.
        snapshot_account = snapshot.get("account") if isinstance(snapshot, dict) and snapshot.get("ok") else None
        ex_positions_raw = (
            snapshot_account.get("positions")
            if isinstance(snapshot_account, dict) and isinstance(snapshot_account.get("positions"), list)
            else None
        )
        # 过滤零仓位：Binance API 返回全部合约仓位（含零余额），
        # 不过滤会导致 hundreds 个零仓位与本地保护单对比产生大量 UNKNOWN
        ex_positions: list[dict] | None = [] if ex_positions_raw is not None else None
        for p in ex_positions_raw or []:
            try:
                if abs(float(p.get("positionAmt", 0) or 0)) <= 1e-6:
                    continue
                assert ex_positions is not None
                ex_positions.append(p)
            except (TypeError, ValueError):
                if ex_positions is not None:
                    ex_positions = None
                break
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
        ledger = getattr(engine, "_ledger", None)
        local_ledger = getattr(ledger, "_transactions", None) if ledger is not None else None
        # Run the semantic comparison for known-flat accounts as well as open
        # positions.  If either independent side is missing, emit UNKNOWN
        # instead of omitting the check (omission used to look like PASS).
        recon = perform_reconciliation(ex_positions, local_positions, None, None, local_ledger)

        # A writable engine has a stronger authority contract than this
        # projection check: the venue snapshot must be joined with the
        # durable local projection and the authorized user-stream facts by
        # ``engine._reconcile``.  A matching REST/local position comparison
        # alone must never advertise PASS for a write-capable process.
        # P1-048: 优先从 FactBus 获取对账事实，fallback 到 engine 私有字段.
        # Refresh the public fact projection before reading it.  For legacy
        # test doubles and external publishers, only the default ``engine``
        # source is accepted; this prevents a stale fact emitted by a prior
        # AutonomousEngine instance from poisoning the current check.
        bus = get_fact_bus()
        fact_source_id = getattr(engine, "_fact_bus_source_id", None)
        collector = getattr(engine, "collect_operational_facts", None)
        if callable(collector):
            try:
                collector()
                fact_source_id = getattr(engine, "_fact_bus_source_id", fact_source_id)
            except Exception:
                fact_source_id = None
        recon_source = f"engine._reconcile:{fact_source_id}" if fact_source_id else "engine"
        recon_fact = bus.get_latest("reconciliation_result", source=recon_source)
        # PKG02 (BDS-P0-001): 所有环境统一对账检查标准。
        # BD-FIX: demo-fapi 慢网络 + 因子组件负载下循环 60-70s 一圈；
        # 90s 阈值保留新鲜度语义且消除边界摩擦（2026-08-13 验收校准）
        _max_age = 90.0

        if recon_fact is not None and recon_fact.payload:
            # P1-048: 使用 FactBus 数据
            authority_ok = bool(recon_fact.payload.get("matched", False))
            authority_status = str(recon_fact.payload.get("status", "UNKNOWN")).upper()
            authority_age = time.time() - recon_fact.timestamp if recon_fact.timestamp > 0 else None
            if not authority_ok or not authority_age or authority_age > _max_age:
                results.append(
                    CheckResult(
                        check_id="runtime.safety.reconciliation",
                        name="深度对账 (MON03 R1~R6)",
                        status=CheckStatus.FAIL,
                        severity=CheckSeverity.P0,
                        message=f"Reconciliation via FactBus: status={authority_status}, age={authority_age}s",
                        evidence={"source": "fact_bus", "status": authority_status, "age_seconds": authority_age},
                    )
                )
            else:
                results.append(convert(build_reconciliation_check(recon), name="深度对账 (MON03 R1~R6)"))
        elif bool(getattr(engine, "_can_write", False)):
            authority = getattr(engine, "_last_reconciliation_result", None)
            authority_status = str(
                getattr(getattr(authority, "status", None), "value", getattr(authority, "status", "UNKNOWN"))
            ).upper()
            checked_at = getattr(authority, "checked_at", None)
            authority_age: float | None = None
            if checked_at is not None:
                try:
                    if getattr(checked_at, "tzinfo", None) is None:
                        from datetime import timezone

                        checked_at = checked_at.replace(tzinfo=timezone.utc)
                    authority_age = max(0.0, time.time() - float(checked_at.timestamp()))
                except (AttributeError, TypeError, ValueError, OverflowError):
                    authority_age = None
            authority_ok = (
                authority is not None
                and bool(getattr(authority, "matched", False))
                and authority_status == "MATCHED"
                and authority_age is not None
                and authority_age <= _max_age
            )
            if not authority_ok:
                results.append(
                    CheckResult(
                        check_id="runtime.safety.reconciliation",
                        name="深度对账 (MON03 R1~R6)",
                        status=CheckStatus.FAIL,
                        severity=CheckSeverity.P0,
                        message=(
                            "Writable reconciliation authority unavailable: "
                            f"status={authority_status},age="
                            f"{authority_age if authority_age is not None else 'UNKNOWN'}s"
                        ),
                        evidence={
                            "source": "engine._last_reconciliation_result",
                            "status": authority_status,
                            "age_seconds": authority_age,
                            "threshold_seconds": _max_age,
                            "matched": bool(getattr(authority, "matched", False)) if authority else False,
                        },
                    )
                )
            else:
                # 引擎对账权威源已验证通过 → PASS
                results.append(
                    CheckResult(
                        check_id="runtime.safety.reconciliation",
                        name="深度对账 (MON03 R1~R6)",
                        status=CheckStatus.PASS,
                        severity=CheckSeverity.P0,
                        message=f"Recon: {recon.matched} matched",
                        evidence={
                            "source": "engine._last_reconciliation_result",
                            "status": authority_status,
                            "age_seconds": authority_age,
                        },
                    )
                )
        else:
            results.append(convert(build_reconciliation_check(recon), name="深度对账 (MON03 R1~R6)"))
    except Exception as exc:
        results.append(failure("runtime.safety.reconciliation", "深度对账 (MON03 R1~R6)", CheckSeverity.P1, exc))

    # === 6. 执行质量 — 订单轨迹 (PKG-MON-05) ===
    try:
        order_trackers = getattr(engine, "_order_trackers", None)
        if isinstance(order_trackers, dict) and order_trackers:
            active_order_ids = getattr(engine, "_active_order_ids", None) or set()
            order_symbols = getattr(engine, "_order_symbols", None) or {}
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
    except Exception as exc:
        results.append(failure("runtime.execution.order_trace", "订单执行轨迹 (PKG-MON-05)", CheckSeverity.P1, exc))

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
    except Exception as exc:
        results.append(failure("runtime.monitoring.self_health", "监控组件健康 (PKG-MON-10)", CheckSeverity.P1, exc))

    # === 7. 因子与策略健康检查（P2，非阻塞，静默失败不影响启动） ===
    try:
        factor_registry = getattr(engine, "_factor_registry", None) if engine is not None else None
        if factor_registry is not None:
            # BD-FIX: 检查函数契约要求快照列表，此前直接传 FactorRegistry /
            # AutonomousEngine / StrategyRiskManager 对象导致 TypeError。
            factor_states = _factor_state_snapshots(factor_registry)
            strategy_states = _strategy_snapshots(engine)
            try:
                results.extend(_convert_multi(check_factors(factor_states), name="因子注册健康"))
            except Exception as exc:
                results.append(failure("runtime.factors.health", "因子注册健康", CheckSeverity.P2, exc))
            try:
                results.extend(_convert_multi(check_factor_strategies(strategy_states), name="因子策略关联"))
            except Exception as exc:
                results.append(failure("runtime.factors.strategies", "因子策略关联", CheckSeverity.P2, exc))
    except Exception as exc:
        results.append(failure("runtime.factors.discovery", "因子健康发现", CheckSeverity.P2, exc))
    try:
        if engine is not None:
            strategy_risk = getattr(engine, "_strategy_risk", None)
            autopilot_id = getattr(engine, "_autopilot_strategy_id", None)
            if strategy_risk is not None and autopilot_id is not None:
                strategy_states = _strategy_snapshots(engine)
                try:
                    results.extend(
                        _convert_multi(check_strategy_signal_silence(strategy_states), name="策略信号沉默检测")
                    )
                except Exception as exc:
                    results.append(failure("runtime.strategy.silence", "策略信号沉默检测", CheckSeverity.P2, exc))
                try:
                    results.extend(_convert_multi(check_strategy_risk_drift(strategy_states), name="策略风险漂移"))
                except Exception as exc:
                    results.append(failure("runtime.strategy.risk_drift", "策略风险漂移", CheckSeverity.P2, exc))
                try:
                    results.extend(_convert_multi(check_strategy_version_drift(strategy_states), name="策略版本漂移"))
                except Exception as exc:
                    results.append(failure("runtime.strategy.version_drift", "策略版本漂移", CheckSeverity.P2, exc))
    except Exception as exc:
        results.append(failure("runtime.strategy.discovery", "策略健康发现", CheckSeverity.P2, exc))

    # === 8. ChaosEngine 周期性健康检查 ===
    if engine is not None:
        try:
            chaos = getattr(engine, "_chaos_engine", None)
            if chaos is not None:
                chaos.run_chaos_cycle()  # 执行一轮故障注入/验证周期
        except Exception as exc:
            results.append(failure("runtime.chaos.health", "ChaosEngine 健康", CheckSeverity.P2, exc))

    return results
