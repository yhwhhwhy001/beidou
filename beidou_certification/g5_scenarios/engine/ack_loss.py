"""ack_loss: 引擎 outbox ack 丢失 → UNKNOWN 状态 → 对账恢复判定。

模拟引擎发送 intent 后交易所响应丢失(TRANSPORT_RESULT_UNKNOWN)的持久化
语义:用 PostgresIntentOutbox(dsn=共享 PG, fencing_token=1, 显式 lease_owner)
实例,先 psycopg 直插 SENDING 测试行(intent_id/lease_owner/fencing_token 与
实例一致,满足 _transition_intent 的所有权检查),再调真实 mark_unknown
生产代码路径转 UNKNOWN —— 不真实下单。随后只读查询交易所该 symbol 挂单
(client.get_open_orders),判定此 clientOrderId 已不在场(GONE),用纯函数
unknown_state_verdict 输出 (True, recoverable_no_duplicate) 即对账可闭合、
无重复下单风险。清理把测试行改 FAILED(模拟恢复终止态,不再计入 UNKNOWN
durable gate)后 DELETE 不留残迹(前缀 g5-ackloss-)。notional 记账 0;
dry_run 早退不碰 PG;run() 自捕获异常返回 FAIL。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import psycopg

from beidou_certification.g5_scenarios.base import (
    NotionalExceededError,
    ScenarioBase,
    ScenarioContext,
    ScenarioResult,
    ScenarioStatus,
)
from beidou_certification.g5_scenarios.runner import SCENARIO_REGISTRY
from beidou_infra.outbox import PostgresIntentOutbox

logger = logging.getLogger(__name__)

# 共享 PG(Homebrew 本机,beidou_testnet);运行时可用 BEIDOU_G5_PG_DSN 覆盖
PG_DSN = os.environ.get("BEIDOU_G5_PG_DSN", "postgresql://beidou_app:beidou_dev_2024@localhost:5432/beidou_testnet")

# 探针 outbox 实例的 fencing 身份:与 _insert_test_outbox_row 写入的行一致,
# mark_unknown 的所有权检查(lease_owner/fencing_token)才会命中。
_PROBE_LEASE_OWNER = "g5-engine-probe"
_PROBE_FENCING_TOKEN = 1

_INTENT_INSERT_SQL = (
    "INSERT INTO v3_order_intents "
    "(intent_id,idempotency_key,client_order_id,account_venue_id,account_id,"
    "instrument_id,side,order_type,quantity,price,time_in_force,payload) "
    "VALUES (%s,%s,NULL,%s,%s,%s,%s,%s,%s,NULL,%s,CAST(%s AS jsonb))"
)

_OUTBOX_INSERT_SQL = (
    "INSERT INTO v3_transactional_outbox "
    "(message_id,intent_id,idempotency_key,client_order_id,payload,status,lease_owner,fencing_token) "
    "VALUES (%s,%s,%s,%s,CAST(%s AS jsonb),'SENDING',%s,%s)"
)


def unknown_state_verdict(persisted_status: str, venue_status: str) -> tuple[bool, str]:
    """UNKNOWN 状态的对账判定:本地持久化状态 + 交易所状态 → (可闭合?, 判定原因)。

    - 本地 UNKNOWN + 交易所已无此单 → 可闭合(对账恢复,无重复下单风险)
    - 本地 UNKNOWN + 交易所仍有活跃单 → 不可闭合,必须保留锚点(防止重复下单)
    - 本地 FILLED + 交易所 GONE → 正常终态,无需恢复
    """
    if persisted_status == "UNKNOWN" and venue_status == "GONE":
        return True, "recoverable_no_duplicate"
    if persisted_status == "UNKNOWN" and venue_status == "ACTIVE":
        return False, "anchor_must_hold"
    if persisted_status == "FILLED" and venue_status == "GONE":
        return True, "terminal_consistent"
    return False, "unresolved"


def _insert_test_outbox_row(
    conn: Any, *, key: str, intent_id: str, message_id: str, client_order_id: str
) -> dict[str, Any]:
    """插入临时测试 intent + SENDING outbox 行(前缀 g5-,FK 链 intents ← outbox)。"""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            _INTENT_INSERT_SQL,
            (
                intent_id,
                key,
                "g5-account",
                "G5USDT",
                "G5USDT",
                "BUY",
                "LIMIT",
                "0.001",
                "GTC",
                '{"probe":"g5-engine-unknown"}',
            ),
        )
        cur.execute(
            _OUTBOX_INSERT_SQL,
            (
                message_id,
                intent_id,
                key,
                client_order_id,
                '{"probe":"g5-engine-unknown"}',
                _PROBE_LEASE_OWNER,
                _PROBE_FENCING_TOKEN,
            ),
        )
    return {
        "action": "insert_test_intent",
        "idempotency_key": key,
        "intent_id": intent_id,
        "message_id": message_id,
        "status": "SENDING",
    }


def _read_outbox_status(conn: Any, intent_id: str) -> str:
    """读回 outbox 行状态(evidence 步骤 2 的断言依据)。"""
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM v3_transactional_outbox WHERE intent_id=%s", (intent_id,))
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"outbox row missing for intent_id={intent_id}")
    return str(row[0])


def _outbox_state_counts(conn: Any) -> dict[str, int]:
    """SELECT status,COUNT(*) GROUP BY status —— 与引擎 _durable_fact_status
    读取的 outbox.stats.state_counts 同源。"""
    with conn.cursor() as cur:
        cur.execute("SELECT status,COUNT(*) FROM v3_transactional_outbox GROUP BY status")
        rows = cur.fetchall() or []
    return {str(row[0]): int(row[1]) for row in rows}


def _recover_to_failed(conn: Any, *, intent_id: str) -> dict[str, Any]:
    """把测试行改 FAILED:模拟对账恢复的终止态,不再计入 UNKNOWN(durable gate 放行)。"""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE v3_transactional_outbox SET status='FAILED',updated_at=CURRENT_TIMESTAMP "
            "WHERE intent_id=%s",
            (intent_id,),
        )
        updated = cur.rowcount if cur.rowcount is not None else 0
    return {"action": "recover_to_failed", "updated": updated}


def _cleanup_test_rows(conn: Any, *, intent_id: str, message_id: str) -> dict[str, Any]:
    """DELETE 本场景测试行(先 events 外键后 outbox 再 intents),零残留。"""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM v3_outbox_events WHERE message_id=%s OR intent_id=%s", (message_id, intent_id))
        events_deleted = cur.rowcount if cur.rowcount is not None else 0
        cur.execute("DELETE FROM v3_transactional_outbox WHERE intent_id=%s OR message_id=%s", (intent_id, message_id))
        outbox_deleted = cur.rowcount if cur.rowcount is not None else 0
        cur.execute("DELETE FROM v3_order_intents WHERE intent_id=%s", (intent_id,))
        intents_deleted = cur.rowcount if cur.rowcount is not None else 0
    return {
        "action": "cleanup_delete",
        "events_deleted": events_deleted,
        "outbox_deleted": outbox_deleted,
        "intents_deleted": intents_deleted,
    }


async def _venue_status(client: Any, symbol: str, client_order_id: str) -> tuple[str, dict[str, Any]]:
    """只读查询交易所该 symbol 挂单,判定此 clientOrderId 是否仍活跃(GONE/ACTIVE)。"""
    result = await client.get_open_orders(symbol)
    if not result.is_ok:
        raise RuntimeError(f"get_open_orders failed: {result.error}")
    open_orders = result.data if isinstance(result.data, list) else []
    present = any(str(order.get("clientOrderId")) == client_order_id for order in open_orders)
    venue_status = "ACTIVE" if present else "GONE"
    return venue_status, {
        "action": "venue_open_orders",
        "symbol": symbol,
        "count": len(open_orders),
        "venue_status": venue_status,
    }


async def _probe_ack_loss(dsn: str, symbol: str, client: Any) -> tuple[list[dict[str, Any]], bool, str, str]:
    """执行 ack_loss 探针,返回 (步骤证据, ok, 判定原因, venue_status)。

    步骤:插 SENDING 测试行 → mark_unknown(真实生产路径)→ 读 PG 断言 UNKNOWN
    → 只读交易所挂单判 GONE → 纯函数判定 → 恢复改 FAILED → DELETE 清理。
    任一步失败抛异常,由 run() 自捕获记 FAIL。
    """
    key = f"g5-ackloss-{int(time.time() * 1000)}"
    intent_id = f"{key}-intent"
    message_id = f"{key}-msg"
    client_order_id = f"{key}-coid"
    steps: list[dict[str, Any]] = []
    outbox = PostgresIntentOutbox(dsn=dsn, lease_owner=_PROBE_LEASE_OWNER, fencing_token=_PROBE_FENCING_TOKEN)
    conn = psycopg.connect(dsn)
    # autocommit:基线计数等先读会在 psycopg3 默认模式下开启隐式事务,令后续
    # INSERT 对本连接外不可见(mark_unknown 走独立连接会查不到行);显式提交
    # 保证测试行对生产代码路径立即可见,事务块(conn.transaction)仍保持原子性。
    conn.autocommit = True
    try:
        steps.append(
            _insert_test_outbox_row(
                conn, key=key, intent_id=intent_id, message_id=message_id, client_order_id=client_order_id
            )
        )
        outbox.mark_unknown(intent_id, "TRANSPORT_RESULT_UNKNOWN")
        steps.append({"action": "mark_unknown", "reason": "TRANSPORT_RESULT_UNKNOWN"})
        status = _read_outbox_status(conn, intent_id)
        steps.append({"action": "read_status", "status": status})
        if status != "UNKNOWN":
            raise RuntimeError(f"mark_unknown did not reach UNKNOWN: status={status}")
        venue_status, venue_step = await _venue_status(client, symbol, client_order_id)
        steps.append(venue_step)
        ok, reason = unknown_state_verdict("UNKNOWN", venue_status)
        steps.append(
            {
                "action": "verdict",
                "persisted_status": "UNKNOWN",
                "venue_status": venue_status,
                "ok": ok,
                "reason": reason,
            }
        )
        return steps, ok, reason, venue_status
    finally:
        try:
            steps.append(_recover_to_failed(conn, intent_id=intent_id))
            steps.append(_cleanup_test_rows(conn, intent_id=intent_id, message_id=message_id))
        except Exception as exc:
            steps.append({"action": "cleanup", "ok": False, "error": str(exc)[:200]})
        conn.close()


class AckLossScenario(ScenarioBase):
    scenario_id = "ack_loss"

    async def run(self, ctx: ScenarioContext) -> ScenarioResult:
        started = time.monotonic()
        steps: list[dict[str, Any]] = []
        try:
            ctx.ledger.record(self.scenario_id, 0.0)
            if ctx.dry_run:
                return ScenarioResult(
                    self.scenario_id, ScenarioStatus.PASS, {"dry_run": True, "steps": steps}, time.monotonic() - started
                )
            if ctx.client is None:
                return self._fail(
                    ScenarioStatus.NOT_VERIFIABLE,
                    "CLIENT_UNAVAILABLE",
                    "需要交易所只读客户端判定 venue 状态",
                    {"steps": steps},
                )
            logger.info("ack_loss: 模拟响应丢失 intent -> mark_unknown(测试行,不真实下单)")
            steps, ok, reason, venue_status = await _probe_ack_loss(PG_DSN, ctx.symbol, ctx.client)
            return ScenarioResult(
                self.scenario_id,
                ScenarioStatus.PASS if ok else ScenarioStatus.FAIL,
                {"steps": steps, "verdict": reason, "venue_status": venue_status, "notional_usdt": 0.0},
                time.monotonic() - started,
                error_type="" if ok else reason.upper(),
            )
        except NotionalExceededError:
            raise  # 名义超限交给 runner fail-fast(资金保护优先)
        except Exception as exc:
            return self._fail(ScenarioStatus.FAIL, type(exc).__name__, str(exc)[:300], {"steps": steps})


SCENARIO_REGISTRY[AckLossScenario.scenario_id] = AckLossScenario
