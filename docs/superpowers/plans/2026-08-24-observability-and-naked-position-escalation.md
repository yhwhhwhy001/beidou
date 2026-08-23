# 可观测性修复与裸仓升级处置 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复四项诊断/安全缺陷:G5 基线污染不抗硬杀、对账消息标签错误、LOCKED 根因不可恢复(可观测性)+ 防抖 A/B 分流、裸仓紧急平仓安全网失效。

**Architecture:** 全部为现有系统的增量修改,不新增子系统。核心思路:①gap reason 从 `_assess_protection_coverage` 贯通到 incident 证据与 supervisor 检查;②防抖器把单一布尔位拆成 repairable/non-repairable 两路计数;③裸仓安全网把内存态连击计数器换成 PG 持久化的 `protection_exposure` 记录(重启不清除);④G5 场景污染基线前先写 journal,preflight 检测搁浅。

**Tech Stack:** Python 3.14、pytest、psycopg、PostgreSQL(宿主机 Homebrew PG 16)、launchd、bash。

**Spec:** `docs/superpowers/specs/2026-08-24-observability-and-naked-position-escalation-design.md`

## Global Constraints

以下约束逐字取自 spec,每个任务的隐含要求:

- **B 类(真故障)行为完全不变**:`lock_after` 数值(testnet 60 轮 / live 12 轮)本轮不调整,74% 根因不可恢复故无依据。
- **A/B 分类规则**:A = 所有 gap reason ∈ {`STOP_LOSS_QUANTITY_UNCOVERED`, `MISSING_SL`, `MISSING_TP`, `ORPHAN_PROTECTION_WITHOUT_VENUE_POSITION`};其余(含 `LOCAL_POSITION_WITHOUT_VENUE_FACT`、execution_fact、reconciliation、supervisor、realtime)全部按 B;A+B 并存按 B;**reason 缺失/无法分类按 B**(fail-closed)。
- **A 类永不 LOCKED,只 DEGRADED**;NO_NEW_RISK 承担全部 fail-closed 语义(它放行 REDUCE/FLATTEN/CANCEL,故引擎活着可修 A)。
- **告警级(1800s)永远先于处置级(7200s)**,给人留 90 分钟介入窗口;4-E3 不得先于 3c 落地。
- **E3 环境门**:`BEIDOU_NAKED_POSITION_AUTOCLOSE` — testnet 默认开启,live/canary 默认关闭且不得被默认值绕过。
- **逐品种处置,绝不使用账户级 `EMERGENCY_FLATTEN`**;平仓只走 `enqueue_reduce_only_market`(fenced executor 唯一写路径)。
- **不可逆动作前后各发一次告警**(AlertDispatcher incident,非 print)。
- 全量回归基线:**3581 passed**(commit b2193fb 实测)。每个任务提交后运行其测试文件;P9 跑全量。
- 工作树必须保持干净:每任务以 commit 收尾,否则引擎预检 git_worktree P0 拒绝启动、watchdog 会跳过自动重启。

---

## 文件结构

| 文件 | 职责 | 任务 |
| --- | --- | --- |
| `beidou_safety/execution/reconciliation.py` | 对账比较器(改标签) | P1 |
| `tests/unit/test_reconciliation_axis_labels.py` | 标签新测试 | P1 |
| `beidou_core/engine.py` | gap 详情暂存、exposure 记录、慢引信、stuck 标记 | P2 P4 P6 P7 |
| `beidou_launcher/runtime.py` | gap_detail 检查暴露 | P2 |
| `beidou_launcher/supervisor.py` | 事故消息带 severity、blocker 摘要、A/B 分类、LOCKED 快照、stuck 扫描 | P3 P5 P6 |
| `beidou_launcher/models.py` | HealthDebounce A/B 两路计数 | P5 |
| `beidou_core/alerts.py` | get_active_incidents 增加 description/gap_reasons 字段 | P2 |
| `deploy/beidou_watchdog.sh` | stuck 标记文件巡检告警 | P6 |
| `beidou_certification/g5_scenarios/engine/reconciliation_mismatch.py` | journal 写/删/恢复 | P8 |
| `beidou_launcher/preflight.py` | g5_journal 残留 P0 检查 | P8 |
| 各测试文件 | 见各任务 | — |

---

## Task 1: 对账 Balance mismatch 消息标签参数化

**Files:**
- Modify: `beidou_safety/execution/reconciliation.py`(compare 签名 ~line 169;balance 消息 ~line 323;compare_three_way 调用 ~line 567-600)
- Test: `tests/unit/test_reconciliation_axis_labels.py`(新建)

**Interfaces:**
- Consumes: 无(首个任务)
- Produces:
  - `ReconciliationEngine.compare(system_facts, exchange_facts, *, max_age, now, balance_rel_tolerance, stale_exempt_second, left_label: str = "system", right_label: str = "exchange") -> ReconciliationResult`
  - `compare_three_way(...)` 内部对三个 pair 传真实标签

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_reconciliation_axis_labels.py`:

```python
"""对账消息标签: 三条轴必须各用真实来源名, 不再硬编码 system/exchange。"""

from datetime import datetime, timezone
from decimal import Decimal

from beidou_safety.execution.reconciliation import ReconciliationEngine, ReconciliationStatus


def _facts(balance: str, source: str):
    return ReconciliationEngine.build_facts(  # type: ignore[attr-defined]
        account_id="acct", venue_id="BINANCE", balance=Decimal(balance),
        positions={}, source=source, captured_at=datetime.now(timezone.utc),
    )


def test_compare_uses_custom_labels_in_balance_mismatch() -> None:
    engine = ReconciliationEngine(balance_rel_tolerance=Decimal("0.01"))
    result = engine.compare(
        _facts("100", "A"), _facts("500", "B"),
        left_label="exchange", right_label="event_stream",
    )
    assert result.status is ReconciliationStatus.MISMATCHED
    line = next(d for d in result.differences if d.startswith("Balance mismatch"))
    assert "exchange=100" in line
    assert "event_stream=500" in line
    assert "system=" not in line


def test_compare_default_labels_stay_backward_compatible() -> None:
    engine = ReconciliationEngine(balance_rel_tolerance=Decimal("0.01"))
    result = engine.compare(_facts("100", "A"), _facts("500", "B"))
    line = next(d for d in result.differences if d.startswith("Balance mismatch"))
    assert "system=100" in line and "exchange=500" in line
```

> 注:`build_facts` 是本仓库 `AccountFactSnapshot` 的既有构造入口;若该测试文件运行时发现构造方式与真实代码不符,以 `tests/unit/test_reconciliation_tolerance.py:14-20` 的 `_facts` helper 为准改写(它已经能造出可用快照)。

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/unit/test_reconciliation_axis_labels.py -v`
Expected: `test_compare_uses_custom_labels_in_balance_mismatch` FAIL(输出仍是 `system=100 exchange=500`)

- [ ] **Step 3: 实现**

`beidou_safety/execution/reconciliation.py`:

1. compare 签名(~line 169)增加两个关键字参数:

```python
    def compare(
        system_facts: AccountFactSnapshot | None,
        exchange_facts: AccountFactSnapshot | None,
        *,
        max_age: timedelta = timedelta(seconds=30),
        now: datetime | None = None,
        balance_rel_tolerance: Decimal = Decimal("0.0001"),
        stale_exempt_second: bool = False,
        left_label: str = "system",
        right_label: str = "exchange",
    ) -> ReconciliationResult:
```

2. balance 消息(~line 323)改用参数:

```python
        if bal_diff > max_tolerance:
            diffs.append(
                f"Balance mismatch: {left_label}={system_facts.balance.amount} "
                f"{right_label}={exchange_facts.balance.amount} "
                f"diff={float(bal_diff):.6f} tolerance={float(max_tolerance):.6f}"
            )
```

3. compare_three_way 的 pair_results(~line 567-600)按对传标签:

```python
        pair_results = (
            (
                "system/exchange",
                ReconciliationEngine.compare(
                    system_facts, exchange_facts,
                    max_age=max_age, now=checked_at,
                    balance_rel_tolerance=balance_rel_tolerance,
                ),
            ),
            (
                "system/event_stream",
                ReconciliationEngine.compare(
                    system_facts, event_facts,
                    max_age=max_age, now=checked_at,
                    balance_rel_tolerance=balance_rel_tolerance,
                    stale_exempt_second=True,
                    left_label="system", right_label="event_stream",
                ),
            ),
            (
                "exchange/event_stream",
                ReconciliationEngine.compare(
                    exchange_facts, event_facts,
                    max_age=max_age, now=checked_at,
                    balance_rel_tolerance=balance_rel_tolerance,
                    stale_exempt_second=True,
                    left_label="exchange", right_label="event_stream",
                ),
            ),
        )
```

- [ ] **Step 4: 运行通过**

Run: `python3 -m pytest tests/unit/test_reconciliation_axis_labels.py tests/unit/test_reconciliation_tolerance.py tests/unit/test_reconciliation_fail_closed_gaps.py -q`
Expected: 全 PASS(兼容性测试证明 system/exchange 轴输出不变)

- [ ] **Step 5: Commit**

```bash
git add beidou_safety/execution/reconciliation.py tests/unit/test_reconciliation_axis_labels.py
git commit -m "fix(recon): Balance mismatch 标签参数化, 第三条轴不再误标 system/exchange"
```

---

## Task 2: gap reason 贯通(引擎 → 事故 → 监督器证据)

**Files:**
- Modify: `beidou_core/engine.py`(`_update_protection_fact` ~line 4246,非 clean 分支末尾;`__init__` ~line 1606 附近加字段)
- Modify: `beidou_core/alerts.py`(`get_active_incidents` ~line 399)
- Modify: `beidou_launcher/runtime.py`(collect_runtime_checks 内,incidents 检查之后 ~line 690)
- Test: `tests/unit/test_protection_gap_reason_surface.py`(新建)

**Interfaces:**
- Consumes: `engine._alerts.get_active_incidents() -> list[dict]`(P1 无关,直接消费现有)
- Produces:
  - `engine._last_protection_gap_detail: list[dict]`(元素 `{"symbol": str, "reason": str}`,最多 6 条)
  - incident dict 新增字段 `"gap_reasons": [str]` 与 `"description": str`
  - 新检查 `CheckResult(check_id="runtime.safety.protection_gap_detail", ...)` 出现在 runtime checks,evidence 含 `{"gaps": [...], "repairable": bool}`

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_protection_gap_reason_surface.py`:

```python
"""gap reason 必须从引擎贯通到 incident 与 runtime 检查 (A/B 分流的输入)。"""

from beidou_core.engine import AutonomousEngine
from beidou_launcher.runtime import collect_runtime_checks


def test_gap_reasons_appear_in_incident_and_runtime_check() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._last_protection_gap_detail = [
        {"symbol": "BTCUSDT", "reason": "STOP_LOSS_QUANTITY_UNCOVERED"}
    ]
    # collect_runtime_checks 内部会读 engine._alerts —— 用最小替身隔离
    class _Alerts:
        def get_active_incidents(self):
            return []
        def get_delivery_health(self):
            return {"critical_pending": 0, "dead_letter": 0, "unknown": 0,
                    "pending": 0, "configured": False}
    engine._alerts = _Alerts()

    checks = collect_runtime_checks(engine)
    gap_checks = [c for c in checks if c.check_id == "runtime.safety.protection_gap_detail"]
    assert gap_checks, "runtime checks 必须包含 protection_gap_detail"
    evidence = gap_checks[0].evidence
    assert evidence["gaps"][0]["reason"] == "STOP_LOSS_QUANTITY_UNCOVERED"
    assert evidence["repairable"] is True


def test_unknown_reason_defaults_repairable_false() -> None:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._last_protection_gap_detail = [{"symbol": "X", "reason": "LOCAL_POSITION_WITHOUT_VENUE_FACT"}]
    class _Alerts:
        def get_active_incidents(self): return []
        def get_delivery_health(self): return {}
    engine._alerts = _Alerts()
    checks = collect_runtime_checks(engine)
    gap_checks = [c for c in checks if c.check_id == "runtime.safety.protection_gap_detail"]
    assert gap_checks and gap_checks[0].evidence["repairable"] is False
```

> 注:若 `collect_runtime_checks` 需要更多 engine 属性,按该函数实际引用逐项补齐最小替身(参考 `tests/unit/test_supervisor_health_contract.py` 的引擎替身写法)。

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/unit/test_protection_gap_reason_surface.py -v`
Expected: FAIL(无 `protection_gap_detail` 检查 / 无 gap_reasons 字段)

- [ ] **Step 3: 实现**

`beidou_core/engine.py`:

1. `__init__` 中(紧挨 `_protection_issues` 初始化附近)加:

```python
        # gap reason 贯通 (可观测性修复): 最近一次保护事实评估的缺口
        # 明细, 供 runtime 检查与 supervisor A/B 分流消费。最多 6 条,
        # 每条 {"symbol": str, "reason": str}。
        self._last_protection_gap_detail: list[dict[str, str]] = []
```

2. `_update_protection_fact` 的非 clean 分支(~line 4234-4262),在限频打印后补:

```python
            # 最近缺口明细持久于内存供 runtime/supervisor 消费
            # (A/B 分流的唯一 reason 来源)。
            if _gap_detail:
                self._last_protection_gap_detail = list(_gap_detail)
```

3. clean 分支(~line 4228-4233)清空:

```python
            self._last_protection_gap_detail = []
```

`beidou_core/alerts.py`:

1. `send_incident`(~line 63)签名增加关键字参数 `gap_reasons: list[str] | None = None`,并在创建/更新 Incident 时带上:

```python
    def send_incident(
        self,
        severity: AlertSeverity,
        title: str,
        description: str,
        auto_action: AutoAction | None = None,
        category: str = "runtime",
        gap_reasons: list[str] | None = None,
    ) -> Incident:
```

创建 Incident 处(`Incident(...)` 调用点,依其字段列表)增加 `gap_reasons=list(gap_reasons or [])`;更新已有事故的分支(de-dup 路径)同步 `existing_inc.gap_reasons = list(gap_reasons or [])`。若 Incident 构造不接受该关键字,在其定义处(实测 `beidou_observability/monitoring/contracts.py:176` 附近)加字段 `gap_reasons: list[str] = field(default_factory=list)`。

2. `get_active_incidents`(~line 399)dict 补两个字段:

```python
                {
                    "incident_id": i.incident_id,
                    "severity": i.severity.value,
                    "title": i.title,
                    "status": i.status.value,
                    "detected_at": i.detected_at.isoformat(),
                    "description": str(getattr(i, "description", "") or "")[:300],
                    "gap_reasons": [str(r) for r in (getattr(i, "gap_reasons", None) or [])],
                }
```

`beidou_core/engine.py` — `_block_unowned_protection_orders`(~line 4160)的 incident 发送带上当前缺口明细(这正是 P5 分类器读 `ev["incidents"][i]["gap_reasons"]` 的数据来源):

```python
        _gap_reasons = [
            str(g.get("reason", ""))
            for g in (getattr(self, "_last_protection_gap_detail", None) or [])
            if g.get("reason")
        ]
        self._alerts.send_incident(
            AlertSeverity.CRITICAL,
            "Protection ownership unknown",
            f"Conditional orders lack durable owner mapping: {sorted(order_ids)}",
            category="protection",
            gap_reasons=_gap_reasons,
        )
```

`beidou_launcher/runtime.py` — incidents 检查追加之后(~line 690,`checks.append(...)` 之后):

```python
    # gap reason 贯通: 保护缺口明细单独成检查, 供 supervisor A/B 分流。
    _gap_detail = getattr(engine, "_last_protection_gap_detail", None) or []
    _repairable = (
        bool(_gap_detail)
        and all(
            str(g.get("reason", ""))
            in {"STOP_LOSS_QUANTITY_UNCOVERED", "MISSING_SL", "MISSING_TP",
                "ORPHAN_PROTECTION_WITHOUT_VENUE_POSITION"}
            for g in _gap_detail
        )
    )
    checks.append(
        CheckResult(
            check_id="runtime.safety.protection_gap_detail",
            name="保护缺口明细",
            status=CheckStatus.WARN if _gap_detail else CheckStatus.PASS,
            severity=CheckSeverity.P1,
            message=(
                f"保护缺口: {_gap_detail}" if _gap_detail else "无保护缺口"
            ),
            evidence={"gaps": _gap_detail, "repairable": _repairable},
        )
    )
```

- [ ] **Step 4: 运行通过**

Run: `python3 -m pytest tests/unit/test_protection_gap_reason_surface.py tests/unit/test_alerts.py -q`
Expected: 全 PASS(若 test_alerts.py 断言 incident dict 键集合,同步补 `description`/`gap_reasons` 断言)

- [ ] **Step 5: Commit**

```bash
git add beidou_core/engine.py beidou_core/alerts.py beidou_launcher/runtime.py tests/unit/test_protection_gap_reason_surface.py
git commit -m "feat(observability): gap reason 贯通到 incident 与 runtime 检查, 为 A/B 分流提供判据"
```

---

## Task 3: blocker 摘要修复 + LOCKED 完整快照

**Files:**
- Modify: `beidou_launcher/supervisor.py`(`summarize_blockers` ~line 71;`_apply_debounce_action` LOCKED 分支 ~line 1048-1058)
- Modify: `beidou_launcher/runtime.py`(incidents 检查 message,~line 688,携带 severity 的紧凑形式)
- Test: `tests/unit/test_supervisor_blocker_aggregation.py`(扩展)
- Test: `tests/unit/test_locked_snapshot.py`(新建)

**Interfaces:**
- Consumes: P2 的 incident dict(`severity`/`description`/`gap_reasons` 字段)
- Produces:
  - `summarize_blockers` 对 `runtime.health.incidents` 优先展示 CRITICAL/HIGH 事故,输出含 severity
  - LOCKED 时写 `evidence/locked-<ts>.json`(路径可用 `BEIDOU_LOCKED_SNAPSHOT_DIR` 覆盖,测试用)

- [ ] **Step 1: 写失败测试**

扩展 `tests/unit/test_supervisor_blocker_aggregation.py`(追加):

```python
def test_incidents_summary_prefers_critical_over_first_item() -> None:
    from beidou_launcher.models import CheckResult, CheckSeverity, CheckStatus
    from beidou_launcher.supervisor import summarize_blockers
    blocker = CheckResult(
        check_id="runtime.health.incidents", name="活动事故",
        status=CheckStatus.FAIL, severity=CheckSeverity.P0,
        message="活动事故",
        evidence={"incidents": [
            {"incident_id": "inc-1-realtime", "severity": "WARNING",
             "title": "Realtime tick error", "status": "DETECTED"},
            {"incident_id": "inc-2-reconciliation", "severity": "CRITICAL",
             "title": "Reconciliation blocked", "status": "DETECTED"},
        ]},
    )
    text = summarize_blockers([blocker])
    assert "CRITICAL" in text
    assert "Reconciliation blocked" in text
    # 首项 WARNING 不得再占据摘要主体
    assert "Realtime tick error" not in text
```

新建 `tests/unit/test_locked_snapshot.py`:

```python
"""LOCKED 时必须落完整未截断的阻断快照 (74% 根因不可恢复的修复)。"""

import json
import os

from beidou_launcher.models import CheckResult, CheckSeverity, CheckStatus


def test_locked_snapshot_written_with_full_blockers(tmp_path, monkeypatch):
    from beidou_launcher import supervisor as sup

    class _Engine:
        pass

    fake = {"state": "LOCKED", "path": str(tmp_path), "runs": []}
    # 最小 harness: 直接调用被抽出的纯函数
    from beidou_launcher.supervisor import _write_locked_snapshot
    blockers = [
        CheckResult(
            check_id="runtime.health.incidents", name="活动事故",
            status=CheckStatus.FAIL, severity=CheckSeverity.P0,
            message="活动事故",
            evidence={"incidents": [
                {"incident_id": "inc-x-reconciliation", "severity": "CRITICAL",
                 "title": "Reconciliation blocked", "status": "DETECTED",
                 "description": "Balance mismatch: system=1 exchange=10736.5 "
                                "diff=10735.5 tolerance=107.36"},
            ]},
        )
    ]
    path = _write_locked_snapshot(blockers, base_dir=tmp_path, now=1700000000.0)
    assert os.path.exists(path)
    payload = json.loads(path.read_text())
    assert "Balance mismatch" in json.dumps(payload["blockers"], ensure_ascii=False)
    assert "10736.5" in json.dumps(payload["blockers"], ensure_ascii=False)  # 未被截断
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/unit/test_supervisor_blocker_aggregation.py tests/unit/test_locked_snapshot.py -q`
Expected: FAIL(`_write_locked_snapshot` 不存在;摘要仍显示首项)

- [ ] **Step 3: 实现**

`beidou_launcher/supervisor.py`:

1. `summarize_blockers` 内,聚合循环之前加 incidents 特殊处理:

```python
    # 可观测性修复: incidents 检查的 message 是事故列表的 repr,
    # 旧逻辑取列表首项 —— 首项常是 WARNING 级非阻断事故, 导致
    # 14/19 次 LOCKED 的真实致死原因被 message[:80] 截断吞掉。
    # 改为优先展示 severity 最高的那条 (CRITICAL/HIGH 才是阻断源)。
    _PRIORITY = {"LOCKDOWN": 4, "CRITICAL": 3, "P0": 3, "HIGH": 2, "P1": 2, "WARNING": 1, "P2": 0}
    for item in blockers:
        if str(getattr(item, "check_id", "")) != "runtime.health.incidents":
            continue
        incs = ((item.evidence or {}).get("incidents") or []) if isinstance(getattr(item, "evidence", None), dict) else []
        if not incs:
            continue
        inc = max(incs, key=lambda i: _PRIORITY.get(str(i.get("severity", "")).upper(), 0))
        item.message = (
            f"活动事故(首列最高级): [{inc.get('severity')}] {inc.get('title')} "
            f"{str(inc.get('description', ''))[:120]}"
        )
```

2. LOCKED 分支(~line 1048-1058),`_send_supervisor_alert` 之前:

```python
        if self.report.supervisor_state != "LOCKED":
            await self._fail_closed(...)
            self.report.supervisor_state = "LOCKED"
            self._last_blocker_fingerprint = summarize_blockers(persistent_blockers)
            _write_locked_snapshot(persistent_blockers)  # 新增
            self._send_supervisor_alert("LOCKED", persistent_blockers)
```

3. 文件末尾(或 `summarize_blockers` 附近)新增纯函数:

```python
def _write_locked_snapshot(
    blockers: list[CheckResult],
    *,
    base_dir: Path | None = None,
    now: float | None = None,
) -> Path:
    """LOCKED 时落未截断的完整阻断快照, 修复根因不可恢复问题。"""
    import json as _json
    import time as _time
    ts = now if now is not None else _time.time()
    base = Path(base_dir) if base_dir is not None else (
        Path(os.environ.get("BEIDOU_LOCKED_SNAPSHOT_DIR", "evidence"))
    )
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"locked-{ts:.0f}.json"
    payload = {
        "locked_at": ts,
        "blockers": [b.to_dict() for b in blockers],
    }
    path.write_text(_json.dumps(payload, ensure_ascii=False, indent=2))
    return path
```

> `CheckResult.to_dict()` 已存在(`models.py:49`),含 evidence 完整内容。

- [ ] **Step 4: 运行通过**

Run: `python3 -m pytest tests/unit/test_supervisor_blocker_aggregation.py tests/unit/test_locked_snapshot.py -q`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add beidou_launcher/supervisor.py tests/unit/test_supervisor_blocker_aggregation.py tests/unit/test_locked_snapshot.py
git commit -m "fix(observability): LOCKED 摘要优先展示真实阻断事故, 并落完整快照到 evidence/"
```

---

## Task 4: protection_exposure 持久化裸露时钟 + E1/E2 重构

**Files:**
- Modify: `beidou_core/engine.py`(新增 `_persist_protection_exposure` / `_clear_protection_exposure`;重构 `_maybe_emergency_close_unprotectable` ~line 9842)
- Test: `tests/unit/test_protection_exposure_ledger.py`(新建)

**Interfaces:**
- Consumes: `self._store`(有 `_write_record(record_type, record_id, payload)` / `_delete_record(record_type, record_id)` / `_get_record(record_type, record_id)` 的 PG 或测试替身);`self._position_generation`
- Produces:
  - `engine._persist_protection_exposure(symbol, generation, reason, *, now) -> dict` 返回写入后的记录
  - `engine._clear_protection_exposure(symbol) -> None`
  - `_maybe_emergency_close_unprotectable(pos_id, symbol, pp, *, now: Callable[[], float] = time.time)`(签名向后兼容)
  - record 形状:`record_type='protection_exposure'`, `record_id=f"exposure:{symbol}"`, payload 含 `symbol` / `position_generation` / `unprotectable_since` / `last_reason` / `attempts`

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_protection_exposure_ledger.py`:

```python
"""protection_exposure 记录: 重启不清除、代数变化清零、E2 需跨度≥60s。"""

import time

from beidou_core.engine import AutonomousEngine


class _MemStore:
    """最小 durable 替身: 记录活在其内部 dict, 模拟跨重启保留。"""

    def __init__(self):
        self.records: dict[tuple[str, str], dict] = {}

    def _get_record(self, record_type: str, record_id: str):
        return self.records.get((record_type, record_id))

    def _write_record(self, record_type: str, record_id: str, payload: dict, **kw) -> bool:
        self.records[(record_type, record_id)] = dict(payload)
        return True

    def _delete_record(self, record_type: str, record_id: str) -> None:
        self.records.pop((record_type, record_id), None)


def _engine() -> AutonomousEngine:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._store = _MemStore()
    engine._position_generation = {"BTCUSDT": 5}
    engine._sl_unprotectable_streak = {}
    engine._policy_id_active = "pol-1"
    engine._policy_version = "v1"
    engine._policy_signature = "sig"
    engine._position_projection = {"BTCUSDT": {"signed_quantity": "-0.0008"}}
    return engine


def test_exposure_persists_and_survives_reinstantiation() -> None:
    engine = _engine()
    rec = engine._persist_protection_exposure("BTCUSDT", "VENUE_REJECT_-2021", now=time.time)
    assert rec["attempts"] == 1 and rec["position_generation"] == 5

    # 模拟重启: 新实例共享同一 store
    engine2 = _engine()
    engine2._store = engine._store
    rec2 = engine2._persist_protection_exposure("BTCUSDT", "VENUE_REJECT_-2021", now=time.time)
    assert rec2["attempts"] == 2
    assert rec2["unprotectable_since"] == rec["unprotectable_since"]


def test_exposure_cleared_on_generation_change() -> None:
    engine = _engine()
    engine._persist_protection_exposure("BTCUSDT", "X", now=time.time)
    engine._position_generation["BTCUSDT"] = 6
    rec = engine._persist_protection_exposure("BTCUSDT", "Y", now=time.time)
    assert rec["attempts"] == 1 and rec["position_generation"] == 6
    assert rec["last_reason"] == "Y"


def test_clear_removes_record() -> None:
    engine = _engine()
    engine._persist_protection_exposure("BTCUSDT", "X", now=time.time)
    engine._clear_protection_exposure("BTCUSDT")
    assert engine._store._get_record("protection_exposure", "exposure:BTCUSDT") is None
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/unit/test_protection_exposure_ledger.py -v`
Expected: FAIL(`_persist_protection_exposure` 不存在)

- [ ] **Step 3: 实现**

`beidou_core/engine.py`,在 `_maybe_emergency_close_unprotectable` 之前新增:

```python
    def _persist_protection_exposure(
        self,
        symbol: str,
        reason: str,
        *,
        now: Any = None,
    ) -> dict[str, Any]:
        """持久化"保护无法建立"裸露时钟 (重启不清除)。

        与内存态连击计数不同, 该记录是 3c stuck 告警与 4-E3 慢引信的
        唯一真相源。position_generation 变化时重新起算 —— 旧仓的账
        不得杀新仓。
        """
        import time as _time
        _clock = now if callable(now) else _time.time
        _ts = _clock()
        _store = getattr(self, "_store", None)
        if _store is None:
            return {}
        _gen = int(getattr(self, "_position_generation", {}).get(symbol, 0) or 0)
        key = f"exposure:{symbol}"
        old = None
        _get = getattr(_store, "_get_record", None)
        if callable(_get):
            try:
                old = _get("protection_exposure", key)
            except Exception:
                old = None
        if old and int((old.get("payload") if isinstance(old, dict) and "payload" in old else old).get("position_generation", 0) or 0) != _gen:
            self._clear_protection_exposure(symbol)
            old = None
        attempts = int((old or {}).get("attempts", 0)) + 1 if old else 1
        payload = {
            "symbol": symbol,
            "position_generation": _gen,
            "unprotectable_since": (old or {}).get("unprotectable_since", _ts),
            "last_reason": str(reason)[:120],
            "attempts": attempts,
        }
        _write = getattr(_store, "_write_record", None)
        if callable(_write):
            try:
                _write("protection_exposure", key, payload)
            except Exception:
                pass
        return payload

    def _clear_protection_exposure(self, symbol: str) -> None:
        _store = getattr(self, "_store", None)
        if _store is None:
            return
        _delete = getattr(_store, "_delete_record", None)
        if callable(_delete):
            try:
                _delete("protection_exposure", f"exposure:{symbol}")
            except Exception:
                pass
```

重构 `_maybe_emergency_close_unprotectable` 开头(替换现有 streak 段,~line 9851-9855):

```python
        import time as _time
        now = now if callable(now) else _time.time
        rec = self._persist_protection_exposure(symbol, "SL_UNPROTECTABLE", now=now)
        streak = int(rec.get("attempts", 0) or 0)
        since = float(rec.get("unprotectable_since", 0.0) or 0.0)
        if streak < 3:
            print(f"[nearline] ⚠️ {symbol}: stop unprotectable x{streak}/3 (waiting for confirmation)")
            return False
        # E2 跨度下限: 3 次确认必须跨 ≥60s, 防紧凑重试一秒连打三枪。
        if now() - since < 60.0:
            print(f"[nearline] ⚠️ {symbol}: unprotectable x{streak} but span<60s, hold")
            return False
```

其余(签名策略门、数量取投影、enqueue)保持原样。成功路径(现有 `_sl_unprotectable_streak.pop`)替换为:

```python
        if ok:
            self._clear_protection_exposure(symbol)
            print(...)
            return True
```

函数签名补 `*, now: Any = None` 参数(现有三个调用点 10657/10859/10922 无需改动,靠默认值)。

- [ ] **Step 4: 运行通过**

Run: `python3 -m pytest tests/unit/test_protection_exposure_ledger.py tests/unit/test_final83g_protection_gap.py -q`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add beidou_core/engine.py tests/unit/test_protection_exposure_ledger.py
git commit -m "fix(safety): 裸仓时钟持久化 (protection_exposure), 重启不再清零连击; E2 加 60s 跨度下限"
```

---

## Task 5: HealthDebounce A/B 两路计数 + supervisor 分类

**Files:**
- Modify: `beidou_launcher/models.py`(`HealthDebounce.feed` ~line 159)
- Modify: `beidou_launcher/supervisor.py`(`_REPAIRABLE_PROTECTION_REASONS` 常量;`_health_feed` 判定点 ~line 1412-1415;`runtime.safety.protection_gap_detail` 加入 `_TRANSIENT_CHECK_IDS`? —— **不**,它必须参与 debounce 但标记 repairable)
- Test: `tests/unit/test_debounce_repairable_split.py`(新建)

**Interfaces:**
- Consumes: P2 的 `runtime.safety.protection_gap_detail` 检查(evidence `{"gaps": [...], "repairable": bool}`);P4 无关
- Produces: `HealthDebounce.feed(has_persistent_blocker: bool, *, repairable: bool = False, now: float | None = None) -> str | None`

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_debounce_repairable_split.py`:

```python
"""A 类(可修复)阻断永不 LOCKED; B 类行为与旧版逐位一致; A+B 并存按 B。"""

from beidou_launcher.models import HealthDebounce


def _feed_n(deb: HealthDebounce, n: int, repairable: bool) -> None:
    for _ in range(n):
        deb.feed(True, repairable=repairable)


def test_repairable_blocks_never_lock() -> None:
    deb = HealthDebounce(degrade_after=6, lock_after=12, window_seconds=200.0)
    _feed_n(deb, 100, repairable=True)
    assert deb.feed(True, repairable=True) is None  # 永不 LOCKED
    # 恢复路径不受影响
    for _ in range(3):
        deb.feed(False, repairable=False)
    assert deb.feed(False, repairable=False) == "RUNNING"


def test_non_repairable_locks_at_threshold_unchanged() -> None:
    deb = HealthDebounce(degrade_after=6, lock_after=12, window_seconds=200.0)
    for i in range(11):
        deb.feed(True, repairable=False)
    assert deb.feed(True, repairable=False) == "LOCKED"  # 第 12 轮, 旧语义不变


def test_mixed_blocks_count_as_non_repairable() -> None:
    deb = HealthDebounce(degrade_after=6, lock_after=12, window_seconds=200.0)
    _feed_n(deb, 11, repairable=True)
    # 第 12 轮混入 B 类 → 按 B 计, 立即 LOCKED
    assert deb.feed(True, repairable=False) == "LOCKED"


def test_repairable_only_degrades_after_threshold() -> None:
    deb = HealthDebounce(degrade_after=6, lock_after=12, window_seconds=200.0)
    for i in range(5):
        deb.feed(True, repairable=True)
    assert deb.feed(True, repairable=True) == "DEGRADED"


def test_classifier_accepts_repairable_gap_and_rejects_unknown() -> None:
    """_classify_repairable: A 类 reason → True; 未知/缺失 → False (按 B)。"""
    from beidou_core.engine import AutonomousEngine
    from beidou_launcher.models import CheckResult, CheckSeverity, CheckStatus
    from beidou_launcher.supervisor import _classify_repairable

    supervisor = AutonomousEngine.__new__(AutonomousEngine)  # 仅作绑定容器
    supervisor._classify_repairable = _classify_repairable.__get__(supervisor, type(supervisor))

    a_only = CheckResult(
        check_id="runtime.safety.protection_gap_detail", name="x",
        status=CheckStatus.WARN, severity=CheckSeverity.P1, message="x",
        evidence={"gaps": [{"symbol": "BTCUSDT", "reason": "STOP_LOSS_QUANTITY_UNCOVERED"}],
                  "repairable": True},
    )
    assert supervisor._classify_repairable([a_only]) is True

    b_gap = CheckResult(
        check_id="runtime.safety.protection_gap_detail", name="x",
        status=CheckStatus.WARN, severity=CheckSeverity.P1, message="x",
        evidence={"gaps": [{"symbol": "BTCUSDT",
                            "reason": "LOCAL_POSITION_WITHOUT_VENUE_FACT"}],
                  "repairable": False},
    )
    assert supervisor._classify_repairable([b_gap]) is False

    unknown = CheckResult(
        check_id="runtime.health.incidents", name="x",
        status=CheckStatus.FAIL, severity=CheckSeverity.P0, message="x",
        evidence={"incidents": [{"incident_id": "i", "severity": "CRITICAL",
                                 "title": "t", "status": "DETECTED"}]},
    )
    assert supervisor._classify_repairable([unknown]) is False
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/unit/test_debounce_repairable_split.py -v`
Expected: FAIL(feed 不接受 repairable)

- [ ] **Step 3: 实现**

`beidou_launcher/models.py` — 重写 `HealthDebounce.feed`:

```python
    def feed(
        self,
        has_persistent_blocker: bool,
        *,
        repairable: bool = False,
        now: float | None = None,
    ) -> str | None:
        """记录一次检查结果, 返回建议状态变更或 None(保持不变)。

        A/B 分流 (2026-08-24): repairable=True 的阻断 (新开仓保护待建等
        引擎活着能修的情形) 只计入 DEGRADED 路径, 永不计入 LOCKED ——
        LOCKED 是进程退出, 会让"待建保护"变成永久裸奔 (见 spec D-3)。
        B 类维持原语义逐位不变。
        """
        import time

        ts = now if now is not None else time.monotonic()
        self.window.append((ts, has_persistent_blocker, bool(repairable)))

        cutoff = ts - self.window_seconds
        while self.window and self.window[0][0] < cutoff:
            self.window.popleft()

        recent = [blocked for _, blocked, _ in self.window]
        recent_b = [blocked for _, blocked, rep in self.window if not rep]

        if len(recent) < self.degrade_after:
            return None

        # LOCKED: 连续 lock_after 次 **B 类**全部持久阻断
        if len(recent_b) >= self.lock_after and all(recent_b[-self.lock_after :]):
            return "LOCKED"

        # DEGRADED: 连续 degrade_after 次全部持久阻断 (A/B 都算)
        if all(recent[-self.degrade_after :]):
            return "DEGRADED"

        if len(recent) >= self.recover_after and not any(recent[-self.recover_after :]):
            return "RUNNING"

        return None
```

`beidou_launcher/supervisor.py`:

1. 模块级常量(类定义外,`summarize_blockers` 附近):

```python
_REPAIRABLE_PROTECTION_REASONS: frozenset[str] = frozenset(
    {
        "STOP_LOSS_QUANTITY_UNCOVERED",
        "MISSING_SL",
        "MISSING_TP",
        "ORPHAN_PROTECTION_WITHOUT_VENUE_POSITION",
    }
)
```

2. 判定点(~line 1412-1415)替换为:

```python
            persistent_blockers = [b for b in blockers if b.check_id not in self._TRANSIENT_CHECK_IDS]
            has_persistent = bool(persistent_blockers)
            repairable = self._classify_repairable(persistent_blockers)
            debounce_action = self._health_debounce.feed(has_persistent, repairable=repairable)
```

3. 新增分类方法:

```python
    def _classify_repairable(self, blockers: list[CheckResult]) -> bool:
        """A/B 分类: 全部阻断均为可修复缺口 → True (永不 LOCKED)。

        reason 缺失或含任何不可修复项 → False (fail-closed 按 B)。
        """
        if not blockers:
            return False
        reasons: list[str] = []
        for b in blockers:
            ev = getattr(b, "evidence", None) or {}
            if not isinstance(ev, dict):
                return False
            # gap_detail 检查直接携带 reason
            for g in ev.get("gaps") or []:
                if isinstance(g, dict) and g.get("reason"):
                    reasons.append(str(g["reason"]))
            # incidents 证据中的 gap_reasons
            for inc in ev.get("incidents") or []:
                if isinstance(inc, dict):
                    reasons.extend(str(r) for r in (inc.get("gap_reasons") or []))
            # 非 protection 语义的阻断 (execution_fact/reconciliation/
            # supervisor/realtime) 没有 reason → 严格按 B
            if not ev.get("gaps") and not any(
                isinstance(i, dict) and i.get("gap_reasons") for i in (ev.get("incidents") or [])
            ):
                return False
        return bool(reasons) and all(r in _REPAIRABLE_PROTECTION_REASONS for r in reasons)
```

- [ ] **Step 4: 运行通过**

Run: `python3 -m pytest tests/unit/test_debounce_repairable_split.py tests/unit/test_supervisor_health_contract.py tests/unit/test_supervisor_boundary_completeness.py -q`
Expected: 全 PASS(若 health_contract 对 feed 的旧调用形式有断言,同步传 repairable=False)

- [ ] **Step 5: Commit**

```bash
git add beidou_launcher/models.py beidou_launcher/supervisor.py tests/unit/test_debounce_repairable_split.py
git commit -m "feat(fail-closed): 防抖 A/B 分流 —— 可修复缺口永不 LOCKED, 真故障语义逐位不变"
```

---

## Task 6: stuck 标记 + watchdog 卡死告警

**Files:**
- Modify: `beidou_core/engine.py`(新增 `_update_stuck_marker`;调用点放在 nearline 主循环末尾或 `_update_protection_fact` 之后)
- Modify: `beidou_launcher/supervisor.py`(监控循环调用 `engine._update_stuck_marker`)
- Modify: `deploy/beidou_watchdog.sh`(读 stuck 文件并 notify)
- Test: `tests/unit/test_stuck_marker.py`(新建)

**Interfaces:**
- Consumes: P4 的 `protection_exposure` 记录(经 `self._store._records("protection_exposure")` 或 `_get_record`);watchdog 的 `STATE_DIR`
- Produces:
  - `engine._update_stuck_marker(*, now: float | None = None, state_dir: str | None = None) -> bool`(返回是否有卡死)
  - 标记文件 `~/Library/Application Support/beidou-watchdog/stuck`,JSON:`{"stuck": true, "items": [{"symbol","reason","unprotectable_since","age_s"}], "updated_at": ts}`
  - watchdog 每轮检查该文件,age_s≥1800 即 notify

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_stuck_marker.py`:

```python
"""卡死标记: A 类裸露 ≥1800s 写 stuck, 消除时删除; 引擎侧纯函数可测。"""

import json
import os

from beidou_core.engine import AutonomousEngine


class _ExposureStore:
    def __init__(self, exposures: list[dict] | None = None):
        self._rows = list(exposures or [])

    def _records(self, record_type: str) -> list[dict]:
        assert record_type == "protection_exposure"
        return [{"payload": dict(r)} for r in self._rows]


def test_stuck_marker_written_when_exposure_exceeds_threshold(tmp_path):
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._store = _ExposureStore([{
        "symbol": "BTCUSDT", "unprotectable_since": 1000.0, "last_reason": "X",
    }])
    state_dir = str(tmp_path)
    ok = engine._update_stuck_marker(now=1000.0 + 1800.0, state_dir=state_dir)
    assert ok is True
    p = os.path.join(state_dir, "stuck")
    assert os.path.exists(p)
    data = json.loads(open(p).read())
    assert data["items"][0]["symbol"] == "BTCUSDT"


def test_stuck_marker_removed_when_exposure_clears(tmp_path):
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._store = _ExposureStore([{
        "symbol": "BTCUSDT", "unprotectable_since": 1000.0, "last_reason": "X",
    }])
    state_dir = str(tmp_path)
    p = os.path.join(state_dir, "stuck")
    open(p, "w").write("{}")
    engine._store._rows = []
    engine._update_stuck_marker(now=1000.0, state_dir=state_dir)
    assert not os.path.exists(p)
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/unit/test_stuck_marker.py -v`
Expected: FAIL(`_update_stuck_marker` 不存在)

- [ ] **Step 3: 实现**

`beidou_core/engine.py`:

```python
    def _update_stuck_marker(
        self,
        *,
        now: float | None = None,
        state_dir: str | None = None,
    ) -> bool:
        """扫描 protection_exposure, ≥1800s 者写 watchdog stuck 标记。

        这是 A 类永不 LOCKED 的配套告警 (spec 3c/D-4): 把"静默停机"
        换成"静默卡死"不可接受, 必须先叫醒人。
        """
        import json as _json
        import os as _os
        import time as _time
        _ts = now if now is not None else _time.time()
        _store = getattr(self, "_store", None)
        rows: list[dict] = []
        _records = getattr(_store, "_records", None)
        if _store is not None and callable(_records):
            try:
                raw = _records("protection_exposure")
                rows = [dict(r.get("payload") if isinstance(r, dict) and "payload" in r else r) for r in raw]
            except Exception:
                rows = []
        stuck = [
            {
                "symbol": str(r.get("symbol", "")),
                "reason": str(r.get("last_reason", "")),
                "unprotectable_since": float(r.get("unprotectable_since", 0.0) or 0.0),
                "age_s": _ts - float(r.get("unprotectable_since", 0.0) or 0.0),
            }
            for r in rows
            if _ts - float(r.get("unprotectable_since", 0.0) or 0.0) >= 1800.0
        ]
        _dir = state_dir or _os.path.expanduser(
            "~/Library/Application Support/beidou-watchdog"
        )
        marker = _os.path.join(_dir, "stuck")
        if not stuck:
            if _os.path.exists(marker):
                try:
                    _os.remove(marker)
                except OSError:
                    pass
            return False
        try:
            _os.makedirs(_dir, exist_ok=True)
            with open(marker, "w") as fh:
                _json.dump(
                    {"stuck": True, "items": stuck, "updated_at": _ts},
                    fh, ensure_ascii=False,
                )
        except OSError:
            pass
        return True
```

`beidou_launcher/supervisor.py` — 监控循环 `_merge_monitoring_checks` 之后(~line 1404 附近):

```python
            _update_stuck = getattr(self.engine, "_update_stuck_marker", None)
            if callable(_update_stuck):
                try:
                    _update_stuck()
                except Exception:
                    pass
```

`deploy/beidou_watchdog.sh` — 在 `# --- 0. 维护模式 ---` 之后插入:

```bash
# --- 0.5 引擎卡死告警 (A 类永不 LOCKED 的配套) ---
# 引擎每轮扫描 protection_exposure, 存在 ≥30 分钟未建起保护的持仓时
# 写 stuck 标记 (引擎侧已按 mtime 续写, 所以以文件 mtime 判断时效);
# 此处读到即弹窗, 用 stuck_last_alert 标记节流 30 分钟。
STUCK_FILE="$STATE_DIR/stuck"
if [ -f "$STUCK_FILE" ]; then
  stuck_mtime=$(stat -f %m "$STUCK_FILE" 2>/dev/null || echo 0)
  if [ $(( now - stuck_mtime )) -gt 1800 ]; then
    rm -f "$STUCK_FILE"   # 引擎已消除卡死却未能删文件 → 过期清理
  else
    last_alert_ts=0
    [ -f "$STATE_DIR/stuck_last_alert" ] && last_alert_ts=$(stat -f %m "$STATE_DIR/stuck_last_alert" 2>/dev/null || echo 0)
    if [ $(( now - last_alert_ts )) -ge 1800 ]; then
      notify "北斗引擎保护卡死" "有持仓超过 30 分钟未能建立保护（详见 ${STUCK_FILE}）。引擎已停止开新仓但仍在尝试修复；请检查后决定是否人工处置。"
      touch "$STATE_DIR/stuck_last_alert"
    fi
  fi
fi
```

> 注意 `set -u`:变量一律 `${VAR}` 形式,中文标点不得紧贴变量名。

- [ ] **Step 4: 运行通过**

Run: `python3 -m pytest tests/unit/test_stuck_marker.py -q`
Run: `bash -n deploy/beidou_watchdog.sh`
Expected: 全 PASS + 语法检查无输出

- [ ] **Step 5: Commit**

```bash
git add beidou_core/engine.py beidou_launcher/supervisor.py deploy/beidou_watchdog.sh tests/unit/test_stuck_marker.py
git commit -m "feat(alert): 保护卡死标记 —— A 类永不 LOCKED 的配套告警 (1800s 弹窗)"
```

---

## Task 7: 4-E3 慢引信(7200s 自动平仓,env 门控)

**Files:**
- Modify: `beidou_core/engine.py`(新增 `_run_slow_fuse`;nearline 主循环调用一次)
- Test: `tests/unit/test_slow_fuse.py`(新建)

**Interfaces:**
- Consumes: P4 的 `protection_exposure` 记录;`_maybe_emergency_close_unprotectable`(P4 后签名);`self._env_mode`
- Produces: `engine._run_slow_fuse(*, now: float | None = None, env: Any = None) -> int`(返回本次平仓意图数)

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_slow_fuse.py`:

```python
"""E3 慢引信: 纯卡死 ≥7200s 才平仓; testnet 默认开, live/canary 默认关。"""

from beidou_core.engine import AutonomousEngine


class _ExposureStore:
    def __init__(self, rows: list[dict] | None = None):
        self._rows = list(rows or [])

    def _records(self, record_type: str) -> list[dict]:
        return [{"payload": dict(r)} for r in self._rows]

    def _delete_record(self, record_type: str, record_id: str) -> None:
        self._rows = [r for r in self._rows if r.get("symbol") != record_id.split(":", 1)[-1]]


class _EnvMode:
    value = "testnet"


def _engine(rows, mode="testnet") -> AutonomousEngine:
    engine = AutonomousEngine.__new__(AutonomousEngine)
    engine._store = _ExposureStore(rows)
    engine._env_mode = _EnvMode() if mode == "testnet" else type("M", (), {"value": "live"})()
    engine._position_generation = {}
    engine._position_projection = {}  # 测试默认空; 触发用例需显式给 signed_quantity
    engine._policy_id_active = "p"
    engine._policy_version = "v"
    engine._policy_signature = "s"
    engine._sl_unprotectable_streak = {}
    engine._slow_fuse_fired = []
    async def _fake_close(pos_id, symbol, pp, **kw):
        engine._slow_fuse_fired.append(symbol)
        return True
    engine._maybe_emergency_close_unprotectable = _fake_close
    return engine


def test_slow_fuse_does_not_fire_before_7200s():
    engine = _engine([{"symbol": "BTCUSDT", "unprotectable_since": 1000.0, "last_reason": ""}])
    import asyncio
    n = asyncio.run(engine._run_slow_fuse(now=1000.0 + 7199.0))
    assert n == 0 and not engine._slow_fuse_fired


def test_slow_fuse_fires_after_7200s_on_testnet():
    engine = _engine([{"symbol": "BTCUSDT", "unprotectable_since": 1000.0, "last_reason": ""}])
    engine._position_projection = {"BTCUSDT": {"signed_quantity": "-0.0008"}}
    import asyncio
    n = asyncio.run(engine._run_slow_fuse(now=1000.0 + 7200.0))
    assert n == 1 and engine._slow_fuse_fired == ["BTCUSDT"]


def test_slow_fuse_off_by_default_in_live():
    engine = _engine([{"symbol": "BTCUSDT", "unprotectable_since": 1000.0, "last_reason": ""}], mode="live")
    engine._position_projection = {"BTCUSDT": {"signed_quantity": "-0.0008"}}
    import asyncio
    n = asyncio.run(engine._run_slow_fuse(now=1000.0 + 7200.0))
    assert n == 0


def test_slow_fuse_skips_when_projection_missing():
    engine = _engine([{"symbol": "BTCUSDT", "unprotectable_since": 1000.0, "last_reason": ""}])
    import asyncio
    n = asyncio.run(engine._run_slow_fuse(now=1000.0 + 7200.0))
    assert n == 0  # 方向不可知 → 不动作 (fail-closed)


def test_explicit_rejection_hands_off_to_e2_not_slow_fuse():
    # 显式拒绝 (-2021 等) 走 E2 快路径, 慢引信不再重复处置
    engine = _engine([{"symbol": "BTCUSDT", "unprotectable_since": 1000.0,
                       "last_reason": "SL_UNPROTECTABLE"}])
    engine._position_projection = {"BTCUSDT": {"signed_quantity": "-0.0008"}}
    import asyncio
    n = asyncio.run(engine._run_slow_fuse(now=1000.0 + 7200.0))
    assert n == 0
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/unit/test_slow_fuse.py -v`
Expected: FAIL(`_run_slow_fuse` 不存在)

- [ ] **Step 3: 实现**

`beidou_core/engine.py`:

```python
    async def _run_slow_fuse(self, *, now: float | None = None, env: Any = None) -> int:
        """4-E3 慢引信: 纯卡死(无显式拒绝)≥7200s → 逐品种紧急平仓。

        - 显式拒绝 (last_reason=SL_UNPROTECTABLE) 已由 E2 快路径处置, 此处跳过
        - testnet 默认开启; live/canary 默认关闭且不得被默认值绕过 (spec D-6)
        - 每品种独立判定, 绝不使用账户级 EMERGENCY_FLATTEN (spec D-7)
        - 平仓前后各发一次告警
        """
        import os as _os
        import time as _time
        _ts = now if now is not None else _time.time()
        _env = env if env is not None else _os.environ
        _mode = str(getattr(getattr(self, "_env_mode", None), "value", "") or "")
        enabled = (
            _mode == "testnet"
            or str(_env.get("BEIDOU_NAKED_POSITION_AUTOCLOSE", "")) == "1"
        )
        if not enabled:
            return 0
        _store = getattr(self, "_store", None)
        rows: list[dict] = []
        _records = getattr(_store, "_records", None)
        if _store is None or not callable(_records):
            return 0
        try:
            raw = _records("protection_exposure")
            rows = [dict(r.get("payload") if isinstance(r, dict) and "payload" in r else r) for r in raw]
        except Exception:
            return 0
        fired = 0
        for r in rows:
            age = _ts - float(r.get("unprotectable_since", 0.0) or 0.0)
            if age < 7200.0:
                continue
            if str(r.get("last_reason", "")) == "SL_UNPROTECTABLE":
                continue  # E2 快路径负责
            symbol = str(r.get("symbol", ""))
            _alerts = getattr(self, "_alerts", None)
            if _alerts is not None:
                with contextlib.suppress(Exception):
                    _alerts.send_incident(
                        AlertSeverity.CRITICAL,
                        "Naked position slow-fuse armed",
                        f"{symbol} 裸露 {age:.0f}s, 即将自动平仓",
                        category="protection",
                    )
            # _maybe_emergency_close_unprotectable 内部会读 pp.side 判方向、
            # pp.quantity 做回退 —— 传最小替身而非 None (投影优先, 替身只兜底):
            # 方向由 _position_projection 的 signed_quantity 符号派生。
            _proj = getattr(self, "_position_projection", {}).get(symbol, {}) or {}
            try:
                _signed = float(_proj.get("signed_quantity", 0.0) or 0.0)
            except (TypeError, ValueError):
                _signed = 0.0
            if _signed == 0.0:
                continue
            _pp = SimpleNamespace(
                side=OrderSide.SELL if _signed > 0 else OrderSide.BUY,
                quantity=abs(_signed),
            )
            try:
                ok = await self._maybe_emergency_close_unprotectable(
                    f"slow-fuse-{symbol}", symbol, _pp,
                    now=lambda: _ts,
                )
            except Exception:
                ok = False
            if ok:
                fired += 1
                if _alerts is not None:
                    with contextlib.suppress(Exception):
                        _alerts.send_incident(
                            AlertSeverity.CRITICAL,
                            "Naked position auto-closed by slow fuse",
                            f"{symbol} 裸露超过 7200s, 已发起逐品种平仓",
                            category="protection",
                        )
        return fired
```

> `SimpleNamespace` 与 `OrderSide` 在 `beidou_core/engine.py` 中已导入(现有代码大量使用);若实测未导入,在文件头补 `from types import SimpleNamespace`。

调用点:nearline 主循环末尾(`_cleanup_excess_orders` 同层,~line 11503 附近)加:

```python
        with contextlib.suppress(Exception):
            await self._run_slow_fuse()
```

- [ ] **Step 4: 运行通过**

Run: `python3 -m pytest tests/unit/test_slow_fuse.py tests/unit/test_final83g_protection_gap.py -q`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add beidou_core/engine.py tests/unit/test_slow_fuse.py
git commit -m "feat(safety): 裸仓慢引信 7200s 逐品种平仓, testnet 默认开 / live 默认关, 前后告警"
```

---

## Task 8: G5 基线 journal(写/删/恢复)+ preflight 搁浅检测

**Files:**
- Modify: `beidou_certification/g5_scenarios/engine/reconciliation_mismatch.py`(`run()` ~line 374;`_execute_flow` ~line 257)
- Modify: `beidou_launcher/preflight.py`(新增检查)
- Test: `tests/unit/test_g5_scenarios/` 内现有测试 + `tests/unit/test_g5_journal.py`(新建)

**Interfaces:**
- Consumes: PG 直连(`psycopg.connect(PG_DSN)`,与场景现有代码一致);preflight 用 `psycopg.connect(database_url, connect_timeout=5, autocommit=True)`(与 `_postgres_authority_probe` 一致)
- Produces:
  - journal 记录:`record_type='g5_journal'`, `record_id='reconciliation_mismatch:baseline'`, payload `{"original_payload": dict, "corrupted_at": iso, "scenario_run_id": str}`
  - preflight 检查 `startup.safety.g5_baseline_journal`(P0 FAIL + 修复指引)

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_g5_journal.py`:

```python
"""G5 journal: 搁浅还原与 preflight 检测 (不依赖真实 PG)。"""

import json

from beidou_certification.g5_scenarios.engine import reconciliation_mismatch as rm


class _FakeConn:
    def __init__(self, rows: dict | None = None):
        self.rows = dict(rows or {})
        self.upserted = []

    def execute(self, sql, params):
        class _Cur:
            def fetchone(self): return None
        return _Cur()

    def transaction(self):
        import contextlib
        return contextlib.nullcontext()


def test_stranded_journal_recovery_payload_roundtrip():
    original = {"balance_amount": "10730.29894895", "positions": {"BTCUSDT": "0.0008"}}
    journal = {"original_payload": original, "corrupted_at": "2026-08-24T03:00:00+00:00", "scenario_run_id": "r1"}
    restored = rm._recover_from_journal({"payload": journal})
    assert restored == original


def test_preflight_reports_journal_presence(tmp_path):
    # 纯函数: 给定 journal 行列表 → CheckResult
    from beidou_launcher.preflight import _g5_journal_check
    from beidou_launcher.models import CheckStatus
    res = _g5_journal_check(
        journal_rows=[{"payload": {"corrupted_at": "x", "scenario_run_id": "r"}}]
    )
    assert res.status is CheckStatus.FAIL
    assert "g5_journal" in res.message
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/unit/test_g5_journal.py -v`
Expected: FAIL(`_recover_from_journal` / `_g5_journal_check` 不存在)

- [ ] **Step 3: 实现**

`reconciliation_mismatch.py`:

1. 常量:

```python
_JOURNAL_RECORD_TYPE = "g5_journal"
_JOURNAL_RECORD_ID = "reconciliation_mismatch:baseline"
```

2. 模块级纯函数:

```python
def _recover_from_journal(journal_row: dict[str, Any]) -> dict[str, Any]:
    """从搁浅 journal 还原原始基线 payload (纯函数, 便于测试)。"""
    payload = journal_row.get("payload", journal_row) if isinstance(journal_row, dict) else {}
    original = payload.get("original_payload")
    if not isinstance(original, dict):
        raise RuntimeError("JOURNAL_PAYLOAD_MISSING: g5_journal 缺少 original_payload")
    return original
```

3. `_execute_flow` 开头(污染前)写 journal;还原成功后删除。`run()` 在 `_read_record` 之后、进入流程之前检查 journal 并先还原:

```python
        journal_row = _read_record(conn, _JOURNAL_RECORD_TYPE, _JOURNAL_RECORD_ID)
        if journal_row is not None:
            stranded = _recover_from_journal(journal_row)
            _upsert_record(conn, _BASELINE_RECORD_TYPE, _BASELINE_RECORD_ID, stranded)
            _delete_record(conn, _JOURNAL_RECORD_TYPE, _JOURNAL_RECORD_ID)
            steps.append({
                "action": "recovered_stranded_journal",
                "hash": _payload_hash(stranded),
            })
```

> 需要新增 `_delete_record` 模块函数(SQL `DELETE FROM v3_runtime_records WHERE record_type=%s AND record_id=%s`,与 store 同构)。

4. `_execute_flow` 内:

```python
        _upsert_record(conn, _JOURNAL_RECORD_TYPE, _JOURNAL_RECORD_ID, {
            "original_payload": dict(original_payload),
            "corrupted_at": _iso_from(t_corrupt),
            "scenario_run_id": str(self._now()),
        })
        # ...污染、观测、还原(现有逻辑)...
        _delete_record(conn, _JOURNAL_RECORD_TYPE, _JOURNAL_RECORD_ID)
```

`preflight.py`:

```python
def _g5_journal_check(journal_rows: list[dict[str, Any]]) -> CheckResult:
    """G5 污染基线后进程被硬杀会留下搁浅 journal —— P0 阻断启动。

    只检测不修复 (检查无副作用): 修复由 g5 场景下次启动自愈, 或人工
    执行场景的 _recover_from_journal。
    """
    if not journal_rows:
        return _result(
            "startup.safety.g5_baseline_journal", "G5 基线 journal",
            True, CheckSeverity.P1, "无搁浅的 G5 基线污染", ""
        )
    return _result(
        "startup.safety.g5_baseline_journal", "G5 基线 journal",
        False, CheckSeverity.P0,
        "无搁浅的 G5 基线污染",
        "检测到搁浅的 G5 基线污染 (record_type='g5_journal', record_id="
        "'reconciliation_mismatch:baseline')。上次认证运行在污染窗口内被"
        "硬杀, 基线 balance_amount 可能仍是哨兵值 \"1\"。修复: 重新运行 "
        "g5 reconciliation_mismatch 场景 (启动时自动还原), 或人工将 "
        "account_opening_projection/default:BINANCE 还原为 journal 内 "
        "original_payload 后删除 g5_journal 行。",
        evidence={"journal_count": len(journal_rows)},
    )
```

在 `_run_preflight` 中 PG 可达时查询并追加该检查(查询异常时按 PASS + evidence 记录 error,不阻断——PG 本身的权威检查已有专门探测):

```python
        try:
            with psycopg.connect(database_url, connect_timeout=5, autocommit=True) as conn:
                cur = conn.execute(
                    "SELECT payload::text FROM v3_runtime_records "
                    "WHERE record_type='g5_journal'"
                )
                rows = [{"payload": json.loads(str(r[0]))} for r in cur.fetchall()]
        except Exception as exc:
            rows = []
            _journal_probe_error = type(exc).__name__
        checks.append(_g5_journal_check(rows))
```

- [ ] **Step 4: 运行通过**

Run: `python3 -m pytest tests/unit/test_g5_journal.py tests/unit/test_preflight_g5_fail_closed.py tests/unit/test_g5_scenarios -q`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add beidou_certification/g5_scenarios/engine/reconciliation_mismatch.py beidou_launcher/preflight.py tests/unit/test_g5_journal.py
git commit -m "fix(g5): 基线污染 journal 化 —— 硬杀可自愈, preflight 检测搁浅并 P0 阻断"
```

---

## Task 9: 全量回归 + 实机验证

**Files:** 无新代码;若回归暴露问题,回对应任务修复。

- [ ] **Step 1: 全量单元测试**

Run: `python3 -m pytest tests/unit -q > /tmp/beidou_unit_p9.txt 2>&1; echo exit=$?; tail -3 /tmp/beidou_unit_p9.txt`
Expected: `3581+新增 passed`(基线 3581 为 P1-P8 之前;新增测试全绿后总数应 >3581;若个别旧测试断言了被改的字段/消息,按"更新断言以反映新语义"修复并注明)

- [ ] **Step 2: bash 语法与部署面检查**

Run: `bash -n deploy/beidou_watchdog.sh && python3 -m pytest tests/unit/test_supervisor_blocker_aggregation.py -q`
Expected: 无输出 + PASS

- [ ] **Step 3: 实机验证(需要引擎重启生效;由用户决定时机)**

改动涉及引擎启动路径与 fail-closed 语义,单元测试不足以证明线上行为。验证流程:

1. 确认 git 树干净后 `launchctl kickstart gui/501/com.beidou.autopilot`
2. 观察 `trading_ready=True`(参考记忆:约 7 分钟)
3. `grep "protection facts not clean" evidence/beidou_engine.log | tail -2` 确认新键名 `position_abs_qty`(b2193fb)与 gap 明细出现在日志
4. 若出现 LOCKED:检查 `evidence/locked-*.json` 存在且内容完整未截断
5. `watchdog.log` 健康时为空;出现卡死场景时验证弹窗

> 引擎重启前必须先 commit 全部改动(preflight git_worktree P0 门禁),watchdog 在脏树时会跳过自动重启——这是预期行为,不是故障。

---

## 自审记录

- **Spec 覆盖**:spec 第 1 节→P8;第 2 节→P1;3a-1→P3、3a-2→P2、3a-3→P3;3b→P5;3c→P6;第 4 节 E1/E2→P4、E3→P7;实施顺序依赖(P7 在 P6 后、P5 在 P2 后)已按序排列;D-4/D-6/D-7 分别落在 P6/P7/P7。
- **类型一致性**:`_persist_protection_exposure` 返回 dict(含 `attempts`/`unprotectable_since`/`position_generation`/`last_reason`),P4 测试与 P6/P7 消费字段名一致;`HealthDebounce.feed(has_persistent_blocker, *, repairable=False, now=None)` 在 P5 定义,P5 测试与 supervisor 调用点一致;`_run_slow_fuse(*, now, env) -> int` 在 P7 定义,测试一致。
- **已知风险标注**:P7 的 `pp=None` 传参需要实现时先确认 `_maybe_emergency_close_unprotectable` 无 `pp` 裸访问;P2 的 `collect_runtime_checks` 最小替身按真实引用补齐;P9 的旧断言更新需注明原因。
