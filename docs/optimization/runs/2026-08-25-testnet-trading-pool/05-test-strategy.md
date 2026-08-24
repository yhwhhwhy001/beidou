# Test strategy — Testnet trading-pool fill race

## Scope

Validate the race where a Binance Testnet `TRADE_LITE` event commits a fill
before the later cumulative `FILLED` order update. The later update may have a
zero delta because the in-memory high-water mark already equals the venue
cumulative quantity.

## Required behavior

- A durable set of `COMMITTED` fill deltas for the same order may certify the
  later cumulative quantity and allow terminal order processing.
- Insufficient committed quantity must remain `FILL_FACT_COMMIT_UNKNOWN`.
- Missing or unreadable durable fill facts must remain fail-closed.
- No risk parameter, leverage limit, or write-authority rule is relaxed.

## Fresh verification

- Red test before implementation:
  `python -m pytest tests/unit/test_full_repository_coverage_engine_runtime.py::test_engine_process_fill_recovery_exception_and_cumulative_race_paths -q`
  failed because the committed TRADE_LITE case was marked UNKNOWN.
- Targeted green test: the same command passed after the fix.
- Impacted regression:
  `python -m pytest tests/unit/test_full_repository_coverage_engine_runtime.py tests/unit/test_full_repository_coverage_engine_deep.py -q`
  — 23 passed.
- Full offline unit and architecture run:
  `python -m pytest tests/unit tests/architecture -q`
  — 3931 passed and 5 governance failures. The five failures were preserved,
  attributed to the changed engine digest/line identities plus three existing
  unannotated Testnet branches, and then rerun after governance refresh.
- Governance rerun:
  `python -m pytest tests/architecture/test_write_capability_registry.py tests/architecture/test_write_registry_independent_oracle.py tests/architecture/test_testnet_exemptions_registry.py -q`
  — 29 passed.
- Target lint: `python -m ruff check --ignore SIM105 ...` passed. The ignored
  SIM105 finding is an unrelated existing line in `engine.py`.

## Restart certification regression

- Added a G5 producer regression requiring ACK-backed ACTIVE durable
  protections to hydrate the in-memory protection projection without any
  persistent write or venue cleanup.
- Added the negative counterpart: a venue-missing protection must keep the
  producer unready and must not remove or adopt durable rows.
- Red phase: both tests failed against the unconditional producer early return.
- Green and impacted regressions: 192 tests passed across protection recovery,
  readiness, G5 producer/preflight, user-stream recovery, replay authorization,
  and protection ownership convergence.
