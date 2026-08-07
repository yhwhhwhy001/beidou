"""统一事实收集器 — 7级优先级+冲突检测 (MON02)。"""
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from beidou_observability.monitoring.snapshot_broker import SnapshotBroker
class FactDomain(str,Enum): EXCHANGE="exchange"; ACCOUNT="account"; SUPERVISOR="supervisor"; PERSISTENT="persistent"; ENGINE="engine"; DERIVED="derived"; LOG="log"
FACT_PRECEDENCE={FactDomain.EXCHANGE:1,FactDomain.ACCOUNT:2,FactDomain.SUPERVISOR:3,FactDomain.PERSISTENT:4,FactDomain.ENGINE:5,FactDomain.DERIVED:6,FactDomain.LOG:7}
@dataclass(slots=True)
class CollectedFact:
    domain:FactDomain; key:str; value:Any; source:str=""; source_timestamp:float|None=None; observed_at:float=0.0; is_authoritative:bool=False; error:str|None=None
    @property
    def is_valid(self): return self.error is None and self.value is not None
@dataclass(slots=True)
class FactCollector:
    broker:SnapshotBroker=field(default_factory=SnapshotBroker); _facts:dict=field(default_factory=dict); _conflicts:list=field(default_factory=list)
    def record_authoritative(self,domain,key,value,*,source="",ts=None):
        fact=CollectedFact(domain=domain,key=key,value=value,source=source,source_timestamp=ts,observed_at=time.monotonic(),is_authoritative=True); self._detect_conflict(fact); self._facts[key]=fact; return fact
    def record_derived(self,key,value,*,source=""):
        fact=CollectedFact(domain=FactDomain.DERIVED,key=key,value=value,source=source,observed_at=time.monotonic()); self._facts[key]=fact; return fact
    def record_error(self,domain,key,error):
        fact=CollectedFact(domain=domain,key=key,value=None,error=error,observed_at=time.monotonic()); self._facts[key]=fact; return fact
    def get(self,key): return self._facts.get(key)
    def get_authoritative(self,key):
        f=self._facts.get(key); return f if (f and f.is_authoritative) else None
    def get_required(self,key):
        f=self._facts.get(key)
        if f is None: return CollectedFact(domain=FactDomain.DERIVED,key=key,value=None,error=f"Required fact '{key}' not found",observed_at=time.monotonic())
        return f
    def has_fresh(self,key,max_age=15.0):
        f=self._facts.get(key)
        if f is None or f.error or f.observed_at<=0: return False
        return (time.monotonic()-f.observed_at)<=max_age
    def _detect_conflict(self,new_fact):
        existing=self._facts.get(new_fact.key)
        if existing and existing.is_authoritative and new_fact.is_authoritative:
            if FACT_PRECEDENCE.get(existing.domain,99)>FACT_PRECEDENCE.get(new_fact.domain,99):
                self._conflicts.append({"key":new_fact.key,"existing":existing.domain.value,"new":new_fact.domain.value,"resolution":"higher_precedence","timestamp":time.monotonic()})
    def conflicts(self): return list(self._conflicts)
    def inspect_runtime_status(self):
        now=time.monotonic()
        fs={k:{"domain":f.domain.value,"authoritative":f.is_authoritative,"valid":f.is_valid,"error":f.error,"age_seconds":round(now-f.observed_at,3) if f.observed_at>0 else None} for k,f in self._facts.items()}
        return {"facts":fs,"conflicts":len(self._conflicts),"snapshots":self.broker.all_snapshots_summary(),"critical_unknowns":[k for k,f in self._facts.items() if f.error and f.domain in (FactDomain.EXCHANGE,FactDomain.ACCOUNT)],"observed_at":now}
    def validate_no_critical_unknown(self):
        uk=[k for k,f in self._facts.items() if f.error and f.domain in (FactDomain.EXCHANGE,FactDomain.ACCOUNT,FactDomain.SUPERVISOR)]
        return len(uk)==0,uk
