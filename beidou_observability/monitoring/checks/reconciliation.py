"""MON03 R1~R6 Reconciliation (stepSize precision)。"""

import time
from dataclasses import dataclass, field

from beidou_observability.monitoring.contracts import CheckSeverity, CheckStatus, MonitoringCheckResult


@dataclass(slots=True)
class ReconciliationResult:
    matched: int = 0
    mismatched: int = 0
    unknown: int = 0
    unsupported: int = 0
    details: list = field(default_factory=list)
    observed_at: float = 0.0

    @property
    def all_matched(self):
        return self.mismatched == 0 and self.unknown == 0


def perform_reconciliation(
    ex_positions, local_positions, ex_orders, local_orders, local_ledger, *, step_size_map=None, temporal_skew=5.0
):
    result = ReconciliationResult(observed_at=time.time())
    step_map = step_size_map or {}
    if ex_positions is None or local_positions is None:
        result.unknown += 1
        result.details.append({"scope": "R1", "status": "MISSING_POSITION_FACT_SOURCE"})
    else:
        ex_map = {p.get("symbol", ""): p for p in ex_positions if p.get("symbol")}
        lo_map = {p.get("symbol", ""): p for p in local_positions if p.get("symbol")}
        for sym in set(ex_map) | set(lo_map):
            ex_p, lo_p = ex_map.get(sym), lo_map.get(sym)
            if ex_p is None or lo_p is None:
                result.unknown += 1
                result.details.append({"scope": "R1", "symbol": sym, "status": "MISSING_ONE_SIDE"})
            else:
                precision = step_map.get(sym, 1e-8)
                if abs(float(ex_p.get("positionAmt", 0)) - float(lo_p.get("positionAmt", 0))) > precision:
                    result.mismatched += 1
                    result.details.append({"scope": "R1", "symbol": sym, "status": "MISMATCHED"})
                else:
                    result.matched += 1
    if ex_orders is not None and local_orders is not None:
        ex_ids = {o.get("orderId") for o in ex_orders if o.get("orderId")}
        lo_ids = {o.get("order_id") for o in local_orders if o.get("order_id")}
        result.matched += len(ex_ids & lo_ids)
        only_ex = len(ex_ids - lo_ids)
        only_lo = len(lo_ids - ex_ids)
        if only_ex or only_lo:
            result.unknown += only_ex + only_lo
            result.details.append({"scope": "R2", "ex_only": only_ex, "lo_only": only_lo})
    if local_ledger is not None:
        result.matched += len(local_ledger)
    else:
        result.unsupported += 1
        result.details.append({"scope": "R5", "status": "UNSUPPORTED"})
    return result


def build_reconciliation_check(r):
    now = time.time()
    if r.mismatched > 0:
        return MonitoringCheckResult(
            check_id="runtime.safety.reconciliation",
            entity_type="system",
            entity_id="recon",
            status=CheckStatus.FAIL,
            severity=CheckSeverity.P0,
            message=f"Recon: {r.matched}M/{r.mismatched}X/{r.unknown}U",
            observed_at=now,
        )
    # ``unsupported`` means a required fact source (currently the durable
    # ledger/R5 side) was unavailable.  It is not a clean zero-difference
    # result: reporting PASS here would let a monitoring-only projection
    # contradict the engine's authoritative three-way gate.
    if r.unsupported > 0:
        return MonitoringCheckResult(
            check_id="runtime.safety.reconciliation",
            entity_type="system",
            entity_id="recon",
            status=CheckStatus.FAIL,
            severity=CheckSeverity.P0,
            message=f"Recon fact source unsupported: {r.unsupported}",
            observed_at=now,
        )
    if r.unknown > 0:
        return MonitoringCheckResult(
            check_id="runtime.safety.reconciliation",
            entity_type="system",
            entity_id="recon",
            # UNKNOWN is a missing fact, not a transient warning.  The
            # supervisor's blocking semantics only treat FAIL P0/P1 as an
            # authority blocker, so downgrade here would recreate the exact
            # stale-fact/resume failure this check is meant to prevent.
            status=CheckStatus.FAIL,
            severity=CheckSeverity.P0,
            message=f"Recon UNKNOWN: {r.matched}M/{r.unknown}U",
            observed_at=now,
        )
    return MonitoringCheckResult(
        check_id="runtime.safety.reconciliation",
        entity_type="system",
        entity_id="recon",
        status=CheckStatus.PASS,
        severity=CheckSeverity.P0,
        message=f"Recon: {r.matched} matched",
        observed_at=now,
    )
