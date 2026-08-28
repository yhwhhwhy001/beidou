# Code review

- Reviewer independence: self-review in the calling task; an independent human/reviewer sign-off is still required for admission.
- Version/diff: `main@ea68bcdc65ed6e2fdb5d749363d9b377e25e7cc2`; local uncommitted implementation diff; no remote mutation.

| Severity | Location | Finding | Evidence | Required action | Status |
|---|---|---|---|---|---|
| P1 | `apps/testnet_verify/runtime.py` | A restart exposed the same primary episode but omitted an already persisted close trace from `trace_ids`. | Regression test reproduced a mismatch, then passed after durable child trace aggregation. | Keep the restart regression test and re-run full integration tests. | FIXED |
| P1 | `beidou_exchange/binance_usdm/rest_client.py` | Signed parameters were assembled without standard URL encoding, allowing the signed material and venue parsing to diverge for special values. | Exact HMAC/request-capture test with a client id containing space and slash. | Keep encoding and the exact-query contract test. | FIXED |
| P1 | `beidou_exchange/core/rule_snapshot.py`, `beidou_exchange/binance_usdm/adapter.py` | A `NOTIONAL` filter using `minNotional` was not accepted by the rule source. | Parameterized adapter contract covers both `MIN_NOTIONAL` and `NOTIONAL`. | Preserve both field forms and fail closed for missing rules. | FIXED |
| P1 | `beidou_strategy/risk/adaptive_sizing_engine.py` | Partially supplied venue rules could fall through to legacy non-executable sizing. | New incomplete-rule test requires `VENUE_RULE_INPUT_INVALID`. | Keep legacy behavior only when no venue fields are supplied; reject partial executable inputs. | FIXED |
| P0 | Testnet E2E | No live venue evidence is present in this local review. | No authorized write campaign was run. | Do not advance Testnet admission or claim `Testnet Ready`. | OPEN / BLOCKING |
| P1 | Repository release gates | Full coverage is 98.10% against the configured 100% gate. | Fresh current-HEAD gate runs. | Preserve the failure; do not lower coverage or treat the legacy runner as the V4 verifier. | OPEN / BLOCKING |

## Decision

`PASS_WITH_CONDITIONS` for the reviewed local implementation scope. The
targeted contracts pass, but this is not an independent review and cannot
replace real Testnet evidence, full current-HEAD verification, or the
acceptance gate.
