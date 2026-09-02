# Acceptance report

Overall G8 decision: `BLOCKED`

Bounded sub-decisions:

- Frozen market-data admission: `PASS_WITH_CONDITIONS`
- Frozen current-Alpha arithmetic/timing baseline: `PASS_WITH_CONDITIONS`
- Additive stateful Alpha/OOS contract slice: `PASS_WITH_CONDITIONS / HISTORICAL_INDEPENDENT_FAIL_REMEDIATED_LOCALLY; POST_REMEDIATION_INDEPENDENT_REVERIFY_UNAVAILABLE`
- User-level outcome "improved overall return": `BLOCKED / NOT_ACHIEVED`
- Paper/Testnet: `HOLD`; Mainnet: `PROHIBITED`

## Scope and authority

- Repository: `/Users/maguannan/beidou`, branch `codex/v4-incident-remediation`, base HEAD `818518e8d6a839d014689f30ad66f44d794e4b60`.
- Authorized external action: bounded unauthenticated public market-data reads for Binance USD-M BTCUSDT, ETHUSDT, BNBUSDT, and SOLUSDT at 1h over `[2026-08-01T00:00:00Z, 2026-08-31T00:00:00Z)`.
- Not authorized and not performed: account/order interfaces, Testnet/Mainnet writes, deployment, restart, external messages, commit, push, or money movement.
- Acceptance covers the frozen dataset, descriptive fixed-policy baseline, and locally verified additive Alpha/OOS contracts. It does not accept a factor candidate, parameter/return change, real OOS result, deployment, trading readiness, or profitability claim.

## Requirement decisions

| Requirement | Acceptance evidence | Result |
|---|---|---|
| ARO-DATA-001..006 | Earlier schema-v2 provenance, integrity, factor-mining, and E0-boundary implementation evidence; current acquisition regression `90 passed` | PASS for the local data boundary |
| ARO-DATA-007 | Isolated artifact paths, scoped status, no trading/runtime action | PASS_WITH_CONDITIONS because the repository worktree remains intentionally dirty with protected V4 work |
| ARO-DATA-008 | 120/120 official checksums; four symbols x 720 rows; archive/API raw-field mismatch `0`; maximum close deviation `0 bps` | PASS_WITH_CONDITIONS because both paths are Binance-operated, not institutionally independent |
| ARO-ALPHA-001 | No Alpha parameter/candidate was selected before data admission | PASS as a safety invariant only |
| ARO-ALPHA-002 | Primary report byte-stable; zero lookahead/same-bar; no-context recomputation matches all portfolio and per-symbol metrics within `1e-12` | PASS_WITH_CONDITIONS / DIAGNOSTIC_ONLY |
| ARO-ALPHA-003 | Retrospective PIT/feature-lineage reconstruction and a frozen Metric Owner policy are recorded, but contemporaneous source custody, a current candidate-scoped decision package, real bound position/cost facts, fill-calibrated costs, candidate-bound impact/capacity, and a real pre-sealed unseen evaluation are absent | BLOCKED / DO_NOT_PROMOTE |
| ARO-ALPHA-004..008 | Additive stateful snapshots/result, explicit `NO_ACTION` preservation, provider fail-closed validation, canonical bindings, exact cost arithmetic, and legacy diagnostic labelling; post-remediation ARO-06 `81 passed`, current full repository `4823 passed` | LOCAL_PASS_WITH_CONDITIONS; historical independent P1/P2 `FAIL` is preserved and remediated locally; no independent post-remediation reverify was available |
| ARO-OOS-001,003 | Governed preseal-to-promotion bindings, store-authority-only v2 construction, v1/v2 isolation, shared OS-process lock, durable audit, crash/partial-state denial; post-remediation ARO-06 `81 passed` | LOCAL_PASS_WITH_CONDITIONS; historical independent P1 authority bypass is preserved and remediated locally; no independent post-remediation reverify was available |
| ARO-OOS-002 | Future-window custody and policy contracts are recorded; the active assessment is `DRAFT_BLOCKED` solely for `MISSING_CANDIDATE_FREEZE`; no real seal or read exists; v1 is research-only/not-v2-eligible | CONTRACT_PASS / ACTIVE_INSTANCE_BLOCKED |

## Independent baseline-verifier boundary

- A separate no-context verifier received only the raw requirements, frozen parquet/manifests/index, and the published `OfflineAlphaApp` interface.
- Before its first result was persisted it did not read `baseline-report.json`, `run_frozen_alpha_baseline.py`, or `test_frozen_alpha_baseline.py` and was not given expected metrics.
- After the first result was sealed, it read the primary artifacts only to reconcile differences.
- Implementation: `docs/optimization/runs/2026-08-31-alpha-return-optimization/tools/run_independent_frozen_alpha_recompute.py`, SHA-256 `f1449d0a8c2fe9bb8558b141dbe659f62b9bf3e6304924f1b16ee80533548200`.
- Final report: `artifacts/analysis/alpha-return-2026-08-01_2026-08-31-v1/independent-recompute.json`, output SHA-256 `ce699de5be8d81ea53253ea9c4f31caf285a65aec38d9cc1ab10ee08fbbac65c`, stable calculation-payload SHA-256 `0a677aa21194c59d696ed588ef697a1353d919538744d7ddad49477148a35589`.

## Reconciliation

| Metric | Primary | Independent | Result |
|---|---:|---:|---|
| Evaluable bars | 669 | 669 | MATCH |
| Lookahead / same-bar | 0 / 0 | 0 / 0 | MATCH |
| Portfolio gross compound return | 0.044007217651851116 | 0.044007217651851116 | MATCH |
| Portfolio diagnostic-net compound return | 0.0240062190462067 | 0.0240062190462067 | MATCH |
| Gross zero-cost equal-weight benchmark | 0.26909192450296726 | 0.26909192450296726 | MATCH |
| Annualized hourly net Sharpe | 2.435898613079093 | 2.4358986130790936 | MATCH within `1e-12` |
| Net maximum drawdown | -0.03304729145251872 | -0.03304729145251872 | MATCH |
| Portfolio turnover units | 30.175100989245593 | 30.175100989245603 | MATCH within `1e-12` |

All checked primary and per-symbol metrics match within absolute tolerance `1e-12`.

## ARO-06 independent-verifier history

- A separate no-author-context behavior verifier was delegated for the additive stateful Alpha/OOS contract slice. Its pre-remediation run produced a reviewable `FAIL` with P1/P2 counterexamples, recorded in the append-only section below.
- The implementation owner repaired those counterexamples and reran the critical tests and static/full gates with fresh evidence. Those checks remain author-run and are not relabelled independent.
- Two additional independent-agent attempts after the repair ended at the service usage limit before producing a verdict. The current bounded disposition is therefore `LOCAL_PASS_WITH_CONDITIONS / POST_REMEDIATION_INDEPENDENT_REVERIFY_UNAVAILABLE`; the preserved historical `FAIL` is not erased. This prevents an unconditional technical acceptance and does not change the already stricter economic `BLOCKED` decision.

## Preserved disagreement and resolution

The first independent result correctly reproduced gross return and the benchmark but reported turnover `15.0875504946` and net return `3.331784192%`. It applied `0.5 * L1` to risky-asset weights only. Those weights represent four independent partially invested capital sleeves, so the extra factor omitted each sleeve's cash/margin leg and halved traded notional. The first output SHA-256 `e745a9351cd00c993b5ab10476769eb5fc5216479de0500050212a3fa7001fba` and mismatch remain embedded in the final report. After changing only the independent verifier to `sum(abs(delta risky weight))`, all metrics reconciled; neither product code nor the primary baseline was changed.

## Sensitivity and limitations

- The primary finite-window result leaves the final exposure open. Charging a hypothetical 6 bps terminal unwind changes net return from `2.400621905%` to `2.395268691%`.
- Strategy and benchmark both exclude close-to-next-open gaps by using the same next-bar open-to-close substrate. A continuous-mark sensitivity changes strategy net return to `2.433377964%` and benchmark return to `27.182773255%`.
- The 6 bps turnover and 0.125 bps/hour adverse funding schedule is explicit but not calibrated to fills. Transaction-cost arithmetic sum is `1.810506059%`; funding-cost arithmetic sum is `0.123974684%`. Impact, latency, and capacity remain `NOT_EVALUATED`.
- Archive and API paths are independently processed but both originate from Binance. This limits source independence.
- The August range was observed before an immutable OOS seal. It cannot be reused as one-shot OOS, and the short single-regime period cannot establish robustness.
- The gross zero-cost equal-weight comparator is an opportunity-cost reference, not an executable costed benchmark.
- Additive stateful v2 now defines `NO_ACTION` preservation and explicit cost-source identity. No real caller, calibrated cost artifact, candidate, or OOS run uses those contracts, and legacy `evaluate()` remains diagnostic-only.

## Fresh evidence

- Data acquisition: `artifacts/evidence/ALPHA-DATA-002-ACQUIRE-FROZEN-MONTH/20260901T030849318588Z.manifest.json`.
- Official checksums: `artifacts/evidence/ALPHA-DATA-002-INDEPENDENT-CHECKSUMS/20260901T031150027337Z.manifest.json`.
- Offline data verification: `artifacts/evidence/ALPHA-DATA-002-INDEPENDENT-VERIFY2/20260901T031250574100Z.manifest.json`.
- Data regression: `artifacts/evidence/ALPHA-DATA-002-ACQUISITION-REGRESSION/20260901T031250574108Z.manifest.json` (`90 passed`).
- Primary baseline: `artifacts/evidence/ALPHA-BASELINE-001-RUN2/20260901T033422960179Z.manifest.json`; focused `3 passed`, impacted `33 passed`, Ruff and compileall PASS.
- Independent recompute: `artifacts/evidence/ALPHA-BASELINE-002-INDEPENDENT-RECOMPUTE/20260901T034752966980Z.manifest.json`.
- Independent static/targeted checks: `ALPHA-BASELINE-002-RUFF`, `ALPHA-BASELINE-002-PYCOMPILE`, and `ALPHA-BASELINE-002-TARGETED` (`6 passed`).
- Lifecycle closure: `ALPHA-CLOSURE-001-STATE-VALIDATION`, `ALPHA-CLOSURE-001-DOC-DIFF-CHECK`, `ALPHA-CLOSURE-001-ARTIFACT-HASHES`, and `ALPHA-CLOSURE-001-SCOPED-STATUS` all PASS.
- ARO-06 review RED/GREEN: `ALPHA-CONTRACT-001-REVIEW-RED-HARDENING` (`16 failed, 59 passed`) -> `ALPHA-CONTRACT-001-REVIEW-GREEN-HARDENING` (`75 passed`).
- ARO-06 final local static/impact: Ruff PASS, strict mypy PASS, compileall PASS, scoped diff-check PASS, and `ALPHA-CONTRACT-001-FINAL-IMPACTED-REGRESSION` (`238 passed`).
- Full repository RED/GREEN: `ALPHA-CONTRACT-001-FINAL-FULL-REGRESSION` (`4 failed, 4814 passed`) -> exact registry repair -> `ALPHA-CONTRACT-001-FINAL-FULL-REGRESSION-R2` (`4818 passed, 1 warning`).
- Registry closure: primary and independent scanners both `PASS / issues=[]`; SHA-256 `d720b2568f6a078554d9398549f886e858ee26dfb280128768c8ee2353481e22`.
- Governance refresh: Metric Owner, future-window contract, cost/impact/capacity and PIT/feature-lineage reports verified under `ARO-GOV-*`, `ARO-COST-*`, and `ARO-PIT-*`; all remain scoped/conditional and do not authorize candidate search.
- Post-remediation ARO-06 local reverify: `ARO-06-LOCAL-REVERIFY` (`81 passed`), research-evidence regression `ARO-RESEARCH-EVIDENCE-REGRESSION` (`43 passed`), current full repository `ARO-FULL-REGRESSION-R3` (`4823 passed`), registry negative checks `ARO-REGISTRY-PRIMARY-NEGATIVE-R3` and `ARO-REGISTRY-INDEPENDENT-NEGATIVE-R3` both `PASS / issues=[]`; current registry SHA-256 `75858e1576253466021f826f80d1169839a99093d218d2a80cdaa353741dd4e9`.

## Acceptance disposition

The frozen dataset and current-policy arithmetic are reviewable and reproducible, and the post-remediation additive Alpha/OOS contracts pass fresh local verification, so those bounded components are accepted with the conditions above. The historical independent ARO-06 `FAIL` is preserved and repaired locally; independent post-remediation re-verification remains unavailable. G8 remains `BLOCKED`: no return-improving change was implemented, no valid unseen comparison exists, the shipped Alpha materially lags the gross rising-market comparator on the observed range, and required factor/backtest governance and economic evidence are missing.

## ARO-06 independent behavior verification

Verifier independence: `aro06-independent` did not implement this slice. It derived and persisted its first command plan from `01-requirements.md`, repository instructions, the authority/active-instance safety fields, and the independent-verification contract before reading the implementation, debugging, code-review, acceptance, or final-summary logs or any existing `ALPHA-CONTRACT-001-*` stdout/stderr. Plan evidence: `artifacts/evidence/ALPHA-CONTRACT-002-INDEPENDENT-PLAN-V1/20260901T152520550173Z.manifest.json` and `artifacts/evidence/ALPHA-CONTRACT-002-INDEPENDENT-PLAN-V2/20260901T152623170935Z.manifest.json`.

Fresh environment: branch `codex/v4-incident-remediation`, HEAD `818518e8d6a839d014689f30ad66f44d794e4b60`, with the previously disclosed dirty shared checkout. Authority remained local tests, read-only source inspection, independent evidence under `artifacts/evidence/`, and this append-only report update. No candidate search, parameter optimization, external or OOS data read, network/account/order access, Testnet/Mainnet write, deployment, restart, commit, push, or money operation was performed. Core tests created only synthetic seal/audit fixtures under pytest temporary directories. A repository scan found no `oos-seal.json`, `oos-governed-seal-v2.json`, or `oos-access-audit.jsonl` file.

| Requirement | Independent observation | Result |
|---|---|---|
| ARO-ALPHA-004..007 | Snapshot type/scope/time/value/source validation, `NO_ACTION` preservation, BUY/SELL raw-score semantics, canonical bindings, pure-market feature hash, and legacy machine label were reproduced by source review and the fresh core/compatibility portfolios. | `PASS` within the additive local-contract scope |
| ARO-ALPHA-008 | Scope/time/direction/range/source and material arithmetic mismatches fail in the existing portfolio, but a provider arithmetic error of about `4.999993e-13` is accepted because `AlphaForecast.cost_verifiable` uses `abs_tol=1e-12`. The v2 call returned a target instead of `FORECAST_COST_ARITHMETIC_INVALID`, contrary to the requirement's exact-arithmetic wording. | `FAIL` (`P2`) |
| ARO-OOS-001 | Store-mediated tests reproduce the intended preseal -> identity -> seal -> receipt -> promotion binding, but the exported `GovernedOOSSealV2.build`, `GovernedOOSAccessReceiptV2.build`, and `GovernedPromotionDecisionV2.build` factories can assemble `PROMOTABLE_V2 / eligible_for_v2_promotion=true` from a ready synthetic preseal, an identity deliberately not bound to it, and unverified legacy value objects. No persisted/reloaded seal was required. | `FAIL` (`P1`) |
| ARO-OOS-002 | Direct v1 objects remain labelled `LEGACY_RESEARCH_ONLY / false`, and the active instance remains draft by inspection; however the public v2 factories permit those unverified legacy seal/receipt values to be wrapped into an eligible v2 decision, so machine-visible v1/v2 isolation is bypassable. | `FAIL` (`P1`) |
| ARO-OOS-003 | Fresh OS-process tests proved exactly one store-issued receipt, dead-owner lock release, pre-append retry, post-commit denial, and malformed partial-audit blocking. However `GovernedOOSAccessReceiptV2.build` can issue a syntactically valid v2 receipt from an in-memory legacy receipt without a committed audit, and that receipt can feed the public eligible-decision factory. The receipt/promotion authority boundary therefore does not enforce the durable one-time path. | `FAIL` (`P1`) |

Fresh command evidence:

- Core Alpha/OOS behavior: `artifacts/evidence/ALPHA-CONTRACT-002-INDEPENDENT-CORE-PYTEST/20260901T153107823088Z.manifest.json` — `78 passed`.
- Legacy compatibility: `artifacts/evidence/ALPHA-CONTRACT-002-INDEPENDENT-LEGACY-COMPAT-PYTEST/20260901T153449007019Z.manifest.json` — `33 passed`.
- Static checks: `artifacts/evidence/ALPHA-CONTRACT-002-INDEPENDENT-RUFF/20260901T153523841637Z.manifest.json`, `artifacts/evidence/ALPHA-CONTRACT-002-INDEPENDENT-MYPY/20260901T153523844602Z.manifest.json`, and `artifacts/evidence/ALPHA-CONTRACT-002-INDEPENDENT-COMPILEALL/20260901T153523848112Z.manifest.json` — PASS.
- No active seal/audit filename found in the repository: `artifacts/evidence/ALPHA-CONTRACT-002-INDEPENDENT-REAL-SEAL-FILE-SCAN/20260901T153523853100Z.manifest.json` — `[]`.
- Promotion-bypass counterexample, bound to the current checkout with `PYTHONPATH=.`: `artifacts/evidence/ALPHA-CONTRACT-002-INDEPENDENT-COUNTEREXAMPLE-PROMOTION-R2/20260901T153900061221Z.manifest.json` — expected negative assertion failed after producing `eligible_for_v2_promotion=true` without store verification or audit.
- Exact-arithmetic counterexample, bound to the current checkout with `PYTHONPATH=.`: `artifacts/evidence/ALPHA-CONTRACT-002-INDEPENDENT-COUNTEREXAMPLE-ALPHA-ARITHMETIC-R2/20260901T153900061166Z.manifest.json` — expected negative assertion failed after v2 accepted the non-exact arithmetic. The earlier non-R2 Alpha harness failure loaded a non-checkout `apps.alpha_app`; it is preserved but is not product evidence.

Counterexample significance: the positive 78-test result contains no direct public-factory bypass case, and its per-field mutation checks begin from store-issued objects. The new pure-memory counterexample demonstrates a false-positive eligibility path rather than mutating product code or tests. It created no seal file and read no OOS data.

Disagreement with the author conclusion: the prior review states that no unresolved local P0/P1 remains and assigns `LOCAL_PASS_WITH_CONDITIONS`. This verifier disagrees because exported public factories allow a v2-eligible promotion object without preseal-identity validation, persisted governed/legacy seal verification, or durable one-time audit. The earlier author-run PASS evidence and full-suite result are not relabelled independent and do not cover this counterexample.

Limitations: this verifier ran 111 focused/compatibility tests, not the historical full repository suite; the checkout is intentionally dirty and conclusions bind to the inspected working-tree contents plus the fresh evidence timestamps, not to a clean commit. Repository filename scanning cannot prove the absence of seal/audit state outside the authorized workspace. No real custody facts, keys, OOS instance, OOS data, or runtime environment were supplied or accessed.

P0: none found. P1: public v2 seal/receipt/promotion factories bypass the store-enforced lifecycle and permit a false eligible marker. P2: the provider cost-arithmetic check accepts a nonzero mismatch inside its absolute tolerance despite the exact-arithmetic requirement.

Decision: `FAIL` for independent ARO-06 behavior acceptance. Product code and tests were not modified; the failures are reported and preserved without repair. Existing economic `BLOCKED`, Paper/Testnet `HOLD`, and Mainnet `PROHIBITED` boundaries remain unchanged.
