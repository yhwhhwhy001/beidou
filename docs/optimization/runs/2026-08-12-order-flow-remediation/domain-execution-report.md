# Exchange execution report

- Authority: local controlled tests only; no exchange side effects.
- Environment identity: local Python test environment, not Testnet or Mainnet.
- Cases verified: real PUT dispatch; ambiguous 5xx/network write becomes UNKNOWN without retry; idempotency/client-id preservation; exact quantity/price quantization; final approval nonce consumption and replay rejection; stale/changed rule rejection; reduce-only emergency selection; confirmed protection coverage; startup UNKNOWN preservation; account/algo/margin stream invalidation.
- Local contract decision: PASS_WITH_CONDITIONS.
- External execution readiness: HOLD / NOT_VERIFIABLE.
- Missing evidence: authoritative PostgreSQL fact chain, real user stream, exchange query-before-retry, partial fill/cancel race, protective-order readback and reconciliation under restart.
- Forbidden in this run: submit, cancel, close, leverage mutation, credential/balance/position mutation, restart and deployment. Git commit/push was subsequently authorized as a separate handoff action.
