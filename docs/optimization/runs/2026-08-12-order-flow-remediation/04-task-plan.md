# Task plan

1. Add behavioral regression tests for environment parity, supervisor/runtime blocking, and normal execution planning.
2. Remove Testnet-specific control, reconciliation, protection, user-stream, and readiness exemptions.
3. Correct REST transport dispatch and classify ambiguous writes as UNKNOWN without retry.
4. Correct final approval verification/nonce consumption and typed algorithm contracts.
5. Introduce exact Decimal venue-rule quantization and validate the final command against the approved envelope.
6. Tighten protection restoration/coverage and UNKNOWN recovery so absence of evidence remains blocking.
7. Run focused tests after every slice, then critical regressions, full suite, static checks, and diff review.
8. Record unresolved PostgreSQL/live-exchange evidence as NOT_VERIFIABLE; do not promote Testnet/Mainnet readiness.
