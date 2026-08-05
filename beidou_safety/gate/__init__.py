"""Gate 认证模块。每个 Gate 独立发证。"""
from __future__ import annotations
from datetime import datetime, timezone
from dataclasses import dataclass, field
from beidou_shared.types import GateResult

@dataclass
class GateCertificate:
    gate_id: str
    result: GateResult
    issued_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    evidence_paths: list[str] = field(default_factory=list)
    p0_issues: list[str] = field(default_factory=list)
    p1_issues: list[str] = field(default_factory=list)
    reviewer: str = "independent_review"
