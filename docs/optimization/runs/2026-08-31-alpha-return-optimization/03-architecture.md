# Architecture and G2 review

## Scope, authority, and stop conditions

- Scope: close the `NO_ACTION` state ambiguity, replace hard-coded/dataset-derived costs in a new additive interface, and make OOS pre-seal readiness machine-verifiable.
- Authority: reversible local source, tests, run documents, and evidence only. No parameter optimization, external data read, real OOS seal, account/order access, Testnet/Mainnet write, release, commit, push, deployment, or restart.
- Protected work: existing V4 incident-remediation changes, especially `beidou_cli/__init__.py`, remain untouched.
- Required evidence: current caller inventory, independent architecture decision, genuine RED, focused GREEN, existing-caller compatibility, impacted regression, static checks, and independent final verification.
- Stop on: scope collision with existing user changes, an architecture `REJECTED/BLOCKED` decision, inability to preserve legacy diagnostics, an untyped fail-open path, or any request for real OOS/trading access.

## HISTORICAL_REJECTED_R1 — non-normative record

Everything in this section through `Decision 1` is preserved only as failed architecture evidence. It must not be used to implement the task. Revision 2 below is the only normative architecture.

### Rejected proposal

Use an expand-migrate-contract sequence:

1. Keep `OfflineAlphaApp.evaluate(data)` unchanged as `LEGACY_DIAGNOSTIC_ONLY`; frozen August tools and the dirty CLI continue to call it.
2. Add immutable shared value objects for a verified strategy-sleeve position and a verified cost schedule, plus a typed `AlphaStateContractError` carrying stable error codes.
3. Add `OfflineAlphaApp.evaluate_stateful(data, *, position, costs)`. It performs all presence, scope, time-window, numeric, and source-digest checks before returning a target.
4. Inject the supplied costs into the existing `TrendAlpha` context and verify that the produced forecast preserved the exact cost values and source digest.
5. Reduce the normalized side deterministically: `NO_ACTION -> position.current_weight`; `BUY|SELL -> forecast.raw_score`; anything else -> typed fail-closed error.
6. Return a new audit-rich result rather than changing the legacy result schema.
7. Reuse the existing opaque OOS seal and hash-chained access audit. Add only a pure pre-seal readiness layer for custody and candidate-freeze inputs that the existing seal does not explicitly require, especially named Metric Owner and metric-policy binding.

No code in this slice selects an Alpha parameter, reads OOS data, constructs an exchange client, or changes runtime activation.

### Rejected interfaces and data

#### Stateful Alpha inputs

| Type / field | Invariant |
|---|---|
| `VerifiedPositionSnapshot` | Binds `strategy_id`, `instrument_id`, `venue_id`, finite `current_weight`, timezone-aware `observed_at`, `valid_until >= observed_at`, and canonical nonzero 64-lowercase-hex `source_hash`. |
| `VerifiedCostSnapshot` | Binds `instrument_id`, `venue_id`, finite/non-negative `expected_fee_bps` and `expected_slippage_bps`, finite signed `expected_funding_bps`, aware validity timestamps, and canonical nonzero SHA-256 `source_hash`. |
| Decision time | `BoundLocalData.observed_at`; both facts must satisfy `fact.observed_at <= decision_time <= fact.valid_until`. |
| Scope | Position strategy/instrument/venue and cost instrument/venue must equal the stateful evaluation scope. |

The application validates deterministic binding and freshness. It does not claim that a syntactically valid digest was signed by an external custodian; that authenticity remains an upstream evidence gate.

#### Stateful result

`StatefulOfflineAlphaResult` contains the existing dataset/target/row count plus:

- normalized `forecast_side`;
- `target_semantics` (`PRESERVE_VERIFIED_POSITION` or `FORECAST_RAW_SCORE`);
- `position_source_hash`;
- `cost_source_hash`.

The forecast hash remains the link between forecast and `AlphaTarget`; exact cost propagation is checked before result construction.

#### Typed failure model

| Error family | Stable codes |
|---|---|
| Presence | `POSITION_SNAPSHOT_REQUIRED`, `COST_SNAPSHOT_REQUIRED` |
| Construction | `*_TIMESTAMP_NOT_AWARE`, `*_VALIDITY_WINDOW_INVALID`, `*_VALUE_NOT_FINITE`, `*_VALUE_NEGATIVE`, `*_SOURCE_HASH_INVALID` |
| Decision-time validity | `*_FACT_FROM_FUTURE`, `*_FACT_EXPIRED` |
| Scope/binding | `POSITION_SCOPE_MISMATCH`, `COST_SCOPE_MISMATCH`, `FORECAST_COST_BINDING_MISMATCH` |
| Side semantics | `UNSUPPORTED_FORECAST_SIDE` |

All errors occur before a stateful target is returned. The legacy diagnostic path preserves its historical behavior and is not upgraded by implication.

#### Rejected OOS two-stage readiness

The existing `beidou_research.experiments.OOSSealStore` already provides immutable canonical seal bytes, a hidden boundary commitment, one-time access, an authenticated append-only audit chain, experiment/checkpoint binding, invalidation, and research-only rollback. It will not be duplicated or modified in this slice.

The additive pure pre-seal layer has two optional inputs:

1. `OOSWindowCustodySpec`: exact UTC boundary, venue, unique ordered symbols, interval, source class/contract digest, custodian, custody-sealed time, and first-read-not-before time.
2. `OOSCandidateFreeze`: code commit/tree digest, strategy-policy digest, metric-policy digest (including all thresholds and decision rules), cost-policy digest, named Metric Owner, and freeze time.

The assessment is `READY_FOR_EXISTING_SEAL` only when both inputs are present, canonical, and ordered as `custody_sealed_at <= candidate_frozen_at < first_read_not_before`; otherwise it is `DRAFT_BLOCKED` with deterministic missing/invalid reasons. A helper verifies that a future `ExperimentRunIdentity` uses the same code commit/tree and a combined policy-binding digest before the existing seal can be claimed by this workflow.

For this run, no true window, custodian, source contract, Metric Owner, or threshold policy is authorized. The only truthful instance is therefore `DRAFT_BLOCKED`; no `OOSSealStore.create()` or `access()` call is made.

### Rejected failure and security model

- Unknown or stale position is never converted to zero exposure and never permits a new stateful target.
- Missing/stale costs are never replaced by defaults in v2.
- `NO_ACTION` cannot accidentally rebalance a live strategy sleeve toward a sub-threshold raw score.
- Cross-symbol, cross-venue, and cross-strategy position replay is rejected; cross-symbol/venue cost replay is rejected.
- A source digest is propagated end to end and checked against the forecast, but digest presence alone is not treated as proof of economic calibration or source authenticity.
- The pre-seal readiness object has no file, network, exchange, or OOS-data access capability.
- Existing OOS keys/audit keys remain out of scope and must never be placed in repository artifacts or chat.

### Rejected migration and rollback

| Phase | Consumer state | Entry criterion | Rollback |
|---|---|---|---|
| Expand | Legacy `evaluate()` plus new v2 contracts coexist | Independent G2 approval and RED evidence | Remove only new exports/tests; legacy callers are unchanged. |
| Migrate | Future callers opt into `evaluate_stateful()` with verified facts | Provider integration and contract tests, separately authorized | Return that caller to legacy diagnostic-only use; no persistent data migration. |
| Contract | Legacy removal/deprecation | All consumers migrated, frozen baseline replaced under a separately sealed experiment, explicit approval | Not authorized or scheduled in this slice. |

There is no database/schema migration, release, or runtime cutover. Rollback preserves all RED/GREEN evidence and the historical baseline report.

### Rejected alternatives

- Reinterpret legacy `NO_ACTION` as zero: rejected because zero is an unverified position change and can increase risk when currently short.
- Continue emitting raw score for `NO_ACTION`: rejected because it contradicts the approved hold semantic.
- Mutate `evaluate()` in place: rejected because it would invalidate existing frozen diagnostics and dirty-CLI compatibility.
- Put state/cost fields onto `BoundLocalData`: rejected because market observations, portfolio facts, and cost evidence have different sources and validity windows.
- Create a second OOS seal store: rejected because the repository already has a stronger immutable seal/audit implementation.
- Create a real September/forward seal during the initial design review: rejected because the required window, custodian, Metric Owner, thresholds, and access authorization had not yet been supplied. A future October custody contract is now recorded separately, but its active assessment remains blocked until candidate freeze.

### Independent review 1

Decision: `REJECTED` (`FAIL`). This failure is preserved and cannot be overwritten by the revision below.

P0 findings:

1. The proposed pure preseal helper was bypassable because public v1 `OOSSealStore.create/access` and `validate_promotable_experiment` did not consume it. V1 could still report `PROMOTABLE` without Metric Owner, metric policy, custodian, or preseal digest.
2. Existing OOS `access()` read the audit chain and appended later without a process lock or atomic claim. Two concurrent callers could both receive `ALLOWED_ONCE`; later invalidation cannot undo the second disclosure.

P1/P2 conditions included full forecast scope/time/range/sign validation, a pure-market feature hash, recomputable snapshot bindings, time-complete result audit, complete preseal-to-identity/seal mapping, legacy/v2 machine-visible separation, canonical IDs/times, dedicated G2 state, provider/bypass/mutation/concurrency tests, and preservation of failed evidence.

Independent reviewer evidence was read-only at HEAD `818518e8d6a839d014689f30ad66f44d794e4b60`; no tests or files were changed by the reviewer. The full reviewer report is preserved in the session record and summarized here before any implementation.

### Decision 1

`REJECTED`. No RED or implementation was authorized from proposal 1.

## Revision 2 proposal

This section and its descendants are the sole normative architecture for ARO-06. RED tests and implementation must map to this version; R1 is never an allowed fallback. RED, GREEN, rejected-review, crash, and audit evidence are append-only task evidence and are never deleted as rollback.

### Canonical encoding and digest formulas

All v2 digests use one exact primitive:

```text
CJSON(value) = json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("utf-8")
D(domain, value) = sha256(domain.encode("ascii") + b"\x00" + CJSON(value)).hexdigest()
```

- JSON object keys are serialized in lexicographic order by `sort_keys=True`; arrays retain the specified order. A digest field is never included in its own input payload.
- Digests are exactly 64 lowercase hexadecimal characters and cannot be all zero. Caller-supplied binding/result/preseal/seal/receipt/decision digests are never trusted: they are computed properties or are recomputed during construction/loading.
- Every datetime is timezone-aware and hashes as UTC `datetime.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")`. Validity is inclusive: `observed_at <= decision_at <= valid_until`. Preseal order is `custody_sealed_at <= candidate_frozen_at < first_read_not_before`.
- Strategy is exactly `offline-alpha-v1`. Instrument, venue, symbol, custodian, owner, and interval inputs must already equal their stripped canonical form; strategy/instrument/venue/symbol use uppercase-or-fixed identifiers as specified by the owning contract, interval uses repository lowercase form such as `1h`. No silent case or whitespace repair occurs.
- Symbols are unique, uppercase, and lexicographically sorted before construction. Canonical universe is `venue + "|" + ",".join(symbols)`.
- Finite zero values hash as JSON `0.0`, including normalization of negative zero to `0.0`.

Exact domain tags and payloads:

| Digest | Domain tag | Canonical payload fields |
|---|---|---|
| position snapshot | `beidou.alpha.position-snapshot.v2` | `schema_version, source_class, strategy_id, instrument_id, venue_id, current_weight, observed_at, valid_until, source_artifact_digest` |
| cost snapshot | `beidou.alpha.cost-snapshot.v2` | `schema_version, source_class, instrument_id, venue_id, expected_fee_bps, expected_slippage_bps, expected_funding_bps, observed_at, valid_until, source_artifact_digest` |
| stateful result | `beidou.alpha.stateful-result.v2` | `schema_version, dataset_id, dataset_version, dataset_content_hash, strategy_id, instrument_id, venue_id, target_weight, forecast_hash, model_version, target_timestamp, row_count, forecast_side, target_semantics, decision_at, position_binding_digest, cost_binding_digest` |
| window custody | `beidou.oos.window-custody.v2` | `schema_version, boundary, venue, symbols, interval, source_endpoint, source_class, source_contract_digest, dataset_manifest_digest, pit_manifest_digest, custodian_id, custody_sealed_at, first_read_not_before` |
| candidate freeze | `beidou.oos.candidate-freeze.v2` | `schema_version, code_commit, code_tree_digest, strategy_policy_digest, metric_policy_digest, cost_policy_digest, metric_owner_id, candidate_frozen_at` |
| draft assessment | `beidou.oos.preseal-assessment.v2` | `schema_version, status, reasons, window_custody_digest_or_null, candidate_freeze_digest_or_null` |
| ready preseal | `beidou.oos.preseal-package.v2` | `schema_version, status, window_custody_digest, candidate_freeze_digest` |
| identity policy | `beidou.oos.identity-policy.v2` | `schema_version, preseal_digest, candidate_freeze_digest, strategy_policy_digest, metric_policy_digest, cost_policy_digest, metric_owner_id` |
| governed seal | `beidou.oos.governed-seal.v2` | `schema_version, governance_version, preseal_digest, window_custody_digest, candidate_freeze_digest, experiment_identity_digest, legacy_seal_digest, governed_at` |
| governed receipt | `beidou.oos.governed-receipt.v2` | `schema_version, governance_version, governed_seal_digest, preseal_digest, legacy_seal_digest, legacy_sequence, legacy_audit_head` |
| governed promotion | `beidou.oos.governed-promotion.v2` | `schema_version, governance_version, status, reasons, eligible_for_v2_promotion, governed_seal_digest, governed_receipt_digest, preseal_digest, experiment_identity_digest, candidate_evaluation_digest` |

V2 schema version is exactly `2.0`. Snapshot source classes are exactly `STRATEGY_SLEEVE_POSITION_FACT` and `COST_MODEL_EVIDENCE`, respectively. Unknown schema/source class is a typed construction error.

### Alpha provider and snapshot boundary

- Rename inputs to `BoundPositionSnapshot` and `BoundCostSnapshot`; they assert deterministic caller binding, not external authenticity or calibration.
- Each snapshot has schema version, canonical nonblank scope, aware timestamps normalized to UTC for hashing, source-artifact digest, and a domain-separated canonical `binding_digest` that covers every field and value.
- `BoundCostSnapshot.binding_digest` covers fee/slippage/funding, scope, fact/expiry times, source artifact digest, and version. Reusing the same source digest with altered costs therefore changes the binding.
- V2 builds pure market features without any legacy cost fields or dataset-as-cost source. Costs are injected only through the forecast context. A changed cost snapshot changes cost/forecast binding but not the pure market `feature_hash`.
- Stateful strategy scope is the explicit application constant `offline-alpha-v1`; the position snapshot and returned forecast must match it. V2 additionally checks exact instrument, venue, UTC decision timestamp, raw score `[-1,1]`, BUY positive, SELL negative, exact cost values/source, and `forecast.cost_verifiable`.
- Stable provider errors add `FORECAST_SCOPE_BINDING_MISMATCH`, `FORECAST_TIMESTAMP_BINDING_MISMATCH`, `FORECAST_DIRECTION_BINDING_MISMATCH`, `FORECAST_COST_BINDING_MISMATCH`, and `FORECAST_COST_ARITHMETIC_INVALID`.
- The result includes decision time, both source-artifact digests, both canonical binding digests, and both fact/expiry time pairs. Legacy `evaluate()` remains computationally unchanged and exposes a machine-readable `LEGACY_DIAGNOSTIC_ONLY` contract marker.
- `StatefulOfflineAlphaResult.result_binding_digest` uses the formula above and covers the dataset, target identity/timestamp, side/semantic, row count, decision, and both snapshot bindings. Source/fact/expiry fields are carried for audit and are already transitively covered by the snapshot bindings.

### Versioned governed OOS path

V1 remains readable/compatible but cannot claim the new governance level. V2 is an additive, separately named API; no silent upgrade occurs.

1. `OOSWindowCustodySpecV2` binds UTC boundary, venue, sorted unique symbols, interval, exact endpoint, trusted research source class, source-contract digest, dataset-manifest digest, PIT/lineage digest, custodian ID, custody time, and first-read-not-before time. Endpoint eligibility is derived with the existing `MarketDataProvenance.from_endpoint` classification and must yield `PUBLIC_READ_ONLY / ECONOMIC_RESEARCH`; Demo, Testnet, unknown, or `EXECUTION_ONLY` is rejected rather than reclassified by a new enum.
2. `OOSCandidateFreezeV2` binds code commit/tree, strategy-policy digest, metric-policy digest including thresholds/decision rules, cost-policy digest, named Metric Owner, and freeze time.
3. `OOSPresealPackageV2` is `READY_FOR_V2_SEAL` only when both valid facts exist and `custody_sealed_at <= candidate_frozen_at < first_read_not_before`. Its domain-separated digest covers both complete payloads. Missing facts produce a versioned `DRAFT_BLOCKED` assessment, never a sealable package.
4. The future experiment identity mapping is exact:

| Preseal fact | Required downstream binding |
|---|---|
| code commit/tree | `ExperimentRunIdentity.code_commit/code_tree_digest` |
| full preseal digest plus candidate policy digests/owner | domain-separated `ExperimentRunIdentity.policy_digest` |
| venue + sorted symbols | canonical `ExperimentRunIdentity.universe` |
| interval | `ExperimentRunIdentity.timeframe` |
| dataset manifest | `ExperimentRunIdentity.dataset_manifest_digest` |
| PIT/lineage manifest | `ExperimentRunIdentity.pit_manifest_digest` and v1 seal `lineage_digest` |
| cost policy | `ExperimentRunIdentity.cost_model` |
| UTC boundary | v1 seal `boundary_digest/commitment` |
| first-read-not-before | v1 seal `evaluation_not_before` |
| identity digest | v1 seal `experiment_identity_digest`; audit events already bind it |
| preseal/governed seal digest | new governed-v2 seal artifact, v2 receipt, and v2 promotion decision |

5. `GovernedOOSSealV2`, `GovernedOOSAccessReceiptV2`, and `GovernedPromotionDecisionV2` are independent frozen types, not subclasses or aliases of v1 objects. A governed v2 seal record wraps the existing opaque boundary seal and adds the preseal digest, identity digest, legacy seal digest, governance version, and its own canonical digest. Creation refuses draft/missing/mismatched facts before writing either record.
6. V2 access accepts only the governed record plus the same preseal/identity bindings and returns a versioned receipt covering legacy audit head, governed seal digest, and preseal digest.
7. V2 promotion first runs all v2 bindings, then the existing lineage/checkpoint/OOS validation. Only its decision can set `eligible_for_v2_promotion=true`.
8. Legacy `OOSSeal`, `OOSAccessReceipt`, `OOSPromotionStatus`, and `PromotableExperimentDecision` each expose additive machine fields/properties `governance_version=1`, `promotion_scope=LEGACY_RESEARCH_ONLY`, and `eligible_for_v2_promotion=false`. Existing status strings and serialized v1 seal/audit bytes remain compatible. V2 APIs require exact v2 types and reject v1 instances; there is no implicit converter. Static/architecture tests allow `eligible_for_v2_promotion=true` only inside the governed-v2 decision factory.
9. Demo/Testnet/unknown/`EXECUTION_ONLY` source classes are rejected before `READY_FOR_V2_SEAL`.

### Atomic one-time access

- Lock primitive is POSIX `fcntl.flock(fd, LOCK_EX)` on `<store-root>/.oos-access.lock`, supported by the authorized macOS/local-filesystem environment. Every public v1/v2 create/access path and governed wrapper for the same root uses this exact path and lock domain.
- All v1 and v2 access grants acquire the lock before seal/audit load and validation and hold it through append, audit-file flush/fsync, first-create parent-directory fsync, and receipt construction. A receipt is returned only after the durable append. Public readers use the corresponding shared lock or a lock-held internal reader; nested lock acquisition is avoided with private `_..._unlocked` helpers.
- A second concurrent caller observes the committed first grant and records a denied repeated-access event; it never receives an unlock-capable receipt.
- OS-released advisory locks avoid stale-owner claims after process death. Crash before audit commit permits a later valid attempt; crash after commit is fail-closed as already accessed and requires research-only disposition, never an automatic second grant.
- Non-canonical/partial audit bytes remain `NOT_VERIFIABLE` and block access. Recovery preserves the corrupt evidence and cannot reconstruct an `ALLOWED_ONCE` receipt from guesses.
- Multi-process tests synchronize competing callers and prove exactly one v1/v2 receipt; separate fault tests cover dead lock holders, failure before append, failure after durable append, and partial audit bytes.

### Governed seal crash protocol

Creation holds the root lock and uses this fixed order:

1. validate READY preseal, exact identity mapping, boundary, and empty governed target;
2. write the immutable legacy `oos-seal.json` with `xb`, flush/fsync the file, then fsync the parent directory;
3. run the explicit `after_legacy_seal_durable` fault boundary;
4. write `oos-governed-seal-v2.json` with `xb`, flush/fsync it, then fsync the parent directory;
5. reload/recompute both records before returning `GovernedOOSSealV2`.

A crash between steps 2 and 4 leaves an orphan v1 seal. It is permanently `LEGACY_RESEARCH_ONLY`, cannot be upgraded by guessing metadata, and makes v2 retry in that root fail with `ORPHAN_LEGACY_SEAL_NOT_V2_UPGRADABLE`. A new v2 attempt requires a new run/store identity and path. A governed record without its exact legacy record is `NOT_VERIFIABLE`. Fault-injection tests stop after each durable boundary and prove that neither case produces a v2 receipt or eligible decision.

### Versioning, migration, and rollback

- Expand: add snapshot v2 and governed OOS v2 APIs; harden the shared v1 access lock; keep v1 computational behavior/status strings.
- Migrate: only explicitly authorized future experiments use v2. Current run creates only a draft assessment and never calls seal/access.
- Contract: v1 economic promotion removal is unscheduled and requires consumer inventory plus separate authorization.
- Rollback of v2 means disabling its eligibility and returning to `DRAFT_BLOCKED/LEGACY_RESEARCH_ONLY`; tests and all failed/partial audit evidence are retained. Existing immutable seal/audit files are never deleted or rewritten.

### Roles and dirty-worktree exception

- Spec/implementation owner: root Codex agent, restricted to the scoped Alpha/shared experiment files, new tests, this run, and its evidence.
- G2 reviewer: `/root/alpha_contract_arch_review`, independent of authoring; first decision REJECTED and revision-2 decision `APPROVED_WITH_CONDITIONS`.
- RED/GREEN evidence collector: implementation owner via `delivery/scripts/collect_evidence.py`; failure evidence is immutable.
- Independent behavioral verifier/acceptor: a fresh no-author-context reviewer after GREEN/static/impact evidence; not the implementer and not pre-authorized to accept profitability or runtime readiness.

The user authorized this local contract slice after the dirty V4 worktree and protected-file boundary were disclosed. This is a scoped exception to the package clean-tree precondition, not permission to edit or absorb existing changes. Before RED, branch/HEAD/status, target-file hashes, zero target-file diff, untracked run scope, and the 33-test pre-change baseline are rebound to evidence. Any target-file drift after that point stops implementation.

## Active G2 decision

`APPROVED_WITH_CONDITIONS / PASS_WITH_CONDITIONS`. The conditions in this normative Revision 2 are frozen and must be encoded in RED before product implementation. This is not Alpha improvement, OOS readiness, profitability acceptance, or runtime authorization.

Inventory evidence includes:

- `artifacts/evidence/ALPHA-CONTRACT-001-CONSUMERS/20260901T092212187608Z.manifest.json`
- `artifacts/evidence/ALPHA-CONTRACT-001-COMPOSITION/20260901T092117388788Z.manifest.json`
- `artifacts/evidence/ALPHA-CONTRACT-001-OOS-MODULE/20260901T092243425968Z.manifest.json`
- `artifacts/evidence/ALPHA-CONTRACT-001-OOS-BINDINGS/20260901T092304511831Z.manifest.json`
- `artifacts/evidence/ALPHA-CONTRACT-001-PRECHANGE-BASELINE/20260901T092727580182Z.manifest.json` (`33 passed`)
