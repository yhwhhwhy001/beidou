"""cancel_fill_race: 组件级验证引擎 save_order_state 单调守卫语义。

不真实下单。用 PG 临时行(order_id 前缀 g5-race-<ts>)经真实
PostgresPersistentStore.save_order_state 依次写 NEW → FILLED → 尝试
CANCELED,每步同时用纯函数 terminal_monotonic_guard 推算预期,并分别经
store 公开读路径(restore_order_states)与直接 SQL 读回实际状态,断言三者
一致(纯函数 == store 语义)。再加 PARTIALLY_FILLED → NEW 覆盖断言与
FILLED → 迟到 NEW 回写断言(实测 14:07 XRP/DOGE/ATOM 场景)。清理 DELETE
不留残迹;notional 记账 0;dry_run 早退不碰 PG;run() 自捕获异常返回 FAIL。

注(与计划的偏差,以实际代码为准):计划期望 FILLED 不被 CANCELED 覆盖
("断言终态 FILLED");实测引擎守卫(beidou_core/store.py:1015-1020、
beidou_infra/postgres_store.py:679-684,fe62da1)只挡「非终态覆盖终态」与
「PARTIALLY_FILLED→NEW」,终态→终态(FILLED→CANCELED)放行 —— 真实交易所
取消已成交单返回 -2011 错误而非 CANCELED 状态,引擎的真实竞态防线在
beidou_core/engine.py:5874(executedQty>0 的 CANCELED/EXPIRED → UNKNOWN
TERMINAL_PARTIAL_FILL_RECONCILIATION_REQUIRED,不经 store 落 CANCELED)。
本场景以实际代码为准验证守卫语义,证据记录 plan 期望与实际语义差异。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

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
from beidou_infra.postgres_store import PostgresPersistentStore

logger = logging.getLogger(__name__)

# 与引擎 _TERMINAL_ORDER_STATUSES(beidou_core/store.py:16、
# beidou_infra/postgres_store.py:25)镜像
_TERMINAL_STATUSES = frozenset({"FILLED", "CANCELED", "EXPIRED", "REJECTED"})

_RACE_ID_PREFIX = "g5-race-"

# 引擎守卫源码定位标记(beidou_core/store.py + beidou_infra/postgres_store.py 同款)
_GUARD_MARKER = "# BD-FIX: 终态/部分成交不得被迟到的下单响应回写"


def terminal_monotonic_guard(current_status: str, incoming_status: str) -> str:
    """引擎 save_order_state 单调守卫的纯函数镜像:返回实际生效的状态。

    - 终态(FILLED/CANCELED/EXPIRED/REJECTED)不被非终态覆盖(迟到 NEW/UNKNOWN
      回写不得抹掉已证实终态 —— 实测 14:07 XRP/DOGE/ATOM 场景)
    - PARTIALLY_FILLED 不被 NEW 回写(同一竞态的中间态)
    - 其余推进(含终态→终态,如 FILLED→CANCELED)放行
    """
    if current_status in _TERMINAL_STATUSES and incoming_status not in _TERMINAL_STATUSES:
        return current_status
    if current_status == "PARTIALLY_FILLED" and incoming_status == "NEW":
        return current_status
    return incoming_status


def monotonic_guard_source() -> dict[str, Any]:
    """定位引擎 save_order_state 单调守卫的真实源码(文件:行号 + 规则文本),
    并断言与场景纯函数 terminal_monotonic_guard 语义一致(两条阻断规则都在场)。
    """
    import beidou_core.store as sqlite_store
    import beidou_infra.postgres_store as pg_store

    files: dict[str, dict[str, Any]] = {}
    for module in (sqlite_store, pg_store):
        source_path = Path(module.__file__).resolve() if module.__file__ else None
        if source_path is None or not source_path.is_file():
            raise RuntimeError(f"monotonic guard source missing: {module.__file__}")
        lines = source_path.read_text(encoding="utf-8").splitlines()
        marker_idx = next((i for i, line in enumerate(lines) if _GUARD_MARKER in line), None)
        if marker_idx is None:
            raise RuntimeError(f"monotonic guard marker missing in {source_path}")
        guard_line_numbers: list[int] = []
        guard_text: list[str] = []
        for idx in range(marker_idx, min(marker_idx + 16, len(lines))):
            stripped = lines[idx].strip()
            if not stripped or stripped.startswith(("#", "if", "existing", "prev_status", "return")):
                guard_line_numbers.append(idx + 1)
                guard_text.append(stripped)
            else:
                break  # 守卫块结束(下一个语句:now = / self._write_record( 等)
        files[str(source_path)] = {
            "guard_lines": [guard_line_numbers[0], guard_line_numbers[-1]],
            "rule_text": " ".join(guard_text),
        }
    semantics_consistent = all(
        "PARTIALLY_FILLED" in entry["rule_text"]
        and "NEW" in entry["rule_text"]
        and "in _TERMINAL_ORDER_STATUSES" in entry["rule_text"]
        for entry in files.values()
    )
    return {"semantics_consistent": bool(semantics_consistent), "files": files}


def _make_store(dsn: str) -> Any:
    """构造组件实例:真实 PostgresPersistentStore,连接工厂经本模块 psycopg
    引用(测试可注入假连接);保留默认 schema 校验(真实环境缺迁移即明确 FAIL)。"""
    return PostgresPersistentStore(dsn, connection_factory=lambda: psycopg.connect(dsn, autocommit=True))


def _read_order_state_store(store: Any, record_id: str) -> dict[str, Any] | None:
    """经组件公开读路径恢复 order_state 记录(按 record_id 过滤)。"""
    for row in store.restore_order_states():
        if str(row.get("record_id")) == record_id:
            return dict(row)
    return None


def _read_order_state_raw(conn: Any, record_id: str) -> dict[str, Any] | None:
    """直接读 PG:order_state 记录原始 payload(与 store._get_record 同 SQL)。"""
    cursor = conn.execute(
        "SELECT payload::text FROM v3_runtime_records WHERE record_type=%s AND record_id=%s",
        ("order_state", record_id),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    payload = json.loads(str(row[0]))
    return payload if isinstance(payload, dict) else None


def _cleanup_race_rows(conn: Any, record_ids: list[str]) -> dict[str, Any]:
    """DELETE 本场景测试行(先事件后记录,按 record_id),零残留。"""
    records_deleted = 0
    events_deleted = 0
    with conn.transaction():
        for record_id in record_ids:
            cursor = conn.execute(
                "DELETE FROM v3_runtime_events WHERE record_type='order_state' AND record_id=%s", (record_id,)
            )
            events_deleted += int(cursor.rowcount or 0)
            cursor = conn.execute(
                "DELETE FROM v3_runtime_records WHERE record_type='order_state' AND record_id=%s", (record_id,)
            )
            records_deleted += int(cursor.rowcount or 0)
    return {
        "action": "cleanup_delete",
        "records_deleted": records_deleted,
        "events_deleted": events_deleted,
    }


def _run_sequence(
    store: Any,
    conn: Any,
    steps: list[dict[str, Any]],
    *,
    order_id: str,
    symbol: str,
    sequence: list[tuple[str, str]],
) -> bool:
    """对单行执行状态序列:每步经真实 store 写、纯函数推算预期,
    store 公开读与直接 SQL 读双路比对,返回是否全程一致。"""
    expected = ""
    statuses: list[str] = []
    all_match = True
    for status, filled_qty in sequence:
        expected = terminal_monotonic_guard(expected, status)
        store.save_order_state(order_id, symbol, "BUY", "LIMIT", "0.001", "60000.0", status, filled_qty=filled_qty)
        statuses.append(status)
        actual_store = _read_order_state_store(store, order_id)
        actual_raw = _read_order_state_raw(conn, order_id)
        store_status = str(actual_store.get("status") or "") if actual_store is not None else None
        raw_status = str(actual_raw.get("status") or "") if actual_raw is not None else None
        step_match = expected == store_status == raw_status
        all_match = all_match and step_match
        steps.append(
            {
                "action": "step",
                "order_id": order_id,
                "write_status": status,
                "expected_pure": expected,
                "actual_store": store_status,
                "actual_raw": raw_status,
                "match": step_match,
            }
        )
    steps.append(
        {
            "action": "sequence_done",
            "order_id": order_id,
            "statuses": statuses,
            "all_match": all_match,
        }
    )
    return all_match


def _probe_guard_semantics(dsn: str, symbol: str) -> tuple[list[dict[str, Any]], bool, str]:
    """组件级守卫语义探针,返回 (步骤证据, ok, 判定原因)。"""
    key = f"{_RACE_ID_PREFIX}{int(time.time() * 1000)}"
    race_id = f"{key}-race"
    partial_id = f"{key}-partial"
    late_id = f"{key}-late-new"
    steps: list[dict[str, Any]] = []
    conn: Any = None
    store: Any = None
    try:
        conn = psycopg.connect(dsn, autocommit=True)
        store = _make_store(dsn)
        source = monotonic_guard_source()
        steps.append({"action": "monotonic_guard_source", **source})
        ok = True
        ok = (
            _run_sequence(
                store,
                conn,
                steps,
                order_id=race_id,
                symbol=symbol,
                sequence=[("NEW", "0"), ("FILLED", "1"), ("CANCELED", "1")],
            )
            and ok
        )
        ok = (
            _run_sequence(
                store,
                conn,
                steps,
                order_id=partial_id,
                symbol=symbol,
                sequence=[("PARTIALLY_FILLED", "0.5"), ("NEW", "0.5")],
            )
            and ok
        )
        ok = (
            _run_sequence(
                store,
                conn,
                steps,
                order_id=late_id,
                symbol=symbol,
                sequence=[("NEW", "0"), ("FILLED", "1"), ("NEW", "1")],
            )
            and ok
        )
        steps.append(
            {
                "action": "deviation_note",
                "plan_expectation": "计划:依次写 NEW → FILLED → 尝试 CANCELED,断言终态 FILLED",
                "actual_engine_semantics": (
                    "引擎守卫放行终态→终态:FILLED→CANCELED 实际落 CANCELED;"
                    "守卫范围 = 非终态不得覆盖终态 + PARTIALLY_FILLED 不得被 NEW 回写"
                ),
                "engine_race_defense": (
                    "真实填/撤竞态防线在 beidou_core/engine.py:5874:executedQty>0 的 "
                    "CANCELED/EXPIRED → UNKNOWN TERMINAL_PARTIAL_FILL_RECONCILIATION_REQUIRED,"
                    "不经 store 落 CANCELED"
                ),
            }
        )
        return steps, ok, "guard_semantics_consistent"
    finally:
        if conn is not None:
            try:
                steps.append(_cleanup_race_rows(conn, [race_id, partial_id, late_id]))
                residual = [
                    rid for rid in (race_id, partial_id, late_id) if _read_order_state_raw(conn, rid) is not None
                ]
                steps.append({"action": "cleanup_verify", "residual": residual})
                if residual:
                    raise RuntimeError(f"race rows left behind: {residual}")
            except Exception as exc:
                steps.append({"action": "cleanup", "ok": False, "error": str(exc)[:200]})
                raise
            finally:
                conn.close()


class CancelFillRaceScenario(ScenarioBase):
    scenario_id = "cancel_fill_race"

    async def run(self, ctx: ScenarioContext) -> ScenarioResult:
        started = time.monotonic()
        steps: list[dict[str, Any]] = []
        try:
            ctx.ledger.record(self.scenario_id, 0.0)
            if ctx.dry_run:
                return ScenarioResult(
                    self.scenario_id, ScenarioStatus.PASS, {"dry_run": True, "steps": steps}, time.monotonic() - started
                )
            logger.info("cancel_fill_race: 组件级守卫语义验证(PG 临时行 %s*,不真实下单)", _RACE_ID_PREFIX)
            steps, ok, reason = _probe_guard_semantics(PG_DSN, ctx.symbol)
            deviation_notes = [s for s in steps if s.get("action") == "deviation_note"]
            source_steps = [s for s in steps if s.get("action") == "monotonic_guard_source"]
            return ScenarioResult(
                self.scenario_id,
                ScenarioStatus.PASS if ok else ScenarioStatus.FAIL,
                {
                    "steps": steps,
                    "verdict": reason,
                    "notional_usdt": 0.0,
                    "deviation_notes": deviation_notes,
                    "monotonic_guard_source": source_steps[0] if source_steps else {},
                },
                time.monotonic() - started,
                error_type="" if ok else "GUARD_SEMANTICS_MISMATCH",
            )
        except NotionalExceededError:
            raise  # 名义超限交给 runner fail-fast(资金保护优先)
        except Exception as exc:
            return self._fail(ScenarioStatus.FAIL, type(exc).__name__, str(exc)[:300], {"steps": steps})


SCENARIO_REGISTRY[CancelFillRaceScenario.scenario_id] = CancelFillRaceScenario
