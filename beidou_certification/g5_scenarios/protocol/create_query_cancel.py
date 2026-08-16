"""create_query_cancel: 下单→查询→撤单→再查询确认 CANCELED。

真实路径:取 exchangeInfo 的 LOT_SIZE minQty 作下单量,以 ticker 现价
作 LIMIT 挂单价(GTC),记账 min_qty × 现价后下单;撤单后最终查询
状态须为 CANCELED。dry_run 记账 0 且不发送任何请求。
"""

from __future__ import annotations

import time
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
            price = str(ticker["lastPrice"])
            ctx.ledger.record(self.scenario_id, min_qty * float(ticker["lastPrice"]))
            order = _require_ok(
                await ctx.client.create_order(
                    ctx.symbol,
                    "BUY",
                    "LIMIT",
                    format(min_qty, "f"),
                    price=price,
                    time_in_force="GTC",
                    client_order_id=f"g5-cqc-{int(time.time() * 1000)}",
                ),
                "create_order",
            )
            steps.append({"action": "order", "order_id": order["orderId"], "status": order.get("status")})
            queried = _require_ok(await ctx.client.get_order(ctx.symbol, int(order["orderId"])), "get_order")
            steps.append({"action": "query", "status": queried.get("status")})
            cancelled = _require_ok(await ctx.client.cancel_order(ctx.symbol, int(order["orderId"])), "cancel_order")
            steps.append({"action": "cancel", "status": cancelled.get("status")})
            final = _require_ok(await ctx.client.get_order(ctx.symbol, int(order["orderId"])), "get_order_after_cancel")
            steps.append({"action": "query_after_cancel", "status": final.get("status")})
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
