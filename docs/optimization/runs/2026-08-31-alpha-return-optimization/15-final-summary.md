# Final summary

## Outcome

The authorized August public dataset is acquired and passes the implemented market-data admission boundary. The already-shipped `OfflineAlphaApp` now has a deterministic, causal diagnostic baseline that a no-context second implementation reproduced within `1e-12`.

The user-level goal "improve overall return" is not achieved. No Alpha forecast policy, parameter, or return candidate was changed. An additive stateful v2 interface and governed OOS v2 contract were implemented locally, but no real position/cost facts, candidate, seal, or OOS read used them. The observed August range cannot be treated as one-shot OOS, and the shipped Alpha materially lagged the gross rising-market comparator. Current disposition is `DIAGNOSTIC_ONLY / DO_NOT_PROMOTE`; Paper and Testnet remain `HOLD`; Mainnet remains `PROHIBITED`.

## Frozen data result

- Binance USD-M BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT; 1h; UTC; `[2026-08-01T00:00:00Z, 2026-08-31T00:00:00Z)`.
- 720 closed bars per symbol; 120/120 official daily checksums passed.
- Archive/API comparison matched all 720 rows and raw fields per symbol; maximum close deviation `0 bps`.
- Dataset assessment: `PASS`; market-data domain gate: `PASS_WITH_CONDITIONS` because both paths are Binance-operated and broader symbol-lifecycle/historical-live parity are outside scope.
- Complete E0 remains `NOT_EVALUATED`: an August PIT/feature-lineage reconstruction now exists, but the source vintage was retrieved after historical decisions and is explicitly not contemporaneously verifiable.

## Current Alpha diagnostic

| Metric | Result |
|---|---:|
| Evaluable hours / signals | 669 / 2,676 |
| Lookahead / same-bar executions | 0 / 0 |
| Gross compound return | +4.400721765% |
| Diagnostic-net compound return | +2.400621905% |
| Annualized hourly net Sharpe | 2.435898613 |
| Net maximum drawdown | -3.304729145% |
| Portfolio turnover | 30.175100989 |
| Gross zero-cost equal-weight comparator | +26.909192450% |
| Active net minus comparator | -24.508570546 percentage points |

Costs are an explicit diagnostic schedule: 6 bps per unit turnover plus 0.125 bps of adverse funding per exposure-hour. They are not fill-calibrated. Impact, capacity, latency, and terminal liquidation are not part of the primary result.

## Independent baseline verification

- A separate no-context implementation used frozen parquet/manifests/index plus the public `OfflineAlphaApp` interface and did not read the primary runner, tests, or report before persisting its first result.
- The first result exposed a turnover-convention disagreement: an extra `0.5 * L1` halved traded notional. That failed comparison is preserved at SHA-256 `e745a9351cd00c993b5ab10476769eb5fc5216479de0500050212a3fa7001fba`.
- After localizing the convention, the verifier used actual four-sleeve traded notional and matched every primary portfolio/per-symbol metric within `1e-12`; product and primary-baseline code were unchanged.
- Final independent report status: `PASS_WITH_CONDITIONS`; output SHA-256 `ce699de5be8d81ea53253ea9c4f31caf285a65aec38d9cc1ab10ee08fbbac65c`; stable calculation payload SHA-256 `0a677aa21194c59d696ed588ef697a1353d919538744d7ddad49477148a35589`.
- Terminal-unwind sensitivity gives net `+2.395268691%`; continuous-mark sensitivity gives strategy net `+2.433377964%` and comparator `+27.182773255%`.

## Additive Alpha/OOS contract result

- `OfflineAlphaApp.evaluate_stateful()` now requires strategy/instrument/venue/time-bound position and cost snapshots. `NO_ACTION` preserves the verified current sleeve weight; BUY/SELL use the provider raw score.
- Provider type, scope, time, sign/range, cost values/source, and arithmetic are validated fail-closed. Snapshot and result digests are canonical and recomputable; legacy `evaluate()` remains compatible and labelled `LEGACY_DIAGNOSTIC_ONLY`.
- Governed OOS v2 now binds custody/candidate preseal -> run identity -> seal -> access receipt -> promotion; v1 stays `LEGACY_RESEARCH_ONLY / not v2 eligible`. Shared `fcntl.flock`, durable append/fsync, race tests, and crash/partial-state cases pass locally.
- The active preseal is still `DRAFT_BLOCKED`: the future custody and policy contracts are recorded, but no candidate freeze, real governed seal, or OOS data access exists. Threshold/candidate binding and contemporaneous source custody remain unverified.
- Local technical status after remediation is `PASS_WITH_CONDITIONS`: ARO-06 authority/arithmetic reverify `81 passed`, research-evidence regression `43 passed`, current full repository `4823 passed`, registry primary/independent negative checks PASS, and scoped mypy/compileall/diff-check PASS. The historical no-author-context ARO-06 verifier produced a preserved P1/P2 `FAIL`; those defects were repaired locally, but no fresh independent post-remediation verdict was available after service usage limits.

## Governance evidence refresh

- Metric Owner policy: verified owner `yhwhhwhy001@gmail.com`, semantic digest `96dbcd31d32ffb53c532a24634c2c85082e4f7d045fd1da24d8521d9495d08b9`, source SHA-256 `2b7f5bf287432b6051e9252f525023b0c23dfa17c2c26aa4e95ab18002e61f70`. The policy is frozen for its original task/scope and explicitly does not grant current candidate, deployment, account, or trading authorization.
- Future unknown window: machine-custodied October 2026 contract from `2026-10-01T00:00:00Z` to `2026-11-01T00:00:00Z`, first read not before `2026-11-02T00:00:00Z`; status `DRAFT_BLOCKED` solely for `MISSING_CANDIDATE_FREEZE`; `candidate_search_started=false`, `oos_data_accessed=false`, and no governed v2 seal exists.
- Cost/impact/capacity: 120/120 official bookDepth checksums, 86,400 depth samples per symbol, 90 funding observations, and 720 hourly-volume rows. Fee/fill/latency calibration is still absent and strategy capacity remains `NOT_VERIFIABLE_NO_CANDIDATE_NET_RETURN`.
- August PIT/feature lineage: 10 roles, 100% contract coverage, 2,676 feature records paired with 2,676 labels, zero feature-after-decision and zero label-before-decision violations; source vintage remains `NOT_VERIFIABLE_RETROSPECTIVE_RECONSTRUCTION`.

## Fresh evidence

- Data acquisition/checksums/verification: `ALPHA-DATA-002-ACQUIRE-FROZEN-MONTH`, `ALPHA-DATA-002-INDEPENDENT-CHECKSUMS`, `ALPHA-DATA-002-INDEPENDENT-VERIFY2`.
- Data regression/static: `ALPHA-DATA-002-ACQUISITION-REGRESSION` (`90 passed`), `ALPHA-DATA-002-TOOLS-RUFF2`, `ALPHA-DATA-002-TOOLS-COMPILE2`.
- Primary baseline: `ALPHA-BASELINE-001-GREEN2` (`3 passed`), `ALPHA-BASELINE-001-RUN2`, `ALPHA-BASELINE-001-IMPACT-REGRESSION` (`33 passed`), Ruff and compileall PASS.
- Independent baseline: `ALPHA-BASELINE-002-INDEPENDENT-RECOMPUTE`, Ruff PASS, py_compile PASS, targeted `6 passed`.
- Lifecycle closure: state/evidence/hash validation PASS, document diff-check PASS, artifact-hash capture PASS, and scoped status captured under `ALPHA-CLOSURE-001-*`.
- ARO-06 contract review: `ALPHA-CONTRACT-001-REVIEW-RED-HARDENING` (`16 failed, 59 passed`) -> `ALPHA-CONTRACT-001-REVIEW-GREEN-HARDENING` (`75 passed`).
- ARO-06 final local verification: `ALPHA-CONTRACT-001-FINAL-IMPACTED-REGRESSION` (`238 passed`), final Ruff/mypy/compileall/diff-check PASS, registry primary/independent PASS, and `ALPHA-CONTRACT-001-FINAL-FULL-REGRESSION-R2` (`4818 passed, 1 warning`).
- Preserved full-suite RED: `ALPHA-CONTRACT-001-FINAL-FULL-REGRESSION` (`4 failed, 4814 passed`) before exact governed-source registry repair.
- Post-remediation ARO-06 local reverify: `ARO-06-LOCAL-REVERIFY` (`81 passed, 1 warning`); research evidence `ARO-RESEARCH-EVIDENCE-REGRESSION` (`43 passed`); current full repository `ARO-FULL-REGRESSION-R3` (`4823 passed`).
- Current full-tree Ruff remains conditional: the fresh run reports only the unrelated pre-existing S310 at `beidou_certification/g5_scenarios/restart/process_restart.py:83`; scoped touched-file checks remain recorded separately as PASS.
- Resume-time closure rerun at `2026-09-02T05:45:11Z` reproduced ARO-06 `81 passed`, research evidence `43 passed`, targeted ARO Ruff PASS, YAML/digest/evidence-reference checks PASS, and full `git diff --check` PASS; no code or external state changed in that rerun.
- Current registry closure: primary and independent negative checks both `PASS / issues=[]`; registry SHA-256 `75858e1576253466021f826f80d1169839a99093d218d2a80cdaa353741dd4e9`, governance digest `e2a9b9f5932348d0350e5afaa61487415f6a373d3a432ddd8a4c8ffd723057c9`.
- Acceptance details and exact manifest paths: `12-acceptance-report.md`.

## Gate decisions

- ARO-DATA-001..006: PASS for the local data-boundary implementation.
- ARO-DATA-007: PASS_WITH_CONDITIONS; existing V4 work was preserved and the tree remains intentionally dirty.
- ARO-DATA-008: PASS_WITH_CONDITIONS for this frozen dataset.
- ARO-ALPHA-001: PASS as a fail-closed selection invariant only.
- ARO-ALPHA-002: PASS_WITH_CONDITIONS for causal arithmetic/reproducibility only.
- ARO-ALPHA-003: BLOCKED; no promotion or improvement claim.
- ARO-ALPHA-004..008: LOCAL_PASS_WITH_CONDITIONS; historical independent P1/P2 failure preserved and remediated locally; independent post-remediation reverify unavailable.
- ARO-OOS-001,003: LOCAL_PASS_WITH_CONDITIONS; historical independent P1 authority failure preserved and remediated locally; independent post-remediation reverify unavailable.
- ARO-OOS-002: contract PASS, active instance `DRAFT_BLOCKED`; no real seal/read.
- G8: `BLOCKED`; G9 remains not applicable; G10 is not authorized.
- Factor-lifecycle gate: `BLOCKED`; backtest-validity gate: `BLOCKED`.

## Blockers to Alpha modification or promotion

1. The Metric Owner policy is frozen and verified only for its original task/scope; no current candidate hypothesis/freeze or strategy-summary decision package is authorized.
2. PIT/feature-lineage records are a retrospective reconstruction; contemporaneous source custody is not proven and complete E0 is not evaluated.
3. Stateful v2 exists only as an additive local contract; no authorized runtime/candidate supplies verified current-position and fill-calibrated cost snapshots, and legacy evaluation remains diagnostic-only.
4. The future-window custody contract exists, but no candidate freeze or real preseal/OOS read exists; the historical independent ARO-06 P1/P2 failure was repaired locally and independent post-remediation confirmation is unavailable.
5. No WFO/CPCV, sensitivity, multiple-testing, multi-regime, correlation/incrementality, fill calibration, or candidate-bound capacity evidence exists for a return candidate.
6. Repository master instructions prohibit premature strategy/return-parameter optimization before prerequisite phase acceptance.
7. The skill pack's referenced CIOS safety contract is absent; the stricter repository master/protocol remains the fallback.

## Authority and next action

Only authorized public data reads plus local reversible tools, tests, reports, and evidence were performed. No account/order call, Testnet/Mainnet write, deployment, restart, external message, commit, push, or money movement occurred.

The next local contract action would be an independent post-remediation ARO-06 behavior verdict when a verifier is available. Before any Alpha candidate or parameter search, keep the candidate freeze and future-window first-read gate intact, bind complete contemporaneous PIT/feature lineage, obtain calibrated cost/impact/capacity evidence and the required current policy package, satisfy repository phase prerequisites, and obtain new explicit authorization. The observed August range and all current return figures must remain diagnostic-only.
