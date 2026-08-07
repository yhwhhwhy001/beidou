#!/usr/bin/env python3
"""北斗运营监控 — 自适应频率 + 全维度覆盖。

频率规则:
- L0 (10min): 初始频率或发现异常后降级
- L1 (30min): L0 连续3次干净 → 升级
- L2 (60min): L1 连续3次干净 → 升级
- 任何异常 → 立即回到 L0

监控维度:
(a) 下单流程: 订单状态机、卡死订单、对账匹配
(b) 保护覆盖: 每个持仓是否有止盈止损单
(c) 模块健康: 生命周期、控制面、心跳、错误计数
(d) 因子/策略: 因子注册、AlphaGraph、交易池、风险预算
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = PROJECT_ROOT / ".beidou" / "supervisor-state.json"
DB_FILE = PROJECT_ROOT / "beidou_state.db"
MONITOR_LOG = PROJECT_ROOT / ".beidou" / "ops_monitor_log.jsonl"
FREQ_STATE = PROJECT_ROOT / ".beidou" / "ops_monitor_freq.json"

FREQ_LEVELS = {
    "L0": {"minutes": 10, "label": "10min (警觉模式)"},
    "L1": {"minutes": 30, "label": "30min (正常模式)"},
    "L2": {"minutes": 60, "label": "60min (稳定模式)"},
}

UPGRADE_THRESHOLD = 3   # 连续 N 次干净后升级
DOWNGRADE_TARGET = "L0"  # 异常后回退目标


def load_supervisor_state() -> dict[str, Any] | None:
    if not STATE_FILE.exists():
        return None
    with open(STATE_FILE) as f:
        return json.load(f)


def query_db(query: str, params: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def load_freq_state() -> dict:
    if FREQ_STATE.exists():
        with open(FREQ_STATE) as f:
            return json.load(f)
    return {"level": "L0", "clean_streak": 0, "last_level_change": "", "total_checks": 0, "last_check_ts": None}


def save_freq_state(state: dict) -> None:
    FREQ_STATE.parent.mkdir(parents=True, exist_ok=True)
    with open(FREQ_STATE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def log_result(entry: dict) -> None:
    MONITOR_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry["logged_at"] = datetime.now(timezone.utc).isoformat()
    with open(MONITOR_LOG, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


# ---------------------------------------------------------------------------
# 维度 (a): 下单流程
# ---------------------------------------------------------------------------
def check_order_flow(state: dict) -> dict:
    issues = []
    details = {}

    # 对账状态
    recon = next((c for c in state.get("checks", []) if c["check_id"] == "runtime.safety.reconciliation"), None)
    if recon:
        details["reconciliation"] = recon["status"]
        if recon["status"] == "FAIL":
            issues.append(f"对账失败: {recon['message'][:120]}")

    # 卡死 NEW 订单（超过 5 分钟仍在 NEW 状态）
    stuck = query_db(
        "SELECT COUNT(*) as cnt FROM order_states WHERE status='NEW' "
        "AND updated_at < datetime('now', '-5 minutes')"
    )
    stuck_count = stuck[0]["cnt"] if stuck else 0
    details["stuck_new_orders"] = stuck_count
    if stuck_count > 0:
        issues.append(f"卡死订单: {stuck_count} 笔 NEW 状态超过 5 分钟")

    # 近期成交统计
    recent_fills = query_db(
        "SELECT COUNT(*) as cnt FROM order_states WHERE status='FILLED' "
        "AND updated_at > datetime('now', '-1 hour')"
    )
    details["fills_last_hour"] = recent_fills[0]["cnt"] if recent_fills else 0

    # 总订单数
    total = query_db("SELECT COUNT(*) as cnt FROM order_states")
    details["total_orders"] = total[0]["cnt"] if total else 0

    # 活跃订单数（系统内追踪）
    active = query_db("SELECT COUNT(*) as cnt FROM order_states WHERE status='NEW'")
    details["active_orders"] = active[0]["cnt"] if active else 0

    return {"dimension": "order_flow", "ok": len(issues) == 0, "issues": issues, "details": details}


# ---------------------------------------------------------------------------
# 维度 (b): 保护覆盖
# ---------------------------------------------------------------------------
def check_protection_coverage(state: dict) -> dict:
    issues = []
    details = {}

    prot = next((c for c in state.get("checks", []) if c["check_id"] == "runtime.safety.protection_coverage"), None)
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

    # DB 层面交叉验证：protection_orders 的 ACTIVE 数量
    active_prots = query_db("SELECT COUNT(*) as cnt FROM protection_orders WHERE status='ACTIVE'")
    details["active_protection_orders"] = active_prots[0]["cnt"] if active_prots else 0

    return {"dimension": "protection_coverage", "ok": len(issues) == 0, "issues": issues, "details": details}


# ---------------------------------------------------------------------------
# 维度 (c): 模块与系统健康
# ---------------------------------------------------------------------------
def check_module_health(state: dict) -> dict:
    issues = []
    details = {}

    checks_to_watch = {
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

    for cid, label in checks_to_watch.items():
        c = next((c for c in state.get("checks", []) if c["check_id"] == cid), None)
        if c is None:
            details[label] = "MISSING"
            issues.append(f"检查项缺失: {label}")
            continue
        details[label] = f"{c['status']}: {c['message'][:80]}"
        if c["status"] == "FAIL":
            issues.append(f"{label}失败: {c['message'][:100]}")
        elif c["status"] == "WARN" and c["severity"] in ("P0", "P1"):
            issues.append(f"{label}警告(P1+): {c['message'][:100]}")

    # P0 阻断数
    blockers = state.get("blockers", [])
    details["p0_blockers"] = len(blockers)
    if blockers:
        for b in blockers:
            issues.append(f"P0阻断: {b['check_id']}: {b['message'][:100]}")

    # Supervisor 状态
    details["supervisor_state"] = state.get("supervisor_state", "UNKNOWN")
    details["trading_ready"] = state.get("trading_ready", False)
    if state.get("supervisor_state") not in ("RUNNING",):
        issues.append(f"监督器状态异常: {state.get('supervisor_state')}")

    return {"dimension": "module_health", "ok": len(issues) == 0, "issues": issues, "details": details}


# ---------------------------------------------------------------------------
# 维度 (d): 因子 / 策略 / 算法生命周期
# ---------------------------------------------------------------------------
def check_strategy_lifecycle(state: dict) -> dict:
    issues = []
    details = {}

    checks_to_watch = {
        "runtime.algorithms.alpha_graph": "AlphaGraph",
        "runtime.algorithms.factor_lifecycle": "因子生命周期",
        "runtime.algorithms.trading_pool": "交易池",
        "runtime.algorithms.risk_budget": "风险预算",
    }

    for cid, label in checks_to_watch.items():
        c = next((c for c in state.get("checks", []) if c["check_id"] == cid), None)
        if c is None:
            details[label] = "MISSING"
            issues.append(f"算法检查项缺失: {label}")
            continue
        details[label] = f"{c['status']}: {c['message'][:100]}"
        if c["status"] == "FAIL":
            issues.append(f"{label}失败: {c['message'][:100]}")

    # 因子详细状态
    factor_check = next(
        (c for c in state.get("checks", []) if c["check_id"] == "runtime.algorithms.factor_lifecycle"), None
    )
    if factor_check:
        lifecycle = factor_check.get("evidence", {}).get("lifecycle", {})
        active_count = sum(1 for v in lifecycle.values() if v == "ACTIVE")
        total_count = len(lifecycle)
        details["factors_active"] = f"{active_count}/{total_count}"
        inactive = {k: v for k, v in lifecycle.items() if v != "ACTIVE"}
        if inactive:
            issues.append(f"非活跃因子: {inactive}")

    # AlphaGraph 详细状态
    alpha_check = next(
        (c for c in state.get("checks", []) if c["check_id"] == "runtime.algorithms.alpha_graph"), None
    )
    if alpha_check:
        evidence = alpha_check.get("evidence", {})
        details["alpha_components"] = len(evidence.get("actual", []))
        details["alpha_missing"] = evidence.get("missing", [])
        details["alpha_invalid"] = evidence.get("invalid", [])
        if evidence.get("missing") or evidence.get("invalid"):
            issues.append(f"AlphaGraph组件异常: missing={evidence.get('missing')} invalid={evidence.get('invalid')}")

    return {"dimension": "strategy_lifecycle", "ok": len(issues) == 0, "issues": issues, "details": details}


# ---------------------------------------------------------------------------
# 频率管理
# ---------------------------------------------------------------------------
def update_frequency(current_freq: dict, all_ok: bool) -> dict:
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    current_freq["last_check_ts"] = now_iso
    current_freq["total_checks"] += 1

    if all_ok:
        current_freq["clean_streak"] += 1
        level = current_freq["level"]
        if level == "L0" and current_freq["clean_streak"] >= UPGRADE_THRESHOLD:
            current_freq["level"] = "L1"
            current_freq["clean_streak"] = 0
            current_freq["last_level_change"] = f"⬆ L0→L1 @ {now_iso}"
        elif level == "L1" and current_freq["clean_streak"] >= UPGRADE_THRESHOLD:
            current_freq["level"] = "L2"
            current_freq["clean_streak"] = 0
            current_freq["last_level_change"] = f"⬆ L1→L2 @ {now_iso}"
    else:
        old_level = current_freq["level"]
        current_freq["level"] = DOWNGRADE_TARGET
        current_freq["clean_streak"] = 0
        if old_level != DOWNGRADE_TARGET:
            current_freq["last_level_change"] = f"⬇ {old_level}→{DOWNGRADE_TARGET} @ {now_iso}"

    return current_freq


def summarize_results(results: list[dict]) -> str:
    all_ok = all(r["ok"] for r in results)
    total_issues = sum(len(r["issues"]) for r in results)
    dims = ", ".join(f"{r['dimension']}={ '✅' if r['ok'] else '❌'}" for r in results)

    if all_ok and total_issues == 0:
        return f"🟢 全部正常 | {dims}"
    elif all_ok:
        return f"🟡 有警告 ({total_issues}) | {dims}"
    else:
        return f"🔴 有异常 ({total_issues}) | {dims}"


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def should_run_now(freq: dict) -> bool:
    """频率门控：检查距上次运行是否已超过当前频率间隔。"""
    level = freq.get("level", "L0")
    interval_minutes = FREQ_LEVELS[level]["minutes"]
    last_ts = freq.get("last_check_ts")
    if last_ts is None:
        return True
    try:
        last_dt = datetime.fromisoformat(last_ts)
        elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
        return elapsed >= (interval_minutes * 60) - 5  # 5秒容差
    except (ValueError, TypeError):
        return True


def run_check() -> int:
    freq = load_freq_state()

    if not should_run_now(freq):
        # 未到执行时间，静默退出
        return 0

    state = load_supervisor_state()

    # 进程存活检查
    pid = state.get("pid") if state else None
    if pid:
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            print(f"❌ 北斗进程 PID={pid} 已终止")
            log_result({"ok": False, "error": f"process PID={pid} not alive", "pid": pid})
            return 1
    elif state is None:
        print("❌ 无法读取 supervisor-state.json，进程可能未运行")
        log_result({"ok": False, "error": "supervisor state not found"})
        return 1

    results = [
        check_order_flow(state),
        check_protection_coverage(state),
        check_module_health(state),
        check_strategy_lifecycle(state),
    ]

    all_ok = all(r["ok"] for r in results)
    freq = update_frequency(freq, all_ok)
    save_freq_state(freq)

    summary = summarize_results(results)
    level_info = FREQ_LEVELS[freq["level"]]

    print("=" * 72)
    print(f"北斗运营监控 | {datetime.now(timezone.utc).isoformat()}")
    print(f"频率: {level_info['label']} | 连续正常: {freq['clean_streak']}次 | 总计: {freq['total_checks']}次")
    print(f"状态: {summary}")
    print("-" * 72)
    for r in results:
        status = "✅" if r["ok"] else "❌"
        print(f"\n{status} 维度: {r['dimension']}")
        for k, v in r["details"].items():
            print(f"    {k}: {v}")
        for issue in r["issues"]:
            print(f"    ⚠️ {issue}")
    print("=" * 72)

    log_entry = {
        "ok": all_ok,
        "summary": summary,
        "freq_level": freq["level"],
        "clean_streak": freq["clean_streak"],
        "total_checks": freq["total_checks"],
        "dimensions": {
            r["dimension"]: {"ok": r["ok"], "issues": r["issues"], "details": r["details"]}
            for r in results
        },
    }
    log_result(log_entry)

    return 0 if all_ok else 1


if __name__ == "__main__":
    try:
        sys.exit(run_check())
    except Exception:
        print(f"❌ 监控脚本异常:\n{traceback.format_exc()}")
        sys.exit(2)
