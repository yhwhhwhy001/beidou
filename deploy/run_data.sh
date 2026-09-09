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
exit "$fail"
