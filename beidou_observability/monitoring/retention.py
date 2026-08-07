"""证据保留策略 — 6-tier (MON12)。"""
from dataclasses import dataclass, field
from enum import Enum
class RetentionTier(str,Enum): FAST_GUARD="fast_guard"; DEEP_AUDIT="deep_audit"; TRACE="trace"; P2_INCIDENT="p2_incident"; P0P1_INCIDENT="p0p1_incident"; CERTIFICATION="certification"
RETENTION_DAYS={RetentionTier.FAST_GUARD:7,RetentionTier.DEEP_AUDIT:30,RetentionTier.TRACE:90,RetentionTier.P2_INCIDENT:90,RetentionTier.P0P1_INCIDENT:365,RetentionTier.CERTIFICATION:-1}
@dataclass(slots=True)
class RetentionPolicy:
    tiers:dict=field(default_factory=lambda:dict(RETENTION_DAYS)); max_db_size_mb:int=500; warn_db_size_mb:int=350
    def retention_seconds(self,tier):
        days=self.tiers.get(tier,30); return float("inf") if days<0 else days*86400.0
    def should_retain(self,tier,record_age_seconds,*,has_open_incident=False,is_certification=False):
        if has_open_incident: return True
        if is_certification and tier==RetentionTier.CERTIFICATION: return True
        return record_age_seconds<self.retention_seconds(tier)
@dataclass(slots=True)
class RetentionRun:
    run_at:float=0.0; tier:RetentionTier=RetentionTier.FAST_GUARD; deleted_count:int=0; dry_run:bool=True; error:str=""; tombstone:str=""
