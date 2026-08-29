# Frozen production-safety modules (PKG-00-M05 / PKG-08)

Date: 2026-08-29 · V4.0 execution · baseline `ea68bcdc` → current HEAD

These modules are **FROZEN / FUTURE_MAINNET** for the default Testnet
verification path.  They are not deleted: they remain importable for
historical audit and the future multi-operator / real-money production path.
The bounded verifier (`apps.testnet_verify`) must not import them; the
architecture boundary is machine-enforced by
`tests/architecture/test_testnet_verification_boundaries.py`.

## Frozen list

| Module / asset | Status | Reason |
|---|---|---|
| `beidou_exchange/core/signed_capability.py` | `FUTURE_MAINNET` (code marker `MODULE_DEPLOYMENT_STATUS`) | External Ed25519 per-order capability issuance is production-governance complexity, not required for local single-operator Testnet verification |
| `beidou_certification` | FROZEN（2026-08-29 起不再作为启动门禁） | G5–G8 certification remains available as an explicitly invoked legacy path; `run_preflight` no longer requires an existing G5 certificate (PKG-08-M03) — the gate only runs when `_run_preflight(..., require_g5_certificate=True)` is called explicitly |
| `beidou_production` | FROZEN | Production release/promotion chain is out of Testnet scope |
| `beidou_chaos` | FROZEN (fault-injection pieces reusable) | Chaos engineering belongs to the future production stage |
| `beidou_launcher` G5/G7 producer + supervisor | DEPRECATED IN TESTNET DEFAULT | Legacy runtime remains the certification runner; the verifier is the sole Testnet verification entry |
| `beidou_core.engine` adaptive sizing helpers (`adaptive_leverage`, `adaptive_position_pct`) | DEPRECATED (PKG-05-M07) | Single sizing authority is `beidou_strategy.risk.adaptive_sizing_engine.compute_adaptive_sizing` |
| HA / leader election / distributed fencing | NOT BUILT | Explicitly out of scope for single-machine Testnet |
| multi-operator approval | NOT BUILT | Out of scope (local single operator) |
| withdrawal/transfer governance | NOT BUILT | Out of scope |

## Never removed (execution-truth capabilities)

Binance HMAC · Mainnet hard deny · exchangeInfo rule quantization · stable
clientOrderId / intent idempotency · query-before-retry · ACK identity
validation · fill/position/reconciliation · absolute notional/leverage caps ·
kill switch · durable evidence.

## Restoration path

Before any real-money work: re-activate the frozen modules behind an explicit
`FUTURE_MAINNET` release gate, restore the Ed25519 capability issuance,
re-run the full G5–G8 certification chain, and obtain an independent
acceptance review.  Nothing in this list is re-enabled by default.
