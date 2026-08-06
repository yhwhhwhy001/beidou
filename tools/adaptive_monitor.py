#!/usr/bin/env python3
"""北斗系统自适应监控 + 自动修复 — 下单全链路检测与自愈。

监控策略:
  1. 初始 10 分钟检查一次
  2. 连续 3 次正常 → 30 分钟检查一次
  3. 连续 3 次正常 → 1 小时检查一次
  4. 任何一次异常 → 立即恢复到 10 分钟
  5. 可自动修复的异常 → 快照状态 → 执行修复 → 验证 → 恢复监控

修复能力:
  - 进程终止 → 自动重启
  - 端口冲突 → 清理僵尸进程 + 重启
  - REALTIME 时钟停滞 → 重启恢复
  - 错误数过高 → 重启恢复
  - 生命周期异常 → 状态恢复 + 重启
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

# ================================================================
# 路径常量
# ================================================================

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(PROJECT_ROOT, "beidou_monitor_state.json")
REPAIR_LOG = os.path.join(PROJECT_ROOT, "beidou_repair_log.jsonl")
AUTOPILOT_CMD = ["python", "-m", "apps.autopilot", "--symbols", "BTCUSDT,ETHUSDT", "--mode", "paper", "--port", "9090"]
HEALTH_URL = "http://localhost:9090"
OK_TO_ESCALATE = 3
MAX_REPAIR_ATTEMPTS = 3
STABILIZE_WAIT = 12  # 重启后等待稳定的秒数

FULL_UNIVERSE = {
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT", "MATICUSDT", "UNIUSDT",
    "ATOMUSDT", "LTCUSDT", "ETCUSDT", "FILUSDT", "APTUSDT", "ARBUSDT",
    "OPUSDT", "NEARUSDT", "INJUSDT", "SUIUSDT", "RUNEUSDT", "SEIUSDT", "TIAUSDT",
}
DEFAULT_SYMBOLS = "BTCUSDT,ETHUSDT"
DEFAULT_MODE = "testnet"
DEFAULT_PORT = 9090


def _build_autopilot_cmd() -> list[str]:
    """从运行中系统检测参数，构建正确的重启命令。"""
    try:
        status = http_get("/status")
        if "error" not in status:
            mode = status.get("mode", DEFAULT_MODE)
            symbols = status.get("symbols", [])
            if set(symbols) >= FULL_UNIVERSE:
                symbols_arg = "ALL"
            elif symbols:
                symbols_arg = ",".join(symbols[:25])
            else:
                symbols_arg = DEFAULT_SYMBOLS
            return ["python", "-m", "apps.autopilot",
                    "--symbols", symbols_arg, "--mode", mode,
                    "--port", str(DEFAULT_PORT)]
    except Exception:
        pass
    return ["python", "-m", "apps.autopilot",
            "--symbols", DEFAULT_SYMBOLS, "--mode", DEFAULT_MODE,
            "--port", str(DEFAULT_PORT)]


# ================================================================
# 状态管理
# ================================================================

CHECK_INTERVALS = [600, 1800, 3600]  # 10min, 30min, 1h (seconds)


class EscalationLevel(int, Enum):
    TEN_MIN = 0
    THIRTY_MIN = 1
    ONE_HOUR = 2


class RepairAction(str, Enum):
    NONE = "none"
    RESTART = "restart"
    KILL_STALE_AND_RESTART = "kill_stale_and_restart"
    MANUAL_REQUIRED = "manual_required"


@dataclass
class RepairRecord:
    timestamp: str
    trigger: str
    action: RepairAction
    success: bool
    detail: str


@dataclass
class MonitorState:
    level: EscalationLevel = EscalationLevel.TEN_MIN
    consecutive_ok: int = 0
    consecutive_fail: int = 0
    last_check: float = 0.0
    last_escalation: str = ""
    total_checks: int = 0
    total_failures: int = 0
    total_repairs: int = 0
    repair_attempts_today: int = 0
    last_repair: str = ""
    checks: list[dict] = field(default_factory=list)
    repairs: list[dict] = field(default_factory=list)

    @property
    def interval_seconds(self) -> int:
        return CHECK_INTERVALS[self.level.value]

    @property
    def interval_label(self) -> str:
        labels = ["10分钟", "30分钟", "1小时"]
        return labels[self.level.value]


def load_state() -> MonitorState:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
            state = MonitorState(
                level=EscalationLevel(data.get("level", 0)),
                consecutive_ok=data.get("consecutive_ok", 0),
                consecutive_fail=data.get("consecutive_fail", 0),
                last_check=data.get("last_check", 0),
                last_escalation=data.get("last_escalation", ""),
                total_checks=data.get("total_checks", 0),
                total_failures=data.get("total_failures", 0),
                total_repairs=data.get("total_repairs", 0),
                repair_attempts_today=data.get("repair_attempts_today", 0),
                last_repair=data.get("last_repair", ""),
                checks=data.get("checks", []),
                repairs=data.get("repairs", []),
            )
            if len(state.checks) > 100:
                state.checks = state.checks[-100:]
            if len(state.repairs) > 50:
                state.repairs = state.repairs[-50:]
            return state
        except (json.JSONDecodeError, KeyError):
            pass
    return MonitorState()


def save_state(state: MonitorState) -> None:
    data = {
        "level": state.level.value,
        "consecutive_ok": state.consecutive_ok,
        "consecutive_fail": state.consecutive_fail,
        "last_check": state.last_check,
        "last_escalation": state.last_escalation,
        "total_checks": state.total_checks,
        "total_failures": state.total_failures,
        "total_repairs": state.total_repairs,
        "repair_attempts_today": state.repair_attempts_today,
        "last_repair": state.last_repair,
        "checks": state.checks[-100:],
        "repairs": state.repairs[-50:],
    }
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=2)


def log_repair(record: RepairRecord) -> None:
    """追加修复日志。"""
    with open(REPAIR_LOG, "a") as f:
        f.write(json.dumps({
            "timestamp": record.timestamp,
            "trigger": record.trigger,
            "action": record.action.value,
            "success": record.success,
            "detail": record.detail,
        }) + "\n")


# ================================================================
# HTTP 工具
# ================================================================


def http_get(endpoint: str) -> dict[str, Any]:
    try:
        req = urllib.request.Request(f"{HEALTH_URL}{endpoint}")
        req.add_header("Connection", "close")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def http_get_text(endpoint: str) -> str:
    try:
        req = urllib.request.Request(f"{HEALTH_URL}{endpoint}")
        req.add_header("Connection", "close")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.read().decode()
    except Exception:
        return ""


# ================================================================
# 下单全链路检查
# ================================================================


@dataclass
class CheckResult:
    timestamp: str
    passed: bool
    details: dict[str, Any]
    failures: list[str]
    warnings: list[str]
    suggested_repair: RepairAction = RepairAction.NONE


def check_order_flow() -> CheckResult:
    failures: list[str] = []
    warnings: list[str] = []
    details: dict[str, Any] = {}
    suggested = RepairAction.NONE

    # ---- 1. 系统存活 ----
    health = http_get("/health")
    if health.get("status") not in ("ok", "HEALTHY"):
        failures.append(f"Health check failed: {health}")
        suggested = RepairAction.RESTART
        return CheckResult(
            timestamp=datetime.now(timezone.utc).isoformat(),
            passed=False, details=details, failures=failures, warnings=warnings,
            suggested_repair=suggested,
        )
    details["uptime_seconds"] = health.get("uptime_seconds", 0)

    # ---- 2. 运行状态 ----
    status = http_get("/status")
    if "error" in status:
        failures.append(f"Status endpoint error: {status['error']}")
        suggested = RepairAction.RESTART
        return CheckResult(
            timestamp=datetime.now(timezone.utc).isoformat(),
            passed=False, details=details, failures=failures, warnings=warnings,
            suggested_repair=suggested,
        )

    details["lifecycle"] = status.get("lifecycle_state", "UNKNOWN")
    details["control"] = status.get("control_action", "UNKNOWN")
    details["mode"] = status.get("mode", "UNKNOWN")

    # ---- 3. 生命周期 ----
    lifecycle = status.get("lifecycle_state", "")
    if lifecycle in ("FAILED", "LOCKED", "BOOTSTRAPPING"):
        failures.append(f"Lifecycle: {lifecycle}")
        suggested = RepairAction.RESTART

    # ---- 4. 控制面 ----
    control = status.get("control_action", "")
    if control in ("LOCK", "EMERGENCY_FLATTEN"):
        failures.append(f"Control plane LOCKED: {control}")
        suggested = RepairAction.MANUAL_REQUIRED  # 不能自动修复
    elif control == "EXIT_ONLY":
        warnings.append(f"Control plane EXIT_ONLY: {control}")
    elif control == "NO_NEW_RISK":
        warnings.append(f"Control plane NO_NEW_RISK: {control}")

    # ---- 5. 活跃告警 ----
    incidents = status.get("active_incidents", [])
    details["incident_count"] = len(incidents)
    critical_incidents = [i for i in incidents if i.get("severity") in ("CRITICAL", "LOCKDOWN")]
    if critical_incidents:
        failures.append(f"CRITICAL incidents: {len(critical_incidents)}")
        for ci in critical_incidents[:3]:
            failures.append(f"  - {ci.get('title')}: {ci.get('status')}")
        suggested = RepairAction.MANUAL_REQUIRED

    warning_incidents = [i for i in incidents if i.get("severity") == "WARNING"]
    if warning_incidents:
        warnings.append(f"WARNING incidents: {len(warning_incidents)}")

    # ---- 6. 策略风险 ----
    risk = status.get("strategy_risk", {})
    details["risk_level"] = risk.get("level", "N/A")
    details["drawdown_pct"] = risk.get("drawdown_pct", 0)
    details["consecutive_losses"] = risk.get("consecutive_losses", 0)

    if risk.get("level") in ("LOCKED", "DEGRADED"):
        failures.append(f"Strategy risk: {risk.get('level')}")
        if risk.get("level") == "LOCKED":
            suggested = RepairAction.MANUAL_REQUIRED

    # ---- 7. 因子活跃度 ----
    active_factors = status.get("active_factors", 0)
    details["active_factors"] = active_factors
    if active_factors < 4:
        warnings.append(f"Low active factors: {active_factors}/8")

    # ---- 8. 指标采集 ----
    metrics_text = http_get_text("/metrics")
    if metrics_text:
        tick_count = _parse_metric(metrics_text, "beidou_tick_count")
        error_count = _parse_metric(metrics_text, "beidou_error_count")
        order_count = _parse_metric(metrics_text, "beidou_order_count")
        active_orders = _parse_metric(metrics_text, "beidou_active_orders")
        positions = _parse_metric(metrics_text, "beidou_positions")

        details["tick_count"] = tick_count
        details["error_count"] = error_count
        details["order_count"] = order_count
        details["active_orders"] = active_orders
        details["positions"] = positions

        if error_count is not None and error_count > 50:
            failures.append(f"Error count critical: {error_count}")
            suggested = RepairAction.RESTART
        elif error_count is not None and error_count > 10:
            warnings.append(f"Error count high: {error_count}")
            if suggested == RepairAction.NONE:
                suggested = RepairAction.RESTART
        elif error_count is not None and error_count > 0:
            warnings.append(f"Errors detected: {error_count}")

        last_realtime = status.get("last_realtime_tick", 0)
        last_nearline = status.get("last_nearline_tick", 0)
        now_ts = time.time()
        details["realtime_lag_seconds"] = round(now_ts - last_realtime, 1) if last_realtime else -1
        details["nearline_lag_seconds"] = round(now_ts - last_nearline, 1) if last_nearline else -1

        if last_realtime and now_ts - last_realtime > 120:
            failures.append(f"REALTIME clock stalled: {details['realtime_lag_seconds']}s lag")
            if suggested == RepairAction.NONE:
                suggested = RepairAction.RESTART
        elif last_realtime and now_ts - last_realtime > 60:
            warnings.append(f"REALTIME clock slow: {details['realtime_lag_seconds']}s lag")

    passed = len(failures) == 0
    return CheckResult(
        timestamp=datetime.now(timezone.utc).isoformat(),
        passed=passed, details=details, failures=failures, warnings=warnings,
        suggested_repair=suggested,
    )


def _parse_metric(text: str, name: str) -> float | None:
    import re
    pattern = rf"^{name}\s+([\d.]+)"
    for line in text.split("\n"):
        if line.startswith(name) and not line.startswith("#"):
            m = re.match(pattern, line)
            if m:
                return float(m.group(1))
    return None


# ================================================================
# 自动修复引擎
# ================================================================


def snapshot_state(state: MonitorState) -> dict:
    """快照当前状态（修复前保存）。"""
    return {
        "snapshot_time": datetime.now(timezone.utc).isoformat(),
        "level": state.level.value,
        "consecutive_ok": state.consecutive_ok,
        "consecutive_fail": state.consecutive_fail,
        "total_checks": state.total_checks,
        "total_failures": state.total_failures,
        "total_repairs": state.total_repairs,
        "repair_attempts_today": state.repair_attempts_today,
    }


def find_autopilot_pids() -> list[int]:
    """查找所有 autopilot 进程 PID。"""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "apps.autopilot"],
            capture_output=True, text=True, timeout=5,
        )
        return [int(pid) for pid in result.stdout.strip().split("\n") if pid]
    except Exception:
        return []


def find_port_owner(port: int = 9090) -> int | None:
    """查找占用端口的进程 PID。"""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True, timeout=5,
        )
        pids = [int(p) for p in result.stdout.strip().split("\n") if p]
        return pids[0] if pids else None
    except Exception:
        return None


def kill_autopilot(graceful: bool = True) -> bool:
    """终止 autopilot 进程。先优雅终止，再强制。"""
    pids = find_autopilot_pids()
    if not pids:
        return True

    for pid in pids:
        try:
            if graceful:
                os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue

    if graceful:
        time.sleep(3)
        # 检查是否还在运行
        pids = find_autopilot_pids()
        if not pids:
            return True

    # 强制终止
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue

    time.sleep(2)
    return len(find_autopilot_pids()) == 0


def free_port(port: int = 9090) -> bool:
    """释放端口。"""
    owner = find_port_owner(port)
    if owner is None:
        return True
    try:
        os.kill(owner, signal.SIGKILL)
        time.sleep(2)
        return find_port_owner(port) is None
    except ProcessLookupError:
        return True


def restart_autopilot() -> tuple[bool, str]:
    """重启 autopilot：保存状态 → 终止进程 → 释放端口 → 启动 → 验证。"""
    # 1. 快照已经由调用者保存

    # 2. 终止旧进程
    if not kill_autopilot(graceful=True):
        return False, "无法终止旧 autopilot 进程"

    # 3. 释放端口
    if not free_port(9090):
        return False, "无法释放端口 9090"

    time.sleep(1)

    # 4. 启动新进程
    try:
        proc = subprocess.Popen(
            _build_autopilot_cmd(),
            cwd=PROJECT_ROOT,
            stdout=open("/tmp/beidou-24h.log", "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,  # 脱离终端
        )
        print(f"[repair] 已启动 autopilot PID={proc.pid}")
    except Exception as e:
        return False, f"启动 autopilot 失败: {e}"

    # 5. 等待稳定
    time.sleep(STABILIZE_WAIT)

    # 6. 验证健康
    for attempt in range(3):
        health = http_get("/health")
        if health.get("status") == "ok":
            status = http_get("/status")
            if status.get("lifecycle_state") == "ACTIVE":
                return True, f"重启成功 PID={proc.pid} uptime={health.get('uptime_seconds', 0):.0f}s"
        time.sleep(3)

    return False, f"重启后验证失败 (尝试 {3} 次)"


def execute_repair(result: CheckResult, state: MonitorState) -> tuple[bool, RepairRecord]:
    """执行自动修复。返回 (是否成功, 修复记录)。"""
    action = result.suggested_repair

    if action == RepairAction.NONE:
        return True, RepairRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            trigger="none", action=RepairAction.NONE, success=True,
            detail="无需修复",
        )

    if action == RepairAction.MANUAL_REQUIRED:
        return False, RepairRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            trigger="; ".join(result.failures[:3]),
            action=RepairAction.MANUAL_REQUIRED, success=False,
            detail="需要人工干预 — 控制面 LOCKED 或 CRITICAL 告警",
        )

    # 检查今日修复次数
    if state.repair_attempts_today >= MAX_REPAIR_ATTEMPTS:
        return False, RepairRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            trigger="; ".join(result.failures[:3]),
            action=RepairAction.MANUAL_REQUIRED, success=False,
            detail=f"今日修复已达上限 ({MAX_REPAIR_ATTEMPTS}次)，需要人工介入",
        )

    # === 执行重启修复 ===
    print(f"\n🔧 [auto-repair] 触发自动修复: {action.value}")
    print(f"   原因: {'; '.join(result.failures[:3])}")

    # 保存快照
    snapshot = snapshot_state(state)
    print(f"   📸 状态快照已保存: 等级={state.interval_label} 检查={state.total_checks} 修复={state.total_repairs}")

    success, detail = restart_autopilot()
    state.total_repairs += 1
    state.repair_attempts_today += 1

    record = RepairRecord(
        timestamp=datetime.now(timezone.utc).isoformat(),
        trigger="; ".join(result.failures[:3]),
        action=action,
        success=success,
        detail=detail,
    )

    if success:
        # 修复成功后恢复监控状态（保持升级等级）
        print(f"   ✅ 修复成功: {detail}")
        print(f"   📋 恢复监控状态: 等级={state.interval_label} 连续正常=0")
        state.consecutive_ok = 0  # 重置计数，但从当前等级继续
        state.consecutive_fail = 0
        state.last_repair = record.timestamp
    else:
        print(f"   ❌ 修复失败: {detail}")
        # 降级到最高频检查
        state.level = EscalationLevel.TEN_MIN
        state.consecutive_ok = 0
        state.consecutive_fail = 0

    state.repairs.append({
        "ts": record.timestamp,
        "trigger": record.trigger,
        "action": record.action.value,
        "success": record.success,
        "detail": record.detail[:200],
        "snapshot": snapshot,
    })

    log_repair(record)
    return success, record


# ================================================================
# 升级/降级逻辑
# ================================================================


def evaluate_escalation(state: MonitorState, result: CheckResult, repair_success: bool) -> tuple[MonitorState, str]:
    """评估升级或降级。修复成功后不降级（保持当前等级）。"""
    action = ""
    state.total_checks += 1
    state.last_check = time.time()

    if result.passed or (result.suggested_repair != RepairAction.NONE and repair_success):
        state.consecutive_ok += 1
        state.consecutive_fail = 0

        if state.consecutive_ok >= OK_TO_ESCALATE and state.level < EscalationLevel.ONE_HOUR:
            old_level = state.level
            state.level = EscalationLevel(state.level.value + 1)
            state.consecutive_ok = 0
            action = f"⬆️ 升级: {old_level.name} → {state.level.name} ({state.interval_label})"
            state.last_escalation = action
    else:
        state.consecutive_fail += 1
        state.consecutive_ok = 0
        state.total_failures += 1

        if state.level > EscalationLevel.TEN_MIN:
            old_level = state.level
            state.level = EscalationLevel.TEN_MIN
            state.consecutive_fail = 0
            action = f"⬇️ 降级: {old_level.name} → TEN_MIN (10分钟)"
            state.last_escalation = action

    state.checks.append({
        "ts": result.timestamp,
        "passed": result.passed,
        "failures": result.failures[:5],
        "warnings": result.warnings[:5],
        "level": state.level.name,
        "repair": result.suggested_repair.value,
        "action": action,
    })

    return state, action


# ================================================================
# 报告输出
# ================================================================


def format_report(result: CheckResult, state: MonitorState, action: str) -> str:
    lines = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    status_icon = "✅" if result.passed else "❌"
    lines.append(f"{'='*60}")
    lines.append(f"  {status_icon} 北斗自适应监控 [{now_str}]")
    lines.append(f"  检查间隔: {state.interval_label} | 连续正常: {state.consecutive_ok} | 连续异常: {state.consecutive_fail}")
    lines.append(f"  累计: 检查{state.total_checks} 异常{state.total_failures} 修复{state.total_repairs} 今日修复{state.repair_attempts_today}/{MAX_REPAIR_ATTEMPTS}")
    lines.append(f"{'='*60}")

    if action:
        lines.append(f"  📢 {action}")
        lines.append("")

    lines.append("  ┌─ 下单全链路 ─────────────────────────────")
    d = result.details

    lines.append(f"  ├─ 系统存活: {'✅' if d.get('uptime_seconds', 0) > 0 else '❌'} "
                 f"(运行 {d.get('uptime_seconds', 0):.0f}s)")
    lines.append(f"  ├─ 生命周期: {'✅' if d.get('lifecycle') == 'ACTIVE' else '⚠️'} "
                 f"{d.get('lifecycle', '?')}")
    lines.append(f"  ├─ 控制面: {'✅' if d.get('control') == 'RESUME' else '⚠️'} "
                 f"{d.get('control', '?')}")
    lines.append(f"  ├─ 告警: {d.get('incident_count', 0)} "
                 f"({'✅' if d.get('incident_count', 0) == 0 else '⚠️'})")
    lines.append(f"  ├─ 策略风险: {'✅' if d.get('risk_level') == 'NORMAL' else '⚠️'} "
                 f"等级={d.get('risk_level', '?')} "
                 f"回撤={d.get('drawdown_pct', 0)}% "
                 f"连亏={d.get('consecutive_losses', 0)}")
    lines.append(f"  ├─ 因子: {d.get('active_factors', 0)}/8 活跃 "
                 f"({'✅' if d.get('active_factors', 0) >= 4 else '⚠️'})")
    lines.append(f"  ├─ REALTIME: Tick={d.get('tick_count', '?')} "
                 f"滞后={d.get('realtime_lag_seconds', '?')}s")
    lines.append(f"  ├─ 错误数: {d.get('error_count', '?')} "
                 f"({'✅' if d.get('error_count', 0) == 0 else '⚠️'})")
    lines.append(f"  ├─ 订单: 成交={d.get('order_count', '?')} "
                 f"活跃={d.get('active_orders', '?')} "
                 f"持仓={d.get('positions', '?')}")
    lines.append(f"  └─ NEARLINE: 滞后={d.get('nearline_lag_seconds', '?')}s")
    lines.append("")

    if result.failures:
        lines.append(f"  ❌ 阻断 (×{len(result.failures)}):")
        for f_item in result.failures[:5]:
            lines.append(f"     - {f_item}")
    if result.warnings:
        lines.append(f"  ⚠️  告警 (×{len(result.warnings)}):")
        for w_item in result.warnings[:5]:
            lines.append(f"     - {w_item}")

    if result.suggested_repair != RepairAction.NONE:
        icon = "🔧" if result.suggested_repair != RepairAction.MANUAL_REQUIRED else "🚨"
        lines.append(f"  {icon} 建议修复: {result.suggested_repair.value}")

    lines.append(f"{'='*60}")
    if state.repairs:
        last_repair = state.repairs[-1]
        lines.append(f"  最近修复: {last_repair['ts'][:19]} "
                     f"{'✅' if last_repair['success'] else '❌'} "
                     f"{last_repair['action']}")
    lines.append(f"{'='*60}")

    return "\n".join(lines)


# ================================================================
# 主入口
# ================================================================


def main() -> None:
    state = load_state()

    # 自适应间隔检查
    now = time.time()
    if state.last_check > 0 and (now - state.last_check) < state.interval_seconds:
        return

    # 1. 执行全链路检查
    result = check_order_flow()

    # 2. 如果需要修复，尝试自动修复
    repair_success = True
    if not result.passed and result.suggested_repair not in (RepairAction.NONE, RepairAction.MANUAL_REQUIRED):
        repair_success, record = execute_repair(result, state)
        if not repair_success and result.suggested_repair != RepairAction.MANUAL_REQUIRED:
            # 自动修复失败，标记为需要人工介入
            result.failures.append(f"自动修复失败: {record.detail}")
            result.suggested_repair = RepairAction.MANUAL_REQUIRED

    # 3. 评估升级/降级
    state, action = evaluate_escalation(state, result, repair_success)
    save_state(state)

    # 4. 输出报告
    report = format_report(result, state, action)
    print(report)

    if action:
        print(f"\n[adaptive-monitor] 状态变更: {action}")


if __name__ == "__main__":
    main()
