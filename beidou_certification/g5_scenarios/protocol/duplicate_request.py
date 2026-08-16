"""duplicate_request: 引擎组件级幂等验证 — 同 idempotency_key 的 intent 只提交一次。

beidou_infra/outbox.py 的 PostgresIntentOutbox.commit 幂等判定内嵌在 SQL:
先 SELECT v3_order_intents.idempotency_key(已存在即返回既有 key,不重复提交),
再 INSERT 由 UNIQUE(idempotency_key) 唯一约束兜底。场景对共享 PG 执行两次
相同 idempotency_key 的 INSERT,断言第二次被 unique 约束(sqlstate 23505)
拒绝;证据记录两次 insert 结果与清理删除。测试行前缀 g5-dup-<ts>,
场景结束在 finally 清理,不残留。notional 记账 0(动的是 PG 测试行,不是交易所)。

判定纯函数 dedupe_verdict:唯一约束拒绝等价交易所 -4015 重复拒绝语义
(→ exchange_rejected);返回已知 orderId → same_order_returned;新 orderId
→ new_order_created。dry_run 路径不触碰 PG。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import psycopg
from psycopg import errors as psycopg_errors

from beidou_certification.g5_scenarios.base import (
    NotionalExceededError,
    ScenarioBase,
    ScenarioContext,
    ScenarioResult,
    ScenarioStatus,
)
from beidou_certification.g5_scenarios.runner import SCENARIO_REGISTRY

logger = logging.getLogger(__name__)

# 共享 PG(Homebrew 本机,beidou_testnet);运行时可用 BEIDOU_G5_PG_DSN 覆盖
PG_DSN = os.environ.get("BEIDOU_G5_PG_DSN", "postgresql://beidou_app:beidou_dev_2024@localhost:5432/beidou_testnet")

_INSERT_INTENT_SQL = (
    "INSERT INTO v3_order_intents "
    "(intent_id,idempotency_key,client_order_id,account_venue_id,account_id,"
    "instrument_id,side,order_type,quantity,price,time_in_force,payload) "
    "VALUES (%s,%s,NULL,%s,%s,%s,%s,%s,%s,NULL,%s,CAST(%s AS jsonb))"
)


def dedupe_verdict(second_response: Any, known: list[int]) -> tuple[bool, str]:
    """判定同 idempotency_key 的第二次提交是否被幂等拒绝。

    second_response 为第二次提交的响应负载:带 code 的错误(交易所/唯一约束
    拒绝,如 -4015)→ 通过;返回已知 orderId(同一 intent)→ 通过;返回新
    orderId(重复成交)→ 失败。
    """
    if isinstance(second_response, dict) and "code" in second_response:
        return True, "exchange_rejected"
    if isinstance(second_response, dict) and "orderId" in second_response:
        known_ids = {str(order_id) for order_id in known}
        if str(second_response["orderId"]) in known_ids:
            return True, "same_order_returned"
        return False, "new_order_created"
    return False, "unrecognized_response"


def _probe_idempotency(dsn: str = PG_DSN) -> tuple[list[dict[str, Any]], bool, str]:
    """对共享 PG 执行两次相同 idempotency_key 的 INSERT,返回 (证据, ok, 判定)。

    第一次 INSERT 成功即代表首次提交落库;第二次 INSERT 被
    UNIQUE(idempotency_key) 拒绝即幂等成立(唯一约束兜底,等价 outbox
    SELECT-已存在-路径)。测试行在 finally 中按 idempotency_key 清理删除。
    PG 连接失败/首插失败直接抛异常,由场景 run() 自捕获记 FAIL。
    """
    key = f"g5-dup-{int(time.time() * 1000)}"
    steps: list[dict[str, Any]] = []
    conn = psycopg.connect(dsn)
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                _INSERT_INTENT_SQL,
                (
                    f"{key}-a",
                    key,
                    "g5",
                    "g5-account",
                    "G5USDT",
                    "BUY",
                    "LIMIT",
                    "0.001",
                    "GTC",
                    '{"probe":"g5-duplicate-request"}',
                ),
            )
        steps.append({"action": "first_insert", "ok": True, "idempotency_key": key, "intent_id": f"{key}-a"})
        try:
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    _INSERT_INTENT_SQL,
                    (
                        f"{key}-b",
                        key,
                        "g5",
                        "g5-account",
                        "G5USDT",
                        "BUY",
                        "LIMIT",
                        "0.001",
                        "GTC",
                        '{"probe":"g5-duplicate-request"}',
                    ),
                )
            steps.append({"action": "second_insert", "ok": True, "idempotency_key": key})
            return steps, False, "duplicate_accepted"
        except psycopg_errors.UniqueViolation as exc:
            sqlstate = getattr(exc, "sqlstate", None) or "23505"
            steps.append(
                {
                    "action": "second_insert",
                    "ok": False,
                    "sqlstate": sqlstate,
                    "error": str(exc)[:200],
                    "idempotency_key": key,
                }
            )
            # 唯一约束拒绝 == 交易所侧重复拒绝语义(-4015 → exchange_rejected)
            ok, reason = dedupe_verdict({"code": int(sqlstate), "msg": "duplicate idempotency_key rejected"}, [])
            return steps, ok, reason
    finally:
        try:
            with conn.transaction(), conn.cursor() as cur:
                cur.execute("DELETE FROM v3_order_intents WHERE idempotency_key=%s", (key,))
                deleted = cur.rowcount if cur.rowcount is not None else 0
            steps.append({"action": "cleanup", "deleted": deleted, "idempotency_key": key})
        except Exception as exc:
            # 清理失败不掩盖原始判定结果,由证据步骤呈现
            steps.append({"action": "cleanup", "ok": False, "error": str(exc)[:200], "idempotency_key": key})
        conn.close()


class DuplicateRequestScenario(ScenarioBase):
    scenario_id = "duplicate_request"

    async def run(self, ctx: ScenarioContext) -> ScenarioResult:
        started = time.monotonic()
        steps: list[dict[str, Any]] = []
        try:
            ctx.ledger.record(self.scenario_id, 0.0)
            if ctx.dry_run:
                return ScenarioResult(
                    self.scenario_id, ScenarioStatus.PASS, {"dry_run": True, "steps": steps}, time.monotonic() - started
                )
            logger.info("duplicate_request: 对共享 PG 执行两次同 idempotency_key INSERT(测试行,非交易所)")
            steps, ok, reason = _probe_idempotency()
            return ScenarioResult(
                self.scenario_id,
                ScenarioStatus.PASS if ok else ScenarioStatus.FAIL,
                {"steps": steps, "verdict": reason},
                time.monotonic() - started,
                error_type="" if ok else reason.upper(),
            )
        except NotionalExceededError:
            raise  # 名义超限交给 runner fail-fast(资金保护优先)
        except Exception as exc:
            return self._fail(ScenarioStatus.FAIL, type(exc).__name__, str(exc)[:300], {"steps": steps})


SCENARIO_REGISTRY[DuplicateRequestScenario.scenario_id] = DuplicateRequestScenario
