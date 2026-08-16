"""引擎组场景三单元测试:position_diff 判定纯函数(brief 单测 verbatim)、
场景全流程(假时钟 + 假连接仿真引擎 30s 对账周期:坏基线 → MISMATCHED →
恢复 → MATCHED)、dry_run 不碰 PG、注册接线、NOT_VERIFIABLE / FAIL 路径
与任何路径下 finally 恢复原始基线。

行为断言真实:假连接按「当前基线 payload」仿真引擎对账事件,写坏基线后
(now - 损坏时刻 ≥ 对账周期 30s)生成 MISMATCHED 事件+快照,恢复后生成
MATCHED 事件 —— 场景的真实轮询循环被完整驱动,而非打桩短路。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from beidou_certification.g5_scenarios.base import NotionalLedger, ScenarioContext, ScenarioStatus
from beidou_certification.g5_scenarios.engine.reconciliation_mismatch import (
    ReconciliationMismatchScenario,
    position_diff,
)
from beidou_certification.g5_scenarios.runner import SCENARIO_REGISTRY

_RECORD_TYPE = "account_opening_projection"
_RECORD_ID = "default:BINANCE"
_RECON_PERIOD = 30.0  # 引擎对账周期实测 ~32s(2026-08-17 06:09:11 → 06:09:43),假仿真取 30s

_ORIGINAL_PAYLOAD: dict[str, Any] = {
    "projection_id": "operator-rebaseline-2026-08-17-v1",
    "account_id": "default",
    "venue_id": "BINANCE",
    "balance_amount": "10545.04904025",
    "balance_currency": "USDT",
    "balance_decimals": 8,
    "positions": {},
    "open_orders": [],
    "captured_at": "2026-08-16T19:38:23.850834+00:00",
    "source": "OPERATOR_REBASELINE",
    "fact_version": "operator-rebaseline-2026-08-17-v1",
    "evidence_hash": "684ed7a3a5063c494253e09b9def8ae6f2b92b4caa5a6441f77c702b35553b75",
    "approval_id": "operator-manual-2026-08-17-v1",
    "complete": True,
    "created_at": "2026-08-16T19:38:23.850834+00:00",
}

# ---- 判定纯函数(brief 单测,verbatim) ----


def test_position_diff() -> None:
    assert position_diff({"BTCUSDT": "1.5"}, {"BTCUSDT": "1.5"}) == {}
    assert position_diff({"BTCUSDT": "1.5"}, {}) == {"BTCUSDT": ("1.5", "0")}
    assert position_diff({}, {"BTCUSDT": "2"}) == {"BTCUSDT": ("0", "2")}
    assert position_diff({"A": "1", "B": "2"}, {"A": "1", "C": "3"}) == {"B": ("2", "0"), "C": ("0", "3")}


# ---- 判定纯函数边界 ----


def test_position_diff_edges() -> None:
    # 双侧同键值不同 → 双值;同键同值 → 不计入;顺序无关
    assert position_diff({"A": "1", "B": "2"}, {"B": "9", "A": "1"}) == {"B": ("2", "9")}
    assert position_diff({"A": "1", "B": "2"}, {"B": "2", "A": "1"}) == {}
    # 空对空
    assert position_diff({}, {}) == {}
    # 全单侧缺失
    assert position_diff({"A": "1", "B": "2"}, {}) == {"A": ("1", "0"), "B": ("2", "0")}
    assert position_diff({}, {"A": "1", "B": "2"}) == {"A": ("0", "1"), "B": ("0", "2")}


# ---- 场景执行上下文与假时钟/假连接 ----


def _ctx(tmp_path: Path, *, dry_run: bool = False) -> ScenarioContext:
    return ScenarioContext(
        client=None,
        ledger=NotionalLedger(1000.0),
        evidence_dir=tmp_path,
        symbol="BTCUSDT",
        dry_run=dry_run,
    )


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


class _FakeClock:
    """假时钟:now() 返回当前假时间,sleep() 直接推进假时间。"""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


class _FakeCursor:
    def __init__(self, conn: "_FakeReconConn", sql: str, params: tuple[Any, ...]) -> None:
        self._conn = conn
        self._sql = sql
        self._params = params
        self.rowcount = 0

    def fetchone(self) -> Any:
        return self._conn._fetchone(self._sql, self._params)

    def fetchall(self) -> list[Any]:
        return self._conn._fetchall(self._sql, self._params)


class _FakeReconConn:
    """内存版 v3_runtime_records/events:按「当前基线 payload」仿真引擎对账。

    引擎语义仿真:写坏基线后(now - 损坏时刻 ≥ 对账周期)生成 MISMATCHED
    事件 + system/exchange 快照;恢复后(now - 恢复时刻 ≥ 对账周期)生成
    MATCHED 事件。emit_mismatch / emit_matched 可关(超时路径);fail_events_query
    可注入查询异常(异常自捕获路径)。
    """

    def __init__(
        self,
        original: dict[str, Any],
        *,
        clock: _FakeClock,
        emit_mismatch: bool = True,
        emit_matched: bool = True,
        recon_period: float = _RECON_PERIOD,
    ) -> None:
        self.original = dict(original)
        self.clock = clock
        self.emit_mismatch = emit_mismatch
        self.emit_matched = emit_matched
        self.recon_period = recon_period
        self.records: dict[tuple[str, str], dict[str, Any]] = {(_RECORD_TYPE, _RECORD_ID): dict(original)}
        self.events: list[dict[str, Any]] = []
        self.corrupt_at: float | None = None
        self.restore_at: float | None = None
        self.fail_events_query = False
        self.upserts = 0
        self.mismatch_created = False
        self.matched_created = False

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _FakeCursor:
        normalized = " ".join(sql.split())
        values = tuple(params)
        if normalized.startswith("INSERT INTO v3_runtime_records"):
            payload = json.loads(str(values[2]))
            self.records[(str(values[0]), str(values[1]))] = payload
            if (str(values[0]), str(values[1])) == (_RECORD_TYPE, _RECORD_ID):
                self.upserts += 1
                if str(payload.get("balance_amount")) != str(self.original.get("balance_amount")):
                    self.corrupt_at = self.clock.now()
                else:
                    self.restore_at = self.clock.now()
        return _FakeCursor(self, normalized, values)

    def _materialize_events(self) -> None:
        now = self.clock.now()
        if (
            self.emit_mismatch
            and not self.mismatch_created
            and self.corrupt_at is not None
            and now - self.corrupt_at >= self.recon_period
        ):
            checked_at = _iso(self.corrupt_at + self.recon_period)
            self.events.append(
                {
                    "event_id": "evt-fake-mismatch",
                    "occurred_at": checked_at,
                    "payload": {
                        "result_id": "recon-fake-mismatch",
                        "account_id": "default",
                        "venue_id": "BINANCE",
                        "status": "MISMATCHED",
                        "matched": False,
                        "differences": [
                            "Balance mismatch: system=1 exchange=10545.04904025 diff=10544.049040 tolerance=105.450491"
                        ],
                        "checked_at": checked_at,
                        "system_snapshot_id": "recon-fake-mismatch",
                        "exchange_snapshot_id": "recon-fake-mismatch",
                    },
                }
            )
            self.records[("reconciliation_snapshot", "recon-fake-mismatch:SYSTEM")] = {"positions": {}}
            self.records[("reconciliation_snapshot", "recon-fake-mismatch:EXCHANGE")] = {"positions": {}}
            self.mismatch_created = True
        if (
            self.emit_matched
            and not self.matched_created
            and self.restore_at is not None
            and now - self.restore_at >= self.recon_period
        ):
            checked_at = _iso(self.restore_at + self.recon_period)
            self.events.append(
                {
                    "event_id": "evt-fake-matched",
                    "occurred_at": checked_at,
                    "payload": {
                        "result_id": "recon-fake-matched",
                        "account_id": "default",
                        "venue_id": "BINANCE",
                        "status": "MATCHED",
                        "matched": True,
                        "differences": [],
                        "checked_at": checked_at,
                        "system_snapshot_id": "recon-fake-matched",
                        "exchange_snapshot_id": "recon-fake-matched",
                    },
                }
            )
            self.matched_created = True

    def _fetchone(self, sql: str, params: tuple[Any, ...]) -> Any:
        if self.fail_events_query and "FROM v3_runtime_events" in sql:
            raise RuntimeError("fake events query failure")
        if sql.startswith("SELECT payload::text FROM v3_runtime_records"):
            payload = self.records.get((str(params[0]), str(params[1])))
            return (json.dumps(payload),) if payload is not None else None
        if sql.startswith("SELECT event_id,payload::text,occurred_at FROM v3_runtime_events"):
            self._materialize_events()
            after = str(params[1])
            candidates = [e for e in self.events if str(e["occurred_at"]) >= after]
            candidates.sort(key=lambda e: str(e["occurred_at"]), reverse=True)
            if not candidates:
                return None
            best = candidates[0]
            return (best["event_id"], json.dumps(best["payload"]), best["occurred_at"])
        return None

    def _fetchall(self, sql: str, params: tuple[Any, ...]) -> list[Any]:
        return []

    @contextmanager
    def transaction(self) -> Iterator["_FakeReconConn"]:
        yield self

    def close(self) -> None:
        return None


def _scenario(
    clock: _FakeClock,
    *,
    min_wait: float = 35.0,
    mismatch_deadline: float = 60.0,
    matched_deadline: float = 60.0,
) -> ReconciliationMismatchScenario:
    return ReconciliationMismatchScenario(
        now=clock.now,
        sleep=clock.sleep,
        poll_interval=5.0,
        min_wait=min_wait,
        mismatch_deadline=mismatch_deadline,
        matched_deadline=matched_deadline,
    )


def _run_flow(
    tmp_path: Path,
    monkeypatch: Any,
    *,
    emit_mismatch: bool = True,
    emit_matched: bool = True,
    fail_events_query: bool = False,
) -> tuple[ReconciliationMismatchScenario, _FakeClock, _FakeReconConn, Any]:
    clock = _FakeClock()
    conn = _FakeReconConn(_ORIGINAL_PAYLOAD, clock=clock, emit_mismatch=emit_mismatch, emit_matched=emit_matched)
    conn.fail_events_query = fail_events_query
    scenario = _scenario(clock)
    monkeypatch.setattr(
        "beidou_certification.g5_scenarios.engine.reconciliation_mismatch.psycopg.connect",
        lambda *args, **kwargs: conn,
    )
    ctx = _ctx(tmp_path)
    return scenario, clock, conn, ctx


# ---- 注册接线 ----


def test_registered() -> None:
    assert SCENARIO_REGISTRY["reconciliation_mismatch"] is ReconciliationMismatchScenario


# ---- dry_run 不碰 PG ----


def test_dry_run_does_not_touch_pg(tmp_path: Path, monkeypatch: Any) -> None:
    def _no_pg(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("dry_run 不得触碰 PG")

    monkeypatch.setattr("beidou_certification.g5_scenarios.engine.reconciliation_mismatch.psycopg.connect", _no_pg)
    ctx = _ctx(tmp_path, dry_run=True)
    result = asyncio.run(ReconciliationMismatchScenario().run(ctx))
    assert result.status == ScenarioStatus.PASS
    assert result.evidence["dry_run"] is True
    assert ctx.ledger.total == 0.0


# ---- 全流程:坏基线 → MISMATCHED → 恢复 → MATCHED ----


def test_full_flow_mismatch_observed_and_recovered(tmp_path: Path, monkeypatch: Any) -> None:
    scenario, clock, conn, ctx = _run_flow(tmp_path, monkeypatch)
    result = asyncio.run(scenario.run(ctx))
    assert result.status == ScenarioStatus.PASS
    evidence = result.evidence
    assert evidence["verdict"] == "mismatch_observed_and_recovered"
    assert evidence["notional_usdt"] == 0.0
    assert ctx.ledger.total == 0.0
    steps = evidence["steps"]

    # 1. 基线读取(hash 证据)
    read = next(s for s in steps if s.get("action") == "read_baseline")
    assert read["record_id"] == _RECORD_ID
    assert read["balance_amount"] == "10545.04904025"

    # 2. 写坏基线:balance_amount=1,positions 原样(纯函数对拍为空)
    corrupt = next(s for s in steps if s.get("action") == "corrupt_baseline")
    assert corrupt["balance_amount"] == "1"
    assert corrupt["positions_untouched"] == {}

    # 3. MISMATCHED 观测:result_id + differences 截断
    mismatch = next(s for s in steps if s.get("action") == "mismatch_observed")
    assert mismatch["result_id"] == "recon-fake-mismatch"
    assert mismatch["status"] == "MISMATCHED"
    assert any("Balance mismatch" in d for d in mismatch["differences_truncated"])

    # 4. observed vs expected 对拍:余额行含坏基线余额 system=1;仓位差为空一致
    recon = next(s for s in steps if s.get("action") == "observed_vs_expected")
    assert recon["system_balance_ok"] is True
    assert recon["expected_position_diff"] == {}
    assert recon["observed_position_lines"] == []
    assert recon["snapshot_consistent"] is True
    assert recon["expected_balance_system"] == "1"
    assert recon["expected_balance_reference"] == "10545.04904025"

    # 5. 恢复 MATCHED + 基线回读验证
    matched = next(s for s in steps if s.get("action") == "matched_observed")
    assert matched["status"] == "MATCHED"
    restore = next(s for s in steps if s.get("action") == "restore_verify")
    assert restore["ok"] is True
    assert restore["positions_restored"] == {}
    final = next(s for s in steps if s.get("action") == "restore_baseline_finally")
    assert final["ok"] is True

    # 6. 轮询行为真实:≥35s 后第一轮即发现 MISMATCHED;恢复后按周期等 MATCHED
    polls = [s for s in steps if s.get("action") == "poll_found"]
    assert polls[0]["want_status"] == "MISMATCHED"
    assert polls[0]["polls"] >= 1
    assert polls[1]["want_status"] == "MATCHED"
    assert clock.t >= 35.0 + 60.0  # 至少消耗 35s 等待 + 恢复后到下一周期

    # 7. 基线零残留:假连接里 records 与原始一致
    assert conn.records[(_RECORD_TYPE, _RECORD_ID)] == _ORIGINAL_PAYLOAD


# ---- MISMATCHED 未观测到 → NOT_VERIFIABLE,基线仍恢复 ----


def test_mismatch_not_observed_not_verifiable(tmp_path: Path, monkeypatch: Any) -> None:
    scenario, clock, conn, ctx = _run_flow(tmp_path, monkeypatch, emit_mismatch=False)
    result = asyncio.run(scenario.run(ctx))
    assert result.status == ScenarioStatus.NOT_VERIFIABLE
    assert result.error_type == "mismatch_not_observed"
    assert "MISMATCHED" in result.error_message
    steps = result.evidence["steps"]
    timeout = next(s for s in steps if s.get("action") == "poll_timeout")
    assert timeout["want_status"] == "MISMATCHED"
    assert timeout["deadline_seconds"] == 60.0
    final = next(s for s in steps if s.get("action") == "restore_baseline_finally")
    assert final["ok"] is True
    # 原始基线仍恢复
    assert conn.records[(_RECORD_TYPE, _RECORD_ID)] == _ORIGINAL_PAYLOAD
    # 轮询确实跑满 60s 窗口(35s 起步 + 5s 步进 × 12 = 95s)
    assert clock.t >= 35.0 + 60.0


# ---- 恢复后 MATCHED 超时 → FAIL,基线仍恢复 ----


def test_matched_recovery_timeout_fail(tmp_path: Path, monkeypatch: Any) -> None:
    scenario, _clock, conn, ctx = _run_flow(tmp_path, monkeypatch, emit_matched=False)
    result = asyncio.run(scenario.run(ctx))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "MatchedRecoveryTimeoutError"
    assert "MATCHED_RECOVERY_TIMEOUT" in result.error_message
    steps = result.evidence["steps"]
    # MISMATCHED 观测到,恢复也执行了
    assert any(s.get("action") == "mismatch_observed" for s in steps)
    timeout = next(s for s in steps if s.get("action") == "poll_timeout")
    assert timeout["want_status"] == "MATCHED"
    # 基线仍恢复(finally)
    assert conn.records[(_RECORD_TYPE, _RECORD_ID)] == _ORIGINAL_PAYLOAD


# ---- 查询异常 → FAIL 自捕获,finally 仍恢复基线 ----


def test_events_query_failure_fail_and_restore(tmp_path: Path, monkeypatch: Any) -> None:
    scenario, _clock, conn, ctx = _run_flow(tmp_path, monkeypatch, fail_events_query=True)
    result = asyncio.run(scenario.run(ctx))
    assert result.status == ScenarioStatus.FAIL
    assert result.error_type == "RuntimeError"
    assert "fake events query failure" in result.error_message
    steps = result.evidence["steps"]
    # 异常路径下 finally 仍恢复原始基线
    final = next(s for s in steps if s.get("action") == "restore_baseline_finally")
    assert final["ok"] is True
    assert conn.records[(_RECORD_TYPE, _RECORD_ID)] == _ORIGINAL_PAYLOAD
    # 未观测到 MISMATCHED 之前就失败 → 没有 mismatch_observed 步骤
    assert not any(s.get("action") == "mismatch_observed" for s in steps)
