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
echo "[$(stamp)] data metrics"
"$BEIDOU" data metrics || { echo "[$(stamp)] FAIL data metrics"; fail=1; }
echo "[$(stamp)] pool refresh"
"$BEIDOU" data pool refresh || { echo "[$(stamp)] FAIL pool refresh"; fail=1; }
echo "[$(stamp)] status"
"$BEIDOU" data status || true
#
# 2026-09-10.  `beidou data onchain|index|macro` shipped that day and were HELD OUT of this job, each
# for a reason written out here: #29 index price because NOTHING read the store, #31 on-chain because a
# year of waiting costs the same 49 requests as one day of it, #32 macro because it has no store at all.
# The rule used was "a feed belongs here when something reads it on a schedule, or when waiting makes
# its history unrecoverable or the catch-up disproportionate".
#
# 2026-09-16: the operator WITHDREW all three.  Modules, commands and tests are out of the tree
# (`git show 71863e9e` has them).  So this block no longer lists anything to add, and what is worth
# keeping is the fact that it was RIGHT and that being right here was not enough:
#
#   This job was the only thing in the repository that had correctly judged those three feeds unread,
#   and it recorded that judgement as a comment.  Six days later a review had to re-derive the same
#   conclusion from scratch - `research_cmd._load` joins only metrics and spot, no leaf reads an
#   on-chain/index/macro column, `.beidou/data/` holds no store for any of them - because a comment in
#   a shell script is not something any guard consults.  `test_every_module_is_reachable_from_an_entry_point`
#   passed the whole time: it asks whether a module CAN be run, and all three could.  Nothing anywhere
#   asked whether anything DID.
#
# The precedent that survives them, and the reason this job stayed small: `data metrics` HAS a panel
# reader (`_load(metrics=…)`) and is still not scheduled here.  (`data spot` is, above, since 2026-09-09.)
# This job is the loop's own evidence - klines, funding, spot, pool - and putting a feed nobody reads
# into it turns a red data job into noise.
#
# 2026-09-27: `data metrics` runs above, and the paragraph before this one is why it did not.  It counted
# one reader of the archive, the research panel.  There was a second, and it reads every hour: `report
# daily` compares the archive with the loop's own snapshot for M-011.  With nothing refreshing the archive
# after its one ingest on 09-09, that comparison re-read the buckets of 09-07 for nineteen days and said
# they agreed, and the two symbols that entered the universe on 09-16 could not be compared at all.  The
# 09-10 rule stands - a feed belongs here when something reads it on a schedule - and the count was
# wrong.  Scoped to that reader: with no arguments the command takes every symbol the snapshot store
# holds, each from its own watermark.  The 205-symbol pit ingest stays a research run by hand.  Placed
# before `pool refresh` for no reason but grouping: it reads the snapshot store, which neither touches.
#
# 2026-09-28 (WP-P4): the tests that read `.beidou/` itself - fixtures compared with the archive they
# were cut from, assertions against the loop's own record.  Each skips where that is absent, which is
# CI and every worktree, so the one checkout that ran them was this one, where nobody runs the gates.
# BNX's fixture went red here on 2026-09-25 (that day's `data repair` filled 504 bars into the gap
# before its 2023-02 redenomination seam) and stayed red, seen by nobody.  This is where they run now.
# Which side is right is the operator's call; this only makes the disagreement impossible to miss.
#
# Green means they RAN.  A pass with anything skipped is a FAIL here: every one of them skips for the
# same reason, the archive is not where it looks, and a check that did not run must not say ok.  A
# FAIL pages and sets the exit code, which is safe because this plist has no KeepAlive to relaunch
# it; `report daily` prints the last verdict.
#
# The same `notify` as run_check.sh and run_governance_gate.sh: one implementation of which shape a
# provider reads and whether it took the message (the reasons are over run_check.sh's copy).  The
# default hourly window and the shared state file: this job runs once a night, so a standing red
# pages once a night, and a rerun by hand inside the hour does not page twice.
notify() {
  echo "[$(stamp)] FAIL archive tests: $1"
  if [ -n "${BEIDOU_ALERTS_WEBHOOK_URL:-}${BEIDOU_ALERTS_WEBHOOK_URL_2:-}" ]; then
    BEIDOU_ALERTS_WEBHOOK_URL="${BEIDOU_ALERTS_WEBHOOK_URL:-}" BEIDOU_ALERTS_WEBHOOK_URL_2="${BEIDOU_ALERTS_WEBHOOK_URL_2:-}" \
      "$REPO/.venv/bin/python" -c '
import asyncio, os, sys
from pathlib import Path
from beidou_live.alerts import WebhookAlerts
alerts = WebhookAlerts(
    os.environ["BEIDOU_ALERTS_WEBHOOK_URL"],
    secondary_url=os.environ["BEIDOU_ALERTS_WEBHOOK_URL_2"],
    state_path=Path(sys.argv[3]),
)
sys.exit(0 if asyncio.run(alerts.send(sys.argv[1], key=sys.argv[2])) else 1)
' "北斗归档专属测试失败：$1" "archive-tests" "$SUPPORT/alert-dedup.json" \
      || echo "[$(stamp)] webhook did NOT deliver the line above (or it was a duplicate inside the window)"
  fi
}
echo "[$(stamp)] archive tests"
output="$("$REPO/.venv/bin/python" -m pytest -m archive -p no:cacheprovider -rfEs 2>&1)"
rc=$?
summary="$(printf '%s\n' "$output" | tail -n 1)"
[ -n "$summary" ] || summary="pytest printed nothing (exit $rc)"
case "$rc:$summary" in
  0:*skipped*) rc=skipped ;;
esac
if [ "$rc" = 0 ]; then
  echo "[$(stamp)] ok   archive tests: $summary"
else
  notify "$(printf '%s\n' "$output" | grep -E '^(FAILED|ERROR|SKIPPED) ' | head -n 3 | tr '\n' ' ')$summary"
  printf '%s\n' "$output" | tail -n 15 | sed 's/^/           /'
  fail=1
fi
exit "$fail"
