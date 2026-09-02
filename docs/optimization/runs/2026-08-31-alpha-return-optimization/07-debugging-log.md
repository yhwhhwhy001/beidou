# Debugging log

## Reproduction

- The repository scanner classified `backfill._feed_endpoint()` as two possible terminal call paths because it used dynamic `getattr` and returned the dynamically read value.
- RED evidence: `artifacts/evidence/ALPHA-DATA-001-SCANNER-RED/20260831T125632279408Z.manifest.json`; the assertion reported `getattr[DYNAMIC]` and `returned_callable[DYNAMIC]`.
- The four registry tests were temporarily green only because those false-positive paths had been registered as read-only terminal records. That was compliance by declaration, not removal of the root cause.

## Timeline and evidence

| Time (UTC) | Observation / experiment | Result |
|---|---|---|
| 2026-08-31T12:53:33Z | Re-ran four registry gates before the fix | `4 passed`; temporary registry records explained the result |
| 2026-08-31T12:56:32Z | Asserted that `_feed_endpoint` produces no terminal path | FAIL with the two exact dynamic identities |
| 2026-08-31T12:57:46Z | Re-ran the same scanner assertion after direct attribute access | PASS |
| 2026-08-31T13:00:21Z | Rebuilt registry and re-ran four gates | `4 passed`; false terminal records absent |
| 2026-08-31T17:42:37Z | Rebuilt after endpoint/timestamp hardening and re-ran four gates | `4 passed` |

## Hypotheses and experiments

1. Hypothesis: the scanner cannot distinguish a read-only dynamic attribute from a returned write callable. Direct `rest_url` / `_rest_url` access should remove both findings without weakening the scanner. Confirmed.
2. Hypothesis: deleting registry records alone would make validation fail because source digests and the discovered terminal set would diverge. Confirmed by the rebuild contract; records were removed only after the source no longer emitted them.
3. Adversarial review found exact-endpoint, duplicate-JSON, and timestamp fail-closed gaps. RED evidence: `artifacts/evidence/ALPHA-DATA-001-RED-REVIEW-HARDEN/20260831T131827110445Z.manifest.json` (`8 failed`, all expected). GREEN evidence: `artifacts/evidence/ALPHA-DATA-001-GREEN-REVIEW-HARDEN/20260831T132240943542Z.manifest.json` (`16 passed`).

## Root cause

- A provenance-only read used generic reflection, which the fail-closed terminal scanner correctly treated as capable of returning a callable.
- Endpoint classification trusted the `fapi.binance.com` host without binding the K-line path, and archive classification accepted an overly broad path prefix.
- Standard JSON decoding accepted duplicate keys, and an out-of-range manifest timestamp could escape as `OverflowError` instead of a gate reason.

## Fix and regression proof

- Replaced reflection with direct attribute access and preserved the `rest_url -> _rest_url -> empty` fallback.
- Canonicalized known Binance REST bases to `/fapi/v1/klines`; only exact public K-line paths and exact USD-M ZIP archive paths are research-eligible.
- Rejected duplicate JSON keys, non-integral bar timestamps, and out-of-range data-end timestamps fail-closed.
- Final impacted regression: `artifacts/evidence/ALPHA-DATA-001-REGRESSION-FINAL2/20260831T174237507138Z.manifest.json` (`203 passed`).
- Final full regression: `artifacts/evidence/ALPHA-DATA-001-FULL-FINAL2/20260831T174600077904Z.manifest.json` (`4740 passed`, one unrelated deprecation warning).

## ARO-06 review-hardening defects

- RED: `artifacts/evidence/ALPHA-CONTRACT-001-REVIEW-RED-HARDENING/20260901T102832791885Z.manifest.json` (`16 failed, 59 passed`).
- Root causes: snapshot helpers coerced string/integer values instead of enforcing the runtime contract; an invalid provider object leaked `AttributeError`; governed dataclasses trusted caller-supplied self-digests after `dataclasses.replace`; and `load_governed_seal()` did not require its exact immutable v1 record.
- Fix: strict scalar/digest types, exact provider-result type, construction-time canonical digest recomputation for all governed stages, and complete governed-plus-legacy load validation.
- GREEN: `artifacts/evidence/ALPHA-CONTRACT-001-REVIEW-GREEN-HARDENING/20260901T103010857127Z.manifest.json` (`75 passed`). Expanded impacted regression: `238 passed`.

## Frozen baseline byte-diff reconciliation

- The byte comparison intentionally failed and is preserved at `artifacts/evidence/ALPHA-CONTRACT-001-FROZEN-BASELINE-BYTE-COMPARE/20260901T103243212347Z.manifest.json`.
- `diff` localized the only change to `alpha.alpha_source_sha256`: additive v2 source changed the file hash from `b8eb56...abb95d` to `10f192...6f998`.
- A whole-document comparison deleting only that provenance field returned `true`; the new reported hash equals the current source file hash. This is `PASS_WITH_EXPLAINED_PROVENANCE_CHANGE`, not a byte-stability claim.

## Final full-suite registry drift

- Reproduction: `artifacts/evidence/ALPHA-CONTRACT-001-FINAL-FULL-REGRESSION/20260901T142716261472Z.manifest.json` failed with `4 failed, 4814 passed, 1 warning`; every failure was in the primary or independent write-registry gate.
- Root cause: the registry correctly hashes all governed production sources and non-evidence JSON artifacts. Acquisition/baseline added 16 governed JSON artifacts; ARO-06 added `oos_governance_v2.py` and changed six governed source files. Their hashes were not yet registered.
- Minimal fix: add those 17 exact paths, replace the six exact hashes, and recompute only the derived governance digest. No scanner exclusion, capability, owner, entrypoint, terminal path, negative-test, or authority rule changed.
- Focused GREEN: primary scanner and independent oracle both returned `PASS / issues=[]`; the four original tests passed at `artifacts/evidence/ALPHA-CONTRACT-001-REGISTRY-TESTS-GREEN/20260901T143340473043Z.manifest.json`.
- Full GREEN: `artifacts/evidence/ALPHA-CONTRACT-001-FINAL-FULL-REGRESSION-R2/20260901T143501099714Z.manifest.json` (`4818 passed, 1 warning`).

## ARO-06 independent counterexamples and remediation

- The no-author-context verifier recorded two valid pre-remediation counterexamples at `20260901T153900Z`: direct public v2 construction could produce an eligible promotion without store/audit authority (P1), and a nonzero forecast-cost arithmetic mismatch within `1e-12` was accepted (P2). The complete report and failing manifests remain preserved in `12-acceptance-report.md` and the `ALPHA-CONTRACT-002-INDEPENDENT-COUNTEREXAMPLE-*` evidence paths.
- Remediation removed public `build()` factories, routes seal/receipt/promotion construction through the governed store authority and complete persisted legacy seal checks, rejects direct eligible-decision construction without the private store token, and uses exact equality for cost arithmetic.
- Fresh local reverify after the remediation passed all 81 Alpha/OOS authority, arithmetic, mutation, race, and crash tests: `artifacts/evidence/ARO-06-LOCAL-REVERIFY/20260902T045840951348Z.manifest.json`. The local reverify is author-run and does not erase or relabel the historical independent `FAIL`.
- Two new independent-agent attempts were blocked by the service usage limit before any post-remediation verdict was produced. This is a missing independent confirmation, not a PASS; G8 remains blocked.
