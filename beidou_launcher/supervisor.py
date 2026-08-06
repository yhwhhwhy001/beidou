"""Foreground supervisor for the Beidou Autopilot process."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .checks import PreflightChecker, find_project_root
from .manifest import EXPECTED_FACTOR_COUNT, HEALTH_PORT
from .models import CheckReport, CheckResult, CheckStatus


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    """Evidence captured from the child process at one point in time."""

    captured_at: float
    ready: dict[str, Any]
    status: dict[str, Any]
    metrics: dict[str, float]

    @property
    def tick_count(self) -> float:
        return self.metrics.get("beidou_tick_count", -1.0)

    @property
    def error_count(self) -> float:
        return self.metrics.get("beidou_error_count", -1.0)

    @property
    def active_factors(self) -> float:
        return self.metrics.get("beidou_active_factors", -1.0)


def _critical_incidents(incidents: Any) -> list[dict[str, Any]]:
    if not isinstance(incidents, list):
        return []
    critical: list[dict[str, Any]] = []
    for incident in incidents:
        if not isinstance(incident, dict):
            continue
        severity = str(incident.get("severity", incident.get("level", ""))).upper()
        if severity in {"CRITICAL", "LOCKDOWN", "P0"}:
            critical.append(incident)
    return critical


def evaluate_runtime_snapshots(
    first: RuntimeSnapshot,
    second: RuntimeSnapshot,
    expected_mode: str,
) -> CheckReport:
    """Evaluate runtime truth using two time-separated snapshots."""

    report = CheckReport(phase="runtime-verification", mode=expected_mode)
    lifecycle = str(second.status.get("lifecycle_state", "UNKNOWN"))
    actual_mode = str(second.status.get("mode", "UNKNOWN"))
    control_action = str(second.status.get("control_action", "UNKNOWN"))
    ready = bool(second.ready.get("ready", False))
    critical = _critical_incidents(second.status.get("active_incidents", []))

    report.results.extend(
        [
            CheckResult(
                code="RT-PROCESS-READY",
                subject="运行就绪",
                status=CheckStatus.PASS if ready else CheckStatus.FAIL,
                message="/ready 返回 ready=true" if ready else "/ready 未通过",
                evidence={"ready": second.ready},
            ),
            CheckResult(
                code="RT-LIFECYCLE",
                subject="生命周期",
                status=CheckStatus.PASS if lifecycle == "ACTIVE" else CheckStatus.FAIL,
                message=f"生命周期状态 {lifecycle}",
                evidence={"lifecycle_state": lifecycle},
            ),
            CheckResult(
                code="RT-MODE",
                subject="运行模式一致性",
                status=CheckStatus.PASS if actual_mode == expected_mode else CheckStatus.FAIL,
                message=f"请求 {expected_mode}, 实际 {actual_mode}",
                evidence={"expected": expected_mode, "actual": actual_mode},
            ),
            CheckResult(
                code="RT-TICK-PROGRESS",
                subject="实时时钟心跳",
                status=(
                    CheckStatus.PASS
                    if first.tick_count >= 0 and second.tick_count > first.tick_count
                    else CheckStatus.FAIL
                ),
                message=f"tick_count {first.tick_count:g} → {second.tick_count:g}",
                evidence={"first": first.tick_count, "second": second.tick_count},
            ),
            CheckResult(
                code="RT-ERROR-DELTA",
                subject="异常计数",
                status=(
                    CheckStatus.PASS
                    if first.error_count >= 0 and second.error_count <= first.error_count
                    else CheckStatus.FAIL
                ),
                message=f"error_count {first.error_count:g} → {second.error_count:g}",
                evidence={"first": first.error_count, "second": second.error_count},
            ),
            CheckResult(
                code="RT-FACTORS",
                subject="运行中因子",
                status=(
                    CheckStatus.PASS if second.active_factors >= EXPECTED_FACTOR_COUNT else CheckStatus.FAIL
                ),
                message=f"active_factors={second.active_factors:g}",
                evidence={"expected_minimum": EXPECTED_FACTOR_COUNT},
            ),
            CheckResult(
                code="RT-INCIDENTS",
                subject="活动事故",
                status=CheckStatus.PASS if not critical else CheckStatus.FAIL,
                message="无 P0/CRITICAL 活动事故" if not critical else f"发现 {len(critical)} 个关键事故",
                evidence={"critical_incidents": critical},
            ),
        ]
    )

    expected_control = "RESUME" if expected_mode in {"paper", "testnet"} else "NO_NEW_RISK"
    report.results.append(
        CheckResult(
            code="RT-CONTROL",
            subject="控制面",
            status=CheckStatus.PASS if control_action == expected_control else CheckStatus.FAIL,
            message=f"控制状态 {control_action}, 期望 {expected_control}",
            evidence={"expected": expected_control, "actual": control_action},
        )
    )
    return report.finish()


def parse_prometheus_metrics(text: str) -> dict[str, float]:
    """Parse the numeric subset emitted by the built-in metrics endpoint."""

    metrics: dict[str, float] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "{" in line:
            continue
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            metrics[parts[0]] = float(parts[1])
        except ValueError:
            continue
    return metrics


class SingleInstanceLock:
    """Atomic supervisor ownership record with stale-process detection."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def read(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def acquire(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existing = self.read()
        existing_pid = int(existing.get("supervisor_pid", 0) or 0)
        if existing and self._pid_alive(existing_pid):
            raise RuntimeError(f"Beidou supervisor is already running (pid={existing_pid})")
        if self.path.exists():
            self.path.unlink()
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
        except Exception:
            self.path.unlink(missing_ok=True)
            raise

    def update(self, data: dict[str, Any]) -> None:
        temp_path = self.path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_path, self.path)

    def release(self) -> None:
        self.path.unlink(missing_ok=True)


class BeidouSupervisor:
    """Start, verify and continuously supervise the existing Autopilot entrypoint."""

    def __init__(
        self,
        mode: str,
        symbols: list[str],
        startup_timeout: int = 180,
        poll_interval: int = 10,
        self_heal: bool = True,
        max_restarts: int = 2,
    ) -> None:
        self.mode = mode
        self.symbols = symbols
        self.startup_timeout = startup_timeout
        self.poll_interval = poll_interval
        self.self_heal = self_heal
        self.max_restarts = max_restarts
        self.root = find_project_root()
        self.runtime_dir = self.root / ".beidou"
        self.evidence_dir = self.root / "evidence" / "BD-STARTUP"
        self.log_path = self.root / "logs" / "beidou-autopilot.log"
        self.lock = SingleInstanceLock(self.runtime_dir / "supervisor.json")
        self.child: subprocess.Popen[bytes] | None = None
        self._stop_requested = False
        self._restart_count = 0
        self._last_snapshot: RuntimeSnapshot | None = None
        self._consecutive_runtime_failures = 0

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{HEALTH_PORT}"

    def run(self) -> int:
        preflight = PreflightChecker(self.mode, self.symbols).run()
        self._persist_report(preflight)
        self._print_report(preflight)
        if not preflight.passed:
            print("[beidou] 启动已阻断：启动前自检存在关键失败。")
            return 2

        identity = {
            "supervisor_pid": os.getpid(),
            "child_pid": 0,
            "mode": self.mode,
            "symbols": self.symbols,
            "endpoint": self.endpoint,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "log_path": str(self.log_path),
        }
        try:
            self.lock.acquire(identity)
        except RuntimeError as exc:
            print(f"[beidou] {exc}")
            return 3

        self._install_signal_handlers()
        try:
            if not self._start_and_verify(identity):
                return 4
            return self._monitor(identity)
        finally:
            self._stop_child()
            self.lock.release()

    def _start_and_verify(self, identity: dict[str, Any]) -> bool:
        self._spawn_child()
        assert self.child is not None
        identity["child_pid"] = self.child.pid
        identity["restart_count"] = self._restart_count
        self.lock.update(identity)

        first = self._wait_for_snapshot()
        if first is None:
            report = CheckReport(phase="runtime-verification", mode=self.mode)
            report.results.append(
                CheckResult(
                    code="RT-STARTUP-TIMEOUT",
                    subject="Autopilot 启动",
                    status=CheckStatus.FAIL,
                    message=f"{self.startup_timeout}s 内未获得可解析健康端点",
                    evidence={"log_path": str(self.log_path)},
                )
            )
            report.finish()
            self._persist_report(report)
            self._print_report(report)
            self._stop_child()
            return False

        time.sleep(6)
        second = self._collect_snapshot()
        if second is None:
            report = CheckReport(phase="runtime-verification", mode=self.mode)
            report.results.append(
                CheckResult(
                    code="RT-ENDPOINT-LOST",
                    subject="Autopilot 健康端点",
                    status=CheckStatus.FAIL,
                    message="首次响应后健康端点丢失",
                )
            )
            report.finish()
        else:
            report = evaluate_runtime_snapshots(first, second, self.mode)
            self._last_snapshot = second

        self._persist_report(report)
        self._print_report(report)
        if not report.passed:
            self._stop_child()
            return False

        print(
            f"[beidou] ✅ 北斗已通过双阶段自检并进入持续巡检。"
            f" mode={self.mode} pid={self.child.pid} endpoint={self.endpoint}"
        )
        print(f"[beidou] 日志: {self.log_path}")
        return True

    def _spawn_child(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-m",
            "apps.autopilot",
            "--mode",
            self.mode,
            "--symbols",
            ",".join(self.symbols),
            "--port",
            str(HEALTH_PORT),
        ]
        with self.log_path.open("ab", buffering=0) as log_handle:
            self.child = subprocess.Popen(
                command,
                cwd=self.root,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

    def _wait_for_snapshot(self) -> RuntimeSnapshot | None:
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline and not self._stop_requested:
            if self.child is None or self.child.poll() is not None:
                return None
            snapshot = self._collect_snapshot()
            if snapshot is not None:
                return snapshot
            time.sleep(1)
        return None

    def _monitor(self, identity: dict[str, Any]) -> int:
        while not self._stop_requested:
            time.sleep(self.poll_interval)
            if self.child is None or self.child.poll() is not None:
                if self._maybe_restart(identity, reason="child process exited"):
                    continue
                print("[beidou] ❌ Autopilot 进程退出，Supervisor 停止。")
                return 5

            current = self._collect_snapshot()
            if current is None or self._last_snapshot is None:
                self._consecutive_runtime_failures += 1
                reason = "health endpoint unavailable"
            else:
                report = evaluate_runtime_snapshots(self._last_snapshot, current, self.mode)
                self._persist_report(report, suffix="runtime")
                failures = [
                    result
                    for result in report.results
                    if result.critical and result.status == CheckStatus.FAIL
                ]
                if failures:
                    self._consecutive_runtime_failures += 1
                    reason = "; ".join(f"{item.code}:{item.message}" for item in failures)
                else:
                    self._consecutive_runtime_failures = 0
                    reason = ""
                self._last_snapshot = current

            if self._consecutive_runtime_failures >= 3:
                print(f"[beidou] ❌ 连续运行异常: {reason}")
                if self._maybe_restart(identity, reason=reason):
                    continue
                return 6

        print("[beidou] 收到停止信号，执行安全关闭。")
        return 0

    def _maybe_restart(self, identity: dict[str, Any], reason: str) -> bool:
        if self.mode == "testnet":
            print("[beidou] Testnet 异常采用 fail-closed：停止进程，不自动恢复风险增加路径。")
            self._stop_child()
            return False
        if not self.self_heal or self._restart_count >= self.max_restarts:
            self._stop_child()
            return False

        self._restart_count += 1
        print(f"[beidou] 尝试自愈重启 {self._restart_count}/{self.max_restarts}: {reason}")
        self._stop_child()
        self._consecutive_runtime_failures = 0
        self._last_snapshot = None
        time.sleep(min(5 * self._restart_count, 15))
        return self._start_and_verify(identity)

    def _collect_snapshot(self) -> RuntimeSnapshot | None:
        try:
            ready = self._get_json("/ready")
            status = self._get_json("/status")
            metrics_text = self._get_text("/metrics")
        except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            return None
        return RuntimeSnapshot(
            captured_at=time.time(),
            ready=ready,
            status=status,
            metrics=parse_prometheus_metrics(metrics_text),
        )

    def _get_json(self, path: str) -> dict[str, Any]:
        request = urllib.request.Request(f"{self.endpoint}{path}", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=3) as response:  # noqa: S310 - localhost only
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            return json.loads(body)

    def _get_text(self, path: str) -> str:
        request = urllib.request.Request(f"{self.endpoint}{path}", method="GET")
        with urllib.request.urlopen(request, timeout=3) as response:  # noqa: S310 - localhost only
            return response.read().decode("utf-8")

    def _stop_child(self) -> None:
        child = self.child
        if child is None or child.poll() is not None:
            self.child = None
            return
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            self.child = None
            return
        try:
            child.wait(timeout=20)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=5)
        self.child = None

    def _install_signal_handlers(self) -> None:
        def _request_stop(signum: int, _frame: Any) -> None:
            del signum
            self._stop_requested = True

        signal.signal(signal.SIGINT, _request_stop)
        signal.signal(signal.SIGTERM, _request_stop)

    def _persist_report(self, report: CheckReport, suffix: str = "") -> None:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        filename = f"{timestamp}-{report.phase}{('-' + suffix) if suffix else ''}.json"
        path = self.evidence_dir / filename
        path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _print_report(report: CheckReport) -> None:
        print(f"[beidou] === {report.phase} ===")
        for result in report.results:
            symbol = {CheckStatus.PASS: "✅", CheckStatus.WARN: "⚠️", CheckStatus.FAIL: "❌"}[result.status]
            print(f"[beidou] {symbol} {result.code} {result.subject}: {result.message}")
        print(
            f"[beidou] result={'PASS' if report.passed else 'FAIL'} "
            f"failures={report.failure_count} warnings={report.warning_count}"
        )

    @classmethod
    def status(cls) -> int:
        root = find_project_root()
        lock = SingleInstanceLock(root / ".beidou" / "supervisor.json")
        data = lock.read()
        if not data:
            print("[beidou] 未发现运行中的 Supervisor。")
            return 1
        supervisor_pid = int(data.get("supervisor_pid", 0) or 0)
        child_pid = int(data.get("child_pid", 0) or 0)
        status = {
            **data,
            "supervisor_alive": lock._pid_alive(supervisor_pid),
            "child_alive": lock._pid_alive(child_pid),
        }
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0

    @classmethod
    def stop(cls) -> int:
        root = find_project_root()
        lock = SingleInstanceLock(root / ".beidou" / "supervisor.json")
        data = lock.read()
        if not data:
            print("[beidou] 未发现运行中的 Supervisor。")
            return 1
        supervisor_pid = int(data.get("supervisor_pid", 0) or 0)
        if not lock._pid_alive(supervisor_pid):
            lock.release()
            print("[beidou] 清理了失效运行锁。")
            return 0
        os.kill(supervisor_pid, signal.SIGTERM)
        print(f"[beidou] 已向 Supervisor pid={supervisor_pid} 发送安全停止信号。")
        return 0
