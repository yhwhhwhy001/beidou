"""PKG-MON-07: Factor/Strategy Lifecycle。"""
import math, time
from dataclasses import dataclass, field
from beidou_observability.monitoring.contracts import CheckSeverity, CheckStatus, MonitoringCheckResult
@dataclass(slots=True)
class FactorState: factor_id:str; value:float=0.0; lifecycle:str="ACTIVE"; last_evaluation:float=0.0; dependencies:list=field(default_factory=list)
def check_factors(states,*,active_strategy_refs=None):
    results=[]; now=time.time(); active=active_strategy_refs or set()
    for fs in states:
        if math.isnan(fs.value) or math.isinf(fs.value): results.append(MonitoringCheckResult(check_id="runtime.health.factor_lifecycle",entity_type="factor",entity_id=fs.factor_id,status=CheckStatus.FAIL,severity=CheckSeverity.P0,message=f"Factor {fs.factor_id}: value={fs.value}",observed_at=now))
        elif fs.lifecycle in ("RETIRED","SUSPENDED") and fs.factor_id in active: results.append(MonitoringCheckResult(check_id="runtime.health.factor_lifecycle",entity_type="factor",entity_id=fs.factor_id,status=CheckStatus.FAIL,severity=CheckSeverity.P0,message=f"Factor {fs.factor_id}: {fs.lifecycle} but referenced",observed_at=now))
        elif fs.last_evaluation>0 and (now-fs.last_evaluation)>600: results.append(MonitoringCheckResult(check_id="runtime.health.factor_lifecycle",entity_type="factor",entity_id=fs.factor_id,status=CheckStatus.WARN,severity=CheckSeverity.P1,message=f"Factor {fs.factor_id}: stale ({now-fs.last_evaluation:.0f}s)",observed_at=now))
    return results
def check_strategies(states):
    results=[]; now=time.time()
    for s in states:
        sid=s.get("strategy_id","unknown"); silence=s.get("signal_silence_seconds",0)
        if silence>3600: results.append(MonitoringCheckResult(check_id="runtime.health.strategy_lifecycle",entity_type="strategy",entity_id=sid,status=CheckStatus.WARN,severity=CheckSeverity.P1,message=f"Signal Silence: {silence:.0f}s",observed_at=now))
        drift=s.get("risk_budget_drift_pct",0)
        if drift>20: results.append(MonitoringCheckResult(check_id="runtime.health.strategy_lifecycle",entity_type="strategy",entity_id=sid,status=CheckStatus.WARN,severity=CheckSeverity.P1,message=f"Risk drift: {drift:.1f}%",observed_at=now))
    return results
