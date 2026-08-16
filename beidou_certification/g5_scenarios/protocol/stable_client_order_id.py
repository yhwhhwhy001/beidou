"""stable_client_order_id: 同 clientOrderId 下单两次,第二次须幂等拒绝或返回同一订单。

判定纯函数 assert_no_duplicate:第二次响应为带 code 的错误(交易所拒绝,如
-4015/-2011)→ 通过;返回已知 orderId(同一订单)→ 通过;返回新 orderId
(重复成交)→ 失败。真实路径记账 min_qty × 现价,以 resting 限价挂单,
任何写操作前打印操作意图(设计规格§4);撤单清理在 finally 保护,
任一步异常(含 open_orders 查询、第二次下单)也不残留挂单。
"""

from __future__ import annotations

import logging
import time
from typing import Any, TypeVar

from beidou_certification.g5_scenarios.base import (
    NotionalExceededError,
    ScenarioBase,
    ScenarioContext,
    ScenarioResult,
    ScenarioStatus,
)
from beidou_certification.g5_scenarios.protocol.create_query_cancel import _resting_buy_price, min_order_quantity
from beidou_certification.g5_scenarios.runner import SCENARIO_REGISTRY
from beidou_exchange.core.error_taxonomy import Result

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _require_ok(result: Result[T], action: str) -> T:
    """解包 Result:失败抛 RuntimeError 携带交易所错误原文进证据。"""
    if not result.is_ok:
        raise RuntimeError(f"{action} failed: {result.error}")
    assert result.data is not None
    return result.data


def assert_no_duplicate(second: Any, orders_snapshot: list[dict[str, Any]]) -> tuple[bool, str]:
    """判定第二次同 clientOrderId 下单是否产生重复订单。

    second 为第二次下单的响应负载:成功 dict(含 orderId)或错误 dict(含 code);
    orders_snapshot 为已知订单(含 orderId 的 dict 列表)。
    """
    if isinstance(second, dict) and "code" in second:
        return True, "exchange_rejected"
    if isinstance(second, dict) and "orderId" in second:
        known = {str(o.get("orderId")) for o in orders_snapshot}
        if str(second["orderId"]) in known:
            return True, "same_order_returned"
        return False, "new_order_created"
    return False, "unrecognized_response"


class StableClientOrderIdScenario(ScenarioBase):
    scenario_id = "stable_client_order_id"

    async def run(self, ctx: ScenarioContext) -> ScenarioResult:
        assert ctx.client is not None
        started = time.monotonic()
        steps: list[dict[str, Any]] = []
        try:
            if ctx.dry_run:
                ctx.ledger.record(self.scenario_id, 0.0)
                return ScenarioResult(
                    self.scenario_id, ScenarioStatus.PASS, {"dry_run": True, "steps": steps}, time.monotonic() - started
                )
            info = _require_ok(await ctx.client.get_exchange_info(ctx.symbol), "get_exchange_info")
            min_qty, _ = min_order_quantity(ctx.symbol, info)
            ticker = _require_ok(await ctx.client.get_ticker(ctx.symbol), "get_ticker")
            price = _resting_buy_price(ctx.symbol, info, str(ticker["lastPrice"]))
            qty = format(min_qty, "f")
            notional = min_qty * float(ticker["lastPrice"])
            ctx.ledger.record(self.scenario_id, notional)
            cid = f"g5-stable-{int(time.time() * 1000)}"
            logger.info("create_order BUY %s %s @ %s notional=%.2f USDT", qty, ctx.symbol, price, notional)
            first = _require_ok(
                await ctx.client.create_order(
                    ctx.symbol,
                    "BUY",
                    "LIMIT",
                    qty,
                    price=price,
                    time_in_force="GTC",
                    client_order_id=cid,
                ),
                "create_order(first)",
            )
            first_id = int(first["orderId"])
            steps.append({"action": "first_order", "order_id": first["orderId"], "status": first.get("status")})
            second_new_id: int | None = None
            try:
                snapshot = _require_ok(await ctx.client.get_open_orders(ctx.symbol), "get_open_orders")
                known: list[dict[str, Any]] = list(snapshot)
                if all(str(o.get("orderId")) != str(first_id) for o in known):
                    known.append({"orderId": first_id})
                steps.append({"action": "snapshot_open_orders", "count": len(snapshot)})
                logger.info(
                    "create_order BUY %s %s @ %s notional=%.2f USDT (dup clientOrderId)",
                    qty,
                    ctx.symbol,
                    price,
                    notional,
                )
                second = await ctx.client.create_order(
                    ctx.symbol, "BUY", "LIMIT", qty, price=price, time_in_force="GTC", client_order_id=cid
                )
                if second.is_ok:
                    second_payload: Any = second.data if isinstance(second.data, dict) else {}
                else:
                    raw = second.error.raw if second.error is not None else None
                    second_payload = raw if isinstance(raw, dict) else {}
                ok, reason = assert_no_duplicate(second_payload, known)
                steps.append({"action": "second_order", "is_ok": second.is_ok, "verdict": reason})
                if isinstance(second_payload, dict) and second_payload.get("orderId") is not None:
                    second_new_id = int(second_payload["orderId"])
            finally:
                # 清理本场景挂单(finally 保护:open_orders 查询或第二次下单
                # 抛异常时也不残留),避免残留订单影响后续场景/对账
                cancel_ids = [first_id] + ([second_new_id] if second_new_id not in (None, first_id) else [])
                for oid in dict.fromkeys(cancel_ids):
                    try:
                        logger.info("cancel_order %s %s %s (cleanup)", oid, qty, ctx.symbol)
                        cancel_res = await ctx.client.cancel_order(ctx.symbol, oid)
                        steps.append({"action": "cleanup_cancel", "order_id": oid, "ok": cancel_res.is_ok})
                    except Exception as exc:
                        steps.append(
                            {"action": "cleanup_cancel", "order_id": oid, "ok": False, "error": str(exc)[:200]}
                        )
            return ScenarioResult(
                self.scenario_id,
                ScenarioStatus.PASS if ok else ScenarioStatus.FAIL,
                {"steps": steps, "client_order_id": cid},
                time.monotonic() - started,
                error_type="" if ok else reason.upper(),
            )
        except NotionalExceededError:
            raise  # 名义超限交给 runner fail-fast(资金保护优先)
        except Exception as exc:
            return self._fail(ScenarioStatus.FAIL, type(exc).__name__, str(exc)[:300], {"steps": steps})


SCENARIO_REGISTRY[StableClientOrderIdScenario.scenario_id] = StableClientOrderIdScenario
