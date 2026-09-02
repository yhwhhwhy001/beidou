# Market data integrity report

- Scope: Binance USD-M `BTCUSDT`, `ETHUSDT`, `BNBUSDT`, `SOLUSDT`; `1h`; UTC; `[2026-08-01T00:00:00Z, 2026-08-31T00:00:00Z)`.
- Isolation: the authorized dataset is stored under `artifacts/datasets/alpha-return-2026-08-01_2026-08-31-v1/`; the legacy `.beidou/data/klines` tree was not overwritten and remains ineligible.
- Primary evidence: 120 official daily archive ZIPs and 120 published checksum files. A separate checksum command verified all 120 files.
- Reconciliation evidence: the unauthenticated public USD-M K-line API was persisted separately and compared offline against the archive. Each symbol matched all 720 rows and all raw fields; maximum close deviation was `0 bps`.
- Completeness/integrity: each symbol has 720 unique, ordered, exactly hourly, closed bars with legal finite OHLCV. Schema-v2 manifests bind endpoint, source class, retrieval time, environment, use, range, row count, canonical content hash, and reconciliation evidence.
- Historical/live boundary: reconnect and real-time parity are `NOT_APPLICABLE` to this immutable historical acquisition, for the concrete reason that no streaming or live store was in scope. Backfill rejection and byte-preservation behavior remain covered by the earlier data-boundary suite. The separate August PIT/feature-lineage artifact is a retrospective reconstruction and does not convert these bytes into a contemporaneous source vintage.
- Symbol lifecycle: the four symbols and dates were explicitly user-scoped and present throughout the frozen range; broader listing/delisting and survivorship coverage was not evaluated.
- Diagnostics: maximum absolute hourly close return was about `3.99%` BTC, `5.85%` ETH, `3.61%` BNB, and `4.97%` SOL; lag-1 return autocorrelation ranged from about `-0.018` to `0.106`. The extreme legacy artifacts are not present.
- Independence limit: archive and API are different paths/source classes but both are operated by Binance. This is not institutional independence, and acquisition authenticity also depends on the recorded DNS/transport evidence.
- Evidence: `ALPHA-DATA-002-ACQUIRE-FROZEN-MONTH`, `ALPHA-DATA-002-INDEPENDENT-CHECKSUMS`, `ALPHA-DATA-002-INDEPENDENT-VERIFY2`, and `ALPHA-DATA-002-ACQUISITION-REGRESSION` (`90 passed`); the later research-evidence regression covering this dataset and PIT/governance artifacts passed `43` tests.
- Dataset assessment: `PASS` for the implemented market-data admission boundary on this frozen dataset.
- Domain gate decision: `PASS_WITH_CONDITIONS` because both reconciliation paths share one operator and broader symbol-lifecycle/historical-live parity are outside scope. This does not by itself complete E0, validate a factor, or prove profitability.
