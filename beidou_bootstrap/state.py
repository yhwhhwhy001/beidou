"""单实例锁、状态快照与启动监督证据。"""

from __future__ import annotations

import json
import os
import signal
import time
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import StartupReport


class InstanceLock:
    """基于 PID 文件的单实例锁；自动清理陈旧 PID。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.acquired = False

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def acquire(self) -> tuple[bool, str]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                existing = int(self.path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                existing = -1
            if self._pid_alive(existing):
                return False, f"北斗实例已运行，PID={existing}"
            with suppress(OSError):
                self.path.unlink()
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
            self.acquired = True
            return True, f"PID={os.getpid()}"
        except FileExistsError:
            return False, "实例锁竞争失败"

    def release(self) -> None:
        if self.acquired:
            with suppress(OSError):
                self.path.unlink()
            self.acquired = False


class EvidenceWriter:
    """原子写当前状态，并按状态变化/60 秒节流追加历史证据。"""

    def __init__(self, project_root: Path) -> None:
        self.runtime_dir = project_root / ".beidou"
        self.evidence_dir = project_root / "evidence" / "bootstrap"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.runtime_dir / "supervisor-state.json"
        self.history_path = self.evidence_dir / "supervisor-history.jsonl"
        self._last_history_fingerprint = ""
        self._last_history_write = 0.0

    def write(self, report: StartupReport) -> None:
        report.updated_at = datetime.now(timezone.utc).isoformat()
        payload = report.to_dict()
        payload["pid"] = os.getpid()
        temp_path = self.state_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self.state_path)

        fingerprint_payload = {
            "phase": payload["phase"],
            "supervisor_state": payload["supervisor_state"],
            "trading_ready": payload["trading_ready"],
            "checks": [(item["check_id"], item["status"], item["message"]) for item in payload["checks"]],
        }
        fingerprint = json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True)
        now = time.monotonic()
        if fingerprint != self._last_history_fingerprint or now - self._last_history_write >= 60.0:
            with self.history_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
            self._last_history_fingerprint = fingerprint
            self._last_history_write = now


def read_status(project_root: Path) -> dict[str, Any] | None:
    path = project_root / ".beidou" / "supervisor-state.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def stop_running_instance(project_root: Path) -> tuple[bool, str]:
    pid_path = project_root / ".beidou" / "beidou.pid"
    if not pid_path.exists():
        return False, "未发现运行中的北斗实例"
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
        os.kill(pid, signal.SIGTERM)
        return True, f"已向 PID={pid} 发送 SIGTERM"
    except (OSError, ValueError) as exc:
        return False, f"停止失败: {exc}"
