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
import os
import re
from contextlib import suppress
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


# A real G7 window must contain enough independent monitoring cycles to make a
# 30-day result meaningful.  The external verifier uses the same floor; the
# producer must never emit a locally PASS certificate that the verifier will
# immediately reject.
MINIMUM_SLI_SAMPLES = 200
STATE_SCHEMA_VERSION = 2


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
        payload = json.dumps(
            {
                "date": self.date,
                "sli": [(s.category.value, s.value, s.passed) for s in self.sli_samples],
                "incidents": (self.incidents_opened, self.incidents_closed),
                "recoveries": self.total_recovery_count,
            },
            sort_keys=True,
        )
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
    minimum_sli_samples: int = MINIMUM_SLI_SAMPLES
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
    is_simulated: bool = False
    # Legacy state files only contained counters.  Such a window may be
    # displayed for diagnosis, but it must never be allowed to certify after a
    # process restart because the underlying samples/incidents are absent.
    evidence_state_complete: bool = True

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
        self._state_load_errors: list[str] = []
        self._load_state_files()

    # ---- Window Management ----

    def create_window(
        self,
        plan_version: str,
        duration_days: int = 30,
        commit: str = "",
        g5_hash: str = "",
        window_id: str | None = None,
    ) -> CertificationWindow:
        explicit_window_id = str(window_id or "").strip()
        if explicit_window_id:
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", explicit_window_id) is None:
                raise ValueError("Invalid G7 window_id")
            if explicit_window_id in self._windows or (self.evidence_dir / f"{explicit_window_id}-state.json").exists():
                raise ValueError(f"G7 window already exists: {explicit_window_id}")
            resolved_window_id = explicit_window_id
        else:
            # Include microseconds and still check the directory so two
            # starters in the same second cannot silently overwrite a window.
            timestamp = datetime.now(timezone.utc)
            resolved_window_id = f"g7-{timestamp.strftime('%Y%m%d-%H%M%S-%f')}"
            while (
                resolved_window_id in self._windows or (self.evidence_dir / f"{resolved_window_id}-state.json").exists()
            ):
                timestamp = datetime.now(timezone.utc)
                resolved_window_id = f"g7-{timestamp.strftime('%Y%m%d-%H%M%S-%f')}"
        window = CertificationWindow(
            window_id=resolved_window_id,
            plan_version=plan_version,
            duration_days=duration_days,
            commit=commit,
            g5_certificate_hash=g5_hash,
        )
        self._windows[resolved_window_id] = window
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
            self._save_state(window)

    def record_batch_sli(self, window_id: str, samples: list[SLISample]) -> None:
        window = self._windows.get(window_id)
        if window and window.status == WindowStatus.RUNNING:
            window.sli_samples.extend(samples)
            self._save_state(window)

    # ---- Incident Tracking ----

    def open_incident(self, window_id: str, incident: IncidentRecord) -> None:
        window = self._windows.get(window_id)
        if window:
            window.incidents.append(incident)
            # P0 incident → reset window
            if incident.severity == IncidentSeverity.P0:
                self.reset_window(window_id, f"P0 incident: {incident.title}")
            else:
                self._save_state(window)

    def close_incident(self, window_id: str, incident_id: str) -> None:
        window = self._windows.get(window_id)
        if window:
            for inc in window.incidents:
                if inc.incident_id == incident_id:
                    inc.resolved = True
                    inc.closed_at = datetime.now(timezone.utc)
                    self._save_state(window)
                    break

    # ---- Reporting ----

    def generate_daily_report(self, window_id: str) -> DailyReport | None:
        window = self._windows.get(window_id)
        if window is None:
            return None

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        existing_report = next((report for report in window.daily_reports if report.date == today), None)
        if existing_report is not None:
            return existing_report
        today_samples = [s for s in window.sli_samples if s.timestamp.strftime("%Y-%m-%d") == today]

        report = DailyReport(
            date=today,
            window_id=window_id,
            sli_samples=today_samples,
            incidents_opened=sum(1 for i in window.incidents if i.opened_at.strftime("%Y-%m-%d") == today),
            incidents_closed=sum(
                1 for i in window.incidents if i.closed_at and i.closed_at.strftime("%Y-%m-%d") == today
            ),
            active_incidents=sum(1 for i in window.incidents if not i.resolved),
            total_recovery_count=window.total_recovery_count,
        )
        report.sign()
        window.daily_reports.append(report)
        self._save_report(window_id, report)
        self._save_state(window)
        return report

    # ---- Evaluation ----

    def evaluate(self, window_id: str) -> dict[str, Any]:
        """评估 G7 窗口 — 签发证书或 FAIL。"""
        window = self._windows.get(window_id)
        if window is None:
            return {"status": "FAIL", "reason": f"Window {window_id} not found"}

        if not window.evidence_state_complete:
            return {
                "status": "NOT_VERIFIABLE",
                "reason": "Persisted window state is a legacy counter-only snapshot; restart the G7 window",
            }
        if window.is_simulated or any(sample.metadata.get("simulated") is True for sample in window.sli_samples):
            return {
                "status": "NOT_VERIFIABLE",
                "reason": "G7 evidence is simulation-only; a fresh real-time window is required",
                "is_simulated": True,
            }

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

        # Check 3: SLI evidence must be present, sufficiently sampled, and
        # complete across the seven categories.  An empty/partial window is
        # NOT_VERIFIABLE, never a PASS by vacuous truth.
        if len(window.sli_samples) < window.minimum_sli_samples:
            return {
                "status": "NOT_VERIFIABLE",
                "reason": f"Only {len(window.sli_samples)}/{window.minimum_sli_samples} SLI samples",
            }
        categories = {sample.category for sample in window.sli_samples}
        if categories != set(SLICategory):
            return {
                "status": "NOT_VERIFIABLE",
                "reason": "SLI evidence does not cover every required category",
            }
        pass_rate = sum(1 for s in window.sli_samples if s.passed) / len(window.sli_samples)
        if pass_rate < 1.0:
            return {
                "status": "FAIL",
                "reason": f"SLI pass rate {pass_rate:.1%} < 100%",
            }

        # Check 4: No evidence gaps
        expected_days = window.duration_days
        if len(window.daily_reports) < expected_days:
            return {
                "status": "FAIL",
                "reason": f"Only {len(window.daily_reports)}/{expected_days} daily reports",
            }

        # Check 5: No reset during window
        if window.reset_count > 0:
            return {
                "status": "FAIL",
                "reason": f"Window reset {window.reset_count} times: {window.reset_reason}",
            }

        # Check 6: no unresolved incidents at the certification boundary.
        active_incidents = [i for i in window.incidents if not i.resolved]
        if active_incidents:
            return {
                "status": "FAIL",
                "reason": f"{len(active_incidents)} active incidents remain",
            }

        # PASS — Generate G7 certificate
        evidence_hash = self._compute_evidence_hash(window)
        ended_at = datetime.now(timezone.utc)
        certificate = {
            "gate": "G7",
            "status": "PASS",
            "window_id": window_id,
            "duration_days": window.duration_days,
            "started_at": window.started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "commit": window.commit,
            "g5_certificate_hash": window.g5_certificate_hash,
            "evidence_hash": evidence_hash,
            "mainnet_prohibited": True,
            "is_simulated": False,
            "summary": {
                "total_sli_samples": len(window.sli_samples),
                "sli_pass_rate": f"{pass_rate:.1%}",
                "total_incidents": len(window.incidents),
                "p0_incidents": len(p0_incidents),
                "active_incidents": sum(1 for i in window.incidents if not i.resolved),
                "total_recoveries": window.total_recovery_count,
                "daily_reports": len(window.daily_reports),
                "resets": window.reset_count,
            },
            "disclaimer": "G7 Unattended certificate does NOT grant Mainnet access. "
            "G8 requires separate human approval and capital ladder plan.",
        }

        window.status = WindowStatus.COMPLETED
        window.ended_at = ended_at
        self._save_state(window)
        self._save_certificate(window_id, certificate)
        return certificate

    def fast_forward(self, window_id: str, duration_days: int = 30) -> dict[str, Any]:
        """快速前进窗口 — 使用历史数据模拟完整认证周期。

        回填 30 天的 SLI 采样和日报，用于验证框架正确性。
        生成的证书标记 is_simulated=True，与真实 G7 证书区分。
        """
        window = self._windows.get(window_id)
        if window is None:
            return {"status": "FAIL", "reason": f"Window {window_id} not found"}

        from datetime import timedelta

        base_date = window.started_at
        window.is_simulated = True
        for day in range(duration_days):
            day_date = base_date + timedelta(days=day)
            day_str = day_date.strftime("%Y-%m-%d")

            # 每日 SLI 采样 (7 项指标)
            for category in SLICategory:
                sample = SLISample(
                    category=category,
                    value=1.0,
                    threshold=0.9,
                    passed=True,
                    timestamp=day_date + timedelta(hours=12),
                    metadata={"simulated": True, "day": day + 1},
                )
                window.sli_samples.append(sample)

            # 日报
            report = DailyReport(
                date=day_str,
                window_id=window_id,
                sli_samples=[s for s in window.sli_samples if s.timestamp.strftime("%Y-%m-%d") == day_str],
                incidents_opened=0,
                incidents_closed=0,
                active_incidents=0,
                total_recovery_count=0,
                uptime_seconds=86400.0,
            )
            report.sign()
            window.daily_reports.append(report)
            self._save_report(window_id, report)
            # Persist after every completed day.  A crash during a later day
            # must leave a recoverable prefix, never an empty in-memory run.
            self._save_state(window)

        # 调整 started_at 使窗口看起来已完成
        window.started_at = base_date - timedelta(days=duration_days)
        self._save_state(window)

        # 评估
        result = self.evaluate(window_id)
        result["is_simulated"] = True
        result["disclaimer"] = (
            "SIMULATED G7 evidence — generated via fast-forward for framework verification. "
            "Real G7 requires 30 days of actual unattended operation. G8 Mainnet remains PROHIBITED."
        )
        if result.get("status") == "PASS":
            # 更新证书文件
            self._save_certificate(window_id, result)

        return result

    # ---- Helpers ----

    def _compute_evidence_hash(self, window: CertificationWindow) -> str:
        def sample_payload(sample: SLISample) -> dict[str, Any]:
            return {
                "category": sample.category.value,
                "value": sample.value,
                "threshold": sample.threshold,
                "passed": sample.passed,
                "timestamp": sample.timestamp.isoformat(),
                "metadata": sample.metadata,
            }

        def incident_payload(incident: IncidentRecord) -> dict[str, Any]:
            return {
                "incident_id": incident.incident_id,
                "severity": incident.severity.value,
                "title": incident.title,
                "description": incident.description,
                "opened_at": incident.opened_at.isoformat(),
                "closed_at": incident.closed_at.isoformat() if incident.closed_at else None,
                "resolved": incident.resolved,
                "evidence": incident.evidence,
            }

        def report_payload(report: DailyReport) -> dict[str, Any]:
            return {
                "date": report.date,
                "window_id": report.window_id,
                "sli_samples": [sample_payload(sample) for sample in report.sli_samples],
                "incidents_opened": report.incidents_opened,
                "incidents_closed": report.incidents_closed,
                "active_incidents": report.active_incidents,
                "total_recovery_count": report.total_recovery_count,
                "uptime_seconds": report.uptime_seconds,
                "report_hash": report.report_hash,
                "generated_at": report.generated_at.isoformat(),
            }

        payload = json.dumps(
            {
                "window_id": window.window_id,
                "plan_version": window.plan_version,
                "duration_days": window.duration_days,
                "started_at": window.started_at.isoformat(),
                "ended_at": window.ended_at.isoformat() if window.ended_at else None,
                "sli_samples": [sample_payload(sample) for sample in window.sli_samples],
                "incidents": [incident_payload(incident) for incident in window.incidents],
                "daily_reports": [report_payload(report) for report in window.daily_reports],
                "reset_count": window.reset_count,
                "reset_reason": window.reset_reason,
                "commit": window.commit,
                "g5_certificate_hash": window.g5_certificate_hash,
                "is_simulated": window.is_simulated,
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def _save_state(self, window: CertificationWindow) -> None:
        path = self.evidence_dir / f"{window.window_id}-state.json"
        data = {
            "state_schema_version": STATE_SCHEMA_VERSION,
            "window_id": window.window_id,
            "plan_version": window.plan_version,
            "duration_days": window.duration_days,
            "status": window.status.value,
            "started_at": window.started_at.isoformat(),
            "ended_at": window.ended_at.isoformat() if window.ended_at else None,
            "elapsed_days": window.elapsed_days(),
            "reset_count": window.reset_count,
            "reset_reason": window.reset_reason,
            "total_recovery_count": window.total_recovery_count,
            "commit": window.commit,
            "g5_certificate_hash": window.g5_certificate_hash,
            "is_simulated": window.is_simulated,
            "sli_samples": [self._serialize_sli(sample) for sample in window.sli_samples],
            "incidents": [self._serialize_incident(incident) for incident in window.incidents],
            "daily_reports": [self._serialize_report(report) for report in window.daily_reports],
            "incident_count": len(window.incidents),
            "sli_sample_count": len(window.sli_samples),
            "report_count": len(window.daily_reports),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._atomic_write_json(path, data)

    def _save_report(self, window_id: str, report: DailyReport) -> None:
        path = self.evidence_dir / f"{window_id}-report-{report.date}.json"
        self._atomic_write_json(path, self._serialize_report(report))

    def _save_certificate(self, window_id: str, certificate: dict) -> None:
        path = self.evidence_dir / f"{window_id}-g7-certificate.json"
        self._atomic_write_json(path, certificate)

    def get_window(self, window_id: str) -> CertificationWindow | None:
        return self._windows.get(window_id)

    @property
    def state_load_errors(self) -> tuple[str, ...]:
        """Return non-fatal legacy/corrupt-state diagnostics.

        Legacy snapshots are retained for audit visibility but are never
        silently treated as complete evidence.  A caller can surface these
        diagnostics in a readiness report without allowing them to certify.
        """

        return tuple(self._state_load_errors)

    def list_windows(self) -> list[dict]:
        return [
            {
                "window_id": w.window_id,
                "status": w.status.value,
                "elapsed_days": w.elapsed_days(),
                "duration_days": w.duration_days,
                "evidence_state_complete": bool(w.evidence_state_complete),
                "is_simulated": bool(w.is_simulated),
            }
            for w in self._windows.values()
        ]

    # ---- G7 重置条件检查 ----

    def check_reset_conditions(
        self,
        window_id: str,
        duplicate_orders: int = 0,
        unprotected_duration_seconds: float = 0.0,
        ledger_mismatch: bool = False,
        evidence_gap: bool = False,
    ) -> list[str]:
        """检查 G7 重置条件 — 任一触发则返回触发原因列表。"""
        triggers: list[str] = []
        protection_slo = 300  # 5 分钟保护 SLO

        if duplicate_orders > 0:
            triggers.append(f"duplicate_orders: {duplicate_orders}")
        if unprotected_duration_seconds > protection_slo:
            triggers.append(f"unprotected_position: {unprotected_duration_seconds:.0f}s > {protection_slo}s SLO")
        if ledger_mismatch:
            triggers.append("ledger_mismatch")
        if evidence_gap:
            triggers.append("evidence_gap")

        if triggers:
            reason = "; ".join(triggers)
            self.reset_window(window_id, reason)

        return triggers

    # ---- Durable state encoding ----

    @staticmethod
    def _serialize_sli(sample: SLISample) -> dict[str, Any]:
        return {
            "category": sample.category.value,
            "value": sample.value,
            "threshold": sample.threshold,
            "passed": sample.passed,
            "timestamp": sample.timestamp.isoformat(),
            "metadata": sample.metadata,
        }

    @staticmethod
    def _serialize_incident(incident: IncidentRecord) -> dict[str, Any]:
        return {
            "incident_id": incident.incident_id,
            "severity": incident.severity.value,
            "title": incident.title,
            "description": incident.description,
            "opened_at": incident.opened_at.isoformat(),
            "closed_at": incident.closed_at.isoformat() if incident.closed_at else None,
            "resolved": incident.resolved,
            "evidence": incident.evidence,
        }

    @classmethod
    def _serialize_report(cls, report: DailyReport) -> dict[str, Any]:
        return {
            "date": report.date,
            "window_id": report.window_id,
            "sli_samples": [cls._serialize_sli(sample) for sample in report.sli_samples],
            "sli_count": len(report.sli_samples),
            "incidents_opened": report.incidents_opened,
            "incidents_closed": report.incidents_closed,
            "active_incidents": report.active_incidents,
            "recoveries": report.total_recovery_count,
            "total_recovery_count": report.total_recovery_count,
            "uptime_seconds": report.uptime_seconds,
            "report_hash": report.report_hash,
            "generated_at": report.generated_at.isoformat(),
        }

    @staticmethod
    def _parse_datetime(value: Any, field_name: str) -> datetime:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must be an ISO timestamp")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field_name} is not a valid ISO timestamp") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _deserialize_sli(cls, value: Any) -> SLISample:
        if not isinstance(value, dict):
            raise ValueError("SLI sample must be an object")
        return SLISample(
            category=SLICategory(value["category"]),
            value=float(value["value"]),
            threshold=float(value["threshold"]),
            passed=bool(value["passed"]),
            timestamp=cls._parse_datetime(value["timestamp"], "SLI timestamp"),
            metadata=dict(value.get("metadata") or {}),
        )

    @classmethod
    def _deserialize_incident(cls, value: Any) -> IncidentRecord:
        if not isinstance(value, dict):
            raise ValueError("incident must be an object")
        closed_at = value.get("closed_at")
        return IncidentRecord(
            incident_id=str(value["incident_id"]),
            severity=IncidentSeverity(value["severity"]),
            title=str(value["title"]),
            description=str(value["description"]),
            opened_at=cls._parse_datetime(value["opened_at"], "incident opened_at"),
            closed_at=cls._parse_datetime(closed_at, "incident closed_at") if closed_at else None,
            resolved=bool(value.get("resolved", False)),
            evidence=dict(value.get("evidence") or {}),
        )

    @classmethod
    def _deserialize_report(cls, value: Any) -> DailyReport:
        if not isinstance(value, dict):
            raise ValueError("daily report must be an object")
        generated_at = value.get("generated_at")
        total_recovery_count = value.get("total_recovery_count")
        if total_recovery_count is None:
            total_recovery_count = value.get("recoveries", 0)
        samples = [cls._deserialize_sli(item) for item in value.get("sli_samples", [])]
        report = DailyReport(
            date=str(value["date"]),
            window_id=str(value["window_id"]),
            sli_samples=samples,
            incidents_opened=int(value.get("incidents_opened", 0)),
            incidents_closed=int(value.get("incidents_closed", 0)),
            active_incidents=int(value.get("active_incidents", 0)),
            total_recovery_count=int(total_recovery_count),
            uptime_seconds=float(value.get("uptime_seconds", 0.0)),
            report_hash=str(value.get("report_hash", "")),
            generated_at=cls._parse_datetime(generated_at, "report generated_at")
            if generated_at
            else datetime.now(timezone.utc),
        )
        return report

    def _load_state_files(self) -> None:
        for path in sorted(self.evidence_dir.glob("*-state.json")):
            try:
                with path.open() as handle:
                    data = json.load(handle)
                window = self._deserialize_window(data)
            except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
                # Do not silently discard a potentially active/corrupt window;
                # retain a diagnostic and leave it unavailable for evaluation.
                self._state_load_errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
                continue
            if window.window_id in self._windows:
                self._state_load_errors.append(f"{path.name}: duplicate window_id {window.window_id}")
                continue
            self._windows[window.window_id] = window
            if window.status in {WindowStatus.CREATED, WindowStatus.RUNNING, WindowStatus.PAUSED} and (
                self._active_window is None or window.started_at > self._active_window.started_at
            ):
                self._active_window = window

    @classmethod
    def _deserialize_window(cls, data: Any) -> CertificationWindow:
        if not isinstance(data, dict):
            raise ValueError("window state must be an object")
        window_id = str(data["window_id"])
        status = WindowStatus(data["status"])
        started_at = cls._parse_datetime(data["started_at"], "window started_at")
        schema_version = int(data.get("state_schema_version", 1))
        if schema_version < STATE_SCHEMA_VERSION:
            # Preserve only the auditable summary.  Missing evidence is
            # explicit, so evaluate() remains NOT_VERIFIABLE after restart.
            return CertificationWindow(
                window_id=window_id,
                plan_version=str(data.get("plan_version", "legacy")),
                duration_days=int(data.get("duration_days", 30)),
                status=status,
                started_at=started_at,
                reset_count=int(data.get("reset_count", 0)),
                evidence_state_complete=False,
            )

        ended_at = data.get("ended_at")
        return CertificationWindow(
            window_id=window_id,
            plan_version=str(data["plan_version"]),
            duration_days=int(data["duration_days"]),
            status=status,
            started_at=started_at,
            ended_at=cls._parse_datetime(ended_at, "window ended_at") if ended_at else None,
            sli_samples=[cls._deserialize_sli(item) for item in data.get("sli_samples", [])],
            incidents=[cls._deserialize_incident(item) for item in data.get("incidents", [])],
            daily_reports=[cls._deserialize_report(item) for item in data.get("daily_reports", [])],
            reset_reason=str(data.get("reset_reason", "")),
            reset_count=int(data.get("reset_count", 0)),
            total_recovery_count=int(data.get("total_recovery_count", 0)),
            commit=str(data.get("commit", "")),
            g5_certificate_hash=str(data.get("g5_certificate_hash", "")),
            is_simulated=bool(data.get("is_simulated", False)),
            evidence_state_complete=True,
        )

    @staticmethod
    def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
        """Write evidence atomically so a crash cannot leave partial JSON."""
        temp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        try:
            with temp_path.open("w") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False, default=str)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        finally:
            with suppress(FileNotFoundError):
                temp_path.unlink()
