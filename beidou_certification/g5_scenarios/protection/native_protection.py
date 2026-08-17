"""native_protection: 原生保护组场景一 — 原生 Algo 条件单挂撤往返与孤儿判定。

真实路径(仅认证轮经用户批准后由 run_g5.py 执行;开发与单测期间禁止真实
交易所调用,所有交易所动作都是可注入依赖,单测注入假驱动):
1. 只读:get_account 拿当前持仓 symbols(空账户 → 场景只挂"孤儿"验证路径);
   get_open_algo_orders 基线
2. 挂 1 个 STOP 条件单(create_algo_order,quantity=min_qty,triggerPrice=
   当前价 × 0.5 远价防触发)→ 断言 open_algos 中出现
3. 撤单(cancel_algo_order)→ 断言消失;证据记录挂撤往返
4. 若账户有真实持仓,额外断言:持仓 symbol 的 SL/TP algo 均存在(引擎职责,
   只读验证)+ 孤儿判定纯函数与交易所事实对拍;孤儿单(空账户下的任何
   open algo)判引擎缺陷 FAIL

notional 记账 0(Algo 单不占用即时资金,仅记录触发风险敞口 0.0);
NotionalExceededError 重新抛出供 runner fail-fast;挂/撤单前 logger.info
打印操作意图(操作类型 + algo 参数摘要 + notional 敞口);dry_run 仅执行
纯函数判定部分,记 NOT_VERIFIABLE("REAL_EXECUTION_REQUIRED"),不触碰任何
真实交易所端点;run() 自捕获异常返回 FAIL。
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any, Awaitable, Callable, TypeVar

from beidou_certification.g5_scenarios.base import (
    NotionalExceededError,
    ScenarioBase,
    ScenarioContext,
    ScenarioResult,
    ScenarioStatus,
)
from beidou_certification.g5_scenarios.protocol.create_query_cancel import _tick_size, min_order_quantity
from beidou_certification.g5_scenarios.runner import SCENARIO_REGISTRY
from beidou_exchange.core.error_taxonomy import Result

logger = logging.getLogger(__name__)

T = TypeVar("T")

# STOP 条件单远价触发点:当前价 × 0.5,防触发(触发价远低于市价,测试窗口内
# 不可能被触达;若极端行情触达,reduceOnly 阻止开新仓)
_FAR_TRIGGER_FACTOR = "0.5"


def _require_ok(result: Result[T], action: str) -> T:
    """解包 Result:失败抛 RuntimeError 携带交易所错误原文进证据。"""
    if not result.is_ok:
        raise RuntimeError(f"{action} failed: {result.error}")
    assert result.data is not None
    return result.data


def algo_orphan_verdict(open_algos: list[dict], position_symbols: set[str]) -> list[dict]:
    """孤儿 algo 单判定:algo 单的 symbol 不在持仓 symbols 中即为孤儿。

    返回原 dict 对象列表(不复制),调用侧与交易所 open algo 事实对拍;
    缺 symbol 字段的单按孤儿处理(fail-closed)。
    """
    return [algo for algo in open_algos if algo.get("symbol") not in position_symbols]


def _far_trigger_price(symbol: str, exchange_info: dict[str, Any], last_price: str) -> str:
    """远价防触发触发价:现价 × 0.5,向下对齐 tickSize(与 _resting_buy_price 同风格)。"""
    raw = Decimal(str(last_price)) * Decimal(_FAR_TRIGGER_FACTOR)
    tick = _tick_size(symbol, exchange_info)
    return str((raw // tick) * tick)


def _position_symbols(account: dict[str, Any]) -> set[str]:
    """从 get_account 响应提取持仓 symbols(仅非零持仓)。

    Binance USDⓈ-M account.positions[].positionAmt 字符串化,缺失/非法金额
    视为零;缺 symbol 或非字符串的行跳过。
    """
    symbols: set[str] = set()
    for row in account.get("positions", []):
        if not isinstance(row, dict):
            continue
        try:
            amt = float(row.get("positionAmt") or 0)
        except (TypeError, ValueError):
            continue
        if abs(amt) <= 1e-12:
            continue
        symbol = row.get("symbol")
        if isinstance(symbol, str) and symbol:
            symbols.add(symbol)
    return symbols


def _algo_id_of(algo: dict[str, Any]) -> int | None:
    """提取 algo 单的 algoId(缺失/非数字 → None)。"""
    raw = algo.get("algoId")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


class NativeProtectionScenario(ScenarioBase):
    scenario_id = "native_protection"

    def __init__(
        self,
        *,
        get_account: Callable[[], Awaitable[dict[str, Any]]] | None = None,
        get_exchange_info: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
        get_ticker: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
        get_open_algo_orders: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None,
        create_algo_order: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
        cancel_algo_order: Callable[[str, int], Awaitable[dict[str, Any]]] | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        """交易所动作全部可注入(单测注入假驱动,禁止真实交易所调用)。

        get_account/get_exchange_info/get_ticker/get_open_algo_orders/
        create_algo_order/cancel_algo_order 的默认实现为真实交易所调用
        (运行时绑定 ctx.client 并解包 Result);场景逻辑只调用注入的依赖。
        """
        self._get_account = get_account or self._get_account_impl
        self._get_exchange_info = get_exchange_info or self._get_exchange_info_impl
        self._get_ticker = get_ticker or self._get_ticker_impl
        self._get_open_algo_orders = get_open_algo_orders or self._get_open_algo_orders_impl
        self._create_algo_order = create_algo_order or self._create_algo_order_impl
        self._cancel_algo_order = cancel_algo_order or self._cancel_algo_order_impl
        self._now = now or time.time
        self._client: Any = None

    # ---- 真实交易所实现(默认依赖;认证轮经用户批准后由 run_g5.py 执行) ----

    async def _get_account_impl(self) -> dict[str, Any]:
        result = await self._client.get_account()
        return _require_ok(result, "get_account")

    async def _get_exchange_info_impl(self, symbol: str) -> dict[str, Any]:
        result = await self._client.get_exchange_info(symbol)
        return _require_ok(result, "get_exchange_info")

    async def _get_ticker_impl(self, symbol: str) -> dict[str, Any]:
        result = await self._client.get_ticker(symbol)
        return _require_ok(result, "get_ticker")

    async def _get_open_algo_orders_impl(self) -> list[dict[str, Any]]:
        result = await self._client.get_open_algo_orders()
        data = _require_ok(result, "get_open_algo_orders")
        if not isinstance(data, list):
            raise RuntimeError("get_open_algo_orders 返回非列表")
        return [item for item in data if isinstance(item, dict)]

    async def _create_algo_order_impl(self, params: dict[str, Any]) -> dict[str, Any]:
        result = await self._client.create_algo_order(params)
        data = _require_ok(result, "create_algo_order")
        if not isinstance(data, dict):
            raise RuntimeError("create_algo_order 返回非对象")
        return data

    async def _cancel_algo_order_impl(self, symbol: str, algo_id: int) -> dict[str, Any]:
        result = await self._client.cancel_algo_order(symbol, algo_id)
        data = _require_ok(result, "cancel_algo_order")
        if not isinstance(data, dict):
            raise RuntimeError("cancel_algo_order 返回非对象")
        return data

    # ---- 场景执行 ----

    async def run(self, ctx: ScenarioContext) -> ScenarioResult:
        started = time.monotonic()
        steps: list[dict[str, Any]] = []
        try:
            ctx.ledger.record(self.scenario_id, 0.0)
            if ctx.dry_run:
                # 跳过真实挂单/撤单,仅执行纯函数判定部分(空事实):孤儿判定在
                # 无持仓无挂单时输出空集 —— 不触碰任何交易所端点
                probe = algo_orphan_verdict([], set())
                return self._fail(
                    ScenarioStatus.NOT_VERIFIABLE,
                    "REAL_EXECUTION_REQUIRED",
                    "native_protection requires real execution (algo 挂撤单只在认证轮经用户批准后执行)",
                    {
                        "dry_run": True,
                        "steps": steps,
                        "pure_verdict_probe": {"open_algos": 0, "position_symbols": 0, "orphans": len(probe)},
                    },
                )
            if ctx.client is None:
                return self._fail(
                    ScenarioStatus.NOT_VERIFIABLE,
                    "CLIENT_UNAVAILABLE",
                    "需要交易所客户端读取账户/挂单并挂撤 algo 条件单",
                    {"steps": steps},
                )
            self._client = ctx.client

            # 1. 只读基线:账户当前持仓 symbols + 存量 open algo(引擎职责基线)
            account = await self._get_account()
            position_symbols = _position_symbols(account)
            steps.append({"action": "account_snapshot", "position_symbols": sorted(position_symbols)})
            baseline_algos = await self._get_open_algo_orders()
            baseline_ids = sorted({str(a.get("algoId")) for a in baseline_algos if a.get("algoId") is not None})
            steps.append({"action": "baseline_open_algos", "count": len(baseline_algos), "algo_ids": baseline_ids})

            # 2. 挂 1 个 STOP 条件单(quantity=min_qty,triggerPrice=当前价×0.5 远价防触发)
            info = await self._get_exchange_info(ctx.symbol)
            min_qty, _ = min_order_quantity(ctx.symbol, info)
            ticker = await self._get_ticker(ctx.symbol)
            trigger = _far_trigger_price(ctx.symbol, info, str(ticker["lastPrice"]))
            qty = format(min_qty, "f")
            params: dict[str, Any] = {
                "symbol": ctx.symbol,
                "side": "SELL",
                "algoType": "CONDITIONAL",
                "type": "STOP_MARKET",
                "quantity": qty,
                "triggerPrice": trigger,
                "reduceOnly": "true",
                "workingType": "CONTRACT_PRICE",
                "clientAlgoId": f"g5-np-{int(self._now() * 1000)}",
            }
            logger.info(
                "native_protection: create_algo_order %s SELL %s %s triggerPrice=%s notional_exposure=0.0 USDT",
                params["algoType"],
                qty,
                ctx.symbol,
                trigger,
            )
            created = await self._create_algo_order(params)
            algo_id = _algo_id_of(created)
            if algo_id is None:
                raise RuntimeError(f"create_algo_order 响应缺少 algoId: {str(created)[:200]}")
            steps.append({"action": "algo_created", "algo_id": algo_id, "algo_status": created.get("algoStatus")})
            cancelled = False
            try:
                open_algos = await self._get_open_algo_orders()
                appeared = any(_algo_id_of(a) == algo_id for a in open_algos)
                steps.append({"action": "algo_appears_in_open", "algo_id": algo_id, "appeared": appeared})
                if not appeared:
                    steps.append(
                        {
                            "action": "algo_appearance_warning",
                            "note": "挂单后 open 列表未出现(幽灵 ACK),仍执行撤单清理",
                        }
                    )
                logger.info("native_protection: cancel_algo_order %s %s %s", algo_id, qty, ctx.symbol)
                cancelled_res = await self._cancel_algo_order(ctx.symbol, algo_id)
                cancelled = True
                steps.append(
                    {"action": "algo_cancelled", "algo_id": algo_id, "status": cancelled_res.get("algoStatus")}
                )
                after_algos = await self._get_open_algo_orders()
                gone = not any(_algo_id_of(a) == algo_id for a in after_algos)
                steps.append({"action": "algo_gone_after_cancel", "algo_id": algo_id, "gone": gone})
            finally:
                if not cancelled:  # 撤单失败也不残留挂单
                    try:
                        logger.info("native_protection: cancel_algo_order %s %s %s (cleanup)", algo_id, qty, ctx.symbol)
                        await self._cancel_algo_order(ctx.symbol, algo_id)
                        steps.append({"action": "cleanup_cancel_algo", "ok": True})
                    except Exception as exc:
                        steps.append({"action": "cleanup_cancel_algo", "ok": False, "error": str(exc)[:200]})

            # 3. 孤儿判定纯函数与交易所事实对拍 + 持仓保护覆盖(引擎职责,只读验证)
            final_open = await self._get_open_algo_orders()
            orphans = algo_orphan_verdict(final_open, position_symbols)
            steps.append(
                {
                    "action": "orphan_verdict",
                    "open_algo_count": len(final_open),
                    "position_count": len(position_symbols),
                    "orphan_count": len(orphans),
                    "orphans": [{"algoId": _algo_id_of(o), "symbol": o.get("symbol")} for o in orphans],
                }
            )
            missing_coverage: list[str] = []
            if position_symbols:
                symbols_with_algo = {a.get("symbol") for a in final_open if a.get("symbol")}
                missing_coverage = [symbol for symbol in sorted(position_symbols) if symbol not in symbols_with_algo]
                steps.append(
                    {
                        "action": "protection_coverage",
                        "position_symbols": sorted(position_symbols),
                        "missing": missing_coverage,
                    }
                )
            if not appeared or not gone:
                return self._fail(
                    ScenarioStatus.FAIL,
                    "ALGO_ROUNDTRIP_FAILED",
                    f"挂撤往返未闭合: appeared={appeared} gone={gone}",
                    {"steps": steps},
                )
            if missing_coverage:
                return self._fail(
                    ScenarioStatus.FAIL,
                    "MISSING_PROTECTION_ALGO",
                    f"持仓 symbol 缺 SL/TP algo: {missing_coverage}",
                    {"steps": steps},
                )
            if orphans:
                return self._fail(
                    ScenarioStatus.FAIL,
                    "ORPHAN_ALGO_DETECTED",
                    f"孤儿 algo 单: {[o.get('symbol') for o in orphans][:10]}",
                    {"steps": steps},
                )
            return ScenarioResult(
                self.scenario_id,
                ScenarioStatus.PASS,
                {
                    "steps": steps,
                    "verdict": "native_protection_roundtrip_ok",
                    "algo_id": algo_id,
                    "trigger_price": trigger,
                    "quantity": qty,
                    "position_symbols": sorted(position_symbols),
                    "notional_usdt": 0.0,
                },
                time.monotonic() - started,
            )
        except NotionalExceededError:
            raise  # 名义超限交给 runner fail-fast(资金保护优先)
        except Exception as exc:
            return self._fail(ScenarioStatus.FAIL, type(exc).__name__, str(exc)[:300], {"steps": steps})


SCENARIO_REGISTRY[NativeProtectionScenario.scenario_id] = NativeProtectionScenario
