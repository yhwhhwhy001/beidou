"""PKG-MON-06: Module Progress Health (INV-004)。"""

import time

from beidou_observability.monitoring.contracts import (
    CheckSeverity,
    CheckStatus,
    MonitoringCheckResult,
)


def check_module_progress(contracts):
    results = []
    now = time.time()
    for c in contracts:
        elapsed = now - c.last_progress_at if c.last_progress_at > 0 else float("inf")
        if elapsed > c.progress_timeout_seconds:
            results.append(
                MonitoringCheckResult(
                    check_id="runtime.health.module_progress",
                    entity_type="module",
                    entity_id=c.module_id,
                    status=CheckStatus.FAIL,
                    severity=c.criticality,
                    message=f"Module {c.module_id}: alive-but-stale ({elapsed:.0f}s>{c.progress_timeout_seconds}s)",
                    observed_at=now,
                )
            )
        else:
            results.append(
                MonitoringCheckResult(
                    check_id="runtime.health.module_progress",
                    entity_type="module",
                    entity_id=c.module_id,
                    status=CheckStatus.PASS,
                    severity=c.criticality,
                    message=f"Module {c.module_id}: OK ({elapsed:.0f}s)",
                    observed_at=now,
                )
            )
    return results


def check_algorithm_probe(probe_result):
    now = time.time()
    if probe_result is None or not probe_result.get("ok"):
        return MonitoringCheckResult(
            check_id="runtime.health.algorithm_probe",
            entity_type="algorithm",
            entity_id="probe",
            status=CheckStatus.FAIL,
            severity=CheckSeverity.P1,
            message=f"Probe FAILED: {probe_result.get('error', 'NO_PROBE') if probe_result else 'NO_PROBE'}",
            observed_at=now,
        )
    return MonitoringCheckResult(
        check_id="runtime.health.algorithm_probe",
        entity_type="algorithm",
        entity_id="probe",
        status=CheckStatus.PASS,
        severity=CheckSeverity.P1,
        message=f"Probe OK: {probe_result.get('proposal_hash', 'N/A')[:16]}",
        observed_at=now,
    )
