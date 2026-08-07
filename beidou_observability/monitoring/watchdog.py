"""Monitor Self-Health — Component Failure Matrix (MON10)。"""
import time
from dataclasses import dataclass, field
from beidou_observability.monitoring.contracts import ComponentHealth, CheckSeverity, CheckStatus, MonitoringCheckResult
@dataclass(slots=True)
class Watchdog:
    heartbeat_interval:float=5.0; max_missed_heartbeats:int=6; last_heartbeat:float=field(default_factory=time.monotonic); missed_count:int=0
    def heartbeat(self): self.last_heartbeat=time.monotonic(); self.missed_count=0
    def check(self):
        now=time.monotonic(); elapsed=now-self.last_heartbeat
        if elapsed>self.heartbeat_interval*self.max_missed_heartbeats:
            self.missed_count+=1; return MonitoringCheckResult(check_id="runtime.health.monitor_self",entity_type="monitor",entity_id="watchdog",status=CheckStatus.FAIL,severity=CheckSeverity.P0,message=f"Monitor STALL: {elapsed:.0f}s",observed_at=time.time())
        return MonitoringCheckResult(check_id="runtime.health.monitor_self",entity_type="monitor",entity_id="watchdog",status=CheckStatus.PASS,severity=CheckSeverity.P1,message=f"Monitor alive ({elapsed:.1f}s)",observed_at=time.time())
COMPONENT_FAILURE_MATRIX={
    "main_loop":ComponentHealth(component="main_loop",healthy=True,safe_action="NO_NEW_RISK",recovery_gate="loop_progress_pass"),
    "scheduler":ComponentHealth(component="scheduler",healthy=True,safe_action="ALERT",recovery_gate="scheduler_progress"),
    "fact_collector":ComponentHealth(component="fact_collector",healthy=True,safe_action="NO_NEW_RISK",recovery_gate="fresh_authoritative_fact"),
    "incident_manager":ComponentHealth(component="incident_manager",healthy=True,safe_action="NO_NEW_RISK",recovery_gate="durable_incident_rw"),
    "evidence_writer":ComponentHealth(component="evidence_writer",healthy=True,safe_action="ALERT",recovery_gate="durable_evidence_probe"),
    "repository":ComponentHealth(component="repository",healthy=True,safe_action="NO_NEW_RISK",recovery_gate="transactional_rw_probe"),
    "watchdog":ComponentHealth(component="watchdog",healthy=True,safe_action="OUTER_WATCHDOG_DETECT",recovery_gate="watchdog_heartbeat"),
    "metrics_exporter":ComponentHealth(component="metrics_exporter",healthy=True,safe_action="NONE",recovery_gate="exporter_recovered"),
}
