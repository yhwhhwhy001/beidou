# Code review

- Reviewer independence: self-review in the calling task; an independent human/reviewer sign-off is still required for admission.
- Version/diff: `main@ea68bcdc65ed6e2fdb5d749363d9b377e25e7cc2`; local uncommitted implementation diff; no remote mutation.

| Severity | Location | Finding | Evidence | Required action | Status |
|---|---|---|---|---|---|
| P1 | `apps/testnet_verify/runtime.py` | A restart exposed the same primary episode but omitted an already persisted close trace from `trace_ids`. | Regression test reproduced a mismatch, then passed after durable child trace aggregation. | Keep the restart regression test and re-run full integration tests. | FIXED |
| P1 | `beidou_exchange/binance_usdm/rest_client.py` | Signed parameters were assembled without standard URL encoding, allowing the signed material and venue parsing to diverge for special values. | Exact HMAC/request-capture test with a client id containing space and slash. | Keep encoding and the exact-query contract test. | FIXED |
| P1 | `beidou_exchange/core/rule_snapshot.py`, `beidou_exchange/binance_usdm/adapter.py` | A `NOTIONAL` filter using `minNotional` was not accepted by the rule source. | Parameterized adapter contract covers both `MIN_NOTIONAL` and `NOTIONAL`. | Preserve both field forms and fail closed for missing rules. | FIXED |
| P1 | `beidou_strategy/risk/adaptive_sizing_engine.py` | Partially supplied venue rules could fall through to legacy non-executable sizing. | New incomplete-rule test requires `VENUE_RULE_INPUT_INVALID`. | Keep legacy behavior only when no venue fields are supplied; reject partial executable inputs. | FIXED |
| P0 | Testnet E2E | Historical live venue evidence exists, but later KeepAlive execution exceeded the bounded campaign authorization. | Repeated manifests and launchd/process evidence. | Preserve historical facts, withdraw bounded-campaign claim, and do not advance admission before remediation revalidation. | OPEN / BLOCKING |
| P1 | Repository release gates | Full coverage is 98.04% against the configured 100% gate. | Fresh current-HEAD gate runs. | Preserve the failure; do not lower coverage or treat the legacy runner as the V4 verifier. | OPEN / BLOCKING |
| P0 | `apps/testnet_verify/runtime.py` | A partial fill could remain nonterminal without owned remainder cancellation, and execution attribution used observational placeholders. | Poll/cancel/close fault fixtures and signed venue-attribution contracts. | Preserve terminal filled-prefix reconciliation and fail UNKNOWN when attribution is absent. | FIXED |
| P0 | `beidou_exchange/testnet_guard.py` | Request context was not bound to every final request field; account aggregate exposure and durable kill-switch race were not enforced at the final authority boundary. | Context mutation, exposure cap and durable switch tests. | Keep exact final-request hash/identity binding and authority-boundary switch check. | FIXED |
| P1 | Testnet trace parity | The runtime reported `MATCH` by comparing its Testnet proposal with itself. | Runtime test now requires `NOT_RUN` and `BACKTEST_AND_PAPER_REQUIRED`. | Supply independent frozen-input Backtest/Paper evidence before parity can pass. | FIXED / EVIDENCE OPEN |
| P0 | `deploy/beidou_testnet_verify.sh`, `deploy/com.beidou.testnet-verify.plist` | The verifier was deployed without `--once` under launchd `KeepAlive=true`, exceeding the authorized bounded campaign. | Live process/launchctl evidence and repeated manifests. | Remove daemon launchers, require `--once` in config, retain kill switch, add architecture regression. | FIXED IN CANDIDATE / REVALIDATION REQUIRED |
| P0 | `apps/testnet_verify/runtime.py::_write_manifest` | `real_testnet_write` scanned the entire historical trace store, so a no-write current run could claim a real write. | Regression test with historical ACK reproduced `true`. | Track attempted/ACK/UNKNOWN facts in the current runtime only; make write-enabled status kill-switch aware. | FIXED |
| P0 | DecisionTrace restart recovery | A later owned reduce-only close left the opening trace at `FILLED`, producing a false unresolved record after the account was flat. | Signed account/open-order reads plus matching CLOSED close trace. | Close only with one-way flat account fact and later direction/quantity-matched CLOSED reduce-only trace. | FIXED / LIVE READ-ONLY VERIFIED |
| P1 | `beidou_launcher.preflight.run_preflight` | The legacy launcher's G5 certificate gate was removed even though V4 only excludes G5 from the separate verifier. | Diff of `4155567d` against package PKG-08 freeze/isolation rule. | Restore legacy fail-closed gate; preserve verifier independence. | FIXED |
| P1 | coverage tests | One test depended on ignored local PostgreSQL config; another used non-yielding sleep doubles and appeared hung. | Clean checkout failure and per-test duration evidence. | Pin database URL in test and make clock doubles deterministic/yielding. | FIXED |

## Decision

`BLOCKED` pending clean full candidate verification and independent review.
Current account risk is reconciled to zero, but the KeepAlive incident
invalidates the earlier bounded-campaign characterization.
