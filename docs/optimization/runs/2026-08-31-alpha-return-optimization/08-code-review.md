# Code review

- Reviewer independence: the original local review was self-review; a subsequent no-author-context verifier produced a preserved pre-remediation `FAIL` with P1/P2 counterexamples. The post-remediation local reverify is author-run; a fresh independent post-remediation verdict was unavailable after service usage limits. The repository contract therefore keeps G8 blocked.
- Version/diff: branch `codex/v4-incident-remediation`, HEAD `818518e8d6a839d014689f30ad66f44d794e4b60`, scoped to the market-data/factor boundary plus mechanical registry refresh and run evidence.

| Severity | Location | Finding | Evidence | Required action | Status |
|---|---|---|---|---|---|
| P1 | `beidou_research/data/backfill.py::_feed_endpoint` | Dynamic reflection was falsely governable as a returned terminal callable. | Scanner RED manifest and exact identities in `07-debugging-log.md`. | Use direct attributes; delete placeholder decisions and rebuild registry. | RESOLVED |
| P1 | `beidou_research/data/dataset_manifest.py::_endpoint_classification` | Host-only public API trust admitted non-K-line paths; archive matching was too broad. | `ALPHA-DATA-001-RED-REVIEW-HARDEN` failed for base URL, order path, lookalike K-line path, and non-ZIP archive. | Require exact `/fapi/v1/klines` or exact USD-M daily/monthly K-line ZIP path. | RESOLVED |
| P1 | `DatasetManifest.read` | Duplicate JSON keys were silently last-value-wins. | RED duplicate-key test. | Reject duplicates at every JSON object level. | RESOLVED |
| P1 | `DatasetManifest.assess_economic_research` | Non-integral timestamps could be normalized by `int`, and extreme timestamps could raise instead of fail closed. | RED timestamp tests. | Add typed gate reasons and catch platform timestamp range errors. | RESOLVED |
| P1 | `apps/factor_miner/worker.py` | API data without a manifest still runs diagnostics; risk was accidental promotion. | Added assertion: `candidates_passed == 0`, every bundle has `dataset_manifest_unbound`, no promotion chain. | Preserve diagnostic behavior but keep promotion fail-closed. | VERIFIED |
| P2 | Skill pack | Referenced `cios-safety-contract.md` is absent at the declared shared path. | Filesystem lookup returned no matching contract. | Use the stricter repository master/protocol and record the limitation. | OPEN_CONDITION |
| P1 | `apps/alpha_app/composition.py` / `TrendAlpha` | Legacy `evaluate()` cannot distinguish HOLD from FLAT for threshold `NO_ACTION` because it has no current-position input. | Frozen baseline counted 2,132 nonzero subthreshold targets across 2,676 signals. | Added `evaluate_stateful()` with a verified current-position snapshot and `NO_ACTION -> PRESERVE_VERIFIED_POSITION`; legacy remains machine-labelled diagnostic-only and cannot be promoted. | RESOLVED_AT_ADDITIVE_V2_INTERFACE |
| P1 | `apps/alpha_app/composition.py` | Legacy fixed forecast costs and dataset-derived `source_hash` are not cost provenance. | Source inspection plus historical Alpha acceptance boundary. | Stateful v2 requires an explicit scope/time/value/source-bound cost snapshot and verifies exact provider arithmetic; legacy remains diagnostic-only. Real fill-calibrated evidence is still absent. | RESOLVED_AT_ADDITIVE_V2_INTERFACE |
| P2 | Frozen baseline comparator/cost closure | The equal-weight comparator is gross and zero-cost, and the finite diagnostic does not charge a forced terminal unwind. | Baseline source review; report construction and cost loop. | Label the comparator as non-investable/opportunity-cost only and keep terminal liquidation, impact, latency, and capacity `NOT_EVALUATED`. | OPEN_CONDITION |

## Frozen-baseline calculation review

- Closed-bar and next-open indices are causal and contiguous; the 51-bar warmup correctly leaves 669 evaluable bars per symbol.
- Per-symbol turnover is `sum(abs(target_t - target_{t-1}))` from initial zero exposure. Averaging the four sleeve turnovers is correct for the stated equal-capital construction.
- Averaging per-symbol hourly returns and compounding is correct for the stated hourly equal-weight comparator. Because it is zero-cost and implicitly rebalanced, it is not presented as an executable benchmark.
- Averaging per-sleeve net returns is algebraically consistent with applying the same linear turnover/funding charges to a four-sleeve equal-capital portfolio.
- A no-context second implementation reproduced these facts within `1e-12`. Its first turnover-convention disagreement was preserved, localized, and corrected before the bounded baseline acceptance row closed as `PASS_WITH_CONDITIONS`.

## Decision

`PASS_WITH_CONDITIONS` for the local data-admission and additive ARO-06 contract implementations, and `BLOCKED` for Alpha candidate modification/promotion. No unresolved P0/P1 remains in those local code slices. Missing real position/cost facts, economic governance, unseen OOS, calibration, impact/capacity, and independent ARO-06 behavior acceptance remain external evidence blockers. This is not release readiness or proof of improved return.

## ARO-06 implementation review

| Severity | Location | Finding | Resolution | Status |
|---|---|---|---|---|
| P1 | `beidou_shared/contracts/alpha_execution.py` | Runtime string/integer coercion could admit a non-canonical snapshot value or SHA-256 despite the typed interface. | Added strict runtime scalar/string checks and RED cases. | RESOLVED |
| P1 | `apps/alpha_app/composition.py` | An injected non-`AlphaForecast` result escaped as `AttributeError` rather than a typed fail-closed provider error. | Require the exact contract type before reading any forecast field. | RESOLVED |
| P1 | `beidou_research/experiments/oos_governance_v2.py` | Frozen governed objects did not recompute their own digest on direct construction/replacement, allowing in-memory field/digest inconsistency. | Added construction-time version, field, state, sequence, and canonical digest validation for seal, receipt, and decision. | RESOLVED |
| P1 | `GovernedOOSSealStoreV2.load_governed_seal` | A governed record could load after its exact legacy seal was removed. | Load and internal verification now require an exact, verifiable v1 record and digest. | RESOLVED |
| P1 evidence gap | governed v2 concurrency/crash tests | Initial GREEN tested v1 OS-process locking but not the governed wrapper or post-commit fault boundary. | Added synchronized v2 OS processes, preappend retry, post-commit denial, partial audit, and per-field mutation cases. | RESOLVED |
| P2 | Frozen report | Byte identity cannot hold when the report intentionally binds the changed Alpha source hash. | Preserve the failed byte comparison; require exact whole-report equality after deleting only `alpha.alpha_source_sha256`, and verify its new value against the source file. | RESOLVED_WITH_EXPLANATION |

Local review decision after remediation: `PASS_WITH_CONDITIONS`, limited to ARO-06 contract behavior. Fresh local authority/arithmetic reverify (`81 passed`), research evidence (`43 passed`), full repository (`4823 passed`), registry negative checks, mypy, compileall, and diff-check pass; full-tree Ruff retains one unrelated S310. The prior independent `FAIL` is preserved and its P1/P2 findings were repaired locally, but no independent post-remediation verdict is claimed because delegated attempts hit the service usage limit. The original Alpha semantic/cost blockers are closed only at the additive interface level; economic calibration, real custody, OOS evidence, factor promotion, and improved return remain blocked.

## Preserved independent findings and remediation status

- P1: public v2 seal/receipt/promotion construction could bypass store authority and durable audit. **Remediated locally** by removing public factories, requiring store-authority construction, validating persisted legacy bindings, and rejecting direct eligible-decision construction.
- P2: provider cost arithmetic accepted a nonzero mismatch within an absolute tolerance. **Remediated locally** by requiring exact equality; a fresh negative test covers a `5e-13` mismatch.
- The original independent evidence remains immutable under `ALPHA-CONTRACT-002-INDEPENDENT-*`. The current local evidence is `artifacts/evidence/ARO-06-LOCAL-REVERIFY/20260902T045840951348Z.manifest.json`; it is not a substitute for an independent post-remediation review.
