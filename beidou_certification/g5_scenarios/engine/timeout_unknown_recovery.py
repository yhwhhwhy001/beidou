"""timeout_unknown_recovery: 请求超时 → UNKNOWN 状态机 → durable gate 阻断 → 恢复。

与 ack_loss 同构(共享其插入/mark_unknown/venue 判定/清理助手),模拟路径为
"请求超时 → UNKNOWN 状态机":插入 UNKNOWN 行后跑
SELECT status,COUNT(*) FROM v3_transactional_outbox GROUP BY status,断言
UNKNOWN 计数 ≥ 基线+1 —— 即引擎 _durable_fact_status 会判
DURABLE_OUTBOX_UNKNOWN → readiness 阻断;恢复(改 FAILED,不再计入 UNKNOWN)
后计数回到基线,清理 DELETE 后计数仍为基线(零残留)。判定纯函数复用
ack_loss.unknown_state_verdict。notional 记账 0;dry_run 早退不碰 PG;
run() 自捕获异常返回 FAIL。
"""

from __future__ import annotations

import logging
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
from beidou_certification.g5_scenarios.engine.ack_loss import (
    _PROBE_FENCING_TOKEN,
    _PROBE_LEASE_OWNER,
    PG_DSN,
    _cleanup_test_rows,
    _insert_test_outbox_row,
    _outbox_state_counts,
    _read_outbox_status,
    _recover_to_failed,
    _venue_status,
    unknown_state_verdict,
)
from beidou_certification.g5_scenarios.runner import SCENARIO_REGISTRY
from beidou_infra.outbox import PostgresIntentOutbox

logger = logging.getLogger(__name__)


async def _probe_timeout_unknown_recovery(dsn: str, symbol: str, client: Any) -> tuple[list[dict[str, Any]], bool, str]:
    """执行 timeout_unknown_recovery 探针,返回 (步骤证据, ok, 判定原因)。

    在 ack_loss 步骤之上增加 durable 计数验证:UNKNOWN 行存在时 UNKNOWN 计数
    ≥ 基线+1(durable gate 阻断),恢复改 FAILED 后回到基线,清理后仍为基线。
    """
    key = f"g5-timeout-{int(time.time() * 1000)}"
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
        baseline = _outbox_state_counts(conn).get("UNKNOWN", 0)
        steps.append({"action": "baseline_counts", "unknown": baseline})
        steps.append(
            _insert_test_outbox_row(
                conn, key=key, intent_id=intent_id, message_id=message_id, client_order_id=client_order_id
            )
        )
        outbox.mark_unknown(intent_id, "REQUEST_TIMEOUT_UNKNOWN")
        steps.append({"action": "mark_unknown", "reason": "REQUEST_TIMEOUT_UNKNOWN"})
        status = _read_outbox_status(conn, intent_id)
        steps.append({"action": "read_status", "status": status})
        if status != "UNKNOWN":
            raise RuntimeError(f"mark_unknown did not reach UNKNOWN: status={status}")
        state_counts = _outbox_state_counts(conn)
        unknown_after = state_counts.get("UNKNOWN", 0)
        steps.append(
            {
                "action": "state_counts_after_unknown",
                "state_counts": state_counts,
                "durable_fact": "DURABLE_OUTBOX_UNKNOWN",
                "readiness_blocked": True,
            }
        )
        if unknown_after < baseline + 1:
            raise RuntimeError(f"UNKNOWN count not raised: baseline={baseline} after={unknown_after}")
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
        steps.append(_recover_to_failed(conn, intent_id=intent_id))
        counts_after_recover = _outbox_state_counts(conn)
        unknown_after_recover = counts_after_recover.get("UNKNOWN", 0)
        steps.append(
            {
                "action": "state_counts_after_recover",
                "state_counts": counts_after_recover,
                "unknown": unknown_after_recover,
            }
        )
        if unknown_after_recover != baseline:
            raise RuntimeError(
                f"UNKNOWN count not restored after recover: baseline={baseline} after={unknown_after_recover}"
            )
        return steps, ok, reason
    finally:
        try:
            steps.append(_cleanup_test_rows(conn, intent_id=intent_id, message_id=message_id))
            counts_after_cleanup = _outbox_state_counts(conn)
            unknown_after_cleanup = counts_after_cleanup.get("UNKNOWN", 0)
            steps.append({"action": "state_counts_after_cleanup", "unknown": unknown_after_cleanup})
            if unknown_after_cleanup != baseline:
                raise RuntimeError(
                    f"UNKNOWN count not at baseline after cleanup: baseline={baseline} after={unknown_after_cleanup}"
                )
        except Exception as exc:
            steps.append({"action": "cleanup", "ok": False, "error": str(exc)[:200]})
        conn.close()


class TimeoutUnknownRecoveryScenario(ScenarioBase):
    scenario_id = "timeout_unknown_recovery"

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
            logger.info("timeout_unknown_recovery: 模拟请求超时 -> UNKNOWN durable 阻断(测试行,不真实下单)")
            steps, ok, reason = await _probe_timeout_unknown_recovery(PG_DSN, ctx.symbol, ctx.client)
            return ScenarioResult(
                self.scenario_id,
                ScenarioStatus.PASS if ok else ScenarioStatus.FAIL,
                {"steps": steps, "verdict": reason, "notional_usdt": 0.0},
                time.monotonic() - started,
                error_type="" if ok else reason.upper(),
            )
        except NotionalExceededError:
            raise  # 名义超限交给 runner fail-fast(资金保护优先)
        except Exception as exc:
            return self._fail(ScenarioStatus.FAIL, type(exc).__name__, str(exc)[:300], {"steps": steps})


SCENARIO_REGISTRY[TimeoutUnknownRecoveryScenario.scenario_id] = TimeoutUnknownRecoveryScenario
