"""Incident FSM+dedupe+remediation allowlist (MON09)。"""

import time
from dataclasses import dataclass, field

from beidou_observability.monitoring.contracts import CheckSeverity, Incident, IncidentStatus
from beidou_observability.monitoring.storm_detector import StormDetector

REMEDIATION_AUTO = {
    "FEED_RECONNECT",
    "FEED_RESUBSCRIBE",
    "REFRESH_ACCOUNT_SNAPSHOT",
    "REFRESH_ALGO_SNAPSHOT",
    "RECONCILIATION_RETRY",
    "RESTART_MONITOR_CHILD_LOOP",
    "QUARANTINE_FACTOR_OR_STRATEGY",
}
REMEDIATION_CONDITIONAL = {
    "RECREATE_MISSING_SL_FROM_EXISTING_PROTECTION_CONTRACT",
    "RECREATE_MISSING_TP_FROM_EXISTING_PROTECTION_CONTRACT",
    "CANCEL_EXACT_LINEAGE_PROVEN_DUPLICATE_PROTECTION",
    "REINITIALIZE_NEARLINE_FROM_VALIDATED_CHECKPOINT",
}
REMEDIATION_NEVER = {
    "CHANGE_POSITION_MODE",
    "INCREASE_LEVERAGE",
    "RELAX_RISK_LIMIT",
    "INCREASE_ORDER_SIZE",
    "CREATE_NEW_RISK_POSITION",
    "CHANGE_API_CREDENTIALS",
    "DELETE_AMBIGUOUS_PROTECTION",
    "MODIFY_STRATEGY_PARAM_TO_CLEAR_ALERT",
}


@dataclass(slots=True)
class IncidentManager:
    incidents: dict = field(default_factory=dict)
    links: list = field(default_factory=list)
    storm_detector: StormDetector = field(default_factory=StormDetector)
    _counter: int = 0

    def create_or_dedupe(self, dedupe_key, check_id="", severity=CheckSeverity.P1, title="", description=""):
        for inc in self.incidents.values():
            if inc.dedupe_key == dedupe_key and inc.status not in (IncidentStatus.RESOLVED,):
                return inc, False
        now = time.time()
        self._counter += 1
        incident = Incident(
            incident_id=f"inc-{int(now * 1000)}-{self._counter}",
            dedupe_key=dedupe_key,
            status=IncidentStatus.DETECTED,
            severity=severity,
            title=title,
            description=description,
            source_check_id=check_id,
            detected_at=now,
        )
        self.incidents[incident.incident_id] = incident
        storm = self.storm_detector.record(dedupe_key, check_id, severity.value)
        if storm and storm.get("is_storm"):
            self._counter += 1
            parent = self.storm_detector.build_storm_incident(storm, f"storm-{int(now * 1000)}-{self._counter}")
            self.incidents[parent.incident_id] = parent
            self.links.append(self.storm_detector.link_child(parent, incident))
            incident.parent_incident_id = parent.incident_id
            incident.is_systemic = True
        return incident, True

    def transition(self, incident_id, new_status):
        inc = self.incidents.get(incident_id)
        if inc is None:
            return None
        valid = {
            IncidentStatus.DETECTED: {IncidentStatus.CONFIRMED, IncidentStatus.RESOLVED},
            IncidentStatus.CONFIRMED: {IncidentStatus.MITIGATING, IncidentStatus.ESCALATED},
            IncidentStatus.MITIGATING: {IncidentStatus.VERIFYING, IncidentStatus.ESCALATED, IncidentStatus.LOCKED},
            IncidentStatus.VERIFYING: {IncidentStatus.RESOLVED, IncidentStatus.ESCALATED},
            IncidentStatus.ESCALATED: {IncidentStatus.MITIGATING, IncidentStatus.LOCKED},
            IncidentStatus.LOCKED: set(),
            IncidentStatus.RESOLVED: set(),
        }
        if new_status not in valid.get(inc.status, set()):
            return None
        inc.status = new_status
        now = time.time()
        if new_status == IncidentStatus.CONFIRMED:
            inc.confirmed_at = now
        elif new_status == IncidentStatus.MITIGATING:
            inc.mitigated_at = now
        elif new_status == IncidentStatus.VERIFYING:
            inc.verified_at = now
        elif new_status == IncidentStatus.RESOLVED:
            inc.resolved_at = now
        return inc

    def is_remediation_allowed(self, action):
        if action in REMEDIATION_AUTO:
            return True, "AUTO"
        if action in REMEDIATION_CONDITIONAL:
            return True, "CONDITIONAL"
        if action in REMEDIATION_NEVER:
            return False, "NEVER_AUTO"
        return False, "NOT_IN_ALLOWLIST"

    def get_active_p0_count(self):
        return sum(
            1
            for inc in self.incidents.values()
            if inc.severity == CheckSeverity.P0 and inc.status not in (IncidentStatus.RESOLVED,)
        )

    def escalate_to_locked(self):
        escalated = []
        for inc in self.incidents.values():
            if inc.severity == CheckSeverity.P0 and inc.status not in (IncidentStatus.RESOLVED, IncidentStatus.LOCKED):
                inc.status = IncidentStatus.LOCKED
                escalated.append(inc)
        return escalated
