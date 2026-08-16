"""clock_skew: 人为注入 +300000ms 时钟偏差,验证 -1021 后签名路径仍可用。

真实路径:先 get_server_time,把 client._clock_offset_ms 置 +300000 后下单,
客户端内部对 -1021 自动重取 server time 校正再重试。下单成功(证明签名路径
经重同步后可用)或返回非时间戳类错误即 PASS。下单成功才记账(min_qty × 现价),
未下单记 0;结束后恢复原偏移,并撤销本场景挂单。
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
from beidou_certification.g5_scenarios.protocol.create_query_cancel import min_order_quantity
from beidou_certification.g5_scenarios.runner import SCENARIO_REGISTRY
from beidou_exchange.core.error_taxonomy import Result

T = TypeVar("T")

SKEW_OFFSET_MS = 300000  # 注入的时钟偏差(服务端时间 - 本地时间),远超 recvWindow 5s


def _require_ok(result: Result[T], action: str) -> T:
    """解包 Result:失败抛 RuntimeError 携带交易所错误原文进证据。"""
    if not result.is_ok:
        raise RuntimeError(f"{action} failed: {result.error}")
    assert result.data is not None
    return result.data


def is_timestamp_error(payload: Any) -> bool:
    """判断错误负载是否为时钟偏差类(-1021 timestamp 超 recvWindow / -1022 signature expired)。"""
    if not isinstance(payload, dict):
        return False
    try:
        return int(payload.get("code") or 0) in (-1021, -1022)
    except (TypeError, ValueError):
        return False


class ClockSkewScenario(ScenarioBase):
    scenario_id = "clock_skew"

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
            st = _require_ok(await ctx.client.get_server_time(), "get_server_time")
            steps.append({"action": "server_time", "server_time": st.get("serverTime")})
            info = _require_ok(await ctx.client.get_exchange_info(ctx.symbol), "get_exchange_info")
            min_qty, _ = min_order_quantity(ctx.symbol, info)
            ticker = _require_ok(await ctx.client.get_ticker(ctx.symbol), "get_ticker")
            price = str(ticker["lastPrice"])
            notional = min_qty * float(ticker["lastPrice"])
            old_offset = ctx.client._clock_offset_ms
            try:
                ctx.client._clock_offset_ms = SKEW_OFFSET_MS
                steps.append({"action": "inject_skew", "offset_ms": SKEW_OFFSET_MS})
                order_res = await ctx.client.create_order(
                    ctx.symbol,
                    "BUY",
                    "LIMIT",
                    format(min_qty, "f"),
                    price=price,
                    time_in_force="GTC",
                    client_order_id=f"g5-skew-{int(time.time() * 1000)}",
                )
            finally:
                ctx.client._clock_offset_ms = old_offset  # 恢复偏移,不污染后续场景
            if order_res.is_ok:
                order = order_res.data if isinstance(order_res.data, dict) else {}
                steps.append(
                    {
                        "action": "order",
                        "order_id": order.get("orderId"),
                        "status": order.get("status"),
                        "resynced": True,
                    }
                )
                try:
                    cancel_res = await ctx.client.cancel_order(ctx.symbol, int(order["orderId"]))
                    steps.append({"action": "cleanup_cancel", "ok": cancel_res.is_ok})
                except Exception as exc:
                    steps.append({"action": "cleanup_cancel", "ok": False, "error": str(exc)[:200]})
                ctx.ledger.record(self.scenario_id, notional)  # 真实下单才记账
                return ScenarioResult(
                    self.scenario_id, ScenarioStatus.PASS, {"steps": steps}, time.monotonic() - started
                )
            payload = (
                order_res.error.raw if order_res.error is not None and isinstance(order_res.error.raw, dict) else {}
            )
            ok = not is_timestamp_error(payload)
            steps.append({"action": "order_error", "code": payload.get("code"), "timestamp_error": not ok})
            return ScenarioResult(
                self.scenario_id,
                ScenarioStatus.PASS if ok else ScenarioStatus.FAIL,
                {"steps": steps},
                time.monotonic() - started,
                error_type="" if ok else "TIMESTAMP_ERROR",
            )
        except NotionalExceededError:
            raise  # 名义超限交给 runner fail-fast(资金保护优先)
        except Exception as exc:
            return self._fail(ScenarioStatus.FAIL, type(exc).__name__, str(exc)[:300], {"steps": steps})


SCENARIO_REGISTRY[ClockSkewScenario.scenario_id] = ClockSkewScenario
