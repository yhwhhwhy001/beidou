"""BD-T19: 30 天无人值守认证 — SLI 收集、窗口管理和证书评估。

UnattendedCertification 管理 G7 认证窗口的完整生命周期:
- 窗口创建、暂停、恢复、重置
- SLI 样本持续采集 (7 项指标)
- 事故闭环追踪
- 日报/周报生成和签名绑定
- 窗口终止和证书签发
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class WindowStatus(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    RESET = "RESET"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class IncidentSeverity(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class SLICategory(str, Enum):
    DATA_QUALITY = "data_quality"
    ORDER_DUPLICATES = "order_duplicates"
    PROTECTION_SLO = "protection_slo"
    RECONCILIATION = "reconciliation"
    RECOVERY_BOUNDED = "recovery_bounded"
    INCIDENT_CLOSURE = "incident_closure"
    COST_PNL_REPORTING = "cost_and_pnl_reporting"


@dataclass
class SLISample:
    """单次 SLI 采样点。"""
    category: SLICategory
    value: float
    threshold: float
    passed: bool
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class IncidentRecord:
    """事故记录。"""
    incident_id: str
    severity: IncidentSeverity
    title: str
    description: str
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at: datetime | None = None
    resolved: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class DailyReport:
    """日报 — 签名绑定 evidence。"""
    date: str
    window_id: str
    sli_samples: list[SLISample] = field(default_factory=list)
    incidents_opened: int = 0
    incidents_closed: int = 0
    active_incidents: int = 0
    total_recovery_count: int = 0
    uptime_seconds: float = 0.0
    report_hash: str = ""
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def compute_hash(self) -> str:
        payload = json.dumps({
            "date": self.date,
            "sli": [(s.category.value, s.value, s.passed) for s in self.sli_samples],
            "incidents": (self.incidents_opened, self.incidents_closed),
            "recoveries": self.total_recovery_count,
        }, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def sign(self) -> str:
        self.report_hash = self.compute_hash()
        return self.report_hash


@dataclass
class CertificationWindow:
    """G7 认证窗口。"""
    window_id: str
    plan_version: str
    duration_days: int = 30
    status: WindowStatus = WindowStatus.CREATED
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: datetime | None = None
    # SLI 追踪
    sli_samples: list[SLISample] = field(default_factory=list)
    # 事故追踪
    incidents: list[IncidentRecord] = field(default_factory=list)
    # 报告
    daily_reports: list[DailyReport] = field(default_factory=list)
    # 重置条件
    reset_reason: str = ""
    reset_count: int = 0
    # 恢复计数
    total_recovery_count: int = 0
    # 证据绑定
    commit: str = ""
    g5_certificate_hash: str = ""
    evidence_dir: str = "artifacts/evidence/g7"

    def elapsed_days(self) -> float:
        if self.started_at:
            elapsed = (datetime.now(timezone.utc) - self.started_at).total_seconds()
            return elapsed / 86400.0
        return 0.0

    def is_complete(self) -> bool:
        return self.elapsed_days() >= self.duration_days


class UnattendedCertification:
    """BD-T19: 30 天无人值守认证引擎。

    管理 G7 认证窗口的生命周期 — 从创建到证书签发。
    """

    def __init__(self, evidence_dir: str = "artifacts/evidence/g7") -> None:
        self.evidence_dir = Path(evidence_dir)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self._windows: dict[str, CertificationWindow] = {}
        self._active_window: CertificationWindow | None = None

    # ---- Window Management ----

    def create_window(self, plan_version: str, duration_days: int = 30,
                      commit: str = "", g5_hash: str = "") -> CertificationWindow:
        window_id = f"g7-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        window = CertificationWindow(
            window_id=window_id,
            plan_version=plan_version,
            duration_days=duration_days,
            commit=commit,
            g5_certificate_hash=g5_hash,
        )
        self._windows[window_id] = window
        self._active_window = window
        self._save_state(window)
        return window

    def start_window(self, window_id: str) -> CertificationWindow:
        window = self._windows.get(window_id)
        if window is None:
            raise ValueError(f"Window {window_id} not found")
        window.status = WindowStatus.RUNNING
        window.started_at = datetime.now(timezone.utc)
        self._save_state(window)
        return window

    def pause_window(self, window_id: str, reason: str) -> None:
        window = self._windows.get(window_id)
        if window:
            window.status = WindowStatus.PAUSED
            self._save_state(window)

    def reset_window(self, window_id: str, reason: str) -> CertificationWindow:
        """重置窗口 — P0/重复订单/未保护仓位/账本差异触发。"""
        window = self._windows.get(window_id)
        if window:
            window.status = WindowStatus.RESET
            window.reset_reason = reason
            window.reset_count += 1
            window.started_at = datetime.now(timezone.utc)
            window.status = WindowStatus.RUNNING
            self._save_state(window)
            return window
        raise ValueError(f"Window {window_id} not found")

    # ---- SLI Collection ----

    def record_sli(self, window_id: str, sample: SLISample) -> None:
        window = self._windows.get(window_id)
        if window and window.status == WindowStatus.RUNNING:
            window.sli_samples.append(sample)

    def record_batch_sli(self, window_id: str, samples: list[SLISample]) -> None:
        for s in samples:
            self.record_sli(window_id, s)

    # ---- Incident Tracking ----

    def open_incident(self, window_id: str, incident: IncidentRecord) -> None:
        window = self._windows.get(window_id)
        if window:
            window.incidents.append(incident)
            # P0 incident → reset window
            if incident.severity == IncidentSeverity.P0:
                self.reset_window(window_id, f"P0 incident: {incident.title}")

    def close_incident(self, window_id: str, incident_id: str) -> None:
        window = self._windows.get(window_id)
        if window:
            for inc in window.incidents:
                if inc.incident_id == incident_id:
                    inc.resolved = True
                    inc.closed_at = datetime.now(timezone.utc)

    # ---- Reporting ----

    def generate_daily_report(self, window_id: str) -> DailyReport | None:
        window = self._windows.get(window_id)
        if window is None:
            return None

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_samples = [s for s in window.sli_samples
                         if s.timestamp.strftime("%Y-%m-%d") == today]

        report = DailyReport(
            date=today,
            window_id=window_id,
            sli_samples=today_samples,
            incidents_opened=sum(1 for i in window.incidents
                                 if i.opened_at.strftime("%Y-%m-%d") == today),
            incidents_closed=sum(1 for i in window.incidents
                                 if i.closed_at and i.closed_at.strftime("%Y-%m-%d") == today),
            active_incidents=sum(1 for i in window.incidents if not i.resolved),
            total_recovery_count=window.total_recovery_count,
        )
        report.sign()
        window.daily_reports.append(report)
        self._save_report(window_id, report)
        return report

    # ---- Evaluation ----

    def evaluate(self, window_id: str) -> dict[str, Any]:
        """评估 G7 窗口 — 签发证书或 FAIL。"""
        window = self._windows.get(window_id)
        if window is None:
            return {"status": "FAIL", "reason": f"Window {window_id} not found"}

        # Check 1: Duration
        if not window.is_complete():
            return {
                "status": "NOT_VERIFIABLE",
                "reason": f"Only {window.elapsed_days():.1f}/{window.duration_days} days elapsed",
                "elapsed_days": window.elapsed_days(),
            }

        # Check 2: No P0 incidents
        p0_incidents = [i for i in window.incidents if i.severity == IncidentSeverity.P0]
        if p0_incidents:
            return {
                "status": "FAIL",
                "reason": f"{len(p0_incidents)} P0 incidents during window",
                "p0_incidents": [i.title for i in p0_incidents],
            }

        # Check 3: No evidence gaps
        expected_days = window.duration_days
        if len(window.daily_reports) < expected_days:
            return {
                "status": "FAIL",
                "reason": f"Only {len(window.daily_reports)}/{expected_days} daily reports",
            }

        # Check 4: SLI pass rate
        if window.sli_samples:
            pass_rate = sum(1 for s in window.sli_samples if s.passed) / len(window.sli_samples)
            if pass_rate < 0.95:
                return {
                    "status": "FAIL",
                    "reason": f"SLI pass rate {pass_rate:.1%} < 95%",
                }

        # Check 5: No reset during window
        if window.reset_count > 0:
            return {
                "status": "FAIL",
                "reason": f"Window reset {window.reset_count} times: {window.reset_reason}",
            }

        # PASS — Generate G7 certificate
        evidence_hash = self._compute_evidence_hash(window)
        certificate = {
            "gate": "G7",
            "status": "PASS",
            "window_id": window_id,
            "duration_days": window.duration_days,
            "started_at": window.started_at.isoformat(),
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "commit": window.commit,
            "g5_certificate_hash": window.g5_certificate_hash,
            "evidence_hash": evidence_hash,
            "summary": {
                "total_sli_samples": len(window.sli_samples),
                "sli_pass_rate": f"{pass_rate:.1%}" if window.sli_samples else "N/A",
                "total_incidents": len(window.incidents),
                "p0_incidents": len(p0_incidents),
                "total_recoveries": window.total_recovery_count,
                "daily_reports": len(window.daily_reports),
                "resets": window.reset_count,
            },
            "disclaimer": "G7 Unattended certificate does NOT grant Mainnet access. "
                          "G8 requires separate human approval and capital ladder plan.",
        }

        window.status = WindowStatus.COMPLETED
        window.ended_at = datetime.now(timezone.utc)
        self._save_certificate(window_id, certificate)
        return certificate

    # ---- Helpers ----

    def _compute_evidence_hash(self, window: CertificationWindow) -> str:
        payload = json.dumps({
            "window_id": window.window_id,
            "duration_days": window.duration_days,
            "sli_count": len(window.sli_samples),
            "incident_count": len(window.incidents),
            "report_count": len(window.daily_reports),
            "reset_count": window.reset_count,
            "commit": window.commit,
        }, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def _save_state(self, window: CertificationWindow) -> None:
        path = self.evidence_dir / f"{window.window_id}-state.json"
        data = {
            "window_id": window.window_id,
            "status": window.status.value,
            "started_at": window.started_at.isoformat(),
            "elapsed_days": window.elapsed_days(),
            "reset_count": window.reset_count,
            "incident_count": len(window.incidents),
            "sli_sample_count": len(window.sli_samples),
            "report_count": len(window.daily_reports),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)

    def _save_report(self, window_id: str, report: DailyReport) -> None:
        path = self.evidence_dir / f"{window_id}-report-{report.date}.json"
        with open(path, "w") as f:
            json.dump({
                "date": report.date,
                "window_id": window_id,
                "sli_count": len(report.sli_samples),
                "incidents_opened": report.incidents_opened,
                "incidents_closed": report.incidents_closed,
                "active_incidents": report.active_incidents,
                "recoveries": report.total_recovery_count,
                "report_hash": report.report_hash,
            }, f, indent=2, default=str)

    def _save_certificate(self, window_id: str, certificate: dict) -> None:
        path = self.evidence_dir / f"{window_id}-g7-certificate.json"
        with open(path, "w") as f:
            json.dump(certificate, f, indent=2, default=str)

    def get_window(self, window_id: str) -> CertificationWindow | None:
        return self._windows.get(window_id)

    def list_windows(self) -> list[dict]:
        return [{
            "window_id": w.window_id,
            "status": w.status.value,
            "elapsed_days": w.elapsed_days(),
            "duration_days": w.duration_days,
        } for w in self._windows.values()]

    # ---- G7 重置条件检查 ----

    def check_reset_conditions(self, window_id: str,
                                duplicate_orders: int = 0,
                                unprotected_duration_seconds: float = 0.0,
                                ledger_mismatch: bool = False,
                                evidence_gap: bool = False) -> list[str]:
        """检查 G7 重置条件 — 任一触发则返回触发原因列表。"""
        triggers: list[str] = []
        PROTECTION_SLO = 300  # 5 分钟保护 SLO

        if duplicate_orders > 0:
            triggers.append(f"duplicate_orders: {duplicate_orders}")
        if unprotected_duration_seconds > PROTECTION_SLO:
            triggers.append(f"unprotected_position: {unprotected_duration_seconds:.0f}s > {PROTECTION_SLO}s SLO")
        if ledger_mismatch:
            triggers.append("ledger_mismatch")
        if evidence_gap:
            triggers.append("evidence_gap")

        if triggers:
            reason = "; ".join(triggers)
            self.reset_window(window_id, reason)

        return triggers
