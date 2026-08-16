#!/usr/bin/env python3
"""Inspect stale Binance Futures Testnet orders without write authority.

The inventory path is read-only. Cancellation remains unavailable until the
write-capability registry and runtime grant path are implemented and certified.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from typing import Any

from beidou_exchange.binance_usdm.rest_client import BinanceRESTClient

TESTNET_REST = "https://demo-fapi.binance.com"
CONFIRMATION = "CANCEL-ALL-TESTNET-ORDERS"


def load_credentials() -> tuple[str, str]:
    """Load credentials without printing their values."""
    api_key = os.environ.get("BEIDOU_BINANCE_API_KEY", "")
    api_secret = os.environ.get("BEIDOU_BINANCE_API_SECRET", "")
    if not api_key or not api_secret:
        raise RuntimeError("BEIDOU_BINANCE_API_KEY and BEIDOU_BINANCE_API_SECRET are required")
    return api_key, api_secret


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="reserved; currently hard-held")
    parser.add_argument("--confirm", default="", help=f"reserved phrase: {CONFIRMATION}")
    return parser.parse_args()


def _count(result: Any) -> int:
    if not result.is_success() or not isinstance(result.data, list):
        category = getattr(getattr(result.error, "category", None), "value", "UNKNOWN")
        raise RuntimeError(f"exchange read failed: {category}")
    return len(result.data)


async def main() -> int:
    args = parse_args()
    if args.execute:
        raise RuntimeError("WRITE_CAPABILITY_REGISTRY_INCOMPLETE")

    api_key, api_secret = load_credentials()
    client = BinanceRESTClient(TESTNET_REST, api_key=api_key, api_secret=api_secret)
    try:
        orders_result = await client.get_open_orders()
        algos_result = await client.get_open_algo_orders()
        normal_count = _count(orders_result)
        algo_count = _count(algos_result)
        print(f"Testnet inventory: normal_orders={normal_count}, algo_orders={algo_count}")

        if not args.execute:
            print("Read-only inspection complete; no orders were cancelled.")
            return 0

        failures = 0
        for order in orders_result.data or []:
            if not isinstance(order, dict) or not order.get("symbol") or order.get("orderId") is None:
                failures += 1
                continue
            result = await client.cancel_order(str(order["symbol"]), int(order["orderId"]))
            failures += int(not result.is_success())

        for algo in algos_result.data or []:
            if not isinstance(algo, dict) or not algo.get("symbol") or algo.get("algoId") is None:
                failures += 1
                continue
            result = await client.cancel_algo_order(str(algo["symbol"]), int(algo["algoId"]))
            failures += int(not result.is_success())

        remaining_orders = _count(await client.get_open_orders())
        remaining_algos = _count(await client.get_open_algo_orders())
        print(
            f"Cancellation verification: failures={failures}, "
            f"remaining_normal={remaining_orders}, remaining_algo={remaining_algos}"
        )
        return 0 if failures == 0 and remaining_orders == 0 and remaining_algos == 0 else 2
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
