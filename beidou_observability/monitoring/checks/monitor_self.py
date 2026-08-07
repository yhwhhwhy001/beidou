"""PKG-MON-10: Monitor Self-Health checks (FR-MON-010)。"""
import time
from beidou_observability.monitoring.contracts import CheckSeverity, CheckStatus, ComponentHealth, MonitoringCheckResult
from beidou_observability.monitoring.watchdog import COMPONENT_FAILURE_MATRIX
def check_component_health(components=None):
    comps=components or COMPONENT_FAILURE_MATRIX; now=time.time(); results=[]
    for name,c in comps.items():
        if not c.healthy: results.append(MonitoringCheckResult(check_id="runtime.health.monitor_self",entity_type="monitor_component",entity_id=name,status=CheckStatus.FAIL if c.safe_action=="NO_NEW_RISK" else CheckStatus.WARN,severity=CheckSeverity.P0 if c.safe_action=="NO_NEW_RISK" else CheckSeverity.P1,message=f"Component {name} UNHEALTHY: {c.safe_action}",observed_at=now))
    return results
def check_monitor_loop_health(last_loop_at,max_stall=30.0):
    now=time.time(); elapsed=now-last_loop_at if last_loop_at>0 else 0
    if elapsed>max_stall: return MonitoringCheckResult(check_id="runtime.health.monitor_self",entity_type="monitor",entity_id="main_loop",status=CheckStatus.FAIL,severity=CheckSeverity.P0,message=f"Main loop STALL: {elapsed:.1f}s>{max_stall}s",observed_at=now)
    return MonitoringCheckResult(check_id="runtime.health.monitor_self",entity_type="monitor",entity_id="main_loop",status=CheckStatus.PASS,severity=CheckSeverity.P1,message=f"Loop OK ({elapsed:.1f}s)",observed_at=now)
