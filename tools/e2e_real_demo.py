"""Retired unsafe end-to-end trading demo.

The former demo owned a direct exchange/order path and could not provide the
durable evidence required by the production gates.  Keeping a fail-closed
entry point at the old path prevents accidental execution while preserving a
clear migration target: ``scripts/testnet/run_g5.py`` plus the production
intent/outbox/executor chain.
"""

from __future__ import annotations


def main() -> int:
    print(  # noqa: T201
        "e2e_real_demo.py is retired: direct exchange demos are disabled; use the governed Testnet runner instead."
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
