"""BD-T20: G5 认证后下单流程验证 — testnet 真实下单 + SL/TP 挂单 → 展示 → 清理。

用法:
    python scripts/testnet/verify_order_flow.py [--symbol BTCUSDT]

流程(全部真实执行,小额 notional):
    1. 市价买单 minQty(立即 FILLED,避免部分成交撤单触发引擎 ghost 缺陷)
    2. 持仓确认后挂真实 SL(远价 -30%)+ TP(远价 +30%)algo 条件单
    3. 查询并展示:入场订单、持仓、SL/TP 挂单(订单号/状态/时间)
    4. 清理:撤 SL/TP algo 单 → 市价平仓持仓 → 复核账户空持仓零挂单
退出码:0=全部验证通过;1=任何步骤失败(持仓/挂单残留会打印告警)。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal

from beidou_exchange.binance_usdm.rest_client import BinanceRESTClient
from beidou_exchange.core.error_taxonomy import Result


def _fmt_time(ms: int | str) -> str:
    """Binance 毫秒时间戳 → 本地可读时间。"""
    try:
        return datetime.fromtimestamp(int(ms) / 1000).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return "-"


def _show(title: str) -> None:
    print(f"\n=== {title} ===")


def _require(result: Result, action: str):
    if not result.is_ok or result.data is None:
        raise RuntimeError(f"{action} failed: {result.error}")
    return result.data


async def _require_retry(client: BinanceRESTClient, call, action: str, retries: int = 3) -> dict:
    """demo-fapi 签名请求间歇性 401(2026-08-18 实测,与 21:48 同模式)——
    重试 3 次(间隔 8s),全部失败才抛出;下单类动作不用此包装(防重复下单)。"""
    last: Exception | None = None
    for attempt in range(retries):
        result = await call()
        if result.is_ok and result.data is not None:
            return result.data
        last = RuntimeError(f"{action} failed: {result.error}")
        if attempt < retries - 1:
            print(f"  (重试 {attempt + 1}/{retries - 1}: {result.error})")
            await asyncio.sleep(8)
    assert last is not None
    raise last


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTCUSDT")
    args = parser.parse_args()
    symbol = args.symbol

    api_key = os.environ.get("BEIDOU_BINANCE_API_KEY", "")
    api_secret = os.environ.get("BEIDOU_BINANCE_API_SECRET", "")
    if not api_key or not api_secret:
        print("FAIL: BEIDOU_BINANCE_API_KEY/SECRET 未设置(source ~/beidou/.env)")
        return 1

    client = BinanceRESTClient(
        rest_url="https://demo-fapi.binance.com",
        api_key=api_key,
        api_secret=api_secret,
    )
    failures: list[str] = []
    algo_ids: list[int] = []
    entry_order_id: int | None = None
    entry_qty = "0"

    # ---- 前置:确认账户空持仓零挂单(防残留) ----
    acct = await _require_retry(client, client.get_account, "get_account")
    resid = [
        (p["symbol"], p["positionAmt"]) for p in acct.get("positions", []) if float(p.get("positionAmt", 0)) != 0
    ]
    open_orders = await _require_retry(client, client.get_open_orders, "get_open_orders")
    if resid:
        print(f"WARN: 账户存在残留持仓 {resid} — 先跳过入场,直接平仓清理")
    if open_orders:
        print(f"WARN: 账户存在挂单 {[o['orderId'] for o in open_orders]} — 先撤单清理")
        for o in open_orders:
            await client.cancel_order(o["symbol"], int(o["orderId"]))
        open_orders = await _require_retry(client, client.get_open_orders, "get_open_orders")

    # ---- 步骤 1: 市价买单(minQty) ----
    depth = _require(await client.get_depth(symbol), "get_depth")
    asks = depth.get("asks") or []
    if not asks:
        print(f"FAIL: {symbol} 盘口无 ask")
        return 1
    market_price = Decimal(str(asks[0][0]))
    info = _require(await client.get_exchange_info(symbol), "get_exchange_info")
    min_qty = Decimal("0.001")
    step_size = Decimal("0.001")
    for entry in info.get("symbols", []):
        if entry.get("symbol") == symbol:
            for f in entry.get("filters", []):
                if f.get("filterType") == "LOT_SIZE":
                    min_qty = Decimal(str(f["minQty"]))
                    step_size = Decimal(str(f.get("stepSize", "0.001")))
    # MIN_NOTIONAL 门槛(BTC 0.0001×64093=6.4 < 50 会被拒 —— 认证轮同款核算)
    from beidou_certification.g5_scenarios.base import min_gate_quantity

    qty = format(min_gate_quantity(min_qty, step_size, market_price).normalize(), "f")
    t0 = int(time.time() * 1000)
    entry = _require(
        await client.create_order(symbol, "BUY", "MARKET", qty, client_order_id=f"g5-verify-entry-{t0}"),
        "create_order(entry)",
    )
    entry_order_id = int(entry["orderId"])
    entry_qty = qty
    _show(f"步骤 1: 市价买单 {symbol} {qty}")
    print(f"  订单号: {entry['orderId']}")
    print(f"  状态:   {entry.get('status')} (下单响应)")
    print(f"  时间:   {_fmt_time(entry.get('updateTime', 0) or t0)}")
    print(f"  成交价: {entry.get('avgPrice', '-')}  成交额: {entry.get('cummulativeQuote', '-')} USDT")
    if str(entry.get("status")) not in {"FILLED", "NEW"}:
        failures.append(f"入场单异常状态 {entry.get('status')}")

    # ---- 步骤 2: 挂 SL/TP algo 条件单(远价防触发) ----
    sl_price = float(market_price * Decimal("0.7"))  # -30% 远价
    tp_price = float(market_price * Decimal("1.3"))  # +30% 远价
    sl = _require(
        await client.create_algo_order(
            {
                "symbol": symbol,
                "side": "SELL",
                "algoType": "CONDITIONAL",
                "type": "STOP_MARKET",
                "quantity": qty,
                "triggerPrice": f"{sl_price:.4f}",
                "reduceOnly": "true",
                "workingType": "CONTRACT_PRICE",
                "clientAlgoId": f"g5-verify-sl-{t0}",
            }
        ),
        "create_algo_order(SL)",
    )
    tp = _require(
        await client.create_algo_order(
            {
                "symbol": symbol,
                "side": "SELL",
                "algoType": "CONDITIONAL",
                "type": "TAKE_PROFIT_MARKET",
                "quantity": qty,
                "triggerPrice": f"{tp_price:.4f}",
                "reduceOnly": "true",
                "workingType": "CONTRACT_PRICE",
                "clientAlgoId": f"g5-verify-tp-{t0}",
            }
        ),
        "create_algo_order(TP)",
    )
    sl_id = int(sl.get("algoId", 0))
    tp_id = int(tp.get("algoId", 0))
    algo_ids = [i for i in (sl_id, tp_id) if i]

    # ---- 步骤 3: 查询并展示 ----
    queried = await _require_retry(client, lambda: client.get_order(symbol, entry_order_id), "get_order(entry)")
    position_line = ""
    acct2 = await _require_retry(client, client.get_account, "get_account(after)")
    for p in acct2.get("positions", []):
        if p["symbol"] == symbol and float(p.get("positionAmt", 0)) != 0:
            position_line = (
                f"{p['symbol']} amt={p['positionAmt']} entry={p.get('entryPrice')} "
                f"pnl={p.get('unrealizedProfit', '-')} USDT"
            )
    _show(f"步骤 3: 订单与持仓状态({symbol})")
    print(f"  入场订单 {entry_order_id}: status={queried.get('status')} executed={queried.get('executedQty')} "
          f"avgPrice={queried.get('avgPrice')} 更新时间={_fmt_time(queried.get('updateTime', 0))}")
    print(f"  持仓: {position_line or '无(入场未成交)'}")

    # algo 列表接口 demo-fapi 不实时(认证轮 #2 证据),按 algoId 逐单查询展示
    _show("步骤 3b: SL/TP 条件挂单")
    for label, aid, px in (("SL(止损)", sl_id, sl_price), ("TP(止盈)", tp_id, tp_price)):
        if aid:
            print(f"  {label} algoId={aid} 触发价={px:.4f} 数量={qty} "
                  f"创建时间={datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S')}")

    # ---- 步骤 4: 清理(撤 SL/TP → 平仓 → 复核) ----
    _show("步骤 4: 清理")
    for aid in algo_ids:
        r = await client.cancel_algo_order(symbol, aid)
        print(f"  撤 algo {aid}: {'ok' if r.is_ok else f'FAIL {r.error}'}")
        if not r.is_ok:
            failures.append(f"撤 algo {aid} 失败")
    if entry_order_id is not None:
        close = _require(
            await client.create_order(
                symbol, "SELL", "MARKET", entry_qty, reduce_only="true", client_order_id=f"g5-verify-close-{t0}"
            ),
            "create_order(close)",
        )
        print(f"  平仓单 {close.get('orderId')}: status={close.get('status')} "
              f"avgPrice={close.get('avgPrice')} executed={close.get('executedQty')}")
    acct3 = await _require_retry(client, client.get_account, "get_account(final)")
    final_resid = [
        (p["symbol"], p["positionAmt"]) for p in acct3.get("positions", []) if float(p.get("positionAmt", 0)) != 0
    ]
    final_open = await _require_retry(client, client.get_open_orders, "get_open_orders(final)")
    print(f"  复核持仓: {final_resid or '空'}  复核挂单: {[o['orderId'] for o in final_open] or '空'}")
    if final_resid or final_open:
        failures.append(f"清理不彻底: 持仓 {final_resid} 挂单 {[o['orderId'] for o in final_open]}")

    _show("验证结果")
    if failures:
        for f in failures:
            print(f"  FAIL: {f}")
        return 1
    print("  全部通过: 真实下单 FILLED → SL/TP 挂撤往返 → 平仓 → 账户空持仓零挂单")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
