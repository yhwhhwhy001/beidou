"""时钟完整性 — monotonic+Exchange drift (MON08)。"""
import time
from dataclasses import dataclass, field
from beidou_observability.monitoring.contracts import CheckSeverity, CheckStatus, MonitoringCheckResult
@dataclass(slots=True)
class ClockIntegrity:
    drift_warn_ms:float=2000.0; drift_block_ms:float=5000.0; _last_ex_ms:int=0; _last_local:float=0.0; _samples:list=field(default_factory=list)
    def record_exchange_time(self,server_time_ms):
        now=time.monotonic()
        if self._last_ex_ms>0:
            drift=(server_time_ms-self._last_ex_ms)-(now-self._last_local)*1000; self._samples.append(drift)
            if len(self._samples)>20: self._samples=self._samples[-20:]
        self._last_ex_ms=server_time_ms; self._last_local=now; return self.average_drift_ms
    @property
    def average_drift_ms(self): return sum(self._samples)/len(self._samples) if self._samples else 0.0
    def check(self):
        now=time.time(); drift=abs(self.average_drift_ms)
        if drift>self.drift_block_ms: return MonitoringCheckResult(check_id="runtime.integrity.clock",entity_type="system",entity_id="clock",status=CheckStatus.FAIL,severity=CheckSeverity.P0,message=f"Clock drift BLOCKED: {drift:.0f}ms>{self.drift_block_ms:.0f}ms",observed_at=now,actual={"drift_ms":round(drift,1)})
        if drift>self.drift_warn_ms: return MonitoringCheckResult(check_id="runtime.integrity.clock",entity_type="system",entity_id="clock",status=CheckStatus.WARN,severity=CheckSeverity.P1,message=f"Clock drift WARN: {drift:.0f}ms",observed_at=now)
        return MonitoringCheckResult(check_id="runtime.integrity.clock",entity_type="system",entity_id="clock",status=CheckStatus.PASS,severity=CheckSeverity.P1,message=f"Clock OK: {drift:.0f}ms",observed_at=now)
    def monotonic_elapsed(self,start): return time.monotonic()-start
