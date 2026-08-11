"""MON03: Account UNKNOWN+stale (INV-002)。"""

import time

from beidou_observability.monitoring.contracts import CheckSeverity, CheckStatus, MonitoringCheckResult


def check_account_permissions(snapshot=None, *, max_age=45.0, required=True):
    """Verify venue permissions from the fresh account fact, not local policy.

    A key may be edited at the venue after startup.  A fresh account snapshot
    therefore must keep proving ``canTrade=true`` and ``canWithdraw=false``
    while writable authority is held.  Missing or malformed fields are
    UNKNOWN and fail closed.
    """

    now = time.time()
    if not required:
        return MonitoringCheckResult(
            check_id="runtime.safety.account_permissions",
            entity_type="account",
            entity_id="permissions",
            status=CheckStatus.PASS,
            severity=CheckSeverity.P1,
            message="写能力关闭；账户权限不参与新增风险授权",
            observed_at=now,
            source="mode_contract",
        )
    if snapshot is None or not snapshot.get("ok"):
        return MonitoringCheckResult(
            check_id="runtime.safety.account_permissions",
            entity_type="account",
            entity_id="permissions",
            status=CheckStatus.FAIL,
            severity=CheckSeverity.P0,
            message="Account permissions UNKNOWN: account snapshot unavailable",
            observed_at=now,
            source="exchange_account_snapshot",
            remediation="NO_NEW_RISK",
        )

    observed = float(snapshot.get("observed_at", 0) or 0)
    age = now - observed if observed > 0 else float("inf")
    account = snapshot.get("account")
    if age < -5 or age > max_age or not isinstance(account, dict):
        if age < -5:
            reason = f"future observed_at age={age:.1f}s"
        elif age > max_age:
            reason = f"stale age={age:.1f}s>{max_age}s"
        else:
            reason = "account payload missing"
        return MonitoringCheckResult(
            check_id="runtime.safety.account_permissions",
            entity_type="account",
            entity_id="permissions",
            status=CheckStatus.FAIL,
            severity=CheckSeverity.P0,
            message=f"Account permissions UNKNOWN: {reason}",
            observed_at=now,
            fact_age_ms=age * 1000,
            source="exchange_account_snapshot",
            remediation="NO_NEW_RISK",
        )

    can_trade = account.get("canTrade")
    can_withdraw = account.get("canWithdraw")
    if not isinstance(can_trade, bool) or not isinstance(can_withdraw, bool):
        return MonitoringCheckResult(
            check_id="runtime.safety.account_permissions",
            entity_type="account",
            entity_id="permissions",
            status=CheckStatus.FAIL,
            severity=CheckSeverity.P0,
            message="Account permissions UNKNOWN: canTrade/canWithdraw missing or invalid",
            actual={"canTrade": can_trade, "canWithdraw": can_withdraw},
            observed_at=now,
            fact_age_ms=age * 1000,
            source="exchange_account_snapshot",
            remediation="NO_NEW_RISK",
        )
    if can_withdraw:
        # PKG02 (BDS-P0-001): 所有环境统一账户权限检查。
        return MonitoringCheckResult(
            check_id="runtime.safety.account_permissions",
            entity_type="account",
            entity_id="permissions",
            status=CheckStatus.FAIL,
            severity=CheckSeverity.P0,
            message="Venue withdrawal permission enabled",
            expected={"canTrade": True, "canWithdraw": False},
            actual={"canTrade": can_trade, "canWithdraw": can_withdraw},
            observed_at=now,
            fact_age_ms=age * 1000,
            source="exchange_account_snapshot",
            remediation="NO_NEW_RISK; disable venue withdrawal permission",
        )
    if not can_trade:
        return MonitoringCheckResult(
            check_id="runtime.safety.account_permissions",
            entity_type="account",
            entity_id="permissions",
            status=CheckStatus.FAIL,
            severity=CheckSeverity.P0,
            message="Venue trading permission disabled",
            expected={"canTrade": True, "canWithdraw": False},
            actual={"canTrade": can_trade, "canWithdraw": can_withdraw},
            observed_at=now,
            fact_age_ms=age * 1000,
            source="exchange_account_snapshot",
            remediation="NO_NEW_RISK",
        )
    return MonitoringCheckResult(
        check_id="runtime.safety.account_permissions",
        entity_type="account",
        entity_id="permissions",
        status=CheckStatus.PASS,
        severity=CheckSeverity.P0,
        message=f"Venue permissions valid (age={age:.1f}s)",
        expected={"canTrade": True, "canWithdraw": False},
        actual={"canTrade": can_trade, "canWithdraw": can_withdraw},
        observed_at=now,
        fact_age_ms=age * 1000,
        source="exchange_account_snapshot",
    )


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
