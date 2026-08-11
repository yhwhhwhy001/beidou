"""INV-006/007 Health Aggregator。"""

from beidou_observability.monitoring.contracts import CheckSeverity, CheckStatus, HealthStatus


def aggregate_health(results):
    """BD-CV50: 监控状态聚合。P1 FAIL 聚合为 RED，不可能 GREEN。"""
    has_p0_fail = any(r.severity == CheckSeverity.P0 and r.status == CheckStatus.FAIL for r in results)
    has_p1_fail = any(r.severity == CheckSeverity.P1 and r.status == CheckStatus.FAIL for r in results)
    has_unknown = any(
        r.status == CheckStatus.UNKNOWN and r.severity in (CheckSeverity.P0, CheckSeverity.P1) for r in results
    )
    has_warn = any(r.status == CheckStatus.WARN for r in results)
    # P0 FAIL、P1 FAIL、UNKNOWN 均→RED
    if has_p0_fail or has_p1_fail or has_unknown:
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
    p0_fails = sum(1 for r in results if r.severity == CheckSeverity.P0 and r.status == CheckStatus.FAIL)
    p1_fails = sum(1 for r in results if r.severity == CheckSeverity.P1 and r.status == CheckStatus.FAIL)
    unknowns = sum(1 for r in results if r.status == CheckStatus.UNKNOWN)
    return {
        "status": status.value,
        "p0_fails": p0_fails,
        "p1_fails": p1_fails,
        "unknowns": unknowns,
        "total": len(results),
    }
