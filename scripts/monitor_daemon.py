#!/usr/bin/env python3
"""北斗运营监控守护进程 — MonitoringService 持久后台运行。

特性:
- 自适应频率: ALERT(10min) → NORMAL(30min) → STABLE(60min)
- 全维度检查: order_flow, protection_coverage, module_health, strategy_lifecycle
- 事故管理: IncidentManager 去重 + 风暴检测
- 证据持久化: .beidou/monitor_evidence.jsonl
- 频率状态: .beidou/monitor_freq.json
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = PROJECT_ROOT / ".beidou" / "supervisor-state.json"
EVIDENCE_FILE = PROJECT_ROOT / ".beidou" / "monitor_evidence.jsonl"
FREQ_FILE = PROJECT_ROOT / ".beidou" / "monitor_freq.json"
PID_FILE = PROJECT_ROOT / ".beidou" / "monitor_daemon.pid"

# 频率配置 (与旧版 ops_monitor 兼容)
FREQ_CONFIG = {
    "ALERT":   {"minutes": 10, "label": "10min (警觉模式)"},
    "NORMAL":  {"minutes": 30, "label": "30min (正常模式)"},
    "STABLE":  {"minutes": 60, "label": "60min (稳定模式)"},
}
UPGRADE_THRESHOLD = 3


def load_supervisor_state() -> dict[str, Any] | None:
    if not STATE_FILE.exists():
        return None
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def load_freq_state() -> dict:
    if FREQ_FILE.exists():
        try:
            with open(FREQ_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"level": "ALERT", "clean_streak": 0, "last_level_change": "", "total_checks": 0, "last_check_ts": None}


def save_freq_state(state: dict) -> None:
    FREQ_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(FREQ_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def log_evidence(entry: dict) -> None:
    EVIDENCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry["logged_at"] = datetime.now(timezone.utc).isoformat()
    with open(EVIDENCE_FILE, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def update_frequency(current: dict, all_ok: bool) -> dict:
    now = datetime.now(timezone.utc)
    current["last_check_ts"] = now.isoformat()
    current["total_checks"] += 1
    if all_ok:
        current["clean_streak"] += 1
        level = current["level"]
        if level == "ALERT" and current["clean_streak"] >= UPGRADE_THRESHOLD:
            current["level"] = "NORMAL"
            current["clean_streak"] = 0
            current["last_level_change"] = f"⬆ ALERT→NORMAL @ {now.isoformat()}"
        elif level == "NORMAL" and current["clean_streak"] >= UPGRADE_THRESHOLD:
            current["level"] = "STABLE"
            current["clean_streak"] = 0
            current["last_level_change"] = f"⬆ NORMAL→STABLE @ {now.isoformat()}"
    else:
        old = current["level"]
        current["level"] = "ALERT"
        current["clean_streak"] = 0
        if old != "ALERT":
            current["last_level_change"] = f"⬇ {old}→ALERT @ {now.isoformat()}"
    return current


# ---------------------------------------------------------------------------
# 维度检查函数
# ---------------------------------------------------------------------------

def check_order_flow(state: dict) -> dict:
    """(a) 下单流程: 对账状态 + 卡死订单。"""
    issues = []
    details = {}
    checks = {c["check_id"]: c for c in state.get("checks", [])}

    recon = checks.get("runtime.safety.reconciliation")
    if recon:
        details["reconciliation"] = recon["status"]
        if recon["status"] == "FAIL":
            issues.append(f"对账失败: {recon['message'][:120]}")
    else:
        details["reconciliation"] = "UNKNOWN"

    # 卡死 NEW 订单（DB 查询不可用时的简化检查）
    details["stuck_new_orders"] = "N/A (daemon mode)"

    # 从状态中提取最近成交统计
    details["fills_last_hour"] = "N/A"
    details["total_orders"] = "N/A"
    details["active_orders"] = "N/A"

    return {"dimension": "order_flow", "ok": len(issues) == 0, "issues": issues, "details": details}


def check_protection_coverage(state: dict) -> dict:
    """(b) 保护覆盖: 每个持仓是否有止盈止损。"""
    issues = []
    details = {}
    checks = {c["check_id"]: c for c in state.get("checks", [])}

    prot = checks.get("runtime.safety.protection_coverage")
    if prot:
        details["coverage"] = prot["status"]
        evidence = prot.get("evidence", {})
        open_symbols = evidence.get("open_symbols", [])
        missing = evidence.get("missing", [])
        details["open_positions"] = len(open_symbols)
        details["missing_protections"] = len(missing)
        if prot["status"] == "FAIL":
            issues.append(f"保护覆盖失败: {prot['message'][:120]}")
        if missing:
            issues.append(f"缺失保护单: {missing}")
    else:
        details["coverage"] = "UNKNOWN"
        issues.append("保护覆盖检查项缺失")

    return {"dimension": "protection_coverage", "ok": len(issues) == 0, "issues": issues, "details": details}


def check_module_health(state: dict) -> dict:
    """(c) 模块健康: 生命周期、心跳、错误、控制面。"""
    issues = []
    details = {}
    checks = {c["check_id"]: c for c in state.get("checks", [])}

    check_map = {
        "runtime.health.lifecycle": "生命周期",
        "runtime.health.realtime_heartbeat": "实时心跳",
        "runtime.health.nearline_heartbeat": "近线心跳",
        "runtime.health.errors": "错误计数",
        "runtime.health.http_server": "HTTP服务",
        "runtime.health.market_data": "行情数据",
        "runtime.health.account_snapshot": "账户快照",
        "runtime.health.incidents": "活跃事故",
        "runtime.safety.control_plane": "控制面",
        "runtime.safety.write_interlock": "写入互锁",
    }

    for cid, label in check_map.items():
        c = checks.get(cid)
        if c is None:
            details[label] = "MISSING"
            continue
        details[label] = f"{c['status']}: {c['message'][:80]}"
        if c["status"] == "FAIL":
            issues.append(f"{label}失败: {c['message'][:100]}")
        elif c["status"] == "WARN" and c.get("severity", "P2") in ("P0", "P1"):
            issues.append(f"{label}警告: {c['message'][:100]}")

    blockers = state.get("blockers", [])
    details["p0_blockers"] = len(blockers)
    for b in blockers:
        issues.append(f"P0阻断: {b['check_id']}: {b.get('message', '')[:100]}")

    details["supervisor_state"] = state.get("supervisor_state", "UNKNOWN")
    details["trading_ready"] = state.get("trading_ready", False)
    if state.get("supervisor_state") not in ("RUNNING",):
        issues.append(f"监督器状态异常: {state.get('supervisor_state')}")

    return {"dimension": "module_health", "ok": len(issues) == 0, "issues": issues, "details": details}


def check_strategy_lifecycle(state: dict) -> dict:
    """(d) 因子/策略生命周期。"""
    issues = []
    details = {}
    checks = {c["check_id"]: c for c in state.get("checks", [])}

    check_map = {
        "runtime.algorithms.alpha_graph": "AlphaGraph",
        "runtime.algorithms.factor_lifecycle": "因子生命周期",
        "runtime.algorithms.trading_pool": "交易池",
        "runtime.algorithms.risk_budget": "风险预算",
    }

    for cid, label in check_map.items():
        c = checks.get(cid)
        if c is None:
            details[label] = "MISSING"
            issues.append(f"检查项缺失: {label}")
            continue
        details[label] = f"{c['status']}: {c['message'][:100]}"
        if c["status"] == "FAIL":
            issues.append(f"{label}失败: {c['message'][:100]}")

    # 因子详细
    factor = checks.get("runtime.algorithms.factor_lifecycle")
    if factor:
        lifecycle = factor.get("evidence", {}).get("lifecycle", {})
        active = sum(1 for v in lifecycle.values() if v == "ACTIVE")
        total = len(lifecycle)
        details["factors_active"] = f"{active}/{total}"
        inactive = {k: v for k, v in lifecycle.items() if v != "ACTIVE"}
        if inactive:
            issues.append(f"非活跃因子: {inactive}")

    # AlphaGraph 详细
    alpha = checks.get("runtime.algorithms.alpha_graph")
    if alpha:
        ev = alpha.get("evidence", {})
        details["alpha_components"] = len(ev.get("actual", []))
        details["alpha_missing"] = ev.get("missing", [])
        details["alpha_invalid"] = ev.get("invalid", [])
        if ev.get("missing") or ev.get("invalid"):
            issues.append(f"AlphaGraph异常: missing={ev.get('missing')} invalid={ev.get('invalid')}")

    return {"dimension": "strategy_lifecycle", "ok": len(issues) == 0, "issues": issues, "details": details}


def should_run_now(freq: dict) -> bool:
    level = freq.get("level", "ALERT")
    interval_minutes = FREQ_CONFIG[level]["minutes"]
    last_ts = freq.get("last_check_ts")
    if last_ts is None:
        return True
    try:
        last_dt = datetime.fromisoformat(last_ts)
        elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
        return elapsed >= (interval_minutes * 60) - 5
    except (ValueError, TypeError):
        return True


def print_summary(results: list[dict]) -> str:
    all_ok = all(r["ok"] for r in results)
    total_issues = sum(len(r["issues"]) for r in results)
    dims = ", ".join(f"{r['dimension']}={ '✅' if r['ok'] else '❌'}" for r in results)
    if all_ok and total_issues == 0:
        return f"🟢 全部正常 | {dims}"
    elif all_ok:
        return f"🟡 有警告 ({total_issues}) | {dims}"
    else:
        return f"🔴 有异常 ({total_issues}) | {dims}"


def run_check(freq: dict) -> tuple[list[dict], bool, dict]:
    """执行一轮监控检查。返回 (results, all_ok, freq)。"""
    state = load_supervisor_state()
    if state is None:
        print(f"[{datetime.now(timezone.utc).isoformat()}] 无法读取 supervisor-state.json")
        return [], False, freq

    # 进程存活检查
    pid = state.get("pid")
    if pid:
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            print(f"[{datetime.now(timezone.utc).isoformat()}] ❌ 北斗进程 PID={pid} 已终止")
            log_evidence({"ok": False, "error": f"process PID={pid} not alive", "pid": pid})
            return [], False, freq

    results = [
        check_order_flow(state),
        check_protection_coverage(state),
        check_module_health(state),
        check_strategy_lifecycle(state),
    ]

    all_ok = all(r["ok"] for r in results)
    freq = update_frequency(freq, all_ok)
    save_freq_state(freq)

    level_info = FREQ_CONFIG[freq["level"]]

    print("=" * 72)
    print(f"北斗运营监控 | {datetime.now(timezone.utc).isoformat()}")
    print(f"频率: {level_info['label']} | 连续正常: {freq['clean_streak']}次 | 总计: {freq['total_checks']}次")
    print(f"状态: {print_summary(results)}")
    print("-" * 72)
    for r in results:
        status = "✅" if r["ok"] else "❌"
        print(f"\n{status} 维度: {r['dimension']}")
        for k, v in r["details"].items():
            print(f"    {k}: {v}")
        for issue in r["issues"]:
            print(f"    ⚠️ {issue}")
    print("=" * 72)

    log_evidence({
        "ok": all_ok,
        "summary": print_summary(results),
        "freq_level": freq["level"],
        "clean_streak": freq["clean_streak"],
        "total_checks": freq["total_checks"],
        "dimensions": {
            r["dimension"]: {"ok": r["ok"], "issues": r["issues"], "details": r["details"]}
            for r in results
        },
    })

    return results, all_ok, freq


# ---------------------------------------------------------------------------
# 守护进程管理
# ---------------------------------------------------------------------------

def daemonize() -> None:
    """双 fork 守护进程化。"""
    if os.fork() > 0:
        return  # 父进程退出
    os.setsid()
    if os.fork() > 0:
        sys.exit(0)  # 第一子进程退出
    # 重定向标准流
    sys.stdout = open(PROJECT_ROOT / ".beidou" / "monitor_daemon.log", "a")
    sys.stderr = sys.stdout
    sys.stdin = open(os.devnull, "r")


def write_pid() -> None:
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def cleanup_pid() -> None:
    try:
        PID_FILE.unlink()
    except Exception:
        pass


def signal_handler(signum, frame):
    print(f"[{datetime.now(timezone.utc).isoformat()}] 收到信号 {signum}，退出守护进程")
    cleanup_pid()
    sys.exit(0)


def run_foreground() -> None:
    """前台运行模式（调试用）。"""
    write_pid()
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    freq = load_freq_state()
    print(f"[{datetime.now(timezone.utc).isoformat()}] 北斗监控守护进程启动 (PID={os.getpid()})")
    print(f"    频率: {FREQ_CONFIG[freq['level']]['label']} | 总计: {freq['total_checks']}次")

    try:
        while True:
            if should_run_now(freq):
                try:
                    run_check(freq)
                except Exception:
                    print(f"[{datetime.now(timezone.utc).isoformat()}] 检查异常:\n{traceback.format_exc()}")
                freq = load_freq_state()  # 重新加载（run_check 已更新）
            time.sleep(30)  # 每 30s 检查是否到执行时间
    finally:
        cleanup_pid()
        print(f"[{datetime.now(timezone.utc).isoformat()}] 监控守护进程退出")


def run_daemon() -> None:
    """后台守护进程模式。"""
    daemonize()
    write_pid()
    signal.signal(signal.SIGTERM, signal_handler)

    freq = load_freq_state()
    print(f"[{datetime.now(timezone.utc).isoformat()}] 北斗监控守护进程启动 (PID={os.getpid()}, daemon)")

    try:
        while True:
            if should_run_now(freq):
                try:
                    run_check(freq)
                except Exception:
                    print(f"[{datetime.now(timezone.utc).isoformat()}] 检查异常:\n{traceback.format_exc()}")
                freq = load_freq_state()
            time.sleep(30)
    finally:
        cleanup_pid()


def stop_daemon() -> bool:
    """停止运行中的守护进程。"""
    if not PID_FILE.exists():
        print("守护进程未运行（无 PID 文件）")
        return False
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
        os.kill(pid, signal.SIGTERM)
        print(f"已发送终止信号到 PID={pid}")
        return True
    except ProcessLookupError:
        print(f"PID 文件存在但进程已退出")
        PID_FILE.unlink()
        return False
    except Exception as e:
        print(f"停止失败: {e}")
        return False


def status_daemon() -> bool:
    """检查守护进程运行状态。"""
    if not PID_FILE.exists():
        print("守护进程未运行")
        return False
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        print(f"守护进程运行中 (PID={pid})")
        if FREQ_FILE.exists():
            freq = load_freq_state()
            print(f"  频率: {FREQ_CONFIG[freq['level']]['label']}")
            print(f"  总计: {freq['total_checks']}次 | 连续正常: {freq['clean_streak']}次")
        return True
    except (ProcessLookupError, OSError):
        print("守护进程已退出（PID 文件残留）")
        PID_FILE.unlink()
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 scripts/monitor_daemon.py [start|start-fg|stop|status|run-once]")
        print("  start      — 后台启动守护进程")
        print("  start-fg   — 前台启动（调试）")
        print("  stop       — 停止守护进程")
        print("  status     — 查看运行状态")
        print("  run-once   — 执行单次检查后退出")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "start":
        if status_daemon():
            print("守护进程已在运行，先执行 stop 再启动")
            sys.exit(1)
        run_daemon()

    elif cmd == "start-fg":
        run_foreground()

    elif cmd == "stop":
        stop_daemon()

    elif cmd == "status":
        status_daemon()

    elif cmd == "run-once":
        freq = load_freq_state()
        results, all_ok, freq = run_check(freq)
        sys.exit(0 if all_ok else 1)

    else:
        print(f"未知命令: {cmd}")
        sys.exit(1)
