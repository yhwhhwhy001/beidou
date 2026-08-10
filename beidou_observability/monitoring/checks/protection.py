"""PKG-MON-04: Protection Semantic Verifier。"""

import os
import time
from dataclasses import dataclass, field

from beidou_observability.monitoring.contracts import (
    CheckSeverity,
    CheckStatus,
    MonitoringCheckResult,
)


@dataclass(slots=True)
class ExchangeEconomicPosition:
    symbol: str
    position_amt: str
    entry_price: str = "0"
    position_side: str = "BOTH"

    @property
    def qty(self):
        return abs(float(self.position_amt))


@dataclass(slots=True)
class StrategyExposureSlice:
    strategy_id: str
    symbol: str
    exposure_pct: float = 0.0
    direction: str = ""


@dataclass(slots=True)
class ProtectionFact:
    protection_id: str
    position_key: str
    kind: str = ""
    order_id: str = ""
    side: str = ""
    position_side: str = ""
    quantity: str = ""
    trigger_price: str = ""
    status: str = ""
    origin_trace_id: str = ""
    strategy_id: str = ""
    intent_id: str = ""
    generation: int = 0
    supersedes_order_id: str = ""
    group_id: str = ""


@dataclass(slots=True)
class ProtectionSemanticResult:
    position_key: str
    exchange_position: ExchangeEconomicPosition | None = None
    protections: list = field(default_factory=list)
    missing_sl: bool = False
    missing_tp: bool = False
    ghost_detected: bool = False
    duplicate_count: int = 0
    ambiguous_count: int = 0
    mode_mismatch: bool = False
    details: list = field(default_factory=list)


def verify_position_protection(position, protections, mode, slices=None):
    result = ProtectionSemanticResult(position_key=position.symbol, exchange_position=position, protections=protections)
    # Only venue-acknowledged ACTIVE orders are protection facts.  A local
    # CREATED/PENDING definition, or an ACTIVE object without a venue id, is
    # an intent/UNKNOWN state and must fail closed as missing coverage.

    def _status_value(value) -> str:
        return str(getattr(value, "value", value) or "").upper()

    venue_active = [
        p
        for p in protections
        if _status_value(getattr(p, "status", "")) == "ACTIVE" and str(getattr(p, "order_id", "") or "").strip()
    ]
    sl_list = [p for p in venue_active if p.kind == "SL"]
    if not sl_list:
        result.missing_sl = True
        result.details.append({"issue": "MISSING_SL"})
    tp_list = [p for p in venue_active if p.kind == "TP"]
    if not tp_list:
        result.missing_tp = True
        result.details.append({"issue": "MISSING_TP"})
    seen = {}
    for p in venue_active:
        lineage_key = f"{p.origin_trace_id}:{p.strategy_id}:{p.generation}"
        if lineage_key in seen:
            if p.supersedes_order_id and p.supersedes_order_id == seen[lineage_key].protection_id:
                continue
            result.duplicate_count += 1
            result.details.append({"issue": "DUPLICATE", "protection_id": p.protection_id})
        seen[lineage_key] = p
    if position.qty == 0 and protections:
        result.ghost_detected = True
        result.details.append({"issue": "GHOST"})
    return result


def build_protection_check(result):
    now = time.time()
    issues = []
    if result.missing_sl:
        issues.append("MISSING_SL")
    if result.missing_tp:
        issues.append("MISSING_TP")
    if result.duplicate_count > 0:
        issues.append(f"DUP({result.duplicate_count})")
    if result.ghost_detected:
        issues.append("GHOST")
    _testnet = os.environ.get("BEIDOU_ENV") == "testnet"
    _sev = CheckSeverity.P1 if _testnet else CheckSeverity.P0
    _status = CheckStatus.WARN if _testnet else CheckStatus.FAIL
    if issues:
        return MonitoringCheckResult(
            check_id="runtime.safety.protection_coverage",
            entity_type="position",
            entity_id=result.position_key,
            status=_status,
            severity=_sev,
            message=f"Protection: {', '.join(issues)}",
            observed_at=now,
        )
    return MonitoringCheckResult(
        check_id="runtime.safety.protection_coverage",
        entity_type="position",
        entity_id=result.position_key,
        status=CheckStatus.PASS,
        severity=CheckSeverity.P0,
        message=f"Protected: {len(result.protections)} orders",
        observed_at=now,
    )
