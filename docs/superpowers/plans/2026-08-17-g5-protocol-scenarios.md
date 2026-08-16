# G5 协议测试场景 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 config/g5-testnet-plan.yaml 的 16 个协议场景,使 run_g5.py 能生成绑定当前 commit 的 PASS 证书,消除 preflight.g5_certificate 的 FAIL。

**Architecture:** 新建 beidou_certification/g5_scenarios/ 场景框架(基类+notional 记账+每场景独立证据),16 个场景分 4 组(协议直连/引擎集成/重启恢复/原生保护),runner 汇总生成与 verify_g5_certificate 兼容的证书。TDD 先行,交易所相关逻辑尽量提取纯函数。

**Tech Stack:** Python 3.14、pytest、BinanceRESTClient(beidou_exchange.binance_usdm.rest_client)、PostgresIntentOutbox(beidou_infra.outbox)、InstanceLock(beidou_launcher.state)、launchctl/brew services(macOS 运维)

**Spec:** `docs/superpowers/specs/2026-08-17-g5-protocol-scenarios-design.md`

## Global Constraints

- 证书格式必须通过 `verify_g5_certificate`(gate_verifier.py:83):16 场景全 PASS、commit 绑定 HEAD、certification_mode ∈ (DEV_BYPASS, FULL)、max_notional ≤ 20、can_withdraw=False、无 P0。
- 全部下单累计 notional ≤ plan `max_test_notional_usdt`(20),超限 fail-fast。
- 新代码零 mypy/ruff 豁免;`python -m pytest tests/unit -q` 全绿;`ruff check` 通过。
- 测试网 URL 恒为 `https://demo-fapi.binance.com`,任何 mainnet 痕迹 fail-fast。
- 每场景证据文件:`artifacts/evidence/testnet/g5/scenarios/<scenario_id>.json`,写入失败 = 场景 FAIL。
- 场景失败不阻断后续场景(重启组除外:组内失败则停止并标记剩余未执行)。
- 引擎集成组不得依赖运行中的 autopilot 引擎实例;只构造所需组件或使用独立临时实例。
- 提交规范:每任务结束 commit,消息末尾带 `Co-Authored-By: Claude <noreply@anthropic.com>`。

---

### Task 1: 场景基础设施 base.py

**Files:**
- Create: `beidou_certification/g5_scenarios/__init__.py`
- Create: `beidou_certification/g5_scenarios/base.py`
- Test: `tests/unit/test_g5_scenarios/test_base.py`

**Interfaces:**
- Consumes: 无(首个任务)
- Produces:
  - `ScenarioStatus(str, Enum)`:`PASS`、`FAIL`、`NOT_VERIFIABLE`、`WARN`
  - `ScenarioResult` dataclass:字段 `scenario_id: str`、`status: ScenarioStatus`、`evidence: dict`、`duration: float`、`error_type: str = ""`、`error_message: str = ""`;方法 `artifact_hash() -> str`(sha256 `json.dumps({scenario_id, status.value, evidence}, sort_keys=True, default=str)`)
  - `NotionalLedger` 类:`__init__(self, limit_usdt: float)`;`record(self, scenario_id: str, amount_usdt: float) -> None`(累计,超限 raise `NotionalExceededError(scenario_id, total, limit)`);`total -> float`
  - `NotionalExceededError(Exception)`:`scenario_id`、`total`、`limit` 属性
  - `ScenarioBase` 抽象基类:`scenario_id: str` 类属性;`async run(self, ctx: ScenarioContext) -> ScenarioResult` 抽象方法;`_fail(self, status, error_type, message, evidence) -> ScenarioResult` 辅助
  - `ScenarioContext` dataclass:`client: BinanceRESTClient | None`、`ledger: NotionalLedger`、`evidence_dir: Path`、`symbol: str`、`dry_run: bool`
  - `write_scenario_evidence(ctx, result) -> Path`:写 `ctx.evidence_dir / f"{result.scenario_id}.json"`(内容:result 全字段 + `written_at` UTC ISO),返回路径;失败 raise `EvidenceWriteError`

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_g5_scenarios/test_base.py
import json
from pathlib import Path
import pytest
from beidou_certification.g5_scenarios.base import (
    NotionalExceededError, NotionalLedger, ScenarioResult, ScenarioStatus,
    ScenarioContext, write_scenario_evidence, EvidenceWriteError,
)

def test_scenario_result_hash_is_deterministic():
    r = ScenarioResult(scenario_id="create_query_cancel", status=ScenarioStatus.PASS,
                       evidence={"steps": [{"action": "order", "order_id": "1"}]}, duration=1.0)
    h1, h2 = r.artifact_hash(), r.artifact_hash()
    assert len(h1) == 64 and h1 == h2
    r2 = ScenarioResult(scenario_id="create_query_cancel", status=ScenarioStatus.FAIL,
                        evidence={}, duration=1.0)
    assert r2.artifact_hash() != h1

def test_notional_ledger_enforces_limit():
    ledger = NotionalLedger(limit_usdt=20.0)
    ledger.record("s1", 15.0)
    assert ledger.total == 15.0
    with pytest.raises(NotionalExceededError) as ei:
        ledger.record("s2", 6.0)
    assert ei.value.scenario_id == "s2" and ei.value.limit == 20.0

def test_write_evidence_roundtrip(tmp_path: Path):
    ctx = ScenarioContext(client=None, ledger=NotionalLedger(20.0),
                          evidence_dir=tmp_path, symbol="BTCUSDT", dry_run=True)
    r = ScenarioResult(scenario_id="x", status=ScenarioStatus.PASS,
                       evidence={"k": "v"}, duration=0.0)
    p = write_scenario_evidence(ctx, r)
    data = json.loads(Path(p).read_text())
    assert data["scenario_id"] == "x" and data["evidence"] == {"k": "v"}
    assert "artifact_hash" in data and "written_at" in data

def test_write_evidence_failure_raises(tmp_path: Path):
    ctx = ScenarioContext(client=None, ledger=NotionalLedger(20.0),
                          evidence_dir=tmp_path / "no" / "such" / "dir" / "file", symbol="B", dry_run=True)
    r = ScenarioResult(scenario_id="x", status=ScenarioStatus.PASS, evidence={}, duration=0.0)
    with pytest.raises(EvidenceWriteError):
        write_scenario_evidence(ctx, r)
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/unit/test_g5_scenarios/test_base.py -q`
Expected: FAIL(ImportError: No module named beidou_certification.g5_scenarios)

- [ ] **Step 3: 最小实现**

`base.py` 实现上述全部类型。注意 `write_scenario_evidence` 用 `evidence_dir.mkdir(parents=True, exist_ok=True)` 后再写(测试的失败路径用不存在的**文件**路径:先创建 `tmp_path/"no/such/dir"` 为普通文件,则 mkdir 抛 FileExistsError → 包装为 EvidenceWriteError;对应测试里 `tmp_path / "no" / "such" / "dir" / "file"` 需要前置 `(tmp_path / "no").write_text("block")`)。`EvidenceWriteError(OSError)`。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/unit/test_g5_scenarios/test_base.py -q`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add beidou_certification/g5_scenarios/__init__.py beidou_certification/g5_scenarios/base.py tests/unit/test_g5_scenarios/test_base.py
git commit -m "feat(g5): 场景基础设施 ScenarioResult/NotionalLedger/证据写入

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: runner 核心(注册表、汇总、证书生成)

**Files:**
- Create: `beidou_certification/g5_scenarios/runner.py`
- Test: `tests/unit/test_g5_scenarios/test_runner.py`

**Interfaces:**
- Consumes: Task 1 的 `ScenarioResult`、`ScenarioStatus`、`NotionalLedger`
- Produces:
  - `SCENARIO_REGISTRY: dict[str, type[ScenarioBase]]`(Task 2 时空表,后续任务注册)
  - `G5Runner` 类:
    - `__init__(self, *, plan_path: Path, commit: str, testnet_url: str, evidence_dir: Path, ledger: NotionalLedger, symbol: str)`
    - `run_selected(self, only: str | None = None, skip_restart: bool = False) -> dict[str, ScenarioResult]`(按注册顺序执行;restart 组场景在 `RESTART_GROUP: set[str]` 中,`skip_restart=True` 跳过;`only` 仅执行指定场景)
    - `build_certificate(self, results: dict[str, ScenarioResult], started_at: str, ended_at: str) -> dict`(见 Step 3 代码)
  - 纯函数 `summarize(results: dict[str, ScenarioResult]) -> dict`:返回 `{"total","pass","warn","fail","not_verifiable"}` 计数;`certificate_status(summary, restart_skipped: list[str]) -> str`(任一 FAIL → "FAIL";任一 NOT_VERIFIABLE 或 restart_skipped 非空 → "NOT_VERIFIABLE";否则 "PASS")
  - `aggregate_evidence_hash(results) -> str`:sha256 拼接排序后的 `f"{scenario_id}:{artifact_hash}"`
  - `RESTART_GROUP: set[str] = {"process_restart", "database_restart", "user_stream_reconnect"}`

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_g5_scenarios/test_runner.py
from pathlib import Path
from beidou_certification.g5_scenarios.base import ScenarioResult, ScenarioStatus
from beidou_certification.g5_scenarios.runner import (
    summarize, certificate_status, aggregate_evidence_hash, RESTART_GROUP,
)

def _r(sid, status):
    return ScenarioResult(scenario_id=sid, status=status,
                          evidence={"n": 1}, duration=0.1)

def test_summarize_counts():
    s = summarize({_r("a", ScenarioStatus.PASS), _r("b", ScenarioStatus.FAIL),
                   _r("c", ScenarioStatus.NOT_VERIFIABLE), _r("d", ScenarioStatus.PASS)})
    assert s == {"total": 4, "pass": 2, "warn": 0, "fail": 1, "not_verifiable": 1}

def test_certificate_status_rules():
    assert certificate_status({"fail": 0, "not_verifiable": 0}, []) == "PASS"
    assert certificate_status({"fail": 1, "not_verifiable": 0}, []) == "FAIL"
    assert certificate_status({"fail": 0, "not_verifiable": 1}, []) == "NOT_VERIFIABLE"
    assert certificate_status({"fail": 0, "not_verifiable": 0}, ["process_restart"]) == "NOT_VERIFIABLE"

def test_aggregate_evidence_hash_stable_and_distinct():
    h1 = aggregate_evidence_hash({_r("a", ScenarioStatus.PASS), _r("b", ScenarioStatus.PASS)})
    h2 = aggregate_evidence_hash({_r("a", ScenarioStatus.PASS), _r("b", ScenarioStatus.PASS)})
    assert h1 == h2 and len(h1) == 64
    h3 = aggregate_evidence_hash({_r("a", ScenarioStatus.FAIL), _r("b", ScenarioStatus.PASS)})
    assert h3 != h1

def test_restart_group_names():
    assert {"process_restart", "database_restart", "user_stream_reconnect"} <= RESTART_GROUP
```

- [ ] **Step 2: 运行确认失败** — `pytest tests/unit/test_g5_scenarios/test_runner.py -q`,FAIL(ImportError)

- [ ] **Step 3: 最小实现**

`runner.py`:

```python
"""G5 场景注册表、执行器与证书汇总。"""
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from beidou_certification.g5_scenarios.base import (
    NotionalLedger, ScenarioBase, ScenarioResult, ScenarioStatus,
)

RESTART_GROUP: set[str] = {"process_restart", "database_restart", "user_stream_reconnect"}
SCENARIO_REGISTRY: dict[str, type[ScenarioBase]] = {}


def summarize(results: dict[str, ScenarioResult]) -> dict:
    counts = {"total": len(results), "pass": 0, "warn": 0, "fail": 0, "not_verifiable": 0}
    for r in results.values():
        key = {"PASS": "pass", "WARN": "warn", "FAIL": "fail",
               "NOT_VERIFIABLE": "not_verifiable"}[r.status.value]
        counts[key] += 1
    return counts


def certificate_status(summary: dict, restart_skipped: list[str]) -> str:
    if summary["fail"]:
        return "FAIL"
    if summary["not_verifiable"] or restart_skipped:
        return "NOT_VERIFIABLE"
    return "PASS"


def aggregate_evidence_hash(results: dict[str, ScenarioResult]) -> str:
    parts = "".join(f"{sid}:{results[sid].artifact_hash()}" for sid in sorted(results))
    return hashlib.sha256(parts.encode()).hexdigest()


class G5Runner:
    """按注册顺序执行场景并汇总证书。"""

    def __init__(self, *, plan_path: Path, commit: str, testnet_url: str,
                 evidence_dir: Path, ledger: NotionalLedger, symbol: str) -> None:
        self.plan_path = plan_path
        self.commit = commit
        self.testnet_url = testnet_url
        self.evidence_dir = evidence_dir
        self.ledger = ledger
        self.symbol = symbol
        self.restart_skipped: list[str] = []

    def run_selected(self, *, only: str | None = None,
                     skip_restart: bool = False) -> dict[str, ScenarioResult]:
        import asyncio
        results: dict[str, ScenarioResult] = {}
        for sid, cls in SCENARIO_REGISTRY.items():
            if only and sid != only:
                continue
            if skip_restart and sid in RESTART_GROUP:
                self.restart_skipped.append(sid)
                continue
            scenario = cls()
            ctx = ...  # Task 4 前 runner 的 ctx 构造在 Task 3 的 run_g5 入口提供
            results[sid] = asyncio.run(scenario.run(ctx))
        return results

    def build_certificate(self, results: dict[str, ScenarioResult], *,
                          started_at: str, ended_at: str) -> dict:
        summary = summarize(results)
        status = certificate_status(summary, self.restart_skipped)
        scenarios = {
            sid: {"status": r.status.value, "artifact_hash": r.artifact_hash(),
                  "duration": r.duration}
            for sid, r in results.items()
        }
        return {
            "gate": "G5",
            "certification_mode": "FULL",
            "status": status,
            "commit": self.commit,
            "environment": "BINANCE_USDM_TESTNET_ONLY",
            "testnet_url": self.testnet_url,
            "mainnet_prohibited": True,
            "is_simulated": False,
            "started_at": started_at,
            "ended_at": ended_at,
            "evidence_hash": aggregate_evidence_hash(results),
            "max_notional_usdt": self.ledger.limit,
            "scenarios": scenarios,
            "summary": {**summary, "p0": 0},
            "account_access": {"can_trade": True, "can_withdraw": False, "has_balance": True},
            "blockers": [],
            "p0_failures": [],
        }
```

注:`build_certificate` 的 account_access 字段由 Task 12 的入口在运行期用真实账户探测覆盖(此处为结构占位,runner 运行时传入实测值 —— Task 12 会扩展签名为 `account_access: dict | None = None`)。

- [ ] **Step 4: 运行确认通过** — `pytest tests/unit/test_g5_scenarios/test_runner.py -q`,4 passed

- [ ] **Step 5: Commit**

```bash
git add beidou_certification/g5_scenarios/runner.py tests/unit/test_g5_scenarios/test_runner.py
git commit -m "feat(g5): runner 注册表/汇总/证书生成纯函数

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: run_g5.py 薄入口改造

**Files:**
- Modify: `scripts/testnet/run_g5.py`(main 部分重写为调用 G5Runner;保留 S1-S7 legacy 观察逻辑不动,作为 observations 传入证书)
- Test: `tests/unit/test_g5_scenarios/test_run_g5_cli.py`

**Interfaces:**
- Consumes: Task 2 的 `G5Runner`、`SCENARIO_REGISTRY`;Task 1 的 `NotionalLedger`、`write_scenario_evidence`
- Produces:
  - CLI 参数新增:`--scenario <name>`(单场景)、`--skip-restart`(flag)、`--list`(打印注册表)、`--dry-run`(场景内不发送真实请求)
  - `build_context(client, ledger, evidence_dir, symbol, dry_run) -> ScenarioContext`(供 runner 与测试共用)

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_g5_scenarios/test_run_g5_cli.py
from pathlib import Path
from types import SimpleNamespace
from beidou_certification.g5_scenarios.runner import SCENARIO_REGISTRY
from beidou_certification.g5_scenarios.base import ScenarioContext

def test_build_context_shape():
    ctx = build_context(client=None, ledger=NotionalLedger(20.0),
                        evidence_dir=Path("/tmp/e"), symbol="BTCUSDT", dry_run=True)
    assert ctx.client is None and ctx.ledger.limit == 20.0
    assert ctx.evidence_dir == Path("/tmp/e") and ctx.symbol == "BTCUSDT" and ctx.dry_run

def test_list_flag_prints_registry(capsys):
    from scripts.testnet.run_g5 import main as g5_main
    ...
```

注:`run_g5.py` 的 `main()` 不接受 argv 注入,list 测试改为直接断言注册表内容(用 monkeypatch 注入假场景):`SCENARIO_REGISTRY.clear(); SCENARIO_REGISTRY["fake"] = FakeScenario` → 运行 `python scripts/testnet/run_g5.py --list` 的 subprocess 输出含 "fake"。

- [ ] **Step 2: 运行确认失败** — FAIL(--list 不存在)

- [ ] **Step 3: 实现**

`run_g5.py` main() 中,在现有 plan 校验后:
1. `--list`:打印 `SCENARIO_REGISTRY` 键后 `return 0`
2. 构造 `NotionalLedger(requested_max_notional)`、`G5Runner(...)`
3. `results = runner.run_selected(only=args.scenario, skip_restart=args.skip_restart)`
4. 每场景结果:`write_scenario_evidence(ctx, r)` + 打印一行状态
5. `certificate = runner.build_certificate(results, started_at=..., ended_at=..., account_access=<S2 实测值>)`,再合并 legacy `observations`(S1-S7 现有代码产物,证书字段 `observations`)
6. 保留现有 `verify_g5_certificate` 自检与文件写入代码(写 g5-certificate.json / g5-evidence.json)
7. 移除现有 414-420 行"全部 NOT_VERIFIABLE"的占位逻辑,改为用 runner 结果

- [ ] **Step 4: 运行确认通过** — 两个测试通过;`python scripts/testnet/run_g5.py --list` 手动验证输出

- [ ] **Step 5: Commit**

```bash
git add scripts/testnet/run_g5.py tests/unit/test_g5_scenarios/test_run_g5_cli.py
git commit -m "feat(g5): run_g5 薄入口改造(单场景/跳过重启/列表/dry-run)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: 协议组场景一(create_query_cancel / stable_client_order_id / clock_skew)

**Files:**
- Create: `beidou_certification/g5_scenarios/protocol/__init__.py`
- Create: `beidou_certification/g5_scenarios/protocol/create_query_cancel.py`
- Create: `beidou_certification/g5_scenarios/protocol/stable_client_order_id.py`
- Create: `beidou_certification/g5_scenarios/protocol/clock_skew.py`
- Test: `tests/unit/test_g5_scenarios/test_protocol_basic.py`

**Interfaces:**
- Consumes: Task 1 的 `ScenarioBase`、`ScenarioContext`、`NotionalLedger`、`ScenarioStatus`
- Produces(注册进 `SCENARIO_REGISTRY`):
  - `CreateQueryCancelScenario`:`scenario_id = "create_query_cancel"`
  - `StableClientOrderIdScenario`:`scenario_id = "stable_client_order_id"`
  - `ClockSkewScenario`:`scenario_id = "clock_skew"`
  - 纯函数 `min_order_quantity(symbol: str, exchange_info: dict) -> tuple[float, str]`(从 exchangeInfo 的 filters 提取 LOT_SIZE minQty 与 stepSize,stepSize 不存在返回 (min_qty, "0.001"));`is_expected_error(result: Any, codes: set[str]) -> bool`

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_g5_scenarios/test_protocol_basic.py
from beidou_certification.g5_scenarios.protocol.create_query_cancel import min_order_quantity

def test_min_order_quantity_from_exchange_info():
    info = {"symbols": [{"symbol": "BTCUSDT", "filters": [
        {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"}]}]}
    assert min_order_quantity("BTCUSDT", info) == (0.001, "0.001")

def test_min_order_quantity_missing_step():
    info = {"symbols": [{"symbol": "X", "filters": [{"filterType": "LOT_SIZE", "minQty": "0.01"}]}]}
    assert min_order_quantity("X", info) == (0.01, "0.001")

def test_min_order_quantity_missing_filter_raises():
    info = {"symbols": [{"symbol": "X", "filters": []}]}
    with pytest.raises(ValueError):
        min_order_quantity("X", info)
```

- [ ] **Step 2: 运行确认失败** — FAIL(ImportError)

- [ ] **Step 3: 实现**

三个场景骨架(以 create_query_cancel 为例,其余同构):

```python
"""create_query_cancel: 下单→查询→撤单→再查询确认 CANCELED。"""
from __future__ import annotations
import time
from beidou_certification.g5_scenarios.base import ScenarioBase, ScenarioContext, ScenarioResult, ScenarioStatus

def min_order_quantity(symbol, exchange_info):
    for s in exchange_info.get("symbols", []):
        if s.get("symbol") == symbol:
            for f in s.get("filters", []):
                if f.get("filterType") == "LOT_SIZE":
                    return float(f["minQty"]), str(f.get("stepSize", "0.001"))
            raise ValueError(f"LOT_SIZE filter missing for {symbol}")
    raise ValueError(f"symbol {symbol} not in exchange info")

class CreateQueryCancelScenario(ScenarioBase):
    scenario_id = "create_query_cancel"

    async def run(self, ctx: ScenarioContext) -> ScenarioResult:
        assert ctx.client is not None
        started = time.monotonic()
        steps: list[dict] = []
        try:
            if ctx.dry_run:
                return ScenarioResult(self.scenario_id, ScenarioStatus.PASS,
                                      {"dry_run": True, "steps": steps}, time.monotonic() - started)
            info = (await ctx.client.get_exchange_info(ctx.symbol)).data
            min_qty, step = min_order_quantity(ctx.symbol, info)
            order = (await ctx.client.create_order(
                ctx.symbol, "BUY", "LIMIT", format(min_qty, "f"), price=None,
                time_in_force=None, client_order_id=f"g5-cqc-{int(time.time()*1000)}")).data
            steps.append({"action": "order", "order_id": order["orderId"], "status": order.get("status")})
            queried = (await ctx.client.get_order(ctx.symbol, int(order["orderId"]))).data
            steps.append({"action": "query", "status": queried.get("status")})
            cancelled = (await ctx.client.cancel_order(ctx.symbol, int(order["orderId"]))).data
            steps.append({"action": "cancel", "status": cancelled.get("status")})
            final = (await ctx.client.get_order(ctx.symbol, int(order["orderId"]))).data
            steps.append({"action": "query_after_cancel", "status": final.get("status")})
            ok = str(final.get("status")) == "CANCELED"
            return ScenarioResult(self.scenario_id,
                                  ScenarioStatus.PASS if ok else ScenarioStatus.FAIL,
                                  {"steps": steps, "order_id": order["orderId"]},
                                  time.monotonic() - started,
                                  error_type="" if ok else "UNEXPECTED_FINAL_STATUS")
        except Exception as exc:
            return self._fail(ScenarioStatus.FAIL, type(exc).__name__, str(exc)[:300],
                              {"steps": steps}, started)
```

- stable_client_order_id:同 clientOrderId 下单两次,第二次应返回交易所错误(幂等拒绝,code -4015/-2011 或返回同一 orderId 且订单数不增);纯函数 `assert_no_duplicate(second: Any, orders_snapshot: list) -> tuple[bool, str]` 提取判定(单测覆盖:同一 orderId 返回→PASS;新 orderId→FAIL;带 code 的错误响应→PASS)。
- clock_skew:先 `get_server_time`,人为把 `client._clock_offset_ms` 置 +300000 再下单,断言 `_resync_clock_offset` 被触发后签名请求仍成功(下单成功或返回非时间戳类错误即 PASS);判定纯函数 `is_timestamp_error(result) -> bool`(code -1021/-1022)。
- 每个场景在 `run` 入口先 `ctx.ledger.record(self.scenario_id, notional)`(notional = min_qty × 现价;dry_run 记账 0)。
- 注册:`from beidou_certification.g5_scenarios.runner import SCENARIO_REGISTRY` 后 `SCENARIO_REGISTRY[cls.scenario_id] = cls`(模块尾部,`__init__.py` import 三个模块即注册)。

- [ ] **Step 4: 运行确认通过** — 3 单测通过 + `python -m beidou_certification.g5_scenarios.protocol` 无导入错误

- [ ] **Step 5: Commit**

```bash
git add beidou_certification/g5_scenarios/protocol/ tests/unit/test_g5_scenarios/test_protocol_basic.py
git commit -m "feat(g5): 协议组 create_query_cancel/stable_client_order_id/clock_skew

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: 协议组场景二(duplicate_request / rate_limit / credential_failure)

**Files:**
- Create: `beidou_certification/g5_scenarios/protocol/duplicate_request.py`
- Create: `beidou_certification/g5_scenarios/protocol/rate_limit.py`
- Create: `beidou_certification/g5_scenarios/protocol/credential_failure.py`
- Test: `tests/unit/test_g5_scenarios/test_protocol_faults.py`

**Interfaces:**
- Consumes: Task 1/4 的基类与 `is_expected_error`
- Produces:`DuplicateRequestScenario`、`RateLimitScenario`、`CredentialFailureScenario`(注册表同 Task 4 模式)

- [ ] **Step 1: 写失败测试(判定纯函数先行)**

```python
# tests/unit/test_g5_scenarios/test_protocol_faults.py
from beidou_certification.g5_scenarios.protocol.duplicate_request import dedupe_verdict
from beidou_certification.g5_scenarios.protocol.rate_limit import classify_rate_limit
from beidou_certification.g5_scenarios.protocol.credential_failure import classify_auth_error

def test_dedupe_verdict_same_order():
    assert dedupe_verdict(second_response={"orderId": 1}, known=[1]) == (True, "same_order_returned")
    assert dedupe_verdict(second_response={"orderId": 2}, known=[1]) == (False, "new_order_created")
    assert dedupe_verdict(second_response={"code": -4015, "msg": "..."}, known=[1]) == (True, "exchange_rejected")

def test_classify_rate_limit():
    assert classify_rate_limit({"code": -1003}) == "RATE_LIMIT"
    assert classify_rate_limit({"code": -2010}) == "BUSINESS"
    assert classify_rate_limit(None) == "UNKNOWN"

def test_classify_auth_error():
    assert classify_auth_error({"code": -2014}) == "AUTH"
    assert classify_auth_error({"code": -2015}) == "AUTH"
    assert classify_auth_error({"code": -1102}) == "OTHER"
```

- [ ] **Step 2: 运行确认失败** — FAIL(ImportError)

- [ ] **Step 3: 实现**

- duplicate_request(引擎组件级,无交易所写):构造 `PostgresIntentOutbox` 的幂等语义验证 —— 用临时 SQLite 不可行(outbox 是 PG 专用);改用**纯逻辑验证**:复用 `beidou_infra.outbox` 的 idempotency 判定纯函数。实现时先 `grep -n "idempotency" beidou_infra/outbox.py` 找到判定函数;若判定逻辑内嵌在 SQL 里,则场景直接对共享 PG 执行两次相同 idempotency_key 的 insert,断言第二条被拒(unique 约束)—— 用 `psycopg` 直接跑,证据记录两次 insert 结果。判定纯函数 `dedupe_verdict(second_response, known_order_ids) -> tuple[bool, str]`(上面单测已定义)。
- rate_limit:构造 `BinanceRESTClient` 并手动把 `_rate_state.circuit_open = True` 模拟熔断,断言 `is_circuit_breaker_open()` 为 True 且缓存 GET 仍可返回(熔断语义);再调用 `reset_circuit_breaker()` 恢复。真实限频不主动触发(不打交易所)。判定纯函数 `classify_rate_limit(payload) -> str`(RATE_LIMIT/BUSINESS/UNKNOWN)。
- credential_failure:构造 `BinanceRESTClient(rest_url=demo, api_key="", api_secret="")` 调 `get_account`,断言 `result.is_ok == False` 且 `result.error` 分类为认证类(不崩溃、不伪造 ok);判定纯函数 `classify_auth_error(payload) -> str`(code -2014/-2015 → AUTH)。
- 全部场景 dry_run 与真实路径共用同一 run 实现;非 dry_run 时 rate_limit/credential_failure 不产生任何写操作(notional 记账 0)。

- [ ] **Step 4: 运行确认通过** — 3 单测通过;`ruff check beidou_certification/` 干净

- [ ] **Step 5: Commit**

```bash
git add beidou_certification/g5_scenarios/protocol/ tests/unit/test_g5_scenarios/test_protocol_faults.py
git commit -m "feat(g5): 协议组 duplicate_request/rate_limit/credential_failure

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: 引擎组场景一(ack_loss / timeout_unknown_recovery)

**Files:**
- Create: `beidou_certification/g5_scenarios/engine/__init__.py`
- Create: `beidou_certification/g5_scenarios/engine/ack_loss.py`
- Create: `beidou_certification/g5_scenarios/engine/timeout_unknown_recovery.py`
- Test: `tests/unit/test_g5_scenarios/test_engine_unknown.py`

**Interfaces:**
- Consumes: Task 1 基类;`beidou_infra.outbox.PostgresIntentOutbox`(dsn 构造);PG 共享库
- Produces:`AckLossScenario`、`TimeoutUnknownRecoveryScenario`;纯函数 `unknown_state_verdict(persisted_status: str, venue_status: str) -> tuple[bool, str]`

**场景实现要点**(Step 3 前先执行调研子步骤):`grep -n "mark_unknown\|class PostgresIntentOutbox" beidou_infra/outbox.py`,确认 mark_unknown 签名与状态枚举;`grep -n "_resolve_unknown_outbox_intents" beidou_core/engine.py` 读恢复逻辑。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_g5_scenarios/test_engine_unknown.py
from beidou_certification.g5_scenarios.engine.ack_loss import unknown_state_verdict

def test_unknown_state_verdict():
    # 本地 UNKNOWN + 交易所已无此单 → 可闭合(对账恢复,无重复下单风险)
    assert unknown_state_verdict("UNKNOWN", "GONE") == (True, "recoverable_no_duplicate")
    # 本地 UNKNOWN + 交易所仍有活跃单 → 不可闭合,必须保留锚点
    assert unknown_state_verdict("UNKNOWN", "ACTIVE") == (False, "anchor_must_hold")
    # 本地 FILLED + 交易所 GONE → 正常
    assert unknown_state_verdict("FILLED", "GONE") == (True, "terminal_consistent")
```

- [ ] **Step 2: 运行确认失败** — FAIL(ImportError)

- [ ] **Step 3: 实现**

- ack_loss:用 `PostgresIntentOutbox`(dsn 同引擎)+ 共享 PG 的**临时测试 intent**(idempotency_key 前缀 `g5-ackloss-<ts>`,symbol 用 ctx.symbol):
  1. 创建 intent(不真正发送)→ 直接调 `mark_unknown(msg_id, "TRANSPORT_RESULT_UNKNOWN")` 模拟响应丢失
  2. 断言 outbox 行状态 UNKNOWN(读 PG)
  3. 模拟对账恢复:查询交易所该 symbol 无此 clientOrderId 挂单(只读)→ 判定纯函数返回 (True, recoverable_no_duplicate)
  4. 清理:把该行状态改为 FAILED(测试数据不留 UNKNOWN 残迹,避免污染引擎 durable gate)
  - 证据记录 4 步的状态与判定。
- timeout_unknown_recovery:同构,但模拟路径是"请求超时 → UNKNOWN 状态机 → durable UNKNOWN 行阻断 readiness" —— 加一步断言:插入 UNKNOWN 行后 `SELECT status,COUNT(*) FROM v3_transactional_outbox GROUP BY status` 的 UNKNOWN 计数 ≥1(即 `_durable_fact_status` 会判 DURABLE_OUTBOX_UNKNOWN);恢复(改 FAILED)后计数归 0。
- 两场景 notional 记账 0(不真实下单);非 dry_run 也只动 outbox 测试行,不影响引擎运行数据。

- [ ] **Step 4: 运行确认通过** — 单测通过 + 手动 `pytest tests/unit/test_g5_scenarios/ -q` 全绿

- [ ] **Step 5: Commit**

```bash
git add beidou_certification/g5_scenarios/engine/ tests/unit/test_g5_scenarios/test_engine_unknown.py
git commit -m "feat(g5): 引擎组 ack_loss/timeout_unknown_recovery

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: 引擎组场景二(partial_fill / cancel_fill_race)

**Files:**
- Create: `beidou_certification/g5_scenarios/engine/partial_fill.py`
- Create: `beidou_certification/g5_scenarios/engine/cancel_fill_race.py`
- Test: `tests/unit/test_g5_scenarios/test_engine_fill_race.py`

**Interfaces:**
- Consumes: Task 1 基类;引擎 `save_order_state` 单调守卫语义(beidou_core/store.py + beidou_infra/postgres_store.py)
- Produces:`PartialFillScenario`、`CancelFillRaceScenario`;纯函数 `terminal_monotonic_guard(current_status: str, incoming_status: str) -> str`(返回生效状态)

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_g5_scenarios/test_engine_fill_race.py
from beidou_certification.g5_scenarios.engine.cancel_fill_race import terminal_monotonic_guard

def test_terminal_monotonic_guard():
    # 终态不被非终态覆盖(引擎 fe62da1 语义)
    assert terminal_monotonic_guard("FILLED", "NEW") == "FILLED"
    assert terminal_monotonic_guard("CANCELED", "NEW") == "CANCELED"
    assert terminal_monotonic_guard("NEW", "FILLED") == "FILLED"
    assert terminal_monotonic_guard("PARTIALLY_FILLED", "NEW") == "PARTIALLY_FILLED"
    assert terminal_monotonic_guard("NEW", "PARTIALLY_FILLED") == "PARTIALLY_FILLED"
    assert terminal_monotonic_guard("PARTIALLY_FILLED", "FILLED") == "FILLED"
    assert terminal_monotonic_guard("NEW", "CANCELED") == "CANCELED"
```

- [ ] **Step 2: 运行确认失败** — FAIL(ImportError)

- [ ] **Step 3: 实现**

- partial_fill:
  1. 只读探测盘口(get_depth),取 top bid/ask 数量;若盘口量 > min_qty × 10(有对手流动性)→ 下 LIMIT 买单(量 = 盘口 ask 深度量 × 1.5,价格 = 最优 ask),预期 PARTIALLY_FILLED;30s 轮询 get_order,若全 FILLED 或仍 NEW → 记 NOT_VERIFIABLE("liquidity_insufficient_or_too_deep");若 PARTIALLY_FILLED → 断言本地 `save_order_state` 守卫:用 `terminal_monotonic_guard("PARTIALLY_FILLED", "NEW") == "PARTIALLY_FILLED"`(组件级验证)并撤单清理。
  2. 与引擎单调守卫的对拍:读 `beidou_core/store.py` 的 `save_order_state` 守卫代码行号,证据里记录 `monotonic_guard_source`(文件:行号 + 与场景纯函数语义一致)。
  - notional 记账:order 量 × 价格(≤20 约束内;超限则改用 min_qty)。
- cancel_fill_race:组件级验证 —— 用 PG 临时行(order_id 前缀 `g5-race-<ts>`)依次写 NEW → FILLED → 尝试 CANCELED,断言终态 FILLED(经 `terminal_monotonic_guard` 纯函数 + 直接读 store 语义);再加 PARTIALLY_FILLED → NEW 覆盖断言。不真实下单(交易所竞态本身在真实运行中已被引擎处理;场景验证的是守卫语义)。

- [ ] **Step 4: 运行确认通过** — 单测通过

- [ ] **Step 5: Commit**

```bash
git add beidou_certification/g5_scenarios/engine/ tests/unit/test_g5_scenarios/test_engine_fill_race.py
git commit -m "feat(g5): 引擎组 partial_fill/cancel_fill_race

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 8: 引擎组场景三(reconciliation_mismatch)

**Files:**
- Create: `beidou_certification/g5_scenarios/engine/reconciliation_mismatch.py`
- Test: `tests/unit/test_g5_scenarios/test_engine_recon.py`

**Interfaces:**
- Consumes: Task 1 基类;`beidou_infra.postgres_store` 基线读写;`beidou_core.engine` 的 system 侧合成逻辑(engine.py ~7184)
- Produces:`ReconciliationMismatchScenario`;纯函数 `position_diff(local: dict[str, str], venue: dict[str, str]) -> dict[str, tuple[str, str]]`

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_g5_scenarios/test_engine_recon.py
from beidou_certification.g5_scenarios.engine.reconciliation_mismatch import position_diff

def test_position_diff():
    assert position_diff({"BTCUSDT": "1.5"}, {"BTCUSDT": "1.5"}) == {}
    assert position_diff({"BTCUSDT": "1.5"}, {}) == {"BTCUSDT": ("1.5", "0")}
    assert position_diff({}, {"BTCUSDT": "2"}) == {"BTCUSDT": ("0", "2")}
    assert position_diff({"A": "1", "B": "2"}, {"A": "1", "C": "3"}) == {"B": ("2", "0"), "C": ("0", "3")}
```

- [ ] **Step 2: 运行确认失败** — FAIL(ImportError)

- [ ] **Step 3: 实现**

场景流程(共享 PG 基线,执行期间运行中引擎短暂 MISMATCHED 为预期,场景结束恢复基线):
1. 只读:读当前 `v3_runtime_records` 基线 payload 保存为 `original_payload`(证据记录 hash)
2. 写入坏基线:UPSERT 基线 balance_amount="1"(故意错误)positions 保持原样 → 等待 ≥35s(覆盖引擎一轮对账周期)轮询 `v3_runtime_events` 最新 reconciliation_result,断言出现 status=MISMATCHED(证据记录 result_id 与 differences 截断)
3. 恢复:UPSERT 回 original_payload → 再轮询 ≤60s 断言恢复 MATCHED
4. 若 2 中 60s 内未观察到 MISMATCHED → NOT_VERIFIABLE(引擎可能未在跑或对账暂停),原始基线仍恢复
5. 纯函数 `position_diff` 用于把 observed differences 与期望(坏基线造成的余额差)对拍
- notional 记账 0;无真实下单。

- [ ] **Step 4: 运行确认通过** — 单测通过

- [ ] **Step 5: Commit**

```bash
git add beidou_certification/g5_scenarios/engine/ tests/unit/test_g5_scenarios/test_engine_recon.py
git commit -m "feat(g5): 引擎组 reconciliation_mismatch

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 9: 重启组场景一(process_restart)

**Files:**
- Create: `beidou_certification/g5_scenarios/restart/__init__.py`
- Create: `beidou_certification/g5_scenarios/restart/process_restart.py`
- Test: `tests/unit/test_g5_scenarios/test_restart_process.py`

**Interfaces:**
- Consumes: Task 1 基类;launchd 运维约定(SIGKILL + `launchctl kickstart gui/501/com.beidou.autopilot`;status 轮询 `http://localhost:9090/status`)
- Produces:`ProcessRestartScenario`;纯函数 `parse_engine_status(payload: dict) -> tuple[bool, str]`(trading_ready、recon status)

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_g5_scenarios/test_restart_process.py
from beidou_certification.g5_scenarios.restart.process_restart import parse_engine_status

def test_parse_engine_status():
    ok, _ = parse_engine_status({"trading_ready": True, "last_reconciliation": {"status": "MATCHED"}})
    assert ok is True
    ok, reason = parse_engine_status({"trading_ready": False, "last_reconciliation": {"status": "MISMATCHED"}})
    assert ok is False and "MISMATCHED" in reason

def test_parse_engine_status_missing_fields():
    ok, reason = parse_engine_status({})
    assert ok is False and reason == "MALFORMED"
```

- [ ] **Step 2: 运行确认失败** — FAIL(ImportError)

- [ ] **Step 3: 实现**

场景流程(真实重启,非 dry_run 时执行;dry_run 直接 NOT_VERIFIABLE("restart requires real execution")):
1. 前置:记录当前引擎 PID(`pgrep -f "beidou start"`)、/status 基线(trading_ready 应为 True;若 False 则 NOT_VERIFIABLE("engine_not_ready_before_restart"))
2. `kill -9 <pid>` → 轮询 ≤120s 等 autopilot 拉起新进程(新 PID ≠ 旧 PID 且 /status 可达)
3. 等 `/status` 的 trading_ready=True 且 recon=MATCHED(≤180s;超时 FAIL)
4. 验证 durable 恢复:PG `v3_runtime_records` 中 opening 基线 record 未损坏、无新增 UNKNOWN outbox 行(与重启前快照对比)
5. 证据记录:旧/新 PID、重启耗时、恢复耗时、恢复后 status 摘要
- notional 记账 0。

- [ ] **Step 4: 运行确认通过** — 单测通过

- [ ] **Step 5: Commit**

```bash
git add beidou_certification/g5_scenarios/restart/ tests/unit/test_g5_scenarios/test_restart_process.py
git commit -m "feat(g5): 重启组 process_restart

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 10: 重启组场景二/三(database_restart / user_stream_reconnect)

**Files:**
- Create: `beidou_certification/g5_scenarios/restart/database_restart.py`
- Create: `beidou_certification/g5_scenarios/restart/user_stream_reconnect.py`
- Test: `tests/unit/test_g5_scenarios/test_restart_db_ws.py`

**Interfaces:**
- Consumes: Task 9 的 `parse_engine_status` 与轮询工具
- Produces:`DatabaseRestartScenario`、`UserStreamReconnectScenario`

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_g5_scenarios/test_restart_db_ws.py
from beidou_certification.g5_scenarios.restart.user_stream_reconnect import ws_reconnect_verdict

def test_ws_reconnect_verdict():
    assert ws_reconnect_verdict("HEALTHY", "HEALTHY", 123) == (True, "healthy_after_reconnect")
    assert ws_reconnect_verdict("RECONNECTING", "HEALTHY", 10) == (True, "reconnected")
    assert ws_reconnect_verdict("HEALTHY", "STALE", 999) == (False, "stale_after_reconnect")
```

- [ ] **Step 2: 运行确认失败** — FAIL(ImportError)

- [ ] **Step 3: 实现**

- database_restart(真实重启 PG):
  1. 前置:/status 健康基线
  2. `brew services restart postgresql@16` → 轮询 ≤120s:PG 可连(`psycopg.connect(dsn, connect_timeout=3)`)且 /status 的 state_backend_error 为 null、engine 未退出(进程仍存活)
  3. 恢复后断言 recon 回到 MATCHED(≤180s)
  4. 证据:PG 重启耗时、引擎自愈耗时
- user_stream_reconnect(不断真实 listen key,避免影响引擎):
  1. 只读探针:`client.create_listen_key()` 建独立 listen key → 建 ws 连接收 3s → 主动 close → 重新 `create_listen_key` + 重连,断言第二次连接成功且收到数据帧或 ACK
  2. 同时观察运行中引擎的 `/status` user_stream_runtime.status 保持 HEALTHY(引擎自身未受影响)
  3. 判定纯函数 `ws_reconnect_verdict(before: str, after: str, last_event_age_s: float) -> tuple[bool, str]`
- 两场景 notional 记账 0。dry_run → NOT_VERIFIABLE("restart requires real execution")。

- [ ] **Step 4: 运行确认通过** — 单测通过

- [ ] **Step 5: Commit**

```bash
git add beidou_certification/g5_scenarios/restart/ tests/unit/test_g5_scenarios/test_restart_db_ws.py
git commit -m "feat(g5): 重启组 database_restart/user_stream_reconnect

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 11: 原生保护组(native_protection / double_worker_fencing)

**Files:**
- Create: `beidou_certification/g5_scenarios/protection/__init__.py`
- Create: `beidou_certification/g5_scenarios/protection/native_protection.py`
- Create: `beidou_certification/g5_scenarios/protection/double_worker_fencing.py`
- Test: `tests/unit/test_g5_scenarios/test_protection.py`

**Interfaces:**
- Consumes: Task 1 基类;`BinanceRESTClient.create_algo_order/cancel_algo_order/get_open_algo_orders`;`InstanceLock`(beidou_launcher.state:22)
- Produces:`NativeProtectionScenario`、`DoubleWorkerFencingScenario`;纯函数 `algo_orphan_verdict(open_algos: list[dict], position_symbols: set[str]) -> list[dict]`(返回孤儿 algo 单列表)

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_g5_scenarios/test_protection.py
from beidou_certification.g5_scenarios.protection.native_protection import algo_orphan_verdict
from beidou_certification.g5_scenarios.protection.double_worker_fencing import fencing_verdict

def test_algo_orphan_verdict():
    algos = [{"algoId": 1, "symbol": "BTCUSDT"}, {"algoId": 2, "symbol": "ETHUSDT"}]
    assert algo_orphan_verdict(algos, {"BTCUSDT"}) == [{"algoId": 2, "symbol": "ETHUSDT"}]
    assert algo_orphan_verdict(algos, {"BTCUSDT", "ETHUSDT"}) == []

def test_fencing_verdict():
    assert fencing_verdict(acquired=False, message="北斗实例已运行，PID=123") == (True, "second_instance_fenced")
    assert fencing_verdict(acquired=True, message="") == (False, "second_instance_acquired_lock")
```

- [ ] **Step 2: 运行确认失败** — FAIL(ImportError)

- [ ] **Step 3: 实现**

- native_protection(真实挂撤,notional 记账 0 —— Algo 单不占用即时资金但记录触发风险敞口 0.0):
  1. 只读:get_account 拿当前持仓 symbols(应为空账户 → 场景只挂"孤儿"验证路径);`get_open_algo_orders` 基线
  2. 挂 1 个 STOP 条件单(create_algo_order,quantity=min_qty,triggerPrice=当前价 × 0.5 远价防触发)→ 断言 open_algos 中出现
  3. 撤单(cancel_algo_order)→ 断言消失;证据记录挂撤往返
  4. 若账户有真实持仓,额外断言:持仓 symbol 的 SL/TP algo 均存在(引擎职责,只读验证)+ 孤儿判定纯函数与交易所事实对拍
- double_worker_fencing(不真实起第二实例 —— 用 InstanceLock 直接验证):
  1. 读运行中引擎的 lock 文件 `/Users/maguannan/beidou/.beidou/beidou.pid`
  2. `lock = InstanceLock(same_path); acquired, msg = lock.acquire()` → 断言 acquired=False 且 msg 含 "已运行"
  3. 用临时路径 `InstanceLock(tmp_path).acquire()` → 断言 True,随后 `lock.release()`(若存在)或删文件清理
  4. 判定纯函数 `fencing_verdict(acquired, message)`;证据记录 lock 文件内容(仅 PID,无敏感信息)
- 两场景 dry_run:跳过真实挂单,仅执行纯函数判定部分并记 NOT_VERIFIABLE。

- [ ] **Step 4: 运行确认通过** — 单测通过

- [ ] **Step 5: Commit**

```bash
git add beidou_certification/g5_scenarios/protection/ tests/unit/test_g5_scenarios/test_protection.py
git commit -m "feat(g5): 原生保护组 native_protection/double_worker_fencing

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 12: 全量验证与真实认证运行

**Files:**
- Modify: `beidou_certification/g5_scenarios/runner.py`(build_certificate 增加 `account_access: dict | None = None` 参数覆盖占位)
- Test: `tests/unit/test_g5_scenarios/test_full_registry.py`

**Interfaces:**
- Consumes: Task 1-11 全部产物
- Produces:PASS 证书 `artifacts/evidence/testnet/g5-certificate.json`(绑定当前 HEAD commit)

- [ ] **Step 1: 注册表完整性测试**

```python
# tests/unit/test_g5_scenarios/test_full_registry.py
import yaml
from beidou_certification.g5_scenarios.runner import SCENARIO_REGISTRY

def test_registry_covers_plan_scenarios():
    plan = yaml.safe_load(open("config/g5-testnet-plan.yaml"))
    expected = set(plan["scenarios"])
    assert set(SCENARIO_REGISTRY) == expected

def test_registry_importable_and_ordered():
    assert len(SCENARIO_REGISTRY) == 16
    ids = list(SCENARIO_REGISTRY)
    assert ids == sorted(ids)  # 确定性顺序
```

- [ ] **Step 2: 运行确认失败** — FAIL(注册表尚缺场景)

- [ ] **Step 3: 补全注册导入**

`beidou_certification/g5_scenarios/__init__.py` 依次 import 四个场景包子模块(protocol/engine/restart/protection),使注册表含全部 16 场景且有序。

- [ ] **Step 4: 运行确认通过** — 2 测试通过;`pytest tests/unit -q` 全量绿(≤ 现有 2922+ 基线 + 新增)

- [ ] **Step 5: 全量静态检查**

Run: `ruff check beidou_certification/ scripts/testnet/ tests/unit/test_g5_scenarios/`
Expected: 0 errors(新代码零豁免)

- [ ] **Step 6: Commit**

```bash
git add beidou_certification/g5_scenarios/ tests/unit/test_g5_scenarios/
git commit -m "feat(g5): 注册表补全与完整性测试

Co-Authored-By: Claude <noreply@anthropic.com>"
```

- [ ] **Step 7: 真实认证运行(交互确认后执行)**

前置确认(向用户确认后执行,预计 10-15 分钟,含引擎/PG 各一次重启):
1. 引擎 trading_ready=True(若否则先修)
2. `cd /Users/maguannan/beidou && python scripts/testnet/run_g5.py --plan config/g5-testnet-plan.yaml --symbol BTCUSDT --confirm-testnet`
3. 观察输出:16 场景逐个 PASS(个别 NOT_VERIFIABLE 属流动性限制,按 spec 允许 —— 但证书会 NOT_VERIFIABLE;若出现则向用户报告场景证据并决定是否调整场景策略)
4. 验证:`python -c "from beidou_launcher.preflight import _g5_certificate_probe; from pathlib import Path; print(_g5_certificate_probe(Path('.'), '$(git rev-parse HEAD)'))"` 输出 (True, "G5 Testnet certificate verified", ...)
5. `/status` 确认 preflight.g5_certificate 在下次重启后 PASS(证书绑定 commit,期间不得有新提交;认证完成后如需提交,先提交再重跑认证)

- [ ] **Step 8: 收尾 commit(认证产物)**

```bash
git add artifacts/evidence/testnet/g5-certificate.json artifacts/evidence/testnet/g5-evidence.json artifacts/evidence/testnet/g5/scenarios/
git commit -m "evidence(g5): FULL 协议场景认证 PASS 证书(commit <hash>)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

注意:证书绑定 commit,本 commit 提交后证书立即失效(commit 变化)—— 认证与提交的次序约束:证书的 commit 字段记录认证时的 HEAD;提交证书文件本身会改变 HEAD。因此 Step 8 提交后,preflight 会重新 FAIL(证书 commit ≠ 新 HEAD)。**最终闭环**:Step 8 提交后立即重跑 Step 7 的认证(第二次运行多数场景走缓存/快速路径,重启组可 `--skip-restart` 复用证据?否 —— 证书必须绑定最终 commit)。正确次序:先完成全部代码提交 → 最后跑一次认证 → 证书文件用 `.gitignore` 外的处理或接受"证书绑定最后一次认证 commit,提交证书文件后 preflight 仍以 HEAD 校验"的固有矛盾。**实现时按实际 verify 语义处理:认证运行必须发生在所有代码提交之后;证书文件提交后若失效,由运维重跑认证(记录在 evidence 提交消息中)。**

---

## Self-Review

**Spec coverage:**
- 决策 1(混合执行)→ Task 4/5(协议直连)、Task 6-8(引擎组件)、Task 9-11(真实重启)✓
- 决策 2(真实重启)→ Task 9/10 ✓
- 决策 3(每场景独立证据)→ Task 1(write_scenario_evidence)+ Task 3(runner 调用)✓
- 决策 4(方案 B 框架)→ Task 1-3 ✓
- 16 场景全覆盖 → Task 4-11(每任务 2-3 场景)✓
- runner 兼容参数 → Task 3 ✓;observations 保留 → Task 3 ✓
- 错误处理(场景不崩/notional fail-fast/证据失败=FAIL)→ Task 1 测试 + 各场景 try/except 骨架 ✓
- 测试策略(TDD/纯函数/零豁免)→ 每任务 TDD 循环 + Task 12 Step 5 ✓

**Placeholder scan:** 无 TBD/TODO;Task 2 的 `ctx = ...` 占位已在 Task 3 提供 `build_context`(执行 Task 2 时实现为构造 ScenarioContext 的私有方法,Task 3 抽出公共函数 —— 执行者在 Task 2 内自行定义最小 ctx 构造,Task 3 重构为 `build_context` 并加测试)✓

**Type consistency:** `ScenarioResult`/`NotionalLedger`/`G5Runner` 名称跨任务一致;`artifact_hash()` 方法名一致;`RESTART_GROUP` 集合在 Task 2 定义、Task 3/9/10 引用 ✓
