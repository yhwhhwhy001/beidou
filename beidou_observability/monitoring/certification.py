"""PKG-MON-11: 72h 认证运行器 — 真实经过时间、不可模拟 (INV-012)。"""
from __future__ import annotations
import hashlib, json, os, time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CERTIFICATION_HOURS = 72
CERTIFICATION_SECONDS = CERTIFICATION_HOURS * 3600

@dataclass(slots=True)
class CertificationState:
    """72h 认证状态 — 不可伪造时间 (INV-012)。"""
    certification_id: str
    started_at: float = field(default_factory=time.time)
    evidence_dir: Path = field(default_factory=lambda: Path("evidence/certification"))
    state: str = "RUNNING"  # RUNNING | PASS | FAIL | CONDITIONAL
    p0_incidents: int = 0
    p1_incidents: int = 0
    checks_completed: int = 0
    restarts: int = 0
    last_evidence_at: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def elapsed_hours(self) -> float:
        return (time.time() - self.started_at) / 3600.0

    @property
    def elapsed_seconds(self) -> float:
        return time.time() - self.started_at

    @property
    def is_complete(self) -> bool:
        return self.elapsed_seconds >= CERTIFICATION_SECONDS

    @property
    def status(self) -> str:
        if self.p0_incidents > 0:
            return "FAIL"
        if self.elapsed_seconds < CERTIFICATION_SECONDS:
            return "NOT_VERIFIED"
        if self.p1_incidents > 0:
            return "CONDITIONAL"
        return "PASS"

    def record_incident(self, severity: str, detail: str) -> None:
        if severity == "P0":
            self.p0_incidents += 1
            self.notes.append(f"P0 at {self.elapsed_hours:.1f}h: {detail}")
        elif severity == "P1":
            self.p1_incidents += 1
            self.notes.append(f"P1 at {self.elapsed_hours:.1f}h: {detail}")

    def record_restart(self, reason: str = "") -> None:
        self.restarts += 1
        self.notes.append(f"RESTART at {self.elapsed_hours:.1f}h: {reason}")

    def heartbeat(self) -> None:
        self.last_evidence_at = time.time()
        self.checks_completed += 1

    def to_manifest(self) -> dict[str, Any]:
        return {
            "certification_id": self.certification_id,
            "started_at_utc": datetime.fromtimestamp(self.started_at, tz=timezone.utc).isoformat(),
            "elapsed_hours": round(self.elapsed_hours, 2),
            "required_hours": CERTIFICATION_HOURS,
            "state": self.state,
            "status": self.status,
            "p0_incidents": self.p0_incidents,
            "p1_incidents": self.p1_incidents,
            "restarts": self.restarts,
            "checks_completed": self.checks_completed,
            "notes": self.notes,
            "inv012_compliant": True,
        }

    def save_manifest(self) -> Path:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        manifest = self.to_manifest()
        manifest_path = self.evidence_dir / "certification_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
        manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        (self.evidence_dir / "manifest.sha256").write_text(f"{manifest_hash}  certification_manifest.json\n")
        return manifest_path

    def finalize(self) -> dict[str, Any]:
        self.state = self.status if self.is_complete else "NOT_VERIFIED"
        path = self.save_manifest()
        return {"manifest": str(path), "status": self.status, "elapsed_hours": round(self.elapsed_hours, 2)}


def create_certification(cert_id: str | None = None) -> CertificationState:
    """创建 72h 认证实例。"""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return CertificationState(
        certification_id=cert_id or f"cert-{ts}",
        started_at=time.time(),
    )


def load_certification(manifest_path: Path) -> CertificationState | None:
    """从持久化 manifest 恢复认证状态。"""
    if not manifest_path.exists():
        return None
    try:
        data = json.loads(manifest_path.read_text())
        state = CertificationState(certification_id=data["certification_id"])
        state.p0_incidents = data.get("p0_incidents", 0)
        state.p1_incidents = data.get("p1_incidents", 0)
        state.checks_completed = data.get("checks_completed", 0)
        state.restarts = data.get("restarts", 0)
        state.notes = data.get("notes", [])
        state.state = data.get("state", "RUNNING")
        return state
    except Exception:
        return None
