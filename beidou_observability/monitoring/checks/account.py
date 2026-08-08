"""MON03: Account UNKNOWN+stale (INV-002)。"""

import time

from beidou_observability.monitoring.contracts import CheckSeverity, CheckStatus, MonitoringCheckResult


def check_account_unknown(snapshot=None, *, max_age=45.0):
    now = time.time()
    if snapshot is None or not snapshot.get("ok"):
        return MonitoringCheckResult(
            check_id="runtime.safety.account",
            entity_type="account",
            entity_id="exchange",
            status=CheckStatus.FAIL,
            severity=CheckSeverity.P0,
            message="Account UNKNOWN: API failure (INV-002)",
            observed_at=now,
        )
    observed = float(snapshot.get("observed_at", 0) or 0)
    age = now - observed if observed > 0 else float("inf")
    if age > max_age:
        return MonitoringCheckResult(
            check_id="runtime.safety.account",
            entity_type="account",
            entity_id="exchange",
            status=CheckStatus.FAIL,
            severity=CheckSeverity.P0,
            message=f"Account STALE: age={age:.1f}s>{max_age}s",
            observed_at=now,
            fact_age_ms=age * 1000,
        )
    return MonitoringCheckResult(
        check_id="runtime.safety.account",
        entity_type="account",
        entity_id="exchange",
        status=CheckStatus.PASS,
        severity=CheckSeverity.P0,
        message=f"Account fresh (age={age:.1f}s)",
        observed_at=now,
        fact_age_ms=age * 1000,
    )


def check_balance_sanity(data=None):
    now = time.time()
    if not data or not isinstance(data, dict) or "totalWalletBalance" not in data:
        return MonitoringCheckResult(
            check_id="runtime.safety.balance_sanity",
            entity_type="account",
            entity_id="balance",
            status=CheckStatus.FAIL,
            severity=CheckSeverity.P0,
            message="Account balance UNKNOWN",
            observed_at=now,
        )
    return MonitoringCheckResult(
        check_id="runtime.safety.balance_sanity",
        entity_type="account",
        entity_id="balance",
        status=CheckStatus.PASS,
        severity=CheckSeverity.P0,
        message="Balance OK",
        observed_at=now,
    )
