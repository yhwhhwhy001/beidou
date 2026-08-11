"""单实例锁、状态快照与启动监督证据。"""

from __future__ import annotations

import json
import logging
import os
import shlex
import signal
import subprocess
import time
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import StartupReport

logger = logging.getLogger(__name__)


class InstanceLock:
    """基于 PID 文件的单实例锁；自动清理陈旧 PID。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.acquired = False

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """Check the PID exists AND belongs to a beidou process.

        PID reuse after crash/kill can assign a dead instance's PID to an
        unrelated process.  Verifying the command name prevents a false
        "already running" block.
        """
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        # Verify the process is actually a beidou launcher, not a reused PID.
        try:
            import subprocess

            cmdline = subprocess.run(
                ["ps", "-p", str(pid), "-o", "comm="],
                capture_output=True,
                text=True,
                timeout=3,
            ).stdout.strip()
            return "beidou" in cmdline.lower() or "python" in cmdline.lower()
        except Exception:
            # On any error (missing ps, timeout, etc.) fall back to the
            # signal check alone — fail closed rather than open.
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

    def write_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """追加结构化事件到证据目录（监督事件日志）。

        用于监控阻断转变、持仓模式变更等监督级事件。
        """
        event: dict[str, Any] = {
            "type": event_type,
            "pid": os.getpid(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **payload,
        }
        path = self.evidence_dir / "supervisor-events.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def write(self, report: StartupReport) -> None:
        report.updated_at = datetime.now(timezone.utc).isoformat()
        payload = report.to_dict()
        payload["pid"] = os.getpid()
        temp_path = self.state_path.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, self.state_path)

        fingerprint_payload = {
            "phase": payload["phase"],
            "supervisor_state": payload["supervisor_state"],
            "trading_ready": payload["trading_ready"],
            "checks": [(item["check_id"], item["status"], item["message"]) for item in payload["checks"]],
        }
        fingerprint = json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True)
        now = time.monotonic()
        if fingerprint != self._last_history_fingerprint or now - self._last_history_write >= 60.0:
            # BD-FIX (P3): 文件超过 10MB 时轮转，仅保留最近 500 条记录
            max_size = 10 * 1024 * 1024
            max_lines = 500
            try:
                if self.history_path.exists() and self.history_path.stat().st_size > max_size:
                    lines = self.history_path.read_text().strip().splitlines()
                    trimmed = lines[-max_lines:]
                    with self.history_path.open("w", encoding="utf-8") as fh:
                        fh.write("\n".join(trimmed) + "\n")
            except (OSError, UnicodeError) as exc:
                logger.warning("supervisor history rotation failed: %s: %s", type(exc).__name__, exc)
            with self.history_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._last_history_fingerprint = fingerprint
            self._last_history_write = now


def read_status(project_root: Path) -> dict[str, Any] | None:
    path = project_root / ".beidou" / "supervisor-state.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except (OSError, json.JSONDecodeError):
        return None


def inspect_runtime_status(project_root: Path) -> dict[str, Any] | None:
    """返回带进程存活性和证据新鲜度的状态，避免展示陈旧 RUNNING。"""
    status = read_status(project_root)
    if status is None:
        return None
    try:
        pid = int(status.get("pid", -1))
    except (TypeError, ValueError):
        pid = -1
    process_alive = InstanceLock._pid_alive(pid)
    try:
        updated_at = datetime.fromisoformat(str(status.get("updated_at", "")))
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        evidence_age = (datetime.now(timezone.utc) - updated_at.astimezone(timezone.utc)).total_seconds()
    except (TypeError, ValueError):
        evidence_age = float("inf")
    fresh = -5 <= evidence_age <= 120
    status["process_alive"] = process_alive
    status["evidence_age_seconds"] = round(evidence_age, 3) if evidence_age != float("inf") else None
    status["effective_state"] = status.get("supervisor_state", "UNKNOWN") if process_alive and fresh else "STALE"
    return status


def stop_running_instance(project_root: Path) -> tuple[bool, str]:
    """只停止具备新鲜监督证据且命令身份匹配的北斗进程。"""
    pid_path = project_root / ".beidou" / "beidou.pid"
    if not pid_path.exists():
        return False, "未发现运行中的北斗实例"
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as exc:
        return False, f"PID 文件无效: {exc}"

    status = read_status(project_root)
    try:
        status_pid = int(status.get("pid", -1)) if status is not None else -1
    except (TypeError, ValueError):
        status_pid = -1
    if status is None or status_pid != pid:
        return False, "拒绝停止：PID 与监督器状态证据不一致"

    try:
        updated_at = datetime.fromisoformat(str(status.get("updated_at", "")))
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - updated_at.astimezone(timezone.utc)).total_seconds()
    except (TypeError, ValueError):
        return False, "拒绝停止：监督器状态时间无效"
    if age_seconds < -5 or age_seconds > 120:
        return False, f"拒绝停止：监督器状态已过期 ({age_seconds:.1f}s)"

    try:
        process = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        command = process.stdout.strip().lower()
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"拒绝停止：无法验证进程身份: {exc}"
    if process.returncode != 0 or not command:
        return False, f"拒绝停止：PID={pid} 不存在或不可查询"
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    basenames = {Path(token).name.lower() for token in tokens}
    module_identity = any("beidou_launcher" in token.lower() for token in tokens)
    script_identity = bool({"beidou", "bd", "北斗"} & basenames)
    if not module_identity and not script_identity:
        return False, f"拒绝停止：PID={pid} 不是北斗进程"

    try:
        os.kill(pid, signal.SIGTERM)
        return True, f"已向已验证的北斗进程 PID={pid} 发送 SIGTERM"
    except OSError as exc:
        return False, f"停止失败: {exc}"


def force_stop_existing(project_root: Path) -> tuple[bool, str]:
    """Compatibility alias for the governed, identity-checked stop path.

    The old implementation treated a PID file as authority and could signal
    an unrelated process (including SIGKILL) after a stale or reused PID.
    Startup must never trade identity safety for convenience, so callers now
    receive the same fresh-state and command-identity checks as ``stop``.
    """

    return stop_running_instance(project_root)
