# Context packet

## Objective

Optimize the alpha module and improve overall return without weakening safety or economic-evidence gates.

## Non-Goals

No Mainnet/Testnet write, deployment, restart, commit, push, external message, or parameter search on untrusted data.

## Status

`DATASET_ADMITTED_BASELINE_DIAGNOSTIC_ALPHA_PROMOTION_BLOCKED`: the isolated August dataset passes market-data admission, governance evidence is recorded, and the ARO-06 authority/arithmetic remediation passes local verification. No return-improving change has been implemented or validated.

## Decisions

| Decision | Reason | Reversible? |
|---|---|---|
| Keep Alpha `DIAGNOSTIC_ONLY / DO_NOT_PROMOTE` | August market data, retrospective PIT/lineage and scoped cost/capacity evidence are recorded, but complete E0, a candidate freeze, contemporaneous source custody, calibrated fills/impact/capacity, and a valid improvement experiment are absent | Yes, after new sealed evidence |
| Keep Mainnet `PROHIBITED` | No authorization or production-readiness evidence | Yes, only through explicit external approval |
| Reject dynamic registry placeholders | Fixing reflection removes the false capability at source | Yes |
| Do not apply threshold-gating exploration | August has been inspected; additive v2 now defines the semantic, but there is no pre-sealed unseen comparison, real bound snapshots, or candidate-search authorization | Yes, after sealed unseen validation and new authorization |

## Evidence

| Evidence | Location |
|---|---|
| Historical full repository PASS (`4818 passed, 1 warning`) | `artifacts/evidence/ALPHA-CONTRACT-001-FINAL-FULL-REGRESSION-R2/20260901T143501099714Z.manifest.json` |
| Current full repository PASS (`4823 passed`) and ARO-06 local reverify (`81 passed`) | `artifacts/evidence/ARO-FULL-REGRESSION-R3/20260902T050110124386Z.manifest.json`; `artifacts/evidence/ARO-06-LOCAL-REVERIFY/20260902T045840951348Z.manifest.json` |
| Current registry primary/independent negative checks PASS, SHA-256 `75858e1576253466021f826f80d1169839a99093d218d2a80cdaa353741dd4e9` | `ARO-REGISTRY-PRIMARY-NEGATIVE-R3`; `ARO-REGISTRY-INDEPENDENT-NEGATIVE-R3` |
| Legacy local data FAIL | `artifacts/evidence/ALPHA-DATA-001-CURRENT-DATA-GATE-FINAL2/20260831T175010931124Z.manifest.json` |
| Authorized frozen data PASS_WITH_CONDITIONS | `artifacts/datasets/alpha-return-2026-08-01_2026-08-31-v1/dataset-index.json`; `ALPHA-DATA-002-INDEPENDENT-VERIFY2` |
| Current Alpha baseline | `artifacts/analysis/alpha-return-2026-08-01_2026-08-31-v1/baseline-report.json` |
| Independent recomputation | `artifacts/analysis/alpha-return-2026-08-01_2026-08-31-v1/independent-recompute.json`; `ALPHA-BASELINE-002-INDEPENDENT-RECOMPUTE` |
| Bounded acceptance | `12-acceptance-report.md` |
| Final state | `current-state.yaml` |

## Files touched

| File group | Change |
|---|---|
| `beidou_research/data/*` | Provenance, canonical hash, integrity admission, safe backfill |
| `apps/factor_miner/worker.py` | Persisted-manifest gate for local mining |
| `beidou_research/economic_truth.py` | E0 market-data binding |
| acquisition/baseline tools and isolated artifacts | Exact public download, offline verification, causal diagnostic, no-context recomputation |
| Alpha/OOS v2 contracts | Additive bound state/cost evaluation, governed preseal-to-promotion chain, atomic one-time access, legacy research-only labels |
| `config/write-capability-registry.json` | Exact governed hashes for new data/analysis artifacts and Alpha/OOS sources; no authority semantics changed |
| tests / registry / this run | Regression, governance digest, lifecycle evidence |

## Open risks

| Risk | Next check |
|---|---|
| Complete E0 lacks PIT and feature lineage | Produce bound manifests before factor evaluation |
| August is contaminated for promotion | Precommit an unseen historical-custody or future forward window before viewing it |
| Additive state/cost contract has no real facts/caller | Require verified current-position and calibrated cost-source snapshots; keep legacy diagnostic-only |
| Historical ARO-06 independent verifier found P1/P2 defects | Local remediation and `81`-test reverify pass; obtain an independent post-remediation verdict when service capacity permits; do not relabel author-run checks independent |
| No robustness/incrementality evidence | Register hypothesis/Metric Owner; run multi-regime WFO/CPCV, sensitivity, correlation, impact, and capacity checks |
| Concurrent V4 work remains in the dirty tree | Preserve it; recheck status/hash before any future edit or verification claim |

## Next actions

1. Obtain an independent post-remediation ARO-06 behavior verdict when a verifier is available; preserve the historical P1/P2 failure.
2. Bind contemporaneous PIT/feature lineage and fill-calibrated cost/impact/capacity evidence; current reports remain scoped/conditional.
3. Create a candidate freeze and pre-seal the unseen October window before any result is viewed; its first-read gate is `2026-11-02T00:00:00Z`.
4. Only after repository phase prerequisites and new explicit authorization, run a bounded Alpha candidate comparison with WFO/CPCV, costs, sensitivity, regimes, correlation, impact, and capacity.
