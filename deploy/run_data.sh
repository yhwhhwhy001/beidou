#!/bin/bash
# Daily refresh of the research data the loop's evidence is built from.
#
# The audit found the parquet store sixteen hours stale with one live pool member missing entirely, and
# nothing scheduled to refresh it.  Research therefore validated on data that stopped before the book it
# was validating started trading.  This job closes that: klines and funding for the current pool, then a
# pool refresh so `universe.json` and the store agree.  It touches no account and places no orders.
#
# `data spot` joined it on 2026-09-09, and the reason is the same one with the feed changed.  DL-D5
# shipped the store, the mapping, the contract and the `basis` leaf, and nothing ever RAN the ingest:
# `.beidou/data/` held no `spot_klines/` and no `spot_map.json`, so `research mine` narrowed the whole
# basis family away on every run and the live gate refused every candidate that reads spot - both
# correctly, both silently, and neither because anything was wrong with the data.
#
# Two costs the operator should know before the first run rather than after it.  The command's default
# is every symbol the kline store holds (880 here against `data sync`'s 32 volume-ranked candidates), and
# 362 of 528 perpetuals have a spot leg, so the FIRST run backfills from `history_start` (2021-01) and
# takes hours; every run after it is one REST tail per mapped symbol.  Narrow it with `--symbols` or
# pre-run it by hand if that does not fit the window.  It is placed after `data sync` because it maps
# what the perp store holds, and before `pool refresh` because neither reads the other's output.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUPPORT="$HOME/Library/Application Support/beidou"
if [ -f "$SUPPORT/env.sh" ]; then
  # shellcheck disable=SC1091
  source "$SUPPORT/env.sh"
elif [ -f "$HOME/.zshrc" ]; then
  eval "$(grep -E '^export BEIDOU_[A-Z0-9_]+=' "$HOME/.zshrc" || true)"
fi
cd "$REPO" || exit 78
BEIDOU="$REPO/.venv/bin/beidou"
[ -x "$BEIDOU" ] || BEIDOU="beidou"
stamp() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }
fail=0
echo "[$(stamp)] data sync"
"$BEIDOU" data sync || { echo "[$(stamp)] FAIL data sync"; fail=1; }
echo "[$(stamp)] data spot"
"$BEIDOU" data spot || { echo "[$(stamp)] FAIL data spot"; fail=1; }
echo "[$(stamp)] pool refresh"
"$BEIDOU" data pool refresh || { echo "[$(stamp)] FAIL pool refresh"; fail=1; }
echo "[$(stamp)] status"
"$BEIDOU" data status || true
#
# 2026-09-10.  `beidou data onchain|index|macro` shipped today and were HELD OUT of this job.  The rule
# used, so the next person decides rather than re-derives: a feed belongs here when something reads it
# on a schedule, or when waiting makes its history unrecoverable or the catch-up disproportionate.
#
#   #29 index price   - the strongest claim of the three: 528/528 perpetuals covered, the same hourly
#                       grid as the klines above, so a skipped day is a 24-bar hole of exactly the kind
#                       `KlineStore.gaps` exists to catch.  Held out anyway because NOTHING reads the
#                       store - `_load` does not join it, and the command itself prints "live gate: 0/4
#                       columns" every run for want of an archive-vs-REST verification.  Its first run
#                       is also a backfill from history_start (2021-01), not a tail.
#                       Add it the day §9A item 5 lands (the index/spot join into the panel):
#                         "$BEIDOU" data index || { echo "[$(stamp)] FAIL data index"; fail=1; }
#   #31 on-chain      - one request per ASSET covers ANY window, so a year of waiting costs the same
#                       49 requests as one day of it: scheduling buys nothing a backfill would not.
#                       Coverage is 49/528 and exactly one column can reach live even on a PASS.
#                       Add it when a leaf actually reads an on-chain column, with an overlapping
#                       window so a missed day repairs itself:
#                         "$BEIDOU" data onchain --from "$(date -u -v-7d +%F)" --to "$(date -u +%F)"
#   #32 macro         - monthly releases, and NO store by its author's scope call, so a daily run would
#                       leave nothing behind at all.  ALFRED re-serves every vintage on request, and
#                       BLS v1's anonymous budget is sized for re-verifying a contract rather than for
#                       a schedule.  It belongs in a re-verification, never in a daily ingest.
#
# The precedent this follows rather than breaks: `data metrics` HAS a panel reader (`_load(metrics=…)`)
# and `data spot` has half of one, and neither is scheduled here.  This job is the loop's own evidence -
# klines, funding, pool - and putting a feed nobody reads into it turns a red data job into noise.
exit "$fail"
