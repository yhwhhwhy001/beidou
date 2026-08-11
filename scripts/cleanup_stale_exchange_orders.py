#!/usr/bin/env python3
"""清理 Binance Testnet 上残留的订单（普通订单 + Algo 条件单）。

上一轮引擎非正常退出（kill -9 / 崩溃）导致交易所残留订单，
新进程无法认领所有权 → protection_owner_unknown → 阻断下单。

用法:
  python scripts/cleanup_stale_exchange_orders.py
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path


TESTNET_REST = "https://demo-fapi.binance.com"

def _signed_request(api_key: str, api_secret: str, method: str, path: str, params: dict | None = None) -> dict:
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 60000
    qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    signature = hmac.new(api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    qs += f"&signature={signature}"

    url = f"{TESTNET_REST}{path}"
    if method == "GET":
        url = f"{url}?{qs}"
        req = urllib.request.Request(url, headers={"X-MBX-APIKEY": api_key})
    elif method == "DELETE":
        url = f"{url}?{qs}"
        req = urllib.request.Request(url, headers={"X-MBX-APIKEY": api_key}, method="DELETE")
    elif method == "POST":
        req = urllib.request.Request(url, data=qs.encode(), headers={"X-MBX-APIKEY": api_key})
    else:
        raise ValueError(f"Unsupported method: {method}")

    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def load_credentials() -> tuple[str, str]:
    """从环境变量或 .env 文件加载 API 凭据。"""
    api_key = os.environ.get("BEIDOU_BINANCE_API_KEY", "")
    api_secret = os.environ.get("BEIDOU_BINANCE_API_SECRET", "")

    if not api_key or not api_secret:
        print("❌ 未找到 API 凭据。请设置环境变量:")
        print("   export BEIDOU_BINANCE_API_KEY='your_key'")
        print("   export BEIDOU_BINANCE_API_SECRET='your_secret'")
        sys.exit(1)

    return api_key, api_secret


async def main() -> None:
    api_key, api_secret = load_credentials()
    print(f"🔗 连接到 Binance Testnet: {TESTNET_REST}")

    # 1. 取消所有普通挂单
    print("\n── 1. 查询并取消普通挂单 ──")
    try:
        orders = _signed_request(api_key, api_secret, "GET", "/fapi/v1/openOrders")
        print(f"   发现 {len(orders)} 个普通挂单")
        for o in orders:
            symbol = o["symbol"]
            oid = o["orderId"]
            try:
                _signed_request(api_key, api_secret, "DELETE", "/fapi/v1/order",
                                {"symbol": symbol, "orderId": oid})
                print(f"   ✅ 已取消 {symbol} 订单 {oid} ({o.get('side')} {o.get('origQty')})")
            except urllib.error.HTTPError as e:
                print(f"   ⚠️  取消失败 {symbol} {oid}: HTTP {e.code}")
    except urllib.error.HTTPError as e:
        print(f"   ⚠️  查询挂单失败: HTTP {e.code}")

    # 2. 取消所有 Algo 条件单
    print("\n── 2. 查询并取消 Algo 条件单 ──")
    try:
        algos = _signed_request(api_key, api_secret, "GET", "/fapi/v1/openAlgoOrders")
        print(f"   发现 {len(algos)} 个 Algo 条件单")
        for a in algos:
            symbol = a["symbol"]
            algo_id = a["algoId"]
            try:
                _signed_request(api_key, api_secret, "DELETE", "/fapi/v1/algoOrder",
                                {"symbol": symbol, "algoId": algo_id})
                print(f"   ✅ 已取消 {symbol} Algo {algo_id} ({a.get('side')} {a.get('orderType')})")
            except urllib.error.HTTPError as e:
                body = e.read().decode()[:200] if e.fp else ""
                print(f"   ⚠️  取消失败 {symbol} {algo_id}: HTTP {e.code} {body}")
    except urllib.error.HTTPError as e:
        print(f"   ⚠️  查询 Algo 失败: HTTP {e.code}")

    # 3. 再次确认清理干净
    print("\n── 3. 最终确认 ──")
    try:
        orders = _signed_request(api_key, api_secret, "GET", "/fapi/v1/openOrders")
        algos = _signed_request(api_key, api_secret, "GET", "/fapi/v1/openAlgoOrders")
        remaining = len(orders) + len(algos)
        if remaining == 0:
            print("   ✅ 交易所订单已全部清理干净")
        else:
            print(f"   ⚠️  仍有 {remaining} 个订单残留（可能需要手动处理）")
    except Exception as e:
        print(f"   ⚠️  最终确认失败: {e}")

    print("\n🎉 清理完成。现在可以重启引擎。")


if __name__ == "__main__":
    asyncio.run(main())
