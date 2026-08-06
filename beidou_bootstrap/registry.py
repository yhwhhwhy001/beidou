"""北斗模块、算法与运行时接线注册表。"""

from __future__ import annotations

import importlib
import time
from collections.abc import Iterable
from typing import Any

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
    "_factor_evaluator",
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


def _value_set(items: Iterable[Any]) -> set[str]:
    values: set[str] = set()
    for item in items:
        value = getattr(item, "value", item)
        values.add(str(value))
    return values


def inspect_engine_wiring(engine: Any, mode: str) -> list[CheckResult]:
    """验证引擎对象、算法图、因子生命周期和风险接线。"""
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

    graph_failed = bool(missing_components or invalid_components or graph_error)
    checks.append(
        _result(
            "runtime.algorithms.alpha_graph",
            "Alpha DAG 完整性",
            CheckStatus.FAIL if graph_failed else CheckStatus.PASS,
            CheckSeverity.P0,
            "Alpha DAG 缺失、校验失败或存在拓扑错误" if graph_failed else "8 个 Alpha 组件均已接线且拓扑可排序",
            evidence={
                "expected": sorted(EXPECTED_ALPHA_COMPONENTS),
                "actual": sorted(component_ids),
                "missing": missing_components,
                "extra": extra_components,
                "invalid": invalid_components,
                "topological_order": topological_order,
                "graph_error": graph_error,
            },
        )
    )

    factor_registry = getattr(engine, "_factor_registry", None)
    factor_map = getattr(factor_registry, "_factors", {}) if factor_registry is not None else {}
    factor_ids = set(factor_map.keys())
    missing_factors = sorted(EXPECTED_FACTORS - factor_ids)
    lifecycle: dict[str, str] = {}
    for factor_id, record in factor_map.items():
        raw_state = getattr(record, "lifecycle", "UNKNOWN")
        lifecycle[factor_id] = str(getattr(raw_state, "value", raw_state))
    active = {factor_id for factor_id, state in lifecycle.items() if state in {"ACTIVE", "CHALLENGER"}}
    strict_mode = mode in {"production", "canary", "live"}
    inactive_expected = sorted(EXPECTED_FACTORS - active)
    factor_failed = bool(missing_factors or (not strict_mode and inactive_expected))
    checks.append(
        _result(
            "runtime.algorithms.factor_lifecycle",
            "因子注册与生命周期",
            CheckStatus.FAIL if factor_failed else CheckStatus.PASS,
            CheckSeverity.P0,
            "因子缺失或未进入可运行生命周期" if factor_failed else "8 个因子均已注册并满足当前模式生命周期要求",
            evidence={
                "expected": sorted(EXPECTED_FACTORS),
                "actual": sorted(factor_ids),
                "missing": missing_factors,
                "inactive_expected": inactive_expected,
                "lifecycle": lifecycle,
                "strict_mode": strict_mode,
            },
        )
    )

    pool = getattr(engine, "_trading_pool", None)
    try:
        active_count = int(pool.active_count()) if pool is not None else 0
    except Exception:
        active_count = 0
    checks.append(
        _result(
            "runtime.algorithms.trading_pool",
            "交易池激活",
            CheckStatus.PASS if active_count > 0 else CheckStatus.FAIL,
            CheckSeverity.P0,
            f"已激活 {active_count} 个交易标的" if active_count > 0 else "没有可交易标的",
            evidence={"active_count": active_count},
        )
    )

    strategy_risk = getattr(engine, "_strategy_risk", None)
    strategy_id = getattr(engine, "_autopilot_strategy_id", None)
    budget = None
    try:
        budget = strategy_risk.get_budget(strategy_id) if strategy_risk is not None else None
    except Exception:
        budget = None
    checks.append(
        _result(
            "runtime.algorithms.risk_budget",
            "策略风险预算",
            CheckStatus.PASS if budget is not None else CheckStatus.FAIL,
            CheckSeverity.P0,
            "风险预算已加载" if budget is not None else "风险预算缺失，禁止增加风险",
            evidence={"strategy_id": str(strategy_id), "budget_type": type(budget).__name__ if budget else None},
        )
    )

    return checks
