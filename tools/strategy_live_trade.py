"""Retired unsafe live-trading probe.

This historical script used to own a second HTTP/order path and could submit
orders outside the governed intent/outbox/adapter chain.  It is intentionally
non-executable now.  Use ``scripts/testnet/run_g5.py`` for an authorized,
fail-closed Testnet protocol run; that runner never treats a partial probe as
an approval certificate.
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
