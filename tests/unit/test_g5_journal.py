"""G5 journal: 搁浅还原与 preflight 检测 (不依赖真实 PG)。

覆盖:
- 纯函数 _recover_from_journal(搁浅 journal → 原始基线 payload 往返)
- 纯函数 _g5_journal_check(有 journal → P0 FAIL;无 journal → PASS;查询
  异常按 PASS + evidence 记录 error)
- 场景全流程(内存假连接):污染前写 journal、还原成功后删 journal、
  NOT_VERIFIABLE 时 finally 兜底还原后删 journal、搁浅 journal 启动自愈
  (并以还原后的真基线作为本次运行的 original_payload)
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import contextmanager
from typing import Any, Iterator

import pytest

from beidou_certification.g5_scenarios.base import NotionalLedger, ScenarioContext, ScenarioStatus
from beidou_certification.g5_scenarios.engine import reconciliation_mismatch as rm
from beidou_certification.g5_scenarios.engine.reconciliation_mismatch import ReconciliationMismatchScenario
from beidou_launcher.models import CheckStatus
from beidou_launcher.preflight import _g5_journal_check

_JOURNAL_RECORD_TYPE = "g5_journal"
_JOURNAL_RECORD_ID = "reconciliation_mismatch:baseline"
_BASELINE_RECORD_TYPE = "account_opening_projection"
_BASELINE_RECORD_ID = "default:BINANCE"
_JOURNAL_KEY = (_JOURNAL_RECORD_TYPE, _JOURNAL_RECORD_ID)
_BASELINE_KEY = (_BASELINE_RECORD_TYPE, _BASELINE_RECORD_ID)

_ORIGINAL_PAYLOAD: dict[str, Any] = {
    "account_id": "default",
    "venue_id": "BINANCE",
    "balance_amount": "10730.29894895",
    "balance_currency": "USDT",
    "positions": {"BTCUSDT": "0.0008"},
    "captured_at": "2026-08-24T03:00:00+00:00",
    "source": "OPERATOR_REBASELINE",
    "complete": True,
}


class _FlowCursor:
    def __init__(self, conn: "_FlowConn", sql: str, params: tuple[Any, ...]) -> None:
        self._conn = conn
        self._sql = sql
        self._params = params

    def fetchone(self) -> Any:
        return self._conn._fetchone(self._sql, self._params)

    def fetchall(self) -> list[Any]:
        return []


class _FlowConn:
    """内存版 v3_runtime_records + 固定事件序列(MISMATCHED → MATCHED)。

    DELETE 真的从内存 records 移除(验证 journal 删净);事件在轮询查询时
    依次返回,配合假时钟驱动场景全流程,无需真实时间等待。
    """

    def __init__(self, *, baseline: dict[str, Any] | None = None, journal: dict[str, Any] | None = None) -> None:
        self.records: dict[tuple[str, str], dict[str, Any]] = {}
        if baseline is not None:
            self.records[_BASELINE_KEY] = baseline
        if journal is not None:
            self.records[_JOURNAL_KEY] = journal
        self.upserted: list[tuple[str, str]] = []
        self.journal_writes: list[dict[str, Any]] = []
        self._mismatch_sent = False

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _FlowCursor:
        normalized = " ".join(sql.split())
        values = tuple(params)
        if normalized.startswith("INSERT INTO v3_runtime_records"):
            payload = json.loads(str(values[2]))
            key = (str(values[0]), str(values[1]))
            self.records[key] = payload
            self.upserted.append(key)
            if key == _JOURNAL_KEY:
                self.journal_writes.append(payload)
        elif normalized.startswith("DELETE FROM v3_runtime_records"):
            self.records.pop((str(values[0]), str(values[1])), None)
        return _FlowCursor(self, normalized, values)

    def _fetchone(self, sql: str, params: tuple[Any, ...]) -> Any:
        if sql.startswith("SELECT payload::text FROM v3_runtime_records"):
            payload = self.records.get((str(params[0]), str(params[1])))
            return (json.dumps(payload),) if payload is not None else None
        if sql.startswith("SELECT event_id,payload::text,occurred_at FROM v3_runtime_events"):
            if not self._mismatch_sent:
                self._mismatch_sent = True
                return (
                    "evt-fake-mismatch",
                    json.dumps(
                        {
                            "result_id": "recon-fake-mismatch",
                            "status": "MISMATCHED",
                            "differences": [
                                "Balance mismatch: system=1 exchange=10730.29894895 "
                                "diff=10729.298949 tolerance=107.302989"
                            ],
                        }
                    ),
                    "2026-08-24T03:01:00+00:00",
                )
            return (
                "evt-fake-matched",
                json.dumps({"result_id": "recon-fake-matched", "status": "MATCHED", "differences": []}),
                "2026-08-24T03:02:00+00:00",
            )
        return None

    @contextmanager
    def transaction(self) -> Iterator["_FlowConn"]:
        yield self

    def close(self) -> None:
        return None


class _FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


def _scenario(clock: _FakeClock | None = None) -> ReconciliationMismatchScenario:
    if clock is None:
        clock = _FakeClock()
    return ReconciliationMismatchScenario(
        now=clock.now,
        sleep=clock.sleep,
        poll_interval=5.0,
        min_wait=35.0,
        mismatch_deadline=60.0,
        matched_deadline=60.0,
    )


def _ctx(tmp_path: Any) -> ScenarioContext:
    return ScenarioContext(
        client=None,
        ledger=NotionalLedger(1000.0),
        evidence_dir=tmp_path,
        symbol="BTCUSDT",
        dry_run=False,
    )


def _run_with_conn(conn: _FlowConn, ctx: ScenarioContext, monkeypatch: Any) -> Any:
    monkeypatch.setattr(
        "beidou_certification.g5_scenarios.engine.reconciliation_mismatch.psycopg.connect",
        lambda *_args, **_kwargs: conn,
    )
    return asyncio.run(_scenario().run(ctx))


# ---- 纯函数:搁浅 journal → 原始基线 payload 往返 ----

def test_stranded_journal_recovery_payload_roundtrip() -> None:
    original = {"balance_amount": "10730.29894895", "positions": {"BTCUSDT": "0.0008"}}
    journal = {"original_payload": original, "corrupted_at": "2026-08-24T03:00:00+00:00", "scenario_run_id": "r1"}
    restored = rm._recover_from_journal({"payload": journal})
    assert restored == original


def test_recover_from_journal_accepts_bare_payload_dict() -> None:
    original = {"balance_amount": "10730.29894895"}
    assert rm._recover_from_journal({"original_payload": original}) == original


def test_recover_from_journal_missing_original_payload_raises() -> None:
    with pytest.raises(RuntimeError, match="JOURNAL_PAYLOAD_MISSING"):
        rm._recover_from_journal({"corrupted_at": "x"})


# ---- 纯函数:preflight _g5_journal_check ----

def test_preflight_reports_journal_presence(tmp_path: Any) -> None:
    res = _g5_journal_check(journal_rows=[{"payload": {"corrupted_at": "x", "scenario_run_id": "r"}}])
    assert res.status is CheckStatus.FAIL
    assert "g5_journal" in res.message


def test_preflight_check_passes_without_journal() -> None:
    res = _g5_journal_check(journal_rows=[])
    assert res.status is CheckStatus.PASS
    assert res.evidence["journal_count"] == 0


def test_preflight_check_records_probe_error_as_pass_evidence() -> None:
    res = _g5_journal_check(journal_rows=[], probe_error="OperationalError")
    assert res.status is CheckStatus.PASS
    assert res.evidence["probe_error"] == "OperationalError"


# ---- 场景全流程:污染前写 journal,还原成功后必删 ----

def test_full_flow_writes_journal_before_corruption_and_deletes_after_restore() -> None:
    conn = _FlowConn(baseline=_ORIGINAL_PAYLOAD)
    steps: list[dict[str, Any]] = []
    scenario = _scenario()
    result = asyncio.run(scenario._execute_flow(conn, _ORIGINAL_PAYLOAD, steps, time.monotonic()))
    assert result.status == ScenarioStatus.PASS
    # 污染前写过 journal(记录 original_payload),还原成功后 journal 必删
    assert len(conn.journal_writes) == 1
    assert conn.journal_writes[0]["original_payload"] == _ORIGINAL_PAYLOAD
    assert conn.journal_writes[0]["corrupted_at"]
    assert conn.journal_writes[0]["scenario_run_id"]
    assert _JOURNAL_KEY not in conn.records
    assert conn.records[_BASELINE_KEY] == _ORIGINAL_PAYLOAD
    written = next(s for s in steps if s.get("action") == "journal_written")
    assert written["corrupted_at"]
    deleted = next(s for s in steps if s.get("action") == "journal_deleted")
    assert deleted["ok"] is True
    # 恢复校验证据仍在
    assert next(s for s in steps if s.get("action") == "restore_verify")["ok"] is True


# ---- NOT_VERIFIABLE:finally 兜底还原基线后也删 journal(不残留假阻断) ----

def test_not_verifiable_finally_restores_baseline_and_deletes_journal(tmp_path: Any, monkeypatch: Any) -> None:
    conn = _FlowConn(baseline=_ORIGINAL_PAYLOAD)
    conn._mismatch_sent = True  # 事件序列只有 MATCHED → MISMATCHED 轮询超时
    result = _run_with_conn(conn, _ctx(tmp_path), monkeypatch)
    assert result.status == ScenarioStatus.NOT_VERIFIABLE
    steps = result.evidence["steps"]
    assert next(s for s in steps if s.get("action") == "restore_baseline_finally")["ok"] is True
    assert next(s for s in steps if s.get("action") == "journal_deleted_finally")["ok"] is True
    assert _JOURNAL_KEY not in conn.records
    assert conn.records[_BASELINE_KEY] == _ORIGINAL_PAYLOAD


# ---- 搁浅 journal 启动自愈:先还原,并以真基线作为本次 original_payload ----

def test_stranded_journal_recovered_on_run_and_flow_uses_true_original(
    tmp_path: Any, monkeypatch: Any
) -> None:
    corrupted = dict(_ORIGINAL_PAYLOAD)
    corrupted["balance_amount"] = "1"  # 上次运行污染窗口内被硬杀留下的哨兵值
    conn = _FlowConn(
        baseline=corrupted,
        journal={
            "original_payload": dict(_ORIGINAL_PAYLOAD),
            "corrupted_at": "2026-08-24T02:00:00+00:00",
            "scenario_run_id": "prev-run-1",
        },
    )
    result = _run_with_conn(conn, _ctx(tmp_path), monkeypatch)
    assert result.status == ScenarioStatus.PASS
    steps = result.evidence["steps"]
    recovered = next(s for s in steps if s.get("action") == "recovered_stranded_journal")
    assert recovered["hash"] == rm._payload_hash(_ORIGINAL_PAYLOAD)
    # 基线被还原为 journal 内真原始值,且本次运行重新写 journal 时记录的
    # original_payload 也是真原始值(而不是被污染的哨兵基线)
    assert conn.records[_BASELINE_KEY] == _ORIGINAL_PAYLOAD
    assert _JOURNAL_KEY not in conn.records
    assert len(conn.journal_writes) == 1
    assert conn.journal_writes[0]["original_payload"] == _ORIGINAL_PAYLOAD
