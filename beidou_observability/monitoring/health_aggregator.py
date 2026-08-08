"""INV-006/007 Health Aggregator。"""

from beidou_observability.monitoring.contracts import CheckSeverity, CheckStatus, HealthStatus


def aggregate_health(results):
    has_p0 = any(r.severity == CheckSeverity.P0 and r.status != CheckStatus.PASS for r in results)
    has_unknown = any(
        r.status == CheckStatus.UNKNOWN and r.severity in (CheckSeverity.P0, CheckSeverity.P1) for r in results
    )
    has_warn = any(r.status == CheckStatus.WARN for r in results)
    if has_p0 or has_unknown:
        return HealthStatus.RED
    if has_warn:
        return HealthStatus.YELLOW
    return HealthStatus.GREEN


def enforce_inv006(status, has_unknown):
    if has_unknown and status == HealthStatus.GREEN:
        return HealthStatus.UNKNOWN
    return status


def enforce_inv007(p0_fail_count, pass_count):
    if p0_fail_count > 0:
        return False, f"{pass_count} PASS + {p0_fail_count} P0 FAIL → 不得 GREEN"
    return True, ""


def health_summary(results):
    status = aggregate_health(results)
    p0_fails = sum(1 for r in results if r.severity == CheckSeverity.P0 and r.status != CheckStatus.PASS)
    unknowns = sum(1 for r in results if r.status == CheckStatus.UNKNOWN)
    _inv007_ok, inv007_msg = enforce_inv007(p0_fails, sum(1 for r in results if r.status == CheckStatus.PASS))
    return {
        "status": status.value,
        "p0_fails": p0_fails,
        "unknowns": unknowns,
        "total": len(results),
        "inv007": inv007_msg,
    }
