# Test Strategy

## Baseline

- Command: targeted manifest, Kline store, backfill, factor CLI, and Economic Truth suites.
- Result: `54 passed`, one third-party deprecation warning.
- Evidence: `artifacts/evidence/ALPHA-DATA-001-BASELINE/20260831T040700022760Z.manifest.json`.

## RED contract

- Endpoint identity: exact trusted hosts only; Demo/Testnet/unknown/lookalike hosts are execution-only.
- Manifest: legacy/missing/unknown schema, invalid retrieval time, content/metadata tampering, and source-class spoofing fail closed.
- Data: duplicate/gapped/unclosed/non-finite/illegal OHLCV fails; large jump/reversal needs cross-source extrema coverage.
- Cross-source: explicit PASS, distinct trusted reference, extrema-and-random sampling, minimum sample count, bounded close deviation, timestamp, and evidence digest.
- Backfill: pre-existing legacy/mismatched provenance is rejected before data bytes change.
- Factor miner: local research cannot recompute away persisted provenance.
- Economic Truth: E0 requires a bound eligible market-data assessment.

## GREEN and regression order

`compile -> targeted unit -> impacted factor/economic suites -> Ruff -> mypy (scoped if full baseline is unavailable) -> git diff --check -> broader regression`

Completion cannot be claimed from the focused suite alone. Full-repository limitations and any pre-existing failures will be reported separately.

## Executed evidence

| Check | Result | Evidence |
|---|---|---|
| Pre-change focused baseline | `54 passed, 1 warning` | `artifacts/evidence/ALPHA-DATA-001-BASELINE/20260831T040700022760Z.manifest.json` |
| Adversarial review RED | `8 failed` as expected | `artifacts/evidence/ALPHA-DATA-001-RED-REVIEW-HARDEN/20260831T131827110445Z.manifest.json` |
| Adversarial review GREEN | `16 passed` | `artifacts/evidence/ALPHA-DATA-001-GREEN-REVIEW-HARDEN/20260831T132240943542Z.manifest.json` |
| Final impacted regression | `203 passed` | `artifacts/evidence/ALPHA-DATA-001-REGRESSION-FINAL2/20260831T174237507138Z.manifest.json` |
| Current registry recheck | `4 passed` | `artifacts/evidence/ALPHA-DATA-001-REGISTRY-RESUME-CHECK/20260831T180000588952Z.manifest.json` |
| Ruff | PASS | `artifacts/evidence/ALPHA-DATA-001-RUFF-FINAL3/20260831T174544276944Z.manifest.json` |
| Scoped strict mypy | PASS for 5 source files | `artifacts/evidence/ALPHA-DATA-001-MYPY-FINAL3/20260831T174544283115Z.manifest.json` |
| Compileall | PASS | `artifacts/evidence/ALPHA-DATA-001-COMPILEALL-FINAL3/20260831T174544299380Z.manifest.json` |
| Diff check | PASS | `artifacts/evidence/ALPHA-DATA-001-DIFF-CHECK-FINAL3/20260831T174544290504Z.manifest.json` |
| Historical full repository | `4740 passed, 1 warning` | `artifacts/evidence/ALPHA-DATA-001-FULL-FINAL2/20260831T174600077904Z.manifest.json` |
| Current local data admission (legacy store) | BTC/ETH/BNB/SOL all `FAIL` | `artifacts/evidence/ALPHA-DATA-001-CURRENT-DATA-GATE-FINAL2/20260831T175010931124Z.manifest.json` |

## Authorized frozen-data evidence

| Check | Result | Evidence |
|---|---|---|
| Exact-scope acquisition | 4 symbols x 720 closed 1h bars; dataset index `PASS` | `artifacts/evidence/ALPHA-DATA-002-ACQUIRE-FROZEN-MONTH/20260901T030849318588Z.manifest.json` |
| Official archive checksums | 120/120 `OK` | `artifacts/evidence/ALPHA-DATA-002-INDEPENDENT-CHECKSUMS/20260901T031150027337Z.manifest.json` |
| Offline second-path verification | 720/720 raw rows and fields per symbol; maximum close deviation `0 bps`; market-data admission `PASS` | `artifacts/evidence/ALPHA-DATA-002-INDEPENDENT-VERIFY2/20260901T031250574100Z.manifest.json` |
| Acquisition regression | `90 passed` | `artifacts/evidence/ALPHA-DATA-002-ACQUISITION-REGRESSION/20260901T031250574108Z.manifest.json` |
| Acquisition tools Ruff / compileall | PASS | `ALPHA-DATA-002-TOOLS-RUFF2`, `ALPHA-DATA-002-TOOLS-COMPILE2` |

The archive and API paths are independently processed source classes but share Binance as issuer/operator. This is not institutional independence.

## Governance evidence refresh

| Check | Result | Evidence |
|---|---|---|
| Metric Owner policy | `PASS_WITH_SCOPE_LIMIT`; `yhwhhwhy001@gmail.com`; approval and independent-review signatures verified; current candidate authorization remains `NOT_GRANTED` | `artifacts/evidence/ARO-GOV-INPUTS-VERIFY/20260902T041609160060Z.manifest.json`; `artifacts/analysis/alpha-return-2026-08-01_2026-08-31-v1/governance/inputs/metric-owner-verification.json` |
| Future unknown window | October 2026 contract sealed; first read not before `2026-11-02T00:00:00Z`; `DRAFT_BLOCKED` only for `MISSING_CANDIDATE_FREEZE`; no data accessed | `artifacts/evidence/ARO-GOV-INPUTS-001/20260902T041608817988Z.manifest.json`; `artifacts/analysis/alpha-return-2026-08-01_2026-08-31-v1/governance/inputs/governance-inputs-report.json` |
| Cost / impact / capacity | `PASS_WITH_CONDITIONS`; 120/120 official bookDepth checksums, 86,400 depth samples per symbol, 90 funding observations and 720 hourly-volume rows; fill fees/latency remain uncalibrated and strategy capacity is `NOT_VERIFIABLE_NO_CANDIDATE_NET_RETURN` | `artifacts/evidence/ARO-COST-CAPACITY-ACQUIRE/20260902T041701745350Z.manifest.json`; `artifacts/evidence/ARO-COST-CAPACITY-VERIFY/20260902T041757898168Z.manifest.json`; `artifacts/analysis/alpha-return-2026-08-01_2026-08-31-v1/governance/cost-capacity/calibration-report.json` |
| August PIT / feature lineage | `PASS_WITH_CONDITIONS`; 10 roles, 100% contract coverage, 2,676 causal feature/label pairs; contemporaneous source vintage remains unverified because the bytes were retrieved retrospectively | `artifacts/evidence/ARO-PIT-LINEAGE-CREATE/20260902T043208626293Z.manifest.json`; `artifacts/evidence/ARO-PIT-LINEAGE-VERIFY/20260902T043209609025Z.manifest.json`; `artifacts/analysis/alpha-return-2026-08-01_2026-08-31-v1/governance/pit-lineage/lineage-report.json` |

## Frozen Alpha diagnostic evidence

| Check | Result | Evidence |
|---|---|---|
| TDD RED | Missing baseline module failed collection as expected | `artifacts/evidence/ALPHA-BASELINE-001-RED/20260901T032334775832Z.manifest.json` |
| Focused GREEN2 | `3 passed` | `artifacts/evidence/ALPHA-BASELINE-001-GREEN2/20260901T033422953958Z.manifest.json` |
| Deterministic report rerun | PASS; report SHA-256 `1968c1ec...77f8f1` unchanged | `artifacts/evidence/ALPHA-BASELINE-001-RUN2/20260901T033422960179Z.manifest.json` |
| Impacted Alpha/data/E0 regression | `33 passed` | `artifacts/evidence/ALPHA-BASELINE-001-IMPACT-REGRESSION/20260901T033422963889Z.manifest.json` |
| Baseline tool Ruff / compileall | PASS | `artifacts/evidence/ALPHA-BASELINE-001-RUFF/20260901T033422967178Z.manifest.json`, `artifacts/evidence/ALPHA-BASELINE-001-COMPILEALL/20260901T033422956923Z.manifest.json` |
| Independent arithmetic/timing recomputation | `PASS_WITH_CONDITIONS`; all primary/per-symbol metrics match within `1e-12`; first disagreement preserved and reconciled | `artifacts/evidence/ALPHA-BASELINE-002-INDEPENDENT-RECOMPUTE/20260901T034752966980Z.manifest.json`, `12-acceptance-report.md` |
| Independent verifier Ruff / py_compile / targeted tests | PASS / PASS / `6 passed` | `ALPHA-BASELINE-002-RUFF`, `ALPHA-BASELINE-002-PYCOMPILE`, `ALPHA-BASELINE-002-TARGETED` |

## Claim boundary

- The suite verifies the local data-admission and promotion boundary, not the economic quality of current data.
- The full repository PASS is technical regression evidence, not independent acceptance or profitability evidence.
- The lone full-suite warning is an existing `datetime.utcnow()` deprecation outside this slice.
- The new frozen dataset passes the market-data sub-gate, but complete E0 remains `NOT_EVALUATED`: PIT/feature-lineage records exist as a retrospective reconstruction and their contemporaneous source vintage is not verifiable.
- The fixed-policy baseline tests only reproducibility and causal arithmetic. It does not establish factor robustness, fill-calibrated costs, WFO/CPCV, one-shot OOS, Paper, Testnet, or live return.

## Authorized stateful-contract RED plan

The next implementation must preserve the pre-change baseline at `artifacts/evidence/ALPHA-CONTRACT-001-PRECHANGE-BASELINE/20260901T092727580182Z.manifest.json` (`33 passed`). New tests are written and executed before product implementation.

| Risk / requirement | Test layer | Required RED observation | GREEN / regression evidence |
|---|---|---|---|
| `NO_ACTION` accidentally uses nonzero raw score | Unit contract with a deterministic injected forecast | Missing `evaluate_stateful`/types or incorrect target | Target equals current verified weight; audit semantic is `PRESERVE_VERIFIED_POSITION` |
| Directional side loses forecast intent | Unit contract, forced BUY/SELL | v2 path unavailable | Target equals exact raw score and semantic is `FORECAST_RAW_SCORE` |
| Unknown/stale/future position | Unit negative matrix | v2 path unavailable | Stable typed presence/future/expired codes; no result |
| Missing/stale/future/invalid cost source | Unit negative matrix | v2 path unavailable | Stable typed codes; no defaults; nonzero canonical SHA-256 required |
| Cross-scope fact replay | Unit negative matrix | v2 path unavailable | strategy/instrument/venue mismatch rejected |
| Forecast rewrites injected cost evidence | Unit provider-contract case | v2 path unavailable | exact cost values and source digest checked before result |
| Forecast rewrites scope/time/direction | Adversarial provider matrix | v2 path unavailable | exact strategy/instrument/venue/time, score range, and BUY/SELL sign enforced |
| Same cost source digest with mutated values | Canonical binding unit case | v2 path unavailable | cost snapshot binding digest changes; pure market feature hash does not |
| Unsupported side inference | Unit provider-contract case | v2 path unavailable | `UNSUPPORTED_FORECAST_SIDE` |
| Legacy diagnostic drift | Existing integration plus deterministic baseline runner | Existing baseline is green before edit | unchanged legacy tests and baseline report SHA-256 |
| Pre-seal claims without governance | Research unit contract | pre-seal readiness API unavailable | missing window/candidate/owner/metric policy stays `DRAFT_BLOCKED` |
| Candidate/window mutation | Research unit/property-style cases | pre-seal readiness API unavailable | digest changes or validation fails; chronology and identity binding enforced |
| Legacy bypass of v2 governance | v1/v2 promotion isolation | v1 remains callable | v1 decision is machine-labelled research-only/not-v2-eligible; every v2 stage refuses missing/draft preseal |
| Preseal downstream mapping | Per-field identity/seal/receipt/promotion mutation matrix | v2 path unavailable | each mapping fails independently with a stable reason |
| Concurrent OOS access | Synchronized multi-process integration | current read-then-append can race | exactly one receipt; all others denied before unlock |
| Access fault recovery | Process/fault-injection integration | current behavior lacks explicit claim semantics | dead lock owner releases; pre-append retry safe; post-commit/partial audit fail closed with evidence preserved |
| Existing OOS regression | Existing research suite | `24 passed` inside pre-change baseline | existing seal/audit/promotion cases remain green |

Execution order after revision-2 G2 approval: `RED stateful/provider + preseal/bypass/mutation + access race -> snapshot/provider contracts -> focused GREEN -> atomic access -> governed v2 path -> legacy offline/baseline -> existing OOS -> Alpha unit/integration -> Ruff -> scoped strict mypy -> compileall -> diff-check -> impacted repository regression -> independent verification`.

Omissions remain explicit: no real cost calibration, impact/capacity, PIT completion, OOS window, OOS access, threshold policy, parameter search, Paper/Testnet, or profitability acceptance is tested by this slice.

## Responsibility separation

| Gate | Responsible role | Independence boundary |
|---|---|---|
| RED author and implementation | Root Codex implementation owner | Must preserve pre-implementation RED manifests and cannot self-upgrade economic/runtime state |
| Evidence capture | Root owner through repository collector | Raw stdout/stderr/exit/hash retained; no summary-only PASS |
| Concurrent/fault validation | Root owner initially; fresh verifier reruns | Real OS processes and filesystem, no thread-only substitute |
| Architecture G2 | `/root/alpha_contract_arch_review` | Revision 1 rejected; Revision 2 accepted with frozen conditions |
| Final behavioral acceptance | Fresh no-author-context verifier | The pre-remediation verifier produced a preserved `FAIL` (P1/P2); post-remediation local reverify passes, but a fresh independent post-remediation verdict is unavailable after service usage limits; author-run checks cannot substitute for independence |

## ARO-06 captured RED

| RED | Observed failure | Evidence |
|---|---|---|
| Alpha stateful/provider | Collection error: `StatefulOfflineAlphaResult` does not exist | `artifacts/evidence/ALPHA-CONTRACT-001-RED-ALPHA-V2/20260901T095912902549Z.manifest.json` |
| Governed OOS v2 | Collection error: governed seal/receipt types do not exist | `artifacts/evidence/ALPHA-CONTRACT-001-RED-OOS-V2/20260901T095912902889Z.manifest.json` |
| OOS v1/v2 architecture | Collection error: governed v2 exports do not exist | `artifacts/evidence/ALPHA-CONTRACT-001-RED-OOS-ARCH/20260901T095912905239Z.manifest.json` |
| Process-atomic OOS access | `2 failed, 1 passed`; both synchronized OS processes received `SUCCESS`, sequence `1`, and the same audit head; `lock_path` is absent | `artifacts/evidence/ALPHA-CONTRACT-001-RED-OOS-ATOMIC/20260901T095912902538Z.manifest.json` |

The four RED manifests predate every product-file edit in ARO-06. The atomic failure is direct behavioral evidence of double receipt issuance, not a missing-symbol proxy.

## ARO-06 local verification status

| Check | Result | Evidence |
|---|---|---|
| Initial Alpha v2 compatibility | `47 passed` | `ALPHA-CONTRACT-001-ALPHA-V2-COMPAT-SLICE1` |
| Initial governed OOS v2 | `20 passed` | `ALPHA-CONTRACT-001-GREEN-OOS-V2-SLICE3` |
| OOS v1/v2 impacted | `47 passed` | `ALPHA-CONTRACT-001-OOS-IMPACTED-REGRESSION-R2` |
| Review hardening RED -> GREEN | `16 failed, 59 passed` -> `75 passed` | `ALPHA-CONTRACT-001-REVIEW-RED-HARDENING`, `ALPHA-CONTRACT-001-REVIEW-GREEN-HARDENING` |
| Expanded Alpha/OOS regression | `238 passed` | `ALPHA-CONTRACT-001-ALPHA-OOS-IMPACTED-REGRESSION` |
| Ruff / scoped strict mypy | PASS / PASS over 5 source files | `ALPHA-CONTRACT-001-REVIEW-RUFF-HARDENING-R2`, `ALPHA-CONTRACT-001-REVIEW-MYPY-HARDENING` |
| Frozen legacy behavior | Full report equal after excluding only the changed source-provenance hash | `ALPHA-CONTRACT-001-FROZEN-BASELINE-NORMALIZED-COMPARE`, `ALPHA-CONTRACT-001-FROZEN-BASELINE-CURRENT-SOURCE-HASH` |

The failed raw byte comparison is preserved because source provenance correctly changed.

## ARO-06 final local closure

| Check | Result | Evidence |
|---|---|---|
| Fresh impacted Alpha/OOS regression | `238 passed` | `artifacts/evidence/ALPHA-CONTRACT-001-FINAL-IMPACTED-REGRESSION/20260901T142644175420Z.manifest.json` |
| Ruff | PASS | `artifacts/evidence/ALPHA-CONTRACT-001-FINAL-RUFF/20260901T142616724317Z.manifest.json` |
| Scoped strict mypy | PASS over 5 source files | `artifacts/evidence/ALPHA-CONTRACT-001-FINAL-MYPY/20260901T142616726645Z.manifest.json` |
| Compileall / scoped diff-check | PASS / PASS | `ALPHA-CONTRACT-001-FINAL-COMPILEALL`, `ALPHA-CONTRACT-001-FINAL-DIFF-CHECK` |
| Full repository RED | `4 failed, 4814 passed, 1 warning`; all failures were governed-source registry drift | `artifacts/evidence/ALPHA-CONTRACT-001-FINAL-FULL-REGRESSION/20260901T142716261472Z.manifest.json` |
| Registry RED -> GREEN | 23 source digest differences reduced to zero; primary and independent scanners PASS; original four tests `4 passed` | `ALPHA-CONTRACT-001-REGISTRY-PRIMARY-GREEN`, `ALPHA-CONTRACT-001-REGISTRY-INDEPENDENT-GREEN`, `ALPHA-CONTRACT-001-REGISTRY-TESTS-GREEN` |
| Historical full repository GREEN | `4818 passed, 1 warning` | `artifacts/evidence/ALPHA-CONTRACT-001-FINAL-FULL-REGRESSION-R2/20260901T143501099714Z.manifest.json` |
| Post-remediation authority/arithmetic reverify | `81 passed, 1 warning`; store-authority-only v2 stages and exact cost equality covered | `artifacts/evidence/ARO-06-LOCAL-REVERIFY/20260902T045840951348Z.manifest.json` |
| Post-remediation research evidence regression | `43 passed, 1 warning` | `artifacts/evidence/ARO-RESEARCH-EVIDENCE-REGRESSION/20260902T045927333962Z.manifest.json` |
| Current full repository regression | `4823 passed` | `artifacts/evidence/ARO-FULL-REGRESSION-R3/20260902T050110124386Z.manifest.json` |
| Current registry negative checks | Primary and independent `PASS / issues=[]`; registry SHA-256 `75858e1576253466021f826f80d1169839a99093d218d2a80cdaa353741dd4e9` | `artifacts/evidence/ARO-REGISTRY-PRIMARY-NEGATIVE-R3/20260902T050536472252Z.manifest.json`, `artifacts/evidence/ARO-REGISTRY-INDEPENDENT-NEGATIVE-R3/20260902T050550340116Z.manifest.json` |
| Current scoped static closure | Compileall/mypy/diff-check PASS; full-tree Ruff retains one unrelated S310 | `artifacts/evidence/ARO-COMPILEALL-R3/20260902T050446506689Z.manifest.json`, `artifacts/evidence/ARO-MYPY-R3/20260902T050651024738Z.manifest.json`, `artifacts/evidence/ARO-DIFF-CHECK-R3/20260902T050651426734Z.manifest.json`, `artifacts/evidence/ARO-STATIC-RUFF-R3/20260902T050446358997Z.manifest.json` |

The post-remediation local verification is `PASS_WITH_CONDITIONS`: the historical independent `FAIL` is preserved as pre-remediation evidence, and no fresh independent post-remediation verdict was obtained because both delegated attempts hit the service usage limit. This does not affect the stricter economic disposition, which remains `BLOCKED / DO_NOT_PROMOTE`.
