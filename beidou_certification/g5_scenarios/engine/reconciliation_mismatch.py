"""reconciliation_mismatch: 引擎组场景三 — 对账不一致观测与自动恢复验证。

共享 PG 基线(record_type='account_opening_projection', record_id='default:BINANCE'):
1. 只读:读当前基线 payload 存为 original_payload(证据记录其 hash)
2. 写坏基线:UPSERT 同 key,balance_amount="1"(故意错误),positions 保持原样
   → 运行中引擎(实测 ~32s 对账周期)下一轮 reconcile 系统侧余额 1 vs 交易所
   真实余额,差值远超容差 → MISMATCHED(引擎只观测不自我修复,失败时
   _safe_no_new_risk 关风险门 —— 短暂 MISMATCHED 为预期)
3. 轮询 v3_runtime_events(record_type='reconciliation_result')断言出现
   status=MISMATCHED(证据记录 result_id 与 differences 截断);
   60s 未观察到 → NOT_VERIFIABLE(引擎可能未在跑或对账暂停)
4. 恢复:UPSERT 回 original_payload → 再轮询 ≤60s 断言恢复 MATCHED(引擎自动恢复)
5. 纯函数 position_diff 把 expected 仓位差(坏基线只改余额 → 期望空)与
   observed differences(引擎 system/exchange 快照的仓位差 + 余额差行)对拍
- 不真实下单;notional 记账 0;dry_run 早退不碰 PG;run() 自捕获异常返回 FAIL;
  任何路径(finally)都恢复原始基线。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import psycopg

from beidou_certification.g5_scenarios.base import (
    NotionalExceededError,
    ScenarioBase,
    ScenarioContext,
    ScenarioResult,
    ScenarioStatus,
)
from beidou_certification.g5_scenarios.engine.ack_loss import PG_DSN
from beidou_certification.g5_scenarios.runner import SCENARIO_REGISTRY

logger = logging.getLogger(__name__)

# 共享 PG 基线 key(与引擎 _build_system_reconciliation_facts 的
# restore_account_opening_projection("default", "BINANCE") 同 key)
_BASELINE_RECORD_TYPE = "account_opening_projection"
_BASELINE_RECORD_ID = "default:BINANCE"
_CORRUPTED_BALANCE = "1"

# 引擎对账周期实测 ~32s(2026-08-17 06:09:11 → 06:09:43 MATCHED 事件间隔);
# 等待窗口按计划:≥35s 后开始轮询(覆盖一轮周期),60s 未观察到 MISMATCHED
# → NOT_VERIFIABLE;恢复后再轮询 ≤60s 断言 MATCHED。
_WAIT_MIN_SECONDS = 35.0
_MISMATCH_DEADLINE_SECONDS = 60.0
_MATCHED_DEADLINE_SECONDS = 60.0
_POLL_INTERVAL_SECONDS = 5.0
_DIFF_TRUNCATE_CHARS = 200

_UPSERT_SQL = (
    "INSERT INTO v3_runtime_records(record_type,record_id,payload,created_at,updated_at) "
    "VALUES (%s,%s,CAST(%s AS jsonb),CURRENT_TIMESTAMP,CURRENT_TIMESTAMP) "
    "ON CONFLICT(record_type,record_id) DO UPDATE SET "
    "payload=excluded.payload,updated_at=excluded.updated_at"
)
_BASELINE_READ_SQL = "SELECT payload::text FROM v3_runtime_records WHERE record_type=%s AND record_id=%s"
_RECON_EVENT_SQL = (
    "SELECT event_id,payload::text,occurred_at FROM v3_runtime_events "
    "WHERE record_type=%s AND occurred_at>=%s "
    "ORDER BY occurred_at DESC,event_id DESC LIMIT 1"
)


class MatchedRecoveryTimeoutError(RuntimeError):
    """恢复基线后引擎未在对账窗口内回到 MATCHED(error_type 自捕获即类名)。"""


def position_diff(local: dict[str, str], venue: dict[str, str]) -> dict[str, tuple[str, str]]:
    """本地(系统侧)与交易所侧仓位字典的差异:仅返回键值不同者,缺失侧补 "0"。

    键集合求并后逐个比较,单侧键/双侧键/多键混合都正确覆盖;值全为 str
    (brief 单测语义):相等 → {},单侧缺失 → (值, "0")/("0", 值),双侧同键
    值不同 → (本地, 交易所)。
    """
    return {
        symbol: (local.get(symbol, "0"), venue.get(symbol, "0"))
        for symbol in sorted(set(local) | set(venue))
        if local.get(symbol, "0") != venue.get(symbol, "0")
    }


def _payload_hash(payload: dict[str, Any]) -> str:
    """确定性 sha256:sort_keys + default=str,与 base.ScenarioResult.artifact_hash 同风格。"""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def _iso_from(now_seconds: float) -> str:
    return datetime.fromtimestamp(now_seconds, tz=timezone.utc).isoformat()


def _read_record(conn: Any, record_type: str, record_id: str) -> dict[str, Any] | None:
    cursor = conn.execute(_BASELINE_READ_SQL, (record_type, record_id))
    row = cursor.fetchone()
    if row is None:
        return None
    payload = json.loads(str(row[0]))
    return payload if isinstance(payload, dict) else None


def _upsert_record(conn: Any, record_type: str, record_id: str, payload: dict[str, Any]) -> None:
    """UPSERT 基线(INSERT ... ON CONFLICT DO UPDATE,与引擎 _write_record 同语义)。"""
    with conn.transaction():
        conn.execute(
            _UPSERT_SQL,
            (record_type, record_id, json.dumps(payload, ensure_ascii=False, default=str)),
        )


def _latest_reconciliation_event(conn: Any, *, after: str) -> dict[str, Any] | None:
    """读 occurred_at >= after 的最新 reconciliation_result 事件(引擎对账结果
    经 PostgresPersistentStore.save_reconciliation_result 写入事件表,payload 含
    status/result_id/differences/checked_at 与双侧快照 id)。"""
    cursor = conn.execute(_RECON_EVENT_SQL, ("reconciliation_result", after))
    row = cursor.fetchone()
    if row is None:
        return None
    payload = json.loads(str(row[1]))
    return {
        "event_id": str(row[0]),
        "payload": payload if isinstance(payload, dict) else {},
        "occurred_at": row[2],
    }


def _reconcile_observed_differences(
    conn: Any,
    event_payload: dict[str, Any],
    *,
    original_payload: dict[str, Any],
    corrupted_payload: dict[str, Any],
) -> dict[str, Any]:
    """position_diff 对拍:坏基线只改余额 → expected 仓位差为空(positions 原样);
    observed differences 中余额行必须含坏基线余额(system=<1>),仓位行与引擎
    system/exchange 快照经 position_diff 得的期望差一致性互证(单侧缺快照时
    snapshot_consistent 为 None,不阻断判定)。"""
    differences = [str(d) for d in event_payload.get("differences", []) or []]
    balance_lines = [d for d in differences if d.startswith("Balance mismatch")]
    position_lines = [d for d in differences if "position" in d.lower()]
    expected_position_diff = position_diff(
        dict(original_payload.get("positions", {})),
        dict(corrupted_payload.get("positions", {})),
    )
    snapshot_position_diff: dict[str, tuple[str, str]] | None = None
    snapshot_consistent: bool | None = None
    system_snapshot_id = event_payload.get("system_snapshot_id")
    exchange_snapshot_id = event_payload.get("exchange_snapshot_id")
    if system_snapshot_id and exchange_snapshot_id:
        system_snap = _read_record(conn, "reconciliation_snapshot", f"{system_snapshot_id}:SYSTEM")
        exchange_snap = _read_record(conn, "reconciliation_snapshot", f"{exchange_snapshot_id}:EXCHANGE")
        if system_snap is not None and exchange_snap is not None:
            snapshot_position_diff = position_diff(
                dict(system_snap.get("positions", {})),
                dict(exchange_snap.get("positions", {})),
            )
            snapshot_consistent = (len(snapshot_position_diff) > 0) == (len(position_lines) > 0)
    system_balance_ok = any(f"system={corrupted_payload.get('balance_amount')}" in line for line in balance_lines)
    return {
        "action": "observed_vs_expected",
        "expected_position_diff": {k: list(v) for k, v in expected_position_diff.items()},
        "observed_position_lines": position_lines,
        "snapshot_position_diff": (
            {k: list(v) for k, v in snapshot_position_diff.items()} if snapshot_position_diff is not None else None
        ),
        "snapshot_consistent": snapshot_consistent,
        "expected_balance_system": corrupted_payload.get("balance_amount"),
        "expected_balance_reference": original_payload.get("balance_amount"),
        "observed_balance_lines": [line[:_DIFF_TRUNCATE_CHARS] for line in balance_lines],
        "system_balance_ok": system_balance_ok,
    }


class ReconciliationMismatchScenario(ScenarioBase):
    scenario_id = "reconciliation_mismatch"

    def __init__(
        self,
        *,
        now: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        poll_interval: float = _POLL_INTERVAL_SECONDS,
        min_wait: float = _WAIT_MIN_SECONDS,
        mismatch_deadline: float = _MISMATCH_DEADLINE_SECONDS,
        matched_deadline: float = _MATCHED_DEADLINE_SECONDS,
    ) -> None:
        """时钟与等待可注入(测试用假时钟/假连接);默认真实时间与真实 sleep。

        _now 默认用 time.time(epoch 秒),与 _iso_from 的 fromtimestamp
        (epoch 基准)同源 —— 生产轮询 after 过滤/证据时间戳才是真实时间,
        不能是 time.monotonic(boot 相对秒被当 epoch 转 → 1970)。
        """
        self._now = now or time.time
        self._sleep = sleep or asyncio.sleep
        self._poll_interval = poll_interval
        self._min_wait = min_wait
        self._mismatch_deadline = mismatch_deadline
        self._matched_deadline = matched_deadline

    async def _poll_for_status(
        self,
        conn: Any,
        *,
        after: str,
        want_status: str,
        min_wait: float,
        deadline: float,
        steps: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """轮询最新 reconciliation_result 事件直到 want_status 或超时。

        min_wait 秒后才开始第一次查询(覆盖引擎一轮对账周期);随后每
        poll_interval 秒查询一次,deadline 秒内未观测到返回 None。
        """
        deadline_at = self._now() + deadline
        next_check = self._now() + min_wait
        polls = 0
        while True:
            delay = next_check - self._now()
            if delay > 0:
                await self._sleep(delay)
            polls += 1
            event = _latest_reconciliation_event(conn, after=after)
            if event is not None and str(event["payload"].get("status", "")) == want_status:
                steps.append(
                    {
                        "action": "poll_found",
                        "want_status": want_status,
                        "polls": polls,
                        "event_id": event["event_id"],
                        "occurred_at": str(event["occurred_at"]),
                    }
                )
                return event
            if self._now() >= deadline_at:
                steps.append(
                    {
                        "action": "poll_timeout",
                        "want_status": want_status,
                        "polls": polls,
                        "deadline_seconds": deadline,
                        "after": after,
                    }
                )
                return None
            next_check = self._now() + self._poll_interval

    async def _execute_flow(
        self,
        conn: Any,
        original_payload: dict[str, Any],
        steps: list[dict[str, Any]],
        started: float,
    ) -> ScenarioResult:
        corrupted = dict(original_payload)
        corrupted["balance_amount"] = _CORRUPTED_BALANCE
        positions_untouched = position_diff(
            dict(original_payload.get("positions", {})),
            dict(corrupted.get("positions", {})),
        )
        steps.append(
            {
                "action": "corrupt_baseline",
                "record_type": _BASELINE_RECORD_TYPE,
                "record_id": _BASELINE_RECORD_ID,
                "balance_amount": _CORRUPTED_BALANCE,
                "hash": _payload_hash(corrupted),
                "positions_untouched": {k: list(v) for k, v in positions_untouched.items()},
            }
        )
        _upsert_record(conn, _BASELINE_RECORD_TYPE, _BASELINE_RECORD_ID, corrupted)
        t_corrupt = self._now()
        mismatch_event = await self._poll_for_status(
            conn,
            after=_iso_from(t_corrupt),
            want_status="MISMATCHED",
            min_wait=self._min_wait,
            deadline=self._mismatch_deadline,
            steps=steps,
        )
        if mismatch_event is None:
            return self._fail(
                ScenarioStatus.NOT_VERIFIABLE,
                "mismatch_not_observed",
                "60s 内未观察到 reconciliation_result MISMATCHED(引擎可能未在运行或对账暂停)",
                {"steps": steps},
            )
        event_payload = mismatch_event["payload"]
        differences = [str(d) for d in event_payload.get("differences", []) or []]
        steps.append(
            {
                "action": "mismatch_observed",
                "event_id": mismatch_event["event_id"],
                "result_id": event_payload.get("result_id"),
                "status": event_payload.get("status"),
                "checked_at": event_payload.get("checked_at"),
                "differences_count": len(differences),
                "differences_truncated": [d[:_DIFF_TRUNCATE_CHARS] for d in differences],
            }
        )
        steps.append(
            _reconcile_observed_differences(
                conn,
                event_payload,
                original_payload=original_payload,
                corrupted_payload=corrupted,
            )
        )
        # 恢复原始基线
        _upsert_record(conn, _BASELINE_RECORD_TYPE, _BASELINE_RECORD_ID, original_payload)
        steps.append({"action": "restore_baseline", "hash": _payload_hash(original_payload)})
        t_restore = self._now()
        matched_event = await self._poll_for_status(
            conn,
            after=_iso_from(t_restore),
            want_status="MATCHED",
            min_wait=0.0,
            deadline=self._matched_deadline,
            steps=steps,
        )
        if matched_event is None:
            raise MatchedRecoveryTimeoutError("MATCHED_RECOVERY_TIMEOUT: 恢复基线后 60s 内引擎未回到 MATCHED")
        matched_payload = matched_event["payload"]
        steps.append(
            {
                "action": "matched_observed",
                "event_id": matched_event["event_id"],
                "result_id": matched_payload.get("result_id"),
                "status": matched_payload.get("status"),
                "checked_at": matched_payload.get("checked_at"),
            }
        )
        current = _read_record(conn, _BASELINE_RECORD_TYPE, _BASELINE_RECORD_ID)
        restored_ok = current is not None and current == original_payload
        steps.append(
            {
                "action": "restore_verify",
                "ok": restored_ok,
                "hash": _payload_hash(current) if current is not None else None,
                "positions_restored": (
                    {
                        k: list(v)
                        for k, v in position_diff(
                            dict(current.get("positions", {})),
                            dict(original_payload.get("positions", {})),
                        ).items()
                    }
                    if current is not None
                    else None
                ),
            }
        )
        return ScenarioResult(
            self.scenario_id,
            ScenarioStatus.PASS if restored_ok else ScenarioStatus.FAIL,
            {
                "steps": steps,
                "verdict": "mismatch_observed_and_recovered",
                "notional_usdt": 0.0,
            },
            time.monotonic() - started,
            error_type="" if restored_ok else "BASELINE_NOT_RESTORED",
        )

    async def run(self, ctx: ScenarioContext) -> ScenarioResult:
        started = time.monotonic()
        steps: list[dict[str, Any]] = []
        conn: Any = None
        original_payload: dict[str, Any] | None = None
        try:
            ctx.ledger.record(self.scenario_id, 0.0)
            if ctx.dry_run:
                return ScenarioResult(
                    self.scenario_id, ScenarioStatus.PASS, {"dry_run": True, "steps": steps}, time.monotonic() - started
                )
            logger.info(
                "reconciliation_mismatch: 写坏基线 -> 观测 MISMATCHED -> 恢复 MATCHED (共享 PG 基线 %s:%s,不真实下单)",
                _BASELINE_RECORD_TYPE,
                _BASELINE_RECORD_ID,
            )
            conn = psycopg.connect(PG_DSN, autocommit=True)
            original_payload = _read_record(conn, _BASELINE_RECORD_TYPE, _BASELINE_RECORD_ID)
            if original_payload is None:
                raise RuntimeError(f"{_BASELINE_RECORD_TYPE} 基线缺失({_BASELINE_RECORD_ID})")
            steps.append(
                {
                    "action": "read_baseline",
                    "record_type": _BASELINE_RECORD_TYPE,
                    "record_id": _BASELINE_RECORD_ID,
                    "hash": _payload_hash(original_payload),
                    "balance_amount": original_payload.get("balance_amount"),
                    "positions": {str(k): str(v) for k, v in dict(original_payload.get("positions", {})).items()},
                }
            )
            return await self._execute_flow(conn, original_payload, steps, started)
        except NotionalExceededError:
            raise  # 名义超限交给 runner fail-fast(资金保护优先)
        except Exception as exc:
            return self._fail(ScenarioStatus.FAIL, type(exc).__name__, str(exc)[:300], {"steps": steps})
        finally:
            # 任何路径都必须恢复原始基线(finally 兜底;恢复后幂等 UPSERT 无副作用)
            if conn is not None and original_payload is not None:
                try:
                    _upsert_record(conn, _BASELINE_RECORD_TYPE, _BASELINE_RECORD_ID, original_payload)
                    current = _read_record(conn, _BASELINE_RECORD_TYPE, _BASELINE_RECORD_ID)
                    restored_ok = current is not None and current == original_payload
                    steps.append(
                        {
                            "action": "restore_baseline_finally",
                            "ok": restored_ok,
                            "hash": _payload_hash(original_payload),
                        }
                    )
                except Exception as exc:
                    steps.append({"action": "restore_baseline_finally", "ok": False, "error": str(exc)[:200]})
                finally:
                    with contextlib.suppress(Exception):
                        conn.close()


SCENARIO_REGISTRY[ReconciliationMismatchScenario.scenario_id] = ReconciliationMismatchScenario
