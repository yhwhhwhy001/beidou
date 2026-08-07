"""快照代理 — Supervisor复用+TTL+single-flight (MON02)。"""
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
class SnapshotSource(str,Enum): SUPERVISOR_FRESH="supervisor_fresh"; SHARED="shared"; DEEP_AUDIT_DIRECT="deep_audit_direct"; STALE="stale"; UNKNOWN="unknown"
SOURCE_PRECEDENCE={SnapshotSource.SUPERVISOR_FRESH:1,SnapshotSource.SHARED:2,SnapshotSource.DEEP_AUDIT_DIRECT:3,SnapshotSource.STALE:10,SnapshotSource.UNKNOWN:99}
@dataclass(slots=True)
class SnapshotFact:
    source:SnapshotSource; data:Any; observed_at:float=0.0; source_timestamp:float|None=None; ttl_seconds:float=15.0; query_owner:str=""
    @property
    def age_seconds(self): return time.monotonic()-self.observed_at if self.observed_at>0 else float("inf")
    @property
    def is_fresh(self): return self.age_seconds<=self.ttl_seconds
    @property
    def is_stale(self): return not self.is_fresh
@dataclass(slots=True)
class SnapshotBroker:
    _snapshots:dict=field(default_factory=dict); account_ttl:float=15.0; algo_ttl:float=15.0; position_mode_ttl:float=300.0; exchange_info_ttl:float=3600.0
    def _key(self,e,s=""): return f"{e}:{s}" if s else e
    def _ttl_for(self,e):
        if "account" in e: return self.account_ttl
        if "algo" in e or "openOrders" in e: return self.algo_ttl
        if "positionSide" in e: return self.position_mode_ttl
        return 15.0
    def feed_supervisor_snapshot(self,endpoint,data,*,account_scope="",observed_at=None,ttl_seconds=None):
        key=self._key(endpoint,account_scope)
        fact=SnapshotFact(source=SnapshotSource.SUPERVISOR_FRESH,data=data,observed_at=observed_at or time.monotonic(),source_timestamp=data.get("observed_at") if isinstance(data,dict) else None,ttl_seconds=ttl_seconds or self._ttl_for(endpoint),query_owner="supervisor")
        self._snapshots[key]=fact; return fact
    def get_snapshot(self,endpoint,*,account_scope="",max_age_seconds=None):
        fact=self._snapshots.get(self._key(endpoint,account_scope))
        if fact is None: return SnapshotFact(source=SnapshotSource.UNKNOWN,data=None,ttl_seconds=self._ttl_for(endpoint))
        age_limit=max_age_seconds or fact.ttl_seconds
        if fact.age_seconds>age_limit: return SnapshotFact(source=SnapshotSource.STALE,data=fact.data,observed_at=fact.observed_at,source_timestamp=fact.source_timestamp,ttl_seconds=fact.ttl_seconds)
        return fact
    def invalidate(self,endpoint,account_scope=""): self._snapshots.pop(self._key(endpoint,account_scope),None)
    def all_snapshots_summary(self): return {k:{"source":f.source.value,"age_seconds":round(f.age_seconds,3),"is_fresh":f.is_fresh} for k,f in self._snapshots.items()}
    def detect_source_conflict(self,endpoint,account_scope=""):
        fact=self._snapshots.get(self._key(endpoint,account_scope))
        if fact and fact.source==SnapshotSource.SUPERVISOR_FRESH and fact.is_stale: return {"endpoint":endpoint,"issue":"stale","age_seconds":fact.age_seconds}
        return None
