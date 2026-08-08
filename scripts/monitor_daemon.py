#!/usr/bin/env python3
"""北斗运营监控守护进程 — V1.1 MonitoringService 包装器。

替代 V1.0 版本，使用 beidou_observability/monitoring/ 模块。

用法:
  python scripts/monitor_daemon.py start      — 后台启动
  python scripts/monitor_daemon.py start-fg   — 前台启动（调试）
  python scripts/monitor_daemon.py stop       — 停止
  python scripts/monitor_daemon.py status     — 查看状态
  python scripts/monitor_daemon.py run-once   — 单次检查
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

STATE_FILE = PROJECT_ROOT / ".beidou" / "supervisor-state.json"
PID_FILE = PROJECT_ROOT / ".beidou" / "monitor_daemon.pid"
FREQ_FILE = PROJECT_ROOT / ".beidou" / "monitor_freq.json"
HEALTH_URL = os.environ.get("BEIDOU_HEALTH_URL", "http://localhost:9090/health")


def load_supervisor_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def run_checks_v11() -> dict:
    """使用 V1.1 MonitoringService 执行全维度检查。"""
    from beidou_observability.monitoring.contracts import (
        CheckSeverity,
        CheckStatus,
        MonitoringCheckResult,
    )
    from beidou_observability.monitoring.health_aggregator import health_summary
    from beidou_observability.monitoring.service import MonitoringService

    svc = MonitoringService()
    state = load_supervisor_state()

    results: list[MonitoringCheckResult] = []

    if state and state.get("checks"):
        for c in state["checks"]:
            with contextlib.suppress(Exception):
                results.append(
                    MonitoringCheckResult(
                        check_id=c.get("check_id", "unknown"),
                        status=CheckStatus(c.get("status", "UNKNOWN")),
                        severity=CheckSeverity(c.get("severity", "P2")),
                        message=c.get("message", ""),
                        evidence_hash=c.get("evidence_hash", ""),
                    )
                )

    summary = health_summary(results)
    freq = svc.repo.get_frequency_state()

    return {
        "health": summary["status"],
        "p0_fails": summary.get("p0_fails", 0),
        "total_checks": len(results),
        "frequency": freq.level.value,
        "interval_s": freq.interval_seconds,
        "open_incidents": len(svc.get_incidents(active_only=True)),
        "checks": [
            {"check_id": r.check_id, "status": r.status.value, "severity": r.severity.value, "message": r.message}
            for r in results
        ],
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }


def write_pid() -> None:
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))


def cleanup_pid() -> None:
    with contextlib.suppress(Exception):
        PID_FILE.unlink()


def signal_handler(signum, frame):
    cleanup_pid()
    sys.exit(0)


def run_foreground() -> None:
    """前台运行 — 每 30s 检查，自适应频率。"""
    write_pid()
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    try:
        while True:
            try:
                import urllib.request

                resp = urllib.request.urlopen(HEALTH_URL, timeout=5)
                json.loads(resp.read())
            except Exception:
                pass

            with contextlib.suppress(Exception):
                run_checks_v11()

            time.sleep(30)
    finally:
        cleanup_pid()


def run_once() -> None:
    """单次检查。"""
    try:
        import urllib.request

        resp = urllib.request.urlopen(HEALTH_URL, timeout=5)
        json.loads(resp.read())
    except Exception:
        pass

    result = run_checks_v11()
    for c in result["checks"]:
        "✅" if c["status"] == "PASS" else "⚠️" if c["status"] == "WARN" else "❌"


def stop_daemon() -> bool:
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        PID_FILE.unlink()
        return False
    except Exception:
        return False


def status_daemon() -> bool:
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError):
        PID_FILE.unlink()
        return False


# CLI
if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "start":
        if status_daemon():
            sys.exit(1)
        # Fork to background
        if os.fork() > 0:
            sys.exit(0)
        os.setsid()
        run_foreground()

    elif cmd == "start-fg":
        run_foreground()

    elif cmd == "stop":
        stop_daemon()

    elif cmd == "status":
        status_daemon()

    elif cmd == "run-once":
        run_once()

    else:
        sys.exit(1)
