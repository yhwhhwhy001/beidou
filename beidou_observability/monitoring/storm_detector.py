"""Incident Storm检测器 (MON09)。"""
import time, hashlib
from dataclasses import dataclass, field
from beidou_observability.monitoring.contracts import CheckSeverity, Incident, IncidentLink, IncidentStatus
@dataclass(slots=True)
class StormDetector:
    window_seconds:float=60.0; unique_dedupe_threshold:int=5; critical_threshold:int=10; _events:list=field(default_factory=list)
    def record(self,dedupe_key,check_id="",severity="P1"):
        now=time.monotonic(); self._events.append({"dedupe_key":dedupe_key,"check_id":check_id,"severity":severity,"timestamp":now})
        cutoff=now-self.window_seconds; self._events=[e for e in self._events if e["timestamp"]>cutoff]
        unique={e["dedupe_key"] for e in self._events}
        if len(unique)>=self.unique_dedupe_threshold: return {"is_storm":True,"unique_count":len(unique),"total_events":len(self._events),"window_seconds":self.window_seconds,"root_cause_fingerprint":self._fingerprint(unique)}
        return None
    def _fingerprint(self,keys): return hashlib.sha256("|".join(sorted(keys)[:10]).encode()).hexdigest()[:16]
    def build_storm_incident(self,info,parent_id=""):
        now=time.time(); return Incident(incident_id=parent_id or f"storm-{int(now)}",dedupe_key=f"systemic:{info.get('root_cause_fingerprint','')}",status=IncidentStatus.DETECTED,severity=CheckSeverity.P0,title=f"Storm: {info.get('unique_count',0)} failures",root_cause_fingerprint=info.get("root_cause_fingerprint",""),detected_at=now,is_systemic=True)
    def link_child(self,parent,child): return IncidentLink(parent_incident_id=parent.incident_id,child_incident_id=child.incident_id,linked_at=time.time(),root_cause_fingerprint=parent.root_cause_fingerprint)
    def is_storm_active(self):
        now=time.monotonic(); cutoff=now-self.window_seconds; recent=[e for e in self._events if e["timestamp"]>cutoff]
        return len({e["dedupe_key"] for e in recent})>=self.unique_dedupe_threshold
