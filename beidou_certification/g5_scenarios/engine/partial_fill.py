"""partial_fill: 盘口流动性探测 → 大额贴价 LIMIT 买单 → PARTIALLY_FILLED 守卫断言。

只读 get_depth 探测盘口:顶部 ask 量 > min_qty×10 才有对手流动性;下单量 =
顶部 ask 深度 × 1.5、价格 = 最优 ask(价格贴市价争取立即成交顶部档,剩余 1/3
挂单即 PARTIALLY_FILLED;不做 resting 远离市价 —— 本场景目标是部分成交而非
不成交)。30s(15×2s)轮询 get_order:PARTIALLY_FILLED → 组件级守卫断言
(terminal_monotonic_guard("PARTIALLY_FILLED","NEW")=="PARTIALLY_FILLED",与引擎
save_order_state 单调守卫语义对拍,证据记录 monotonic_guard_source)并撤单清理;
全 FILLED 或窗口内仍 NEW → NOT_VERIFIABLE("liquidity_insufficient_or_too_deep")。
notional = 量×价格,超限改用 min_qty(量小通常全成交 → NOT_VERIFIABLE),兜底
仍超限 → NotionalExceededError 交 runner fail-fast。dry_run 早退不碰任何接口;
run() 自捕获异常返回 FAIL。
"""

from __future__ import annotations

import asyncio
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
from beidou_certification.g5_scenarios.engine.cancel_fill_race import (
    monotonic_guard_source,
    terminal_monotonic_guard,
)
from beidou_certification.g5_scenarios.runner import SCENARIO_REGISTRY
from beidou_exchange.core.error_taxonomy import Result

logger = logging.getLogger(__name__)

T = TypeVar("T")

_POLL_INTERVAL_SECONDS = 2.0
_MAX_POLLS = 15  # 2s × 15 ≈ 30s 轮询窗口
_LIQUIDITY_GATE_MULTIPLE = Decimal("10")  # 顶部 ask 量须 > min_qty × 10
_OVERSIZE_MULTIPLE = Decimal("1.5")  # 下单量 = 顶部 ask 深度 × 1.5(争取部分成交)
_NOT_VERIFIABLE_REASON = "liquidity_insufficient_or_too_deep"
_TERMINAL_NO_CANCEL = frozenset({"FILLED", "CANCELED", "EXPIRED", "REJECTED"})


def _require_ok(result: Result[T], action: str) -> T:
    """解包 Result:失败抛 RuntimeError 携带交易所错误原文进证据。"""
    if not result.is_ok:
        raise RuntimeError(f"{action} failed: {result.error}")
    assert result.data is not None
    return result.data


def _min_qty_and_step(symbol: str, exchange_info: dict[str, Any]) -> tuple[Decimal, Decimal]:
    """从 exchangeInfo 提取 LOT_SIZE minQty/stepSize;缺失即 ValueError。"""
    for entry in exchange_info.get("symbols", []):
        if entry.get("symbol") == symbol:
            for f in entry.get("filters", []):
                if f.get("filterType") == "LOT_SIZE":
                    return Decimal(str(f["minQty"])), Decimal(str(f.get("stepSize", "0.001")))
            raise ValueError(f"LOT_SIZE filter missing for {symbol}")
    raise ValueError(f"symbol {symbol} not in exchange info")


def _format_qty(qty: Decimal) -> str:
    """Decimal → 定点字符串(去尾零,拒绝科学计数法),满足 LOT_SIZE 步进要求。"""
    return format(qty.normalize(), "f")


class PartialFillScenario(ScenarioBase):
    scenario_id = "partial_fill"

    async def run(self, ctx: ScenarioContext) -> ScenarioResult:
        started = time.monotonic()
        steps: list[dict[str, Any]] = []
        order_id: int | None = None
        cancelled = False
        observed = "NEW"
        client: Any = None
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
                    "需要交易所只读客户端探测盘口与轮询订单",
                    {"steps": steps},
                )
            assert ctx.client is not None
            client = ctx.client

            info = _require_ok(await client.get_exchange_info(ctx.symbol), "get_exchange_info")
            min_qty, step_size = _min_qty_and_step(ctx.symbol, info)
            depth = _require_ok(await client.get_depth(ctx.symbol), "get_depth")
            asks = depth.get("asks") or []
            bids = depth.get("bids") or []
            steps.append(
                {
                    "action": "depth_probe",
                    "best_ask": str(asks[0][0]) if asks else "",
                    "top_ask_qty": str(asks[0][1]) if asks else "",
                    "top_bid": str(bids[0][0]) if bids else "",
                }
            )
            if not asks:
                return self._not_verifiable_liquidity(steps)
            ask_price = Decimal(str(asks[0][0]))
            ask_qty = Decimal(str(asks[0][1]))
            if ask_qty <= min_qty * _LIQUIDITY_GATE_MULTIPLE:
                return self._not_verifiable_liquidity(steps)

            # 量 = 顶部 ask 深度 × 1.5,对齐 stepSize,且不低于 min_qty
            qty = (ask_qty * _OVERSIZE_MULTIPLE // step_size) * step_size
            qty = max(qty, min_qty)
            price = str(ask_price)
            notional = float(qty * ask_price)
            if notional > ctx.ledger.limit_usdt:
                # 名义超限:改用 min_qty 兜底(量小通常全成交 → NOT_VERIFIABLE);
                # 兜底仍超限 → ledger.record 抛 NotionalExceededError 交 runner fail-fast
                steps.append(
                    {
                        "action": "notional_fallback",
                        "original_qty": _format_qty(qty),
                        "notional_usdt": notional,
                        "fallback_qty": _format_qty(min_qty),
                    }
                )
                qty = min_qty
                notional = float(qty * ask_price)
            ctx.ledger.record(self.scenario_id, notional)

            logger.info(
                "create_order BUY %s %s @ %s notional=%.2f USDT (partial_fill 探针)",
                _format_qty(qty),
                ctx.symbol,
                price,
                notional,
            )
            order = _require_ok(
                await client.create_order(
                    ctx.symbol,
                    "BUY",
                    "LIMIT",
                    _format_qty(qty),
                    price=price,
                    time_in_force="GTC",
                    client_order_id=f"g5-pfill-{int(time.time() * 1000)}",
                ),
                "create_order",
            )
            order_id = int(order["orderId"])
            steps.append(
                {
                    "action": "place_order",
                    "order_id": order_id,
                    "qty": _format_qty(qty),
                    "price": price,
                    "notional_usdt": notional,
                    "status": order.get("status"),
                }
            )

            for poll_no in range(1, _MAX_POLLS + 1):
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                queried = _require_ok(await client.get_order(ctx.symbol, order_id), "get_order")
                observed = str(queried.get("status", "NEW"))
                steps.append(
                    {
                        "action": "poll",
                        "poll_no": poll_no,
                        "status": observed,
                        "executed_qty": str(queried.get("executedQty", "")),
                    }
                )
                if observed in {"PARTIALLY_FILLED", "FILLED", "CANCELED", "EXPIRED", "REJECTED"}:
                    break

            if observed == "PARTIALLY_FILLED":
                # 组件级守卫断言:迟到的 NEW 回写不得抹掉已观察到的 PARTIALLY_FILLED
                guard_result = terminal_monotonic_guard("PARTIALLY_FILLED", "NEW")
                guard_ok = guard_result == "PARTIALLY_FILLED"
                steps.append(
                    {
                        "action": "guard_component_assert",
                        "current_status": "PARTIALLY_FILLED",
                        "incoming_status": "NEW",
                        "expected": "PARTIALLY_FILLED",
                        "actual": guard_result,
                        "ok": guard_ok,
                    }
                )
                source = monotonic_guard_source()
                steps.append({"action": "monotonic_guard_source", **source})
                if not guard_ok:
                    return self._fail(
                        ScenarioStatus.FAIL,
                        "GUARD_MISMATCH",
                        f"terminal_monotonic_guard returned {guard_result}",
                        {"steps": steps},
                    )
                logger.info("cancel_order %s %s (partial fill cleanup)", order_id, ctx.symbol)
                res = await client.cancel_order(ctx.symbol, order_id)  # 异常 → finally 重试清理
                cancelled = True
                steps.append(
                    {
                        "action": "cancel",
                        "ok": res.is_ok,
                        "status": (res.data or {}).get("status") if res.is_ok else None,
                        "error": str(res.error) if not res.is_ok else "",
                    }
                )
                final = _require_ok(await client.get_order(ctx.symbol, order_id), "get_order_after_cancel")
                final_status = str(final.get("status", ""))
                steps.append({"action": "query_after_cancel", "status": final_status})
                ok = final_status == "CANCELED"
                return ScenarioResult(
                    self.scenario_id,
                    ScenarioStatus.PASS if ok else ScenarioStatus.FAIL,
                    {
                        "steps": steps,
                        "partial_status": "PARTIALLY_FILLED",
                        "final_status_after_cancel": final_status,
                        "notional_usdt": notional,
                        "monotonic_guard_source": source,
                    },
                    time.monotonic() - started,
                    error_type="" if ok else "UNEXPECTED_FINAL_STATUS",
                )
            if observed == "FILLED":
                # 全成交:盘口太薄,无法观察到部分成交
                return self._not_verifiable_liquidity(steps, notional_usdt=notional)
            if observed in {"CANCELED", "EXPIRED", "REJECTED"}:
                return self._not_verifiable_terminal(steps, notional_usdt=notional)
            # 窗口结束仍 NEW:盘口过深/价位移动,未观察到成交
            return self._not_verifiable_liquidity(steps, notional_usdt=notional)
        except NotionalExceededError:
            raise  # 名义超限交给 runner fail-fast(资金保护优先)
        except Exception as exc:
            return self._fail(ScenarioStatus.FAIL, type(exc).__name__, str(exc)[:300], {"steps": steps})
        finally:
            # 撤单清理:已下且未撤、且非已知终态(异常路径 observed 停留初始值 NEW → 照常撤)
            if order_id is not None and not cancelled and client is not None:
                if observed in _TERMINAL_NO_CANCEL:
                    steps.append({"action": "skip_cleanup_cancel", "reason": f"order already terminal: {observed}"})
                else:
                    try:
                        logger.info("cancel_order %s %s (cleanup)", order_id, ctx.symbol)
                        res = await client.cancel_order(ctx.symbol, order_id)
                        steps.append(
                            {
                                "action": "cleanup_cancel",
                                "ok": res.is_ok,
                                "status": (res.data or {}).get("status") if res.is_ok else None,
                                "error": str(res.error) if not res.is_ok else "",
                            }
                        )
                    except Exception as exc:
                        steps.append({"action": "cleanup_cancel", "ok": False, "error": str(exc)[:200]})

    def _not_verifiable_liquidity(self, steps: list[dict[str, Any]], *, notional_usdt: float = 0.0) -> ScenarioResult:
        """流动性不足/盘口过深/全成交 → 无法验证部分成交语义。"""
        return self._fail(
            ScenarioStatus.NOT_VERIFIABLE,
            "LIQUIDITY_INSUFFICIENT_OR_TOO_DEEP",
            _NOT_VERIFIABLE_REASON,
            {"steps": steps, "notional_usdt": notional_usdt},
        )

    def _not_verifiable_terminal(self, steps: list[dict[str, Any]], *, notional_usdt: float = 0.0) -> ScenarioResult:
        """意外终态(未观察到部分成交即 CANCELED/EXPIRED/REJECTED)。"""
        return self._fail(
            ScenarioStatus.NOT_VERIFIABLE,
            "UNEXPECTED_TERMINAL_WITHOUT_PARTIAL_FILL",
            "unexpected_terminal_without_partial_fill",
            {"steps": steps, "notional_usdt": notional_usdt},
        )


SCENARIO_REGISTRY[PartialFillScenario.scenario_id] = PartialFillScenario
