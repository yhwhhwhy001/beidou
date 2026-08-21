"""clock_skew: 人为注入 +300000ms 时钟偏差,验证 -1021 后签名路径仍可用。

真实路径:先 get_server_time,把 client._clock_offset_ms 置 +300000 后下单;
若响应 -1021(timestamp_error)且客户端未自动 resync,显式调
client._resync_clock_offset()(rest_client.py:124)重新校准后再下单,断言
第二次成功,证据记录 resync 前后 offset 与两次下单结果。下单成功(证明
签名路径经重同步后可用)或返回非时间戳类错误即 PASS。下单成功才记账
(过门槛量 × 现价),未下单记 0;写操作前打印操作意图(设计规格§4);偏移
恢复仅当未产生真实 resync 校准(或注入前已有校准)时还原 —— resync 后
offset 为真实校准值,不回退到注入值;成功时撤单清理,且记账在 finally
保证(撤单异常也不漏记账)。
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
    min_gate_quantity,
)
from beidou_certification.g5_scenarios.protocol.create_query_cancel import _resting_buy_price, min_order_quantity
from beidou_certification.g5_scenarios.runner import SCENARIO_REGISTRY
from beidou_exchange.core.error_taxonomy import Result

logger = logging.getLogger(__name__)

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
            min_qty, step_size = min_order_quantity(ctx.symbol, info)
            ticker = _require_ok(await ctx.client.get_ticker(ctx.symbol), "get_ticker")
            price = _resting_buy_price(ctx.symbol, info, str(ticker["lastPrice"]))
            qty = format(min_gate_quantity(Decimal(str(min_qty)), Decimal(str(step_size)), Decimal(price)), "f")
            notional = float(Decimal(qty) * Decimal(price))
            old_offset = ctx.client._clock_offset_ms
            resynced = False
            attempt = 1
            try:
                ctx.client._clock_offset_ms = SKEW_OFFSET_MS
                steps.append({"action": "inject_skew", "offset_ms": SKEW_OFFSET_MS})
                logger.info("create_order BUY %s %s @ %s notional=%.2f USDT (skew)", qty, ctx.symbol, price, notional)
                order_res = await ctx.client.create_order(
                    ctx.symbol,
                    "BUY",
                    "LIMIT",
                    qty,
                    price=price,
                    time_in_force="GTC",
                    client_order_id=f"g5-skew-{int(time.time() * 1000)}",
                )
                # 客户端内部对 -1021 自动 resync 校正偏移 → 注入值被替换即真实发生过 resync
                resynced = ctx.client._clock_offset_ms != SKEW_OFFSET_MS
                payload = (
                    order_res.error.raw if order_res.error is not None and isinstance(order_res.error.raw, dict) else {}
                )
                if not order_res.is_ok and is_timestamp_error(payload):
                    # 首次下单被 -1021 拒(客户端未自动 resync,偏移仍是注入值)
                    # → 显式重校准后重新下单,证据记录两次结果
                    steps.append(
                        {
                            "action": "order_error",
                            "attempt": 1,
                            "code": payload.get("code"),
                            "timestamp_error": True,
                            "resynced": resynced,
                        }
                    )
                    offset_before_resync = ctx.client._clock_offset_ms
                    await ctx.client._resync_clock_offset()
                    resynced = ctx.client._clock_offset_ms != SKEW_OFFSET_MS
                    steps.append(
                        {
                            "action": "explicit_resync",
                            "offset_before_ms": offset_before_resync,
                            "offset_after_ms": ctx.client._clock_offset_ms,
                            "resynced": resynced,
                        }
                    )
                    logger.info(
                        "create_order BUY %s %s @ %s notional=%.2f USDT (after explicit resync)",
                        qty,
                        ctx.symbol,
                        price,
                        notional,
                    )
                    attempt = 2
                    order_res = await ctx.client.create_order(
                        ctx.symbol,
                        "BUY",
                        "LIMIT",
                        qty,
                        price=price,
                        time_in_force="GTC",
                        client_order_id=f"g5-skew-{int(time.time() * 1000)}",
                    )
            finally:
                # 仅在未产生真实 resync 校准(或注入前已有校准需还原)时恢复注入前偏移;
                # 注入前无校准且 resync 已产生真实值时保留 resync 结果,不污染后续场景
                if not (resynced and old_offset == 0):
                    ctx.client._clock_offset_ms = old_offset
            if order_res.is_ok:
                order = order_res.data if isinstance(order_res.data, dict) else {}
                steps.append(
                    {
                        "action": "order",
                        "attempt": attempt,
                        "order_id": order.get("orderId"),
                        "status": order.get("status"),
                        "resynced": resynced,
                    }
                )
                try:
                    logger.info("cancel_order %s %s %s (cleanup)", order.get("orderId"), qty, ctx.symbol)
                    cancel_res = await ctx.client.cancel_order(ctx.symbol, int(order["orderId"]))
                    steps.append({"action": "cleanup_cancel", "ok": cancel_res.is_ok})
                except Exception as exc:
                    steps.append({"action": "cleanup_cancel", "ok": False, "error": str(exc)[:200]})
                finally:
                    ctx.ledger.record(self.scenario_id, notional)  # 真实下单才记账(finally:撤单异常也不漏)
                return ScenarioResult(
                    self.scenario_id, ScenarioStatus.PASS, {"steps": steps}, time.monotonic() - started
                )
            payload = (
                order_res.error.raw if order_res.error is not None and isinstance(order_res.error.raw, dict) else {}
            )
            ok = not is_timestamp_error(payload)
            steps.append(
                {
                    "action": "order_error",
                    "attempt": attempt,
                    "code": payload.get("code"),
                    "timestamp_error": not ok,
                    "resynced": resynced,
                }
            )
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
