# Context

- Run ID: `2026-08-31-alpha-return-optimization`
- Repository/version: `/Users/maguannan/beidou`, branch `codex/v4-incident-remediation`, HEAD `818518e8d6a839d014689f30ad66f44d794e4b60`.
- User goal: optimize the alpha module and improve overall return.
- Current status: `DATASET_ADMITTED_BASELINE_DIAGNOSTIC_ALPHA_PROMOTION_BLOCKED`.
- Current decision: `DO_NOT_PROMOTE`. The authorized frozen market dataset passes the implemented market-data admission gate and the already-shipped Alpha has a reproducible diagnostic baseline, but complete E0, sealed OOS, cost calibration, impact/capacity, and a valid improvement experiment remain absent.
- Governing priority: capital safety > fact-chain correctness > data/order consistency > net risk-adjusted return.
- Authority: local read-only analysis plus reversible source, test, lifecycle-evidence, and dataset-artifact changes. On 2026-09-01 the user explicitly authorized bounded public market-data reads for the most recent month. No commit, push, deployment, restart, account/order request, Testnet/Mainnet write, external message, or money movement is authorized.
- Protected work: the checkout already contains extensive V4 incident-remediation changes and Testnet evidence. This run is restricted to non-overlapping research/factor files and its own evidence paths.
- Mainnet: `PROHIBITED`. Testnet writes are not authorized by this run.
- Applicable repository instructions: `00_EXECUTION_MASTER.md`, `01_AGENT_OPERATING_PROTOCOL.md`, `PACKAGE_README.md`, and `README.md`; no `AGENTS.md` was found.
- Applicable lifecycle specialists already selected: complete lifecycle, onboarding/constraints, deep analysis, market-data integrity, backtest validity, factor lifecycle, TDD, and completion verification.
- Template limitation: the skills reference `docs/optimization/runs/_shared/templates`, but that directory is absent. This run follows the closest existing run-artifact format and does not claim template conformance.
- Baseline evidence: `artifacts/evidence/ALPHA-DATA-001-BASELINE/20260831T040700022760Z.manifest.json` (`54 passed`, Python 3.14.7, pytest 8.4.2).
- Final local implementation evidence: post-remediation ARO-06 authority/arithmetic regression `81 passed`, research-evidence regression `43 passed`, current full repository `4823 passed`; primary and independent registry negative checks, scoped mypy, compileall, and diff-check passed. Full-tree Ruff retains one unrelated pre-existing S310. Exact manifests are indexed in `15-final-summary.md`.
- Current registry evidence: SHA-256 `75858e1576253466021f826f80d1169839a99093d218d2a80cdaa353741dd4e9`; primary and independently implemented scanners both returned `PASS / issues=[]` at `ARO-REGISTRY-PRIMARY-NEGATIVE-R3` and `ARO-REGISTRY-INDEPENDENT-NEGATIVE-R3`.
- Acceptance boundary: the public dataset has checksum and second-path verification, and a no-context second implementation reproduced the diagnostic baseline within `1e-12`. Metric Owner, retrospective PIT/feature-lineage, cost/impact/capacity, and a future-window custody contract are now recorded, but candidate freeze, contemporaneous source custody, and independent post-remediation ARO-06 acceptance remain unavailable. These checks cannot accept the user-level return objective; G8 remains `BLOCKED`.
- Stop conditions: any attempt to use Demo/Testnet data as economic truth; provenance laundering across sources; failure to preserve existing changes; network trading/account calls; unavailable trustworthy historical data; or tests showing the proposed boundary can be bypassed.

## Why the legacy data was rejected

The local 1h parquet files are internally well formed, but their manifests contain only symbol, interval, row range, and content hash. They do not bind venue, endpoint, environment, retrieval time, or permitted use. The active Paper, Testnet, and safety-only configurations all use `https://demo-fapi.binance.com`.

Fresh local inspection also found economically implausible behavior: BTC has a `+97.27%` maximum hourly return and an opposite-sign large-return reversal; lag-1 hourly-return autocorrelation is approximately `-0.35` to `-0.43` across BTC/ETH/BNB/SOL. Optimizing against that legacy sample would reward data artifacts rather than alpha.

## Authorized first slice

1. Introduce a versioned market-data provenance contract.
2. Classify Demo/Testnet endpoints as `EXECUTION_ONLY`.
3. Require content verification, trustworthy public read-only provenance, cross-source evidence, and anomaly coverage before economic research.
4. Make the factor-mining local-store path consume the persisted manifest instead of recomputing a provenance-free hash.
5. Bind the E0 economic-truth gate to the verified market-data assessment.

This slice is implemented and locally verified. It establishes when research must stop; it does not itself demonstrate higher return.

## Authorized data-acquisition slice

- Venue: Binance USD-M perpetual public market data.
- Symbols: `BTCUSDT`, `ETHUSDT`, `BNBUSDT`, `SOLUSDT`.
- Interval: `1h`, UTC event time.
- Frozen range: `[2026-08-01T00:00:00Z, 2026-08-31T00:00:00Z)`; 30 complete UTC days, 720 expected bars per symbol.
- Primary source: exact Binance public USD-M archive ZIP path on `data.binance.vision`, including published checksum verification.
- Reconciliation source: exact unauthenticated public K-line API path `https://fapi.binance.com/fapi/v1/klines`, which the implemented contract treats as a distinct research source class.
- Output: a new isolated frozen dataset under `artifacts/datasets/`; the legacy `.beidou/data/klines` tree must not be overwritten.
- Stop conditions: missing archive/checksum, range drift, gaps, duplicates, malformed/unclean bars, API disagreement over 5 bps, future/unclosed bars, or any redirect/host outside the exact allowlist.

## Data-acquisition outcome

- The exact authorized range was acquired into `artifacts/datasets/alpha-return-2026-08-01_2026-08-31-v1/` without overwriting the legacy store.
- Each symbol contains 720 closed hourly bars. All 120 official daily ZIP checksums passed, and the archive/API comparison matched all 720 rows and raw fields per symbol with maximum close deviation `0 bps`.
- The result is `PASS` for the implemented market-data admission boundary. Both paths are Binance-operated, so this is source-path/class reconciliation rather than institutional independence.

## Frozen baseline outcome

- Only `apps.alpha_app.OfflineAlphaApp` and its published default policy were evaluated; no parameter search or candidate selection occurred.
- Signals use data through closed bar `t` and execute at the next bar open. The run recorded 2,676 signal rows, zero lookahead violations, and zero same-bar executions.
- On 669 evaluable hours, the equal-sleeve portfolio returned about `+4.40%` gross and `+2.40%` after the explicit diagnostic cost schedule, with about `-3.30%` maximum drawdown. The gross, zero-cost equal-weight price benchmark returned about `+26.91%` over the same open-to-close observations.
- These are descriptive in-sample diagnostics. The range was viewed before any OOS seal, only about 135 evaluable bars post-date the Alpha implementation commit, and no complete PIT/feature lineage, Metric Owner policy, fill-calibrated cost, impact, or capacity evidence exists.
- The frozen baseline continues to interpret legacy `AlphaTarget.target_weight` directly and is explicitly diagnostic-only. Additive v2 now specifies and tests `NO_ACTION -> preserve verified current position`, but no real caller/candidate supplies the required position and calibrated cost snapshots; threshold-based return changes remain prohibited without sealed unseen evidence and new authorization.
