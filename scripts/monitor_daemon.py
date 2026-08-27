#!/usr/bin/env python3
"""北斗运营监控守护进程 — V1.2 本地检查持久化。

替代 V1.1 版本：
- 持久化检查结果到 MonitoringRepository (SQLite)
- 交叉验证健康端点与监督器状态
- 写入本地状态文件 (JSONL)

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
import logging
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
DAEMON_LOG = PROJECT_ROOT / ".beidou" / "monitor_daemon.jsonl"
HEALTH_URL = os.environ.get("BEIDOU_HEALTH_URL", "http://localhost:9090/health")
logger = logging.getLogger("beidou.monitor_daemon")


def load_supervisor_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("supervisor state could not be read: %s", type(exc).__name__)
        return None


def persist_result(result: dict) -> None:
    """追加检查结果到 JSONL 日志文件（持久化）。"""
    DAEMON_LOG.parent.mkdir(parents=True, exist_ok=True)
    try:
        with DAEMON_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except Exception as exc:
        logger.error("monitor result persistence failed: %s", type(exc).__name__)


def cross_validate_health(health_data: dict | None, state: dict | None) -> dict[str, str]:
    """交叉验证健康端点与监督器状态。"""
    issues: dict[str, str] = {}
    if health_data is None and state is None:
        issues["cross_validate"] = "BOTH_UNAVAILABLE"
    elif health_data is None:
        issues["cross_validate"] = "HEALTH_ENDPOINT_DOWN"
    elif state is None:
        issues["cross_validate"] = "SUPERVISOR_STATE_MISSING"
    else:
        h_ready = health_data.get("ready", False)
        s_state = state.get("supervisor_state", "UNKNOWN")
        if h_ready and s_state not in ("RUNNING",):
            issues["cross_validate"] = f"HEALTH_READY_BUT_SUPERVISOR_{s_state}"
        elif not h_ready and s_state == "RUNNING":
            issues["cross_validate"] = "SUPERVISOR_RUNNING_BUT_HEALTH_NOT_READY"
    return issues


def run_checks_v12() -> dict:
    """使用 V1.2 MonitoringService 执行全维度检查并持久化。"""
    from beidou_observability.monitoring.contracts import (
        CheckSeverity,
        CheckStatus,
        MonitoringCheckResult,
    )
    from beidou_observability.monitoring.health_aggregator import health_summary
    from beidou_observability.monitoring.service import MonitoringService

    svc = MonitoringService()
    state = load_supervisor_state()

    # 交叉验证: 获取健康端点响应并与监督器状态比对
    health_data: dict | None = None
    try:
        import urllib.request

        resp = urllib.request.urlopen(HEALTH_URL, timeout=5)
        health_data = json.loads(resp.read())
    except Exception as exc:
        logger.warning("health endpoint unavailable: %s", type(exc).__name__)

    cross_issues = cross_validate_health(health_data, state)

    results: list[MonitoringCheckResult] = []

    if state and state.get("checks"):
        for c in state["checks"]:
            try:
                results.append(
                    MonitoringCheckResult(
                        check_id=c.get("check_id", "unknown"),
                        status=CheckStatus(c.get("status", "UNKNOWN")),
                        severity=CheckSeverity(c.get("severity", "P2")),
                        message=c.get("message", ""),
                        evidence_hash=c.get("evidence_hash", ""),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("invalid supervisor check evidence: %s", type(exc).__name__)
                results.append(
                    MonitoringCheckResult(
                        check_id="monitor_daemon.state_parse",
                        status=CheckStatus.FAIL,
                        severity=CheckSeverity.P1,
                        message=f"INVALID_SUPERVISOR_CHECK:{type(exc).__name__}",
                    )
                )

    # 交叉验证失败作为额外检查
    if cross_issues:
        for key, msg in cross_issues.items():
            results.append(
                MonitoringCheckResult(
                    check_id="monitor_daemon.cross_validate",
                    status=CheckStatus.FAIL,
                    severity=CheckSeverity.P1,
                    message=f"{key}: {msg}",
                )
            )

    summary = health_summary(results)
    freq = svc.repo.get_frequency_state()

    result = {
        "health": summary["status"],
        "p0_fails": summary.get("p0_fails", 0),
        "total_checks": len(results),
        "frequency": freq.level.value,
        "interval_s": freq.interval_seconds,
        "cross_validation": cross_issues,
        "checks": [
            {"check_id": r.check_id, "status": r.status.value, "severity": r.severity.value, "message": r.message}
            for r in results
        ],
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }

    # V1.2: 持久化本地监控事实。
    persist_result(result)

    # 持久化到 MonitoringRepository
    with contextlib.suppress(Exception):
        svc.repo.save_check_results(results)

    return result


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
    """前台运行 — 每 30s 检查并持久化。"""
    write_pid()
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    print(f"[monitor_daemon] PID={os.getpid()} 已启动, 轮询间隔=30s, 日志={DAEMON_LOG}")
    try:
        while True:
            result = run_checks_v12()
            p0 = result["p0_fails"]
            health = result["health"]
            print(
                f"[monitor_daemon] {datetime.now(timezone.utc).strftime('%H:%M:%S')} "
                f"health={health} p0_fails={p0} checks={result['total_checks']}"
            )
            if result.get("cross_validation"):
                print(f"[monitor_daemon] ⚠️ 交叉验证失败: {result['cross_validation']}")
            time.sleep(30)
    finally:
        cleanup_pid()
        print("[monitor_daemon] 已停止")


def run_once() -> None:
    """单次检查 — 输出到 stdout + 持久化。"""
    result = run_checks_v12()
    print(f"Health: {result['health']} | P0 Fails: {result['p0_fails']} | Checks: {result['total_checks']}")
    if result.get("cross_validation"):
        print(f"Cross-validation: {result['cross_validation']}")
    for c in result["checks"]:
        emoji = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌", "UNKNOWN": "❔"}.get(c["status"], "❔")
        print(f"  {emoji} [{c['severity']}] {c['check_id']}: {c['message']}")


def stop_daemon() -> bool:
    if not PID_FILE.exists():
        print("守护进程未运行")
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        print(f"已向 PID={pid} 发送 SIGTERM")
        return True
    except ProcessLookupError:
        PID_FILE.unlink()
        print("守护进程已退出，清理 PID 文件")
        return False
    except Exception as exc:
        print(f"停止失败: {exc}")
        return False


def status_daemon() -> bool:
    if not PID_FILE.exists():
        print("守护进程未运行")
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        print(f"守护进程运行中: PID={pid}")
        return True
    except (ProcessLookupError, OSError):
        PID_FILE.unlink()
        print("守护进程已退出（陈旧 PID 文件已清理）")
        return False


# CLI
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: monitor_daemon.py {start|start-fg|stop|status|run-once}")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "start":
        if status_daemon():
            sys.exit(1)
        if os.fork() > 0:
            sys.exit(0)
        os.setsid()
        run_foreground()

    elif cmd == "start-fg":
        run_foreground()

    elif cmd == "stop":
        sys.exit(0 if stop_daemon() else 1)

    elif cmd == "status":
        sys.exit(0 if status_daemon() else 1)

    elif cmd == "run-once":
        run_once()

    else:
        print(f"未知命令: {cmd}")
        sys.exit(1)
