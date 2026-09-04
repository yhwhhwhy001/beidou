#!/bin/bash
# Daily refresh of the research data the loop's evidence is built from.
#
# The audit found the parquet store sixteen hours stale with one live pool member missing entirely, and
# nothing scheduled to refresh it.  Research therefore validated on data that stopped before the book it
# was validating started trading.  This job closes that: klines and funding for the current pool, then a
# pool refresh so `universe.json` and the store agree.  It touches no account and places no orders.
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
echo "[$(stamp)] pool refresh"
"$BEIDOU" data pool refresh || { echo "[$(stamp)] FAIL pool refresh"; fail=1; }
echo "[$(stamp)] status"
"$BEIDOU" data status || true
exit "$fail"
