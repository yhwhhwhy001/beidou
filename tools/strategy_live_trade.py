"""Retired unsafe live-trading probe.

This historical script used to own a second HTTP/order path and could submit
orders outside the governed intent/outbox/adapter chain.  It is intentionally
non-executable now.  The sole Testnet verification entry is
``python -m apps.testnet_verify`` (V4.0, PKG-10); ``scripts/testnet/run_g5.py``
remains the authorized legacy G5 certification runner and is not a
verification entry.
"""

from __future__ import annotations


def main() -> int:
    print(  # noqa: T201
        "strategy_live_trade.py is retired: direct live trading probes are "
        "disabled; use the governed Testnet runner instead."
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
