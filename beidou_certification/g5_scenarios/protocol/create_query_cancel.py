"""create_query_cancel: 下单→查询→撤单→再查询确认 CANCELED。

真实路径:取 exchangeInfo 的 LOT_SIZE minQty 作下单量,以低于现价
5% 且对齐 tickSize 的 resting 限价挂单(GTC),记账 min_qty × 现价后
下单;任何写操作前打印操作意图与金额(设计规格§4);撤单清理在
finally 保护,任一步异常也不残留挂单;最终查询状态须为 CANCELED。
dry_run 记账 0 且不发送任何请求。
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any, TypeVar

from beidou_certification.g5_scenarios.base import (
    NotionalExceededError,
    ScenarioBase,
    ScenarioContext,
    ScenarioResult,
    ScenarioStatus,
)
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


def min_order_quantity(symbol: str, exchange_info: dict[str, Any]) -> tuple[float, str]:
    """从 exchangeInfo 提取 LOT_SIZE minQty/stepSize;缺失即 ValueError。"""
    for s in exchange_info.get("symbols", []):
        if s.get("symbol") == symbol:
            for f in s.get("filters", []):
                if f.get("filterType") == "LOT_SIZE":
                    return float(f["minQty"]), str(f.get("stepSize", "0.001"))
            raise ValueError(f"LOT_SIZE filter missing for {symbol}")
    raise ValueError(f"symbol {symbol} not in exchange info")


def _tick_size(symbol: str, exchange_info: dict[str, Any]) -> Decimal:
    """从 exchangeInfo 提取 PRICE_FILTER tickSize;缺失即 ValueError。"""
    for s in exchange_info.get("symbols", []):
        if s.get("symbol") == symbol:
            for f in s.get("filters", []):
                if f.get("filterType") == "PRICE_FILTER":
                    return Decimal(str(f.get("tickSize", "0.01")))
            raise ValueError(f"PRICE_FILTER missing for {symbol}")
    raise ValueError(f"symbol {symbol} not in exchange info")


def _resting_buy_price(symbol: str, exchange_info: dict[str, Any], last_price: str) -> str:
    """低于现价 5% 且对齐 tickSize 的 resting 买价(现价×0.95 向下取整到 tick)。"""
    raw = Decimal(last_price) * Decimal("0.95")
    tick = _tick_size(symbol, exchange_info)
    return str((raw // tick) * tick)


class CreateQueryCancelScenario(ScenarioBase):
    scenario_id = "create_query_cancel"

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
            logger.info("create_order BUY %s %s @ %s notional=%.2f USDT", qty, ctx.symbol, price, notional)
            order = _require_ok(
                await ctx.client.create_order(
                    ctx.symbol,
                    "BUY",
                    "LIMIT",
                    qty,
                    price=price,
                    time_in_force="GTC",
                    client_order_id=f"g5-cqc-{int(time.time() * 1000)}",
                ),
                "create_order",
            )
            order_id = int(order["orderId"])
            steps.append({"action": "order", "order_id": order["orderId"], "status": order.get("status")})
            cancelled = False
            final: dict[str, Any] = {}
            try:
                queried = _require_ok(await ctx.client.get_order(ctx.symbol, order_id), "get_order")
                steps.append({"action": "query", "status": queried.get("status")})
                logger.info("cancel_order %s %s %s", order_id, qty, ctx.symbol)
                cancelled_res = _require_ok(await ctx.client.cancel_order(ctx.symbol, order_id), "cancel_order")
                cancelled = True
                steps.append({"action": "cancel", "status": cancelled_res.get("status")})
                final = _require_ok(await ctx.client.get_order(ctx.symbol, order_id), "get_order_after_cancel")
                steps.append({"action": "query_after_cancel", "status": final.get("status")})
            finally:
                if not cancelled:  # 任一步异常也撤销挂单,不残留(设计规格§4:写操作→finally 恢复)
                    try:
                        logger.info("cancel_order %s %s %s (cleanup)", order_id, qty, ctx.symbol)
                        cleanup = await ctx.client.cancel_order(ctx.symbol, order_id)
                        steps.append({"action": "cleanup_cancel", "ok": cleanup.is_ok})
                    except Exception as exc:
                        steps.append({"action": "cleanup_cancel", "ok": False, "error": str(exc)[:200]})
            ok = str(final.get("status")) == "CANCELED"
            return ScenarioResult(
                self.scenario_id,
                ScenarioStatus.PASS if ok else ScenarioStatus.FAIL,
                {"steps": steps, "order_id": order["orderId"]},
                time.monotonic() - started,
                error_type="" if ok else "UNEXPECTED_FINAL_STATUS",
            )
        except NotionalExceededError:
            raise  # 名义超限交给 runner fail-fast(资金保护优先)
        except Exception as exc:
            return self._fail(ScenarioStatus.FAIL, type(exc).__name__, str(exc)[:300], {"steps": steps})


SCENARIO_REGISTRY[CreateQueryCancelScenario.scenario_id] = CreateQueryCancelScenario
