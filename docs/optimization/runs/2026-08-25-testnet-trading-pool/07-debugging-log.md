# Debugging log — Testnet trading-pool fill race

## Runtime evidence

- Environment: Binance USD-M Testnet (`demo-fapi.binance.com`).
- Pool requested and started: BTCUSDT, ETHUSDT, SOLUSDT.
- BTCUSDT signal was correctly skipped because risk-sized notional was below
  the venue minimum.
- SOLUSDT order `4187303975` filled `0.15` at average price `96.38`.
- ETHUSDT orders `16771413118` and `16771420181` filled `0.009` at
  `2500.63` and `0.010` at `2500.30`; the latter startup UNKNOWN was
  adjudicated from venue facts to durable `FILLED` before certification.
- Durable fill event `lite:4187303975:87857179` is `COMMITTED`.
- Durable order state is `FILLED`; position projection is `0.15 @ 96.38`.
- The ledger transaction has balanced debit and credit postings.
- Exchange protection was restored and read back for both open symbols. SOL
  retained a `0.15` stop and take-profit pair; ETH was resized to a `0.019`
  stop and take-profit pair after the second fill.
- Reconciliation remained `MATCHED`; the supervisor moved to `NO_NEW_RISK`
  solely because of `FILL_FACT_COMMIT_UNKNOWN`.

## Root cause

The user stream committed the TRADE_LITE fill and advanced the in-memory
high-water mark before the REST cumulative `FILLED` observation arrived. The
later observation therefore produced a zero delta and a different event id.
The old code required that exact event id to exist as `COMMITTED`, ignoring the
already committed TRADE_LITE quantity, and raised a false UNKNOWN incident.

## Repair invariant

The later cumulative observation is accepted only when durable committed fill
deltas for the same order cover the full venue cumulative quantity. Any
missing, malformed, insufficient, or unreadable evidence remains UNKNOWN.

## Limitations

This is Testnet execution and recovery evidence only. It is not evidence of
profitability, G7 duration completion, Mainnet readiness, or real-funds safety.

## Restart certification blocker

The first restart attempt correctly rejected the old commit-bound G5
certificate. A fresh G5 run then timed out because its hard-write-held producer
reported `MISSING_SL` and `MISSING_TP` for the existing ETHUSDT and SOLUSDT
positions, although venue and durable reads showed two active Algo protections
for each symbol. Root cause was an unconditional early return in durable
protection restoration when `producer_only` was enabled; monitoring therefore
received an empty local protection projection. The repair hydrates only
ACK-backed facts in memory. Ambiguous facts still reject producer readiness and
cannot trigger cleanup while writes are held.
