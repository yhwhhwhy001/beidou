# Implementation log — Testnet trading-pool fill race

## Changes

- Added `_committed_fill_qty_covers` to prove, from durable `COMMITTED` fill
  deltas, that a later venue cumulative fill is already fully accounted for.
- Updated the zero-delta `FILLED` path to accept either the exact committed
  cumulative event or complete committed quantity coverage for the same order.
- Kept missing, malformed, insufficient, and unreadable evidence fail-closed.
- Added positive and negative regression coverage for the observed race.
- Added missing governance annotations for three pre-existing Testnet branches.
- Mechanically refreshed `config/write-capability-registry.json` with
  `python scripts/rebuild_write_registry.py` after the governed source changed.
- Replaced the G5 producer's unconditional protection-restore success with a
  read-only hydration path using durable ACTIVE rows plus venue account and
  Algo inventory facts.
- Preserved fail-closed behavior for missing inventory, stale quantity,
  generation conflict, side mismatch, flat-position residue, and PENDING rows
  requiring durable adoption. Producer mode never performs the corresponding
  cleanup, cancellation, or persistence update.

## Explicit non-changes

- No API credentials were read or printed.
- No Mainnet endpoint, transfer, or withdrawal action was used.
- No signed risk limit, signal threshold, leverage cap, or position sizing
  parameter was changed.
