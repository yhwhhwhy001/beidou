"""证据模块 — UNKNOWN blocking + 确定性hash (MON01-03/04)。"""
from __future__ import annotations
import time, hashlib
from dataclasses import dataclass, field
from typing import Any
from beidou_observability.monitoring.contracts import CheckSeverity, CheckStatus, MonitoringCheckResult, compute_evidence_hash
@dataclass(slots=True)
class EvidenceRecord:
    check_id: str; status: CheckStatus; severity: CheckSeverity; message: str=""
    expected: Any=None; actual: Any=None; source: str=""; source_timestamp: float|None=None; observed_at: float=0.0
    fact_age_ms: float|None=None; evidence_hash: str=""; trace: dict=field(default_factory=dict)
    def compute_hash(self): return compute_evidence_hash({"check_id":self.check_id,"status":self.status.value,"severity":self.severity.value,"message":self.message,"expected":str(self.expected),"actual":str(self.actual),"source":self.source,"source_timestamp":self.source_timestamp,"observed_at":self.observed_at})
    def is_blocking(self): return (self.severity in (CheckSeverity.P0, CheckSeverity.P1) and self.status == CheckStatus.UNKNOWN) or (self.severity == CheckSeverity.P0 and self.status == CheckStatus.FAIL)
@dataclass(slots=True)
class EvidenceBundle:
    bundle_id: str; created_at: float=field(default_factory=time.monotonic); records: list=field(default_factory=list); bundle_hash: str=""
    def add(self, r):
        if not r.evidence_hash: r.evidence_hash = r.compute_hash()
        self.records.append(r)
    def finalize(self): self.bundle_hash = hashlib.sha256("|".join(sorted(r.evidence_hash for r in self.records)).encode()).hexdigest()[:32]
    def has_blocking_failure(self): return any(r.is_blocking() for r in self.records)
    def p0_count(self): return sum(1 for r in self.records if r.severity == CheckSeverity.P0 and r.status != CheckStatus.PASS)
    def summary(self): return {"bundle_id":self.bundle_id,"total":len(self.records),"pass":sum(1 for r in self.records if r.status==CheckStatus.PASS),"warn":sum(1 for r in self.records if r.status==CheckStatus.WARN),"fail":sum(1 for r in self.records if r.status==CheckStatus.FAIL),"unknown":sum(1 for r in self.records if r.status==CheckStatus.UNKNOWN),"p0_issues":self.p0_count(),"blocking":self.has_blocking_failure(),"bundle_hash":self.bundle_hash}
def from_check_result(r): return EvidenceRecord(check_id=r.check_id, status=r.status, severity=r.severity, message=r.message, expected=r.expected, actual=r.actual, source=r.source, source_timestamp=r.source_timestamp, observed_at=r.observed_at, fact_age_ms=r.fact_age_ms, evidence_hash=r.compute_evidence_hash())
def verify_evidence_integrity(rec): return rec.compute_hash() == rec.evidence_hash
