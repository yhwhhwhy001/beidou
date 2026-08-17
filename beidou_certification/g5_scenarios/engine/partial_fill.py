"""partial_fill: 动态扫描浅盘口品种 → LIMIT 下单 → 确定性 PARTIALLY_FILLED 守卫断言。

Ruling-16(认证轮 #7 证据:INJUSDT 盘口从实测 2.1 变为 132778.9,testnet 做市
盘全局动态摆动,固定候选列表失效):动态扫描替代固定候选 ——
get_exchange_info() 全量取 status=TRADING 且以 USDT 结尾的品种(rest_client
的 iceberg_qty 参数保留,公共 API 不删;partial_fill 独立于 --symbol 运行,
控制器裁决的 plan 偏离,场景语义不变),按 best_ask(探测价)过滤
price ∈ [0.05, 100](min_gate 对齐粒度合理),seed 用场景启动时 injected now
整数部分随机采样最多 30 个(可复现),逐个探测 depth,选第一个满足
top_ask_qty>0 且 top_ask_qty×3 < min_gate_qty(该品种,min_gate 用全量
exchangeInfo 的 minQty/step + best_ask 计算,无需 ticker 调用)的品种
(浅盘口判定,×3 隐藏量容差保证部分成交);全 miss 后 sleep 10s 再扫一轮
(每轮重新随机采样),最多 3 轮;仍全 miss → NOT_VERIFIABLE
("liquidity_insufficient_or_too_deep",证据记每轮采样数与 miss 数)。
下单:LIMIT,qty=min_gate_qty(该品种动态计算、stepSize 对齐),price=best_ask,
GTC,普通限价(不带 iceberg)。notional 记账:入场 50.3 + 平仓 50.3 = 100.6;
全轮 100.6+171 ≈ 272 ≤ cap 400。
轮询:下单后首查 0.5s、之后 1s 间隔、30 次 ≈ 30s 窗口(余量挂单在盘口,市场
扫单可能 1-2s 内全成,首查要快);PARTIALLY_FILLED → 组件级守卫断言
(terminal_monotonic_guard("PARTIALLY_FILLED","NEW")=="PARTIALLY_FILLED",与引擎
save_order_state 单调守卫语义对拍,证据记录 monotonic_guard_source)并撤单清理;
全 FILLED 或窗口内仍 NEW → NOT_VERIFIABLE。平仓量下限 = min_gate_qty
(reduceOnly=true 下 quantity 可大于持仓 —— INJ 成交 2.1 时平仓单 qty=12.1
只平 2.1,notional 50.3 合规;实际仅平持仓量)。累计超限 → NotionalExceededError
交 runner fail-fast。dry_run 早退不碰任何接口;run() 自捕获异常返回 FAIL。

成交后平仓清理(认证轮 #2 根因:partial_fill 全成交留下 0.0022 BTC 持仓 →
引擎对账 MISMATCHED → trading_ready=False → 后续场景 NOT_VERIFIABLE):
一旦观察到 PARTIALLY_FILLED 或 FILLED 即存在真实持仓,必须反向市价单平仓
(side 反转、quantity=executedQty、reduceOnly=true 防开新仓;平仓价优先取
响应 avgPrice,缺失回退入场价;notional 记账平仓单金额),任何路径不得带
持仓离开场景 —— 平仓置于 try/except/finally 结构保证(含 NOT_VERIFIABLE
与异常路径,finally 兜底覆盖终态带成交与 NEW 带成交路径):平仓失败 →
close_failed 证据 + FAIL(持仓污染后续场景不可接受)。部分成交路径保留
现有 PASS 目标:平掉已成交部分,余量撤单。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from decimal import Decimal
from typing import Any, Awaitable, Callable, TypeVar

from beidou_certification.g5_scenarios.base import (
    NotionalExceededError,
    ScenarioBase,
    ScenarioContext,
    ScenarioResult,
    ScenarioStatus,
    min_gate_quantity,
)
from beidou_certification.g5_scenarios.engine.cancel_fill_race import (
    monotonic_guard_source,
    terminal_monotonic_guard,
)
from beidou_certification.g5_scenarios.runner import SCENARIO_REGISTRY
from beidou_exchange.core.error_taxonomy import Result

logger = logging.getLogger(__name__)

T = TypeVar("T")

_POLL_INTERVAL_SECONDS = 1.0  # 首查后 1s 间隔轮询
_FIRST_POLL_DELAY_SECONDS = 0.5  # 下单后首查 0.5s(余量挂单在盘口,市场扫单可能 1-2s 内全成)
_MAX_POLLS = 30  # 0.5s + 29×1s ≈ 30s 轮询窗口
# Ruling-16:动态扫描浅盘口品种(partial_fill 独立于 --symbol 运行,控制器裁决
# 的 plan 偏离,场景语义不变)。认证轮 #7:INJUSDT 盘口从实测 2.1 变为 132778.9
# (testnet 做市盘全局动态摆动),固定候选列表失效 → 从全量 exchangeInfo 取
# status=TRADING 且 USDT 结尾品种,best_ask ∈ [0.05, 100] 过滤(min_gate 对齐
# 粒度合理),seed 用场景启动时 injected now 整数部分随机采样最多 30 个
# (可复现),逐品种探测 depth;全 miss 后 sleep 10s 再扫一轮,最多 3 轮。
_SCAN_ROUNDS = 3  # 最多 3 轮扫描(轮间 sleep 10s,总等待 ≤20s < 60s 上限)
_SCAN_SAMPLE_SIZE = 30  # 每轮随机采样品种数上限
_SCAN_RETRY_INTERVAL_SECONDS = 10.0  # 全 miss 后重扫间隔(注入睡眠 seam)
_PRICE_FILTER_MIN = Decimal("0.05")  # 候选价格下界(min_gate 对齐粒度)
_PRICE_FILTER_MAX = Decimal("100")  # 候选价格上界
_SHALLOW_MULTIPLE = Decimal("3")  # 浅盘口判定:top_ask×3 < min_gate_qty(隐藏量容差)
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

    def __init__(
        self,
        *,
        now: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """now/sleep 为流动性探测与订单轮询的时钟/睡眠 seam(单测注入假实现)。

        默认依赖为 time.time / asyncio.sleep(认证轮真实执行);单测注入假时钟
        或 noop 睡眠,禁止真实等待与真实交易所调用。
        """
        self._now = now or time.time
        self._sleep = sleep or asyncio.sleep

    async def run(self, ctx: ScenarioContext) -> ScenarioResult:
        started = time.monotonic()
        steps: list[dict[str, Any]] = []
        order_id: int | None = None
        cancelled = False
        observed = "NEW"
        client: Any = None
        # 平仓清理状态(认证轮 #2 根因防护):last_executed_qty 记录最近轮询
        # 成交量为 0 即无持仓;close_attempted 标记分支平仓已执行(成功或失败),
        # 供 finally 兜底判断;entry_price/close_side 供异常路径反向下单。
        last_executed_qty: Decimal = Decimal("0")
        close_attempted = False
        entry_price: Decimal = Decimal("0")
        close_side = "SELL"
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

            # ---- 动态扫描浅盘口品种(Ruling-16,fix round 8)----
            # 认证轮 #7:INJUSDT 盘口从实测 2.1 变为 132778.9,固定候选列表失效
            # (testnet 做市盘全局动态摆动)→ 动态扫描替代固定候选:
            # get_exchange_info() 全量取 status=TRADING 且 USDT 结尾品种(缺
            # LOT_SIZE 的跳过),best_ask ∈ [0.05, 100] 过滤(min_gate 对齐粒度
            # 合理),seed 用 injected now 整数部分随机采样最多 30 个(可复现),
            # 逐个探测 depth(谓词:top_ask>0 且 top_ask×3 < min_gate_qty,
            # min_gate 用全量 exchangeInfo 的 minQty/step + best_ask 计算,
            # 无需 ticker 调用);全 miss 后 sleep 10s 再扫一轮(每轮重新随机
            # 采样),最多 3 轮;仍全 miss → NOT_VERIFIABLE(证据记每轮采样数
            # 与 miss 数)。
            full_info = _require_ok(await client.get_exchange_info(), "get_exchange_info")
            lot_sizes: dict[str, tuple[Decimal, Decimal]] = {}
            for entry in full_info.get("symbols", []):
                if entry.get("status") != "TRADING" or not str(entry.get("symbol", "")).endswith("USDT"):
                    continue
                try:
                    lot_sizes[str(entry["symbol"])] = _min_qty_and_step(str(entry["symbol"]), full_info)
                except ValueError:
                    continue  # 缺 LOT_SIZE 的品种跳过
            rng = random.Random(int(self._now()))  # noqa: S311 - 候选采样可复现(seed=now),非加密用途
            candidates = list(lot_sizes)
            entry_price = Decimal("0")
            min_gate_qty = Decimal("0")
            selected_symbol = ""
            for round_no in range(1, _SCAN_ROUNDS + 1):
                sampled = rng.sample(candidates, min(_SCAN_SAMPLE_SIZE, len(candidates)))
                misses = 0
                for candidate in sampled:
                    min_qty, step_size = lot_sizes[candidate]
                    depth = _require_ok(await client.get_depth(candidate), "get_depth")
                    asks = depth.get("asks") or []
                    bids = depth.get("bids") or []
                    probe: dict[str, Any] = {
                        "action": "depth_probe",
                        "symbol": candidate,
                        "round_no": round_no,
                        "best_ask": str(asks[0][0]) if asks else "",
                        "top_ask_qty": str(asks[0][1]) if asks else "",
                        "top_bid": str(bids[0][0]) if bids else "",
                    }
                    if asks:
                        ask_price = Decimal(str(asks[0][0]))
                        ask_qty = Decimal(str(asks[0][1]))
                        candidate_min_gate = min_gate_quantity(min_qty, step_size, ask_price)
                        probe["min_gate_qty"] = _format_qty(candidate_min_gate)
                        if not (_PRICE_FILTER_MIN <= ask_price <= _PRICE_FILTER_MAX):
                            probe["reason"] = "price_out_of_range"
                        elif ask_qty > 0 and ask_qty * _SHALLOW_MULTIPLE < candidate_min_gate:
                            selected_symbol = candidate
                            entry_price = ask_price
                            min_gate_qty = candidate_min_gate
                            probe["selected"] = True
                            steps.append(probe)
                            break
                        else:
                            probe["reason"] = "not_shallow"
                    else:
                        probe["reason"] = "no_ask"
                    misses += 1
                    steps.append(probe)
                round_summary: dict[str, Any] = {
                    "action": "scan_round",
                    "round_no": round_no,
                    "samples": len(sampled),
                    "misses": misses,
                    "hit": bool(selected_symbol),
                }
                steps.append(round_summary)
                if selected_symbol or round_no == _SCAN_ROUNDS:
                    break
                await self._sleep(_SCAN_RETRY_INTERVAL_SECONDS)
            if not selected_symbol:
                return self._fail(
                    ScenarioStatus.NOT_VERIFIABLE,
                    "LIQUIDITY_INSUFFICIENT_OR_TOO_DEEP",
                    _NOT_VERIFIABLE_REASON,
                    {"steps": steps, "scan_rounds": [s for s in steps if s.get("action") == "scan_round"]},
                )

            # 下单:LIMIT,qty=min_gate_qty(该品种动态计算、stepSize 对齐),
            # price=best_ask,GTC,普通限价(不带 iceberg);浅盘口判定保证
            # top_ask×3 < qty → 必然部分成交(余量挂盘),入场 notional ≈ 50.3
            symbol = selected_symbol
            qty = min_gate_qty
            price = str(entry_price)
            notional = float(qty * entry_price)
            ctx.ledger.record(self.scenario_id, notional)

            logger.info(
                "create_order BUY %s %s @ %s notional=%.2f USDT (partial_fill 浅盘口探针)",
                _format_qty(qty),
                symbol,
                price,
                notional,
            )
            order = _require_ok(
                await client.create_order(
                    symbol,
                    "BUY",
                    "LIMIT",
                    _format_qty(qty),
                    price=price,
                    time_in_force="GTC",
                    client_order_id=f"g5-pfill-{int(self._now() * 1000)}",
                ),
                "create_order",
            )
            order_id = int(order["orderId"])
            entry_side = str(order.get("side", "BUY")).upper()
            close_side = "SELL" if entry_side == "BUY" else "BUY"
            steps.append(
                {
                    "action": "place_order",
                    "order_id": order_id,
                    "symbol": symbol,
                    "qty": _format_qty(qty),
                    "price": price,
                    "notional_usdt": notional,
                    "status": order.get("status"),
                }
            )

            for poll_no in range(1, _MAX_POLLS + 1):
                # 首查 0.5s(余量挂单在盘口,市场扫单可能 1-2s 内全成,首查要快),
                # 之后 1s 间隔;窗口 ≈ 0.5 + 29×1 = 29.5s
                await self._sleep(_FIRST_POLL_DELAY_SECONDS if poll_no == 1 else _POLL_INTERVAL_SECONDS)
                queried = _require_ok(await client.get_order(symbol, order_id), "get_order")
                observed = str(queried.get("status", "NEW"))
                last_executed_qty = Decimal(str(queried.get("executedQty", "0") or "0"))
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
                logger.info("cancel_order %s %s (partial fill cleanup)", order_id, symbol)
                res = await client.cancel_order(symbol, order_id)  # 异常 → finally 重试清理
                cancelled = True
                steps.append(
                    {
                        "action": "cancel",
                        "ok": res.is_ok,
                        "status": (res.data or {}).get("status") if res.is_ok else None,
                        "error": str(res.error) if not res.is_ok else "",
                    }
                )
                final = _require_ok(await client.get_order(symbol, order_id), "get_order_after_cancel")
                final_status = str(final.get("status", ""))
                steps.append({"action": "query_after_cancel", "status": final_status})
                # 平仓量取撤单后最新成交:撤单竞态成交(final_status=FILLED)时
                # 轮询值 last_executed_qty 是 stale,直接平会留残余;final 缺失
                # executedQty 时 max 回退轮询值。
                final_executed_qty = Decimal(str(final.get("executedQty", "0") or "0"))
                close_qty = max(last_executed_qty, final_executed_qty)
                # 平掉已成交部分(持仓量小但同样污染后续场景;余量已撤单,防
                # 撤单前平仓导致余量再成交重新开仓)。平仓失败 → close_failed
                # 证据 + FAIL(持仓残留不可接受)。
                close_notional: float | None = None
                if close_qty > 0:
                    try:
                        close_notional = await self._close_position(
                            ctx,
                            steps,
                            symbol=symbol,
                            close_side=close_side,
                            close_qty=close_qty,
                            fallback_price=entry_price,
                            min_gate_qty=min_gate_qty,
                        )
                    except Exception as exc:
                        steps.append(
                            {
                                "action": "close_failed",
                                "side": close_side,
                                "qty": _format_qty(close_qty),
                                "error": str(exc)[:300],
                            }
                        )
                        return self._fail(
                            ScenarioStatus.FAIL,
                            "CLOSE_FAILED",
                            f"PARTIALLY_FILLED 后平仓失败(持仓残留会污染后续场景): {exc}",
                            {
                                "steps": steps,
                                "partial_status": "PARTIALLY_FILLED",
                                "final_status_after_cancel": final_status,
                                "notional_usdt": notional,
                                "closed": False,
                                "close_needed": True,
                            },
                        )
                    finally:
                        close_attempted = True
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
                        "close_notional_usdt": close_notional,
                        "closed": close_qty > 0,  # 无成交(executedQty=0)如实标注
                        "close_needed": close_qty > 0,
                    },
                    time.monotonic() - started,
                    error_type="" if ok else "UNEXPECTED_FINAL_STATUS",
                )
            if observed == "FILLED":
                # 全成交:盘口太薄,无法观察到部分成交;但成交产生真实持仓 →
                # 反向市价单平仓清理,防持仓污染后续场景(认证轮 #2 根因)。
                # 平仓成功 → 维持 NOT_VERIFIABLE(仍未观察到部分成交);平仓
                # 失败 → close_failed 证据 + FAIL(持仓残留不可接受)。
                filled_close_notional: float | None = None
                if last_executed_qty > 0:
                    try:
                        filled_close_notional = await self._close_position(
                            ctx,
                            steps,
                            symbol=symbol,
                            close_side=close_side,
                            close_qty=last_executed_qty,
                            fallback_price=entry_price,
                            min_gate_qty=min_gate_qty,
                        )
                    except Exception as exc:
                        steps.append(
                            {
                                "action": "close_failed",
                                "side": close_side,
                                "qty": _format_qty(last_executed_qty),
                                "error": str(exc)[:300],
                            }
                        )
                        return self._fail(
                            ScenarioStatus.FAIL,
                            "CLOSE_FAILED",
                            f"FILLED 后平仓失败(持仓残留会污染后续场景): {exc}",
                            {"steps": steps, "notional_usdt": notional, "closed": False},
                        )
                    finally:
                        close_attempted = True
                return self._fail(
                    ScenarioStatus.NOT_VERIFIABLE,
                    "LIQUIDITY_INSUFFICIENT_OR_TOO_DEEP",
                    _NOT_VERIFIABLE_REASON,
                    {
                        "steps": steps,
                        "notional_usdt": notional,
                        "close_notional_usdt": filled_close_notional,
                        "closed": last_executed_qty > 0,  # 无持仓(executedQty=0)如实标注
                        "close_needed": last_executed_qty > 0,
                    },
                )
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
                        logger.info("cancel_order %s %s (cleanup)", order_id, symbol)
                        res = await client.cancel_order(symbol, order_id)
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
            # 持仓清理保证(认证轮 #2 根因):只要观察到成交数量(executedQty>0)
            # 就存在真实持仓,且分支平仓未执行 → 兜底平仓。覆盖全部提前出口:
            # 守卫断言/撤单/查询异常、CANCELED/EXPIRED/REJECTED 竞态部分成交
            # (交易所真实状态:撤单成功但已部分成交)、窗口结束仍 NEW 但已成交;
            # EXPIRED/REJECTED 时 executedQty 恒 0 不会误平。此路径结果已是
            # FAIL/异常,平仓失败仅记录 close_failed(不再升级状态,但绝不静默
            # 带持仓离开场景)。
            if last_executed_qty > 0 and not close_attempted:
                try:
                    logger.info(
                        "close_position %s %s %s (cleanup)", close_side, _format_qty(last_executed_qty), symbol
                    )
                    await self._close_position(
                        ctx,
                        steps,
                        symbol=symbol,
                        close_side=close_side,
                        close_qty=last_executed_qty,
                        fallback_price=entry_price,
                        min_gate_qty=min_gate_qty,
                    )
                except Exception as exc:
                    steps.append(
                        {
                            "action": "close_failed",
                            "side": close_side,
                            "qty": _format_qty(last_executed_qty),
                            "error": str(exc)[:300],
                            "phase": "cleanup",
                        }
                    )

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

    async def _close_position(
        self,
        ctx: ScenarioContext,
        steps: list[dict[str, Any]],
        *,
        symbol: str,
        close_side: str,
        close_qty: Decimal,
        fallback_price: Decimal,
        min_gate_qty: Decimal,
    ) -> float:
        """反向市价单平仓已成交持仓,返回平仓单 notional(USDT)。

        side 反转入场方向,reduceOnly=true 防开新仓(平仓方向判断错误也不开
        新仓);平仓量下限 = min_gate_qty(订单层面 notional 按 quantity 计算
        ≥ MIN_NOTIONAL 保证交易所不拒绝;reduceOnly 语义下 quantity 可大于
        持仓,实际仅平掉持仓量 —— 0.0008 边界成交的平仓单恒合法,fix round
        5 根因:round 4 去掉命中下界后极小成交量的平仓单被拒绝 → CLOSE_FAILED);
        平仓价优先取响应 avgPrice(市价单真实成交价),缺失回退入场价;
        平仓单金额 qty×price 记入 ledger。创建失败(Result 错误或异常)抛出,
        调用方记录 close_failed 并 FAIL —— 持仓污染后续场景不可接受
        (认证轮 #2 根因:partial_fill 全成交留下持仓 → 引擎对账 MISMATCHED)。
        """
        client = ctx.client
        assert client is not None
        close_qty = max(close_qty, min_gate_qty)
        created = _require_ok(
            await client.create_order(
                symbol,
                close_side,
                "MARKET",
                _format_qty(close_qty),
                reduce_only="true",
                client_order_id=f"g5-pfill-close-{int(time.time() * 1000)}",
            ),
            "close_position_market_order",
        )
        avg_price = created.get("avgPrice")
        price = Decimal(str(avg_price)) if avg_price is not None else fallback_price
        close_notional = float(close_qty * price)
        ctx.ledger.record(self.scenario_id, close_notional)
        steps.append(
            {
                "action": "close",
                "side": close_side,
                "qty": _format_qty(close_qty),
                "price": str(price),
                "price_source": "avg_price" if avg_price is not None else "entry_price",
                "notional_usdt": close_notional,
                "order_id": int(created.get("orderId", 0)),
                "status": created.get("status"),
                "executed_qty": str(created.get("executedQty", "")),
            }
        )
        return close_notional


SCENARIO_REGISTRY[PartialFillScenario.scenario_id] = PartialFillScenario
