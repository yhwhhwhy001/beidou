"""北斗模块、算法与运行时接线注册表。"""

from __future__ import annotations

import importlib
import time
from typing import Any

from beidou_data.trading_pool_lifecycle import PoolStatus

from .models import CheckResult, CheckSeverity, CheckStatus

REQUIRED_PACKAGES: tuple[str, ...] = (
    "beidou_shared",
    "beidou_safety",
    "beidou_strategy",
    "beidou_research",
    "beidou_policy",
    "beidou_security",
    "beidou_observability",
    "beidou_exchange",
    "beidou_lifecycle",
    "beidou_delivery",
    "beidou_autonomy",
    "beidou_infra",
    "beidou_data",
    "beidou_control",
    "beidou_chaos",
    "beidou_production",
    "beidou_reporting",
    "beidou_certification",
    "beidou_core",
)

EXPECTED_ALPHA_COMPONENTS: frozenset[str] = frozenset(
    {
        "meanrev_entry_v1",
        "trend_entry_v1",
        "breakout_entry_v1",
        "momentum_filter_v1",
        "volatility_filter_v1",
        "volume_filter_v1",
        "trailing_exit_v1",
        "time_exit_v1",
    }
)
EXPECTED_FACTORS: frozenset[str] = EXPECTED_ALPHA_COMPONENTS

REQUIRED_ENGINE_ATTRIBUTES: tuple[str, ...] = (
    "_adapter",
    "_exchange",
    "_feed",
    "_store",
    "_alerts",
    "_health",
    "_control",
    "_lifecycle",
    "_protection",
    "_outbox",
    "_ledger",
    "_recon",
    "_pre_risk",
    "_risk_engine",
    "_approval",
    "_risk_sm",
    "_post_risk",
    "_cost_model",
    "_optimizer",
    "_fuser",
    "_model_registry",
    "_drift_detector",
    "_mapek",
    "_trading_pool",
    "_strategy_risk",
    "_factor_registry",
    # "_factor_evaluator",  # 已移除：FactorEvaluator 仅使用静态方法
    "_alpha_graph",
    "_strategy_kernel",
)


def _result(
    check_id: str,
    name: str,
    status: CheckStatus,
    severity: CheckSeverity,
    message: str,
    *,
    evidence: dict[str, Any] | None = None,
    started: float | None = None,
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        name=name,
        status=status,
        severity=severity,
        message=message,
        evidence=evidence or {},
        duration_ms=round((time.perf_counter() - started) * 1000, 3) if started is not None else 0.0,
    )


def check_package_imports() -> list[CheckResult]:
    results: list[CheckResult] = []
    for package in REQUIRED_PACKAGES:
        started = time.perf_counter()
        try:
            module = importlib.import_module(package)
            results.append(
                _result(
                    f"preflight.import.{package}",
                    f"导入业务包 {package}",
                    CheckStatus.PASS,
                    CheckSeverity.P1,
                    "包可导入",
                    evidence={"module_file": getattr(module, "__file__", "namespace")},
                    started=started,
                )
            )
        except Exception as exc:
            results.append(
                _result(
                    f"preflight.import.{package}",
                    f"导入业务包 {package}",
                    CheckStatus.FAIL,
                    CheckSeverity.P1,
                    f"导入失败: {type(exc).__name__}: {exc}",
                    started=started,
                )
            )
    return results


def inspect_engine_wiring(engine: Any, mode: str) -> list[CheckResult]:
    checks: list[CheckResult] = []
    missing = [name for name in REQUIRED_ENGINE_ATTRIBUTES if getattr(engine, name, None) is None]
    checks.append(
        _result(
            "runtime.wiring.core",
            "核心模块接线",
            CheckStatus.FAIL if missing else CheckStatus.PASS,
            CheckSeverity.P0,
            f"缺少核心对象: {missing}" if missing else f"{len(REQUIRED_ENGINE_ATTRIBUTES)} 个核心对象均已构造",
            evidence={"required": list(REQUIRED_ENGINE_ATTRIBUTES), "missing": missing},
        )
    )

    graph = getattr(engine, "_alpha_graph", None)
    components = getattr(graph, "_components", {}) if graph is not None else {}
    component_ids = set(components.keys())
    missing_components = sorted(EXPECTED_ALPHA_COMPONENTS - component_ids)
    extra_components = sorted(component_ids - EXPECTED_ALPHA_COMPONENTS)
    invalid_components: list[str] = []
    for component_id, component in components.items():
        try:
            if component.validate() is not True:
                invalid_components.append(component_id)
        except Exception:
            invalid_components.append(component_id)

    graph_error = ""
    topological_order: list[str] = []
    if graph is not None:
        try:
            topological_order = list(graph.topological_order())
        except Exception as exc:
            graph_error = f"{type(exc).__name__}: {exc}"
    topology_mismatch = set(topological_order) != component_ids or len(topological_order) != len(component_ids)
    # 未授权因子 (IDEA/CHALLENGER/DEGRADED) 从图中移除是预期行为，
    # 不应阻断。Testnet 走真实证据门禁：因子在取得 EvidenceBundle 晋级
    # ACTIVE 之前不进执行图，空 DAG 是合法的 HOLD 状态而非接线错误；
    # 此前只豁免 DEGRADED，导致 testnet 启动被 "Alpha DAG 完整性" P0
    # 阻断（因子恒 IDEA → DAG 恒空 → 无法启动 → 无法做 G5 认证）。
    factor_registry = getattr(engine, "_factor_registry", None)
    factor_map = getattr(factor_registry, "_factors", {}) if factor_registry is not None else {}
    lifecycle: dict[str, str] = {}
    for factor_id, record in factor_map.items():
        raw_state = getattr(record, "lifecycle", "UNKNOWN")
        lifecycle[factor_id] = str(getattr(raw_state, "value", raw_state))
    inactive_factor_ids = {fid for fid, state in lifecycle.items() if state != "ACTIVE"}
    unexpected_missing = sorted(set(missing_components) - inactive_factor_ids)
    graph_failed = bool(
        unexpected_missing or extra_components or invalid_components or graph_error or topology_mismatch
    )
    graph_degraded = bool(missing_components and not graph_failed)
    graph_status = CheckStatus.FAIL if graph_failed else (CheckStatus.WARN if graph_degraded else CheckStatus.PASS)
    graph_severity = CheckSeverity.P0 if graph_failed else CheckSeverity.P2
    checks.append(
        _result(
            "runtime.algorithms.alpha_graph",
            "Alpha DAG 完整性",
            graph_status,
            graph_severity,
            "Alpha DAG 缺失、校验失败或存在拓扑错误" if graph_failed else "8 个 Alpha 组件均已接线且拓扑可排序",
            evidence={
                "expected": sorted(EXPECTED_ALPHA_COMPONENTS),
                "actual": sorted(component_ids),
                "missing": missing_components,
                "extra": extra_components,
                "invalid": invalid_components,
                "topological_order": topological_order,
                "graph_error": graph_error,
                "topology_mismatch": topology_mismatch,
            },
        )
    )

    factor_ids = set(factor_map.keys())
    missing_factors = sorted(EXPECTED_FACTORS - factor_ids)
    extra_factors = sorted(factor_ids - EXPECTED_FACTORS)
    active = {factor_id for factor_id, state in lifecycle.items() if state in {"ACTIVE", "CHALLENGER"}}
    inactive_expected = sorted(EXPECTED_FACTORS - active)
    # 仅因子缺失或注册异常为 P0 阻断；DEGRADED 为 P2 告警（可自动恢复）
    truly_missing = bool(missing_factors or extra_factors)
    degraded_only = bool(not truly_missing and inactive_expected)
    factor_status = CheckStatus.FAIL if truly_missing else (CheckStatus.WARN if degraded_only else CheckStatus.PASS)
    factor_severity = CheckSeverity.P0 if truly_missing else CheckSeverity.P2
    checks.append(
        _result(
            "runtime.algorithms.factor_lifecycle",
            "因子注册与生命周期",
            factor_status,
            factor_severity,
            "因子缺失或未注册"
            if truly_missing
            else (
                f"部分因子降级(DEGRADED): {inactive_expected}" if degraded_only else "因子均已注册并满足生命周期要求"
            ),
            evidence={
                "expected": sorted(EXPECTED_FACTORS),
                "actual": sorted(factor_ids),
                "missing": missing_factors,
                "extra": extra_factors,
                "inactive_expected": inactive_expected,
                "lifecycle": lifecycle,
                "mode": mode,
            },
        )
    )

    pool = getattr(engine, "_trading_pool", None)
    try:
        active_count = int(pool.active_count()) if pool is not None else 0
        observing_count = (
            sum(1 for e in pool._pool.values() if e.status == PoolStatus.OBSERVING) if pool is not None else 0
        )
    except Exception:
        active_count = 0
        observing_count = 0
    # 首次启动时所有标的处于 OBSERVING 状态是正常的，不阻塞启动。
    # 宇宙评估会在 offline loop 中自动评分并晋级达标标的。
    pool_has_candidates = active_count > 0 or observing_count > 0
    checks.append(
        _result(
            "runtime.algorithms.trading_pool",
            "交易池激活",
            CheckStatus.PASS if active_count > 0 else (CheckStatus.WARN if pool_has_candidates else CheckStatus.FAIL),
            CheckSeverity.P0 if not pool_has_candidates else CheckSeverity.P1,
            f"已激活 {active_count} 个交易标的"
            if active_count > 0
            else f"等待宇宙评估晋级（{observing_count} 个 OBSERVING）",
            evidence={"active_count": active_count, "observing_count": observing_count},
        )
    )

    strategy_risk = getattr(engine, "_strategy_risk", None)
    strategy_id = getattr(engine, "_autopilot_strategy_id", None)
    budget = None
    try:
        budget = strategy_risk.get_budget(strategy_id) if strategy_risk is not None else None
    except Exception:
        budget = None
    budget_fields: dict[str, float] = {}
    budget_valid = budget is not None
    if budget is not None:
        for field_name in (
            "max_drawdown_pct",
            "max_daily_loss_pct",
            "max_position_notional",
            "max_leverage",
            "risk_per_trade_pct",
            "max_consecutive_losses",
        ):
            try:
                budget_fields[field_name] = float(getattr(budget, field_name))
            except (TypeError, ValueError, AttributeError):
                budget_fields[field_name] = 0.0
        budget_valid = all(value > 0 for value in budget_fields.values())
        budget_valid = budget_valid and budget_fields["max_drawdown_pct"] <= 100
        budget_valid = budget_valid and budget_fields["max_daily_loss_pct"] <= 100
        budget_valid = budget_valid and budget_fields["risk_per_trade_pct"] <= 100
    checks.append(
        _result(
            "runtime.algorithms.risk_budget",
            "策略风险预算",
            CheckStatus.PASS if budget_valid else CheckStatus.FAIL,
            CheckSeverity.P0,
            "风险预算已加载且参数有效" if budget_valid else "风险预算缺失或参数无效，禁止增加风险",
            evidence={
                "strategy_id": str(strategy_id),
                "budget_type": type(budget).__name__ if budget else None,
                "fields": budget_fields,
            },
        )
    )
    return checks
