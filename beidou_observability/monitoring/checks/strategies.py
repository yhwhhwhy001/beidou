"""PKG-MON-07: Strategy Lifecycle — Signal Silence, risk drift, version (FR-MON-007)。"""

import time

from beidou_observability.monitoring.contracts import CheckSeverity, CheckStatus, MonitoringCheckResult


def check_strategy_signal_silence(strategies):
    now = time.time()
    results = []
    for s in strategies:
        sid = s.get("strategy_id", "unknown")
        silence = s.get("signal_silence_seconds", 0)
        if silence > 3600:
            results.append(
                MonitoringCheckResult(
                    check_id="runtime.health.strategy_lifecycle",
                    entity_type="strategy",
                    entity_id=sid,
                    status=CheckStatus.WARN,
                    severity=CheckSeverity.P1,
                    message=f"Signal Silence: {silence:.0f}s",
                    observed_at=now,
                )
            )
    return results


def check_strategy_risk_drift(strategies, max_drift=20.0):
    now = time.time()
    results = []
    for s in strategies:
        sid = s.get("strategy_id", "unknown")
        drift = s.get("risk_budget_drift_pct", 0)
        if drift > max_drift:
            results.append(
                MonitoringCheckResult(
                    check_id="runtime.health.strategy_lifecycle",
                    entity_type="strategy",
                    entity_id=sid,
                    status=CheckStatus.WARN,
                    severity=CheckSeverity.P1,
                    message=f"Risk drift: {drift:.1f}%",
                    observed_at=now,
                    actual={"drift_pct": drift},
                )
            )
    return results


def check_strategy_version_drift(strategies):
    now = time.time()
    results = []
    for s in strategies:
        sid = s.get("strategy_id", "unknown")
        stale = s.get("stale_factor_count", 0)
        if stale > 0:
            results.append(
                MonitoringCheckResult(
                    check_id="runtime.health.strategy_lifecycle",
                    entity_type="strategy",
                    entity_id=sid,
                    status=CheckStatus.WARN,
                    severity=CheckSeverity.P1,
                    message=f"Stale factor deps: {stale}",
                    observed_at=now,
                )
            )
    return results
