"""统一事故生命周期、持久化回放、去重和 remediation allowlist (MON09)。"""

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from beidou_observability.monitoring.contracts import (
    CheckSeverity,
    IncidentStatus,
)
from beidou_observability.monitoring.contracts import (
    Incident as MonitoringIncident,
)
from beidou_observability.monitoring.storm_detector import StormDetector
from beidou_observability.telemetry import (
    AlertSeverity,
    AutoAction,
)
from beidou_observability.telemetry import (
    Incident as AlertIncident,
)
from beidou_observability.telemetry import (
    IncidentStatus as AlertIncidentStatus,
)

logger = logging.getLogger(__name__)

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
    event_log_path: str | None = None
    alert_incidents: dict[str, AlertIncident] = field(default_factory=dict, init=False)
    _alert_lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _alert_io_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _alert_load_errors: list[str] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.event_log_path:
            self._load_alert_events()

    # ------------------------------------------------------------------
    # Unified production incident path
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_datetime(value: Any, *, default: datetime | None = None) -> datetime:
        if value is None or value == "":
            return default or datetime.now(timezone.utc)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _datetime_or_none(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    def _alert_record(self, event_type: str, incident: AlertIncident, **extra: Any) -> dict[str, Any]:
        return {
            "event_type": event_type,
            "event_at": datetime.now(timezone.utc).isoformat(),
            "incident_id": incident.incident_id,
            "dedupe_key": incident.dedupe_key,
            "severity": incident.severity.value,
            "title": incident.title,
            "description": incident.description,
            "category": incident.root_cause_category or "runtime",
            "source_check_id": incident.source_check_id,
            "entity_type": incident.entity_type,
            "entity_id": incident.entity_id,
            "correlation_id": str(incident.correlation_id or ""),
            "evidence_hash": incident.evidence_hash,
            "auto_action": incident.auto_action.value,
            "detected_at": incident.detected_at.isoformat(),
            "acknowledged_at": self._datetime_or_none(incident.acknowledged_at),
            "resolved_at": self._datetime_or_none(incident.resolved_at),
            "status": incident.status.value,
            "gap_reasons": list(incident.gap_reasons),
            "resolution": incident.resolution_reason,
            **extra,
        }

    def _append_alert_event(self, event_type: str, incident: AlertIncident, **extra: Any) -> None:
        """Append a replayable incident event before exposing it as active."""
        if not self.event_log_path:
            return
        path = Path(self.event_log_path)
        record = self._alert_record(event_type, incident, **extra)
        try:
            with self._alert_io_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                    handle.flush()
                    import os

                    os.fsync(handle.fileno())
        except OSError as exc:
            logger.critical("incident event persistence failed: %s", type(exc).__name__)
            raise

    @staticmethod
    def _alert_from_record(record: dict[str, Any]) -> AlertIncident:
        category = str(record.get("category") or record.get("root_cause_category") or "runtime")
        title = str(record.get("title", ""))
        dedupe_key = str(record.get("dedupe_key") or f"{category}:{title}")
        raw_correlation = str(record.get("correlation_id") or "")
        status = AlertIncidentStatus(str(record.get("status", AlertIncidentStatus.DETECTED.value)))
        return AlertIncident(
            incident_id=str(record["incident_id"]),
            severity=AlertSeverity(str(record["severity"])),
            title=title,
            description=str(record.get("description", "")),
            correlation_id=raw_correlation or None,
            root_cause_category=category,
            dedupe_key=dedupe_key,
            source_check_id=str(record.get("source_check_id", "")),
            entity_type=str(record.get("entity_type", "")),
            entity_id=str(record.get("entity_id", "")),
            evidence_hash=str(record.get("evidence_hash", "")),
            status=status,
            auto_action=AutoAction(str(record.get("auto_action", AutoAction.NOOP.value))),
            detected_at=IncidentManager._parse_datetime(record.get("detected_at")),
            acknowledged_at=(
                IncidentManager._parse_datetime(record.get("acknowledged_at"))
                if record.get("acknowledged_at")
                else None
            ),
            resolved_at=(
                IncidentManager._parse_datetime(record.get("resolved_at")) if record.get("resolved_at") else None
            ),
            gap_reasons=[str(reason) for reason in (record.get("gap_reasons") or [])],
            resolution_reason=str(record.get("resolution", "")),
        )

    def _load_alert_events(self) -> None:
        path = Path(self.event_log_path or "")
        if not path.exists():
            return
        try:
            with path.open(encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                        if not isinstance(record, dict) or not record.get("incident_id"):
                            raise ValueError("invalid incident event")
                        event_type = str(record.get("event_type") or "OPEN")
                        incident_id = str(record["incident_id"])
                        if event_type == "RESOLUTION" or str(record.get("status", "")) in {
                            AlertIncidentStatus.RESOLVED.value,
                            AlertIncidentStatus.CLOSED.value,
                        }:
                            self.alert_incidents.pop(incident_id, None)
                            continue
                        incident = self._alert_from_record(record)
                        self.alert_incidents[incident.incident_id] = incident
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                        self._alert_load_errors.append(f"line {line_number}: {type(exc).__name__}")
        except OSError as exc:
            self._alert_load_errors.append(f"file: {type(exc).__name__}")
            logger.critical("incident event load failed: %s", type(exc).__name__)

        # Old JSONL records had no durable dedupe key.  Collapse only exact
        # category/title duplicates so restart does not recreate a storm.
        latest_by_key: dict[str, AlertIncident] = {}
        for incident in sorted(self.alert_incidents.values(), key=lambda item: item.detected_at):
            latest_by_key[incident.dedupe_key] = incident
        self.alert_incidents = {incident.incident_id: incident for incident in latest_by_key.values()}

    def get_active_alert(self, dedupe_key: str) -> AlertIncident | None:
        with self._alert_lock:
            return next(
                (incident for incident in self.alert_incidents.values() if incident.dedupe_key == dedupe_key),
                None,
            )

    def create_or_dedupe_alert(
        self,
        severity: AlertSeverity,
        title: str,
        description: str,
        *,
        auto_action: AutoAction | None = None,
        category: str = "runtime",
        dedupe_key: str | None = None,
        source_check_id: str = "",
        entity_type: str = "",
        entity_id: str = "",
        correlation_id: str | None = None,
        evidence_hash: str = "",
        gap_reasons: list[str] | None = None,
    ) -> tuple[AlertIncident, bool]:
        """Create/update a telemetry Incident and durably record its event."""
        key = dedupe_key or f"{category}:{title}"
        action = auto_action or AutoAction.ALERT
        with self._alert_lock:
            existing = self.get_active_alert(key)
            if existing is not None:
                previous = (
                    existing.severity,
                    existing.description,
                    existing.auto_action,
                    existing.source_check_id,
                    existing.entity_type,
                    existing.entity_id,
                    str(existing.correlation_id or ""),
                    existing.evidence_hash,
                    tuple(existing.gap_reasons),
                )
                existing.severity = severity
                existing.description = description
                existing.auto_action = action
                existing.root_cause_category = category
                existing.source_check_id = source_check_id or existing.source_check_id
                existing.entity_type = entity_type or existing.entity_type
                existing.entity_id = entity_id or existing.entity_id
                existing.correlation_id = correlation_id or existing.correlation_id
                existing.evidence_hash = evidence_hash or existing.evidence_hash
                existing.gap_reasons = list(gap_reasons or [])
                existing._last_updated = datetime.now(timezone.utc)
                current = (
                    existing.severity,
                    existing.description,
                    existing.auto_action,
                    existing.source_check_id,
                    existing.entity_type,
                    existing.entity_id,
                    str(existing.correlation_id or ""),
                    existing.evidence_hash,
                    tuple(existing.gap_reasons),
                )
                if current != previous:
                    self._append_alert_event("UPDATE", existing)
                return existing, False

            self._counter += 1
            now = datetime.now(timezone.utc)
            incident = AlertIncident(
                incident_id=f"inc-{now.strftime('%Y%m%d%H%M%S%f')}-{self._counter}",
                severity=severity,
                title=title,
                description=description,
                correlation_id=correlation_id or None,
                root_cause_category=category,
                dedupe_key=key,
                source_check_id=source_check_id,
                entity_type=entity_type,
                entity_id=entity_id,
                evidence_hash=evidence_hash,
                auto_action=action,
                detected_at=now,
                gap_reasons=list(gap_reasons or []),
            )
            self._append_alert_event("OPEN", incident)
            self.alert_incidents[incident.incident_id] = incident
            return incident, True

    def resolve_alert(
        self,
        incident_id: str,
        *,
        resolution: str = "resolved",
        evidence_hash: str = "",
    ) -> AlertIncident | None:
        with self._alert_lock:
            incident = self.alert_incidents.get(incident_id)
            if incident is None:
                return None
            if evidence_hash:
                incident.evidence_hash = evidence_hash
            incident.resolve(resolution)
            self._append_alert_event("RESOLUTION", incident, resolution=resolution)
            return self.alert_incidents.pop(incident_id)

    def get_alert_by_id(self, incident_id: str) -> AlertIncident | None:
        with self._alert_lock:
            return self.alert_incidents.get(incident_id)

    def get_alert_load_errors(self) -> list[str]:
        with self._alert_lock:
            return list(self._alert_load_errors)

    def create_or_dedupe(self, dedupe_key, check_id="", severity=CheckSeverity.P1, title="", description=""):
        for inc in self.incidents.values():
            if inc.dedupe_key == dedupe_key and inc.status not in (IncidentStatus.RESOLVED,):
                return inc, False
        now = time.time()
        self._counter += 1
        incident = MonitoringIncident(
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
