#!/bin/bash
# Periodic health check for the unattended loop (M-011 + the clock guard).
#
# `live status --check` fails on a stale heartbeat, a loop stuck in ERROR, or a host clock that has drifted
# from the venue.  `live verify --check` recomputes the last cycle's model output from public data and fails
# if it no longer reproduces - the monitor that KILL-027 was missing.  Failures go to the alert webhook when
# one is configured, and always to this job's log.
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
stamp() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }
# This is the path that tells the operator the LOOP IS DOWN, and it was the second copy of a bug that
# made both alert paths silent.  It posted Slack's flat {"text": ...} to a Lark bot, which answers
# HTTP 200 with {"code": 19002, "msg": "params error, msg_type need"} and drops the message - so
# `curl -f` succeeded, the `|| echo` never fired, and the script reported nothing wrong while nothing
# was delivered.  Measured 2026-09-07 against the real bot, not inferred.
#
# It now calls the same `WebhookAlerts` the loop uses rather than hand-rolling a third copy: one
# implementation of "which shape does this provider read" and "did it actually take the message".
notify() {
  echo "[$(stamp)] FAIL $1: $2"
  if [ -n "${BEIDOU_ALERTS_WEBHOOK_URL:-}" ]; then
    "$REPO/.venv/bin/python" -c '
import asyncio, sys
from pathlib import Path
from beidou_live.alerts import WebhookAlerts
# DL-L3 same-source dedup.  KILL-R7 counted 36 identical FAIL lines over 36 hours and they came from
# THIS job: a fresh process every hour, so the in-memory dedup dict is empty every time.  The state
# file is what makes the window mean anything here; the key is the check name, so a standing problem
# re-announces itself once an hour instead of once a run.
alerts = WebhookAlerts(sys.argv[1], state_path=Path(sys.argv[4]))
sys.exit(0 if asyncio.run(alerts.send(sys.argv[2], key=sys.argv[3])) else 1)
' "$BEIDOU_ALERTS_WEBHOOK_URL" "北斗巡检失败（$1）：$2" "check-$1" "$SUPPORT/alert-dedup.json" \
      || echo "[$(stamp)] webhook did NOT deliver the line above (or it was a duplicate inside the window)"
  fi
}
failed=0
for check in status verify; do
  if output="$("$REPO/.venv/bin/beidou" live "$check" --check 2>&1)"; then
    echo "[$(stamp)] ok   $check"
  else
    failed=1
    notify "$check" "$(echo "$output" | tail -n 3 | tr '\n' ' ')"
  fi
done
# The drift verdict used to be computed and then discarded: nothing ever sent it anywhere.  `report daily
# --check` exits non-zero on an ALERT - equity drift, per-strategy income drift (M-002/M-010), or more
# construction changes in a week than the plan allows.
if output="$("$REPO/.venv/bin/beidou" report daily --check 2>&1)"; then
  echo "[$(stamp)] ok   report"
else
  failed=1
  notify "report" "$(echo "$output" | tail -n 3 | tr '\n' ' ')"
fi
# L3's criterion, which nothing computed until 2026-09-09: the soak ran for a rule that lived in prose.
# REPORTED, NOT GATED, and the distinction is deliberate.  L3 is a criterion that accumulates over seven
# days, so it is FALSE for the first six by construction; wiring it into `failed` would page the operator
# hourly for a week and teach them that this alert means nothing.  So it prints and `$failed` is left
# alone.  The command's own `--check` does gate - on the no-decision reading, not the literal one - and
# is there for whatever finally consumes this (a promotion gate, a weekly report), not for the hourly.
if [ -f "$REPO/.beidou/paper-l3/cycles.jsonl" ]; then
  if output="$("$REPO/.venv/bin/beidou" live soak --check 2>&1)"; then
    echo "[$(stamp)] ok   soak"
  else
    echo "[$(stamp)] soak not yet passing:"; echo "$output" | sed "s/^/           /"
  fi
fi
exit "$failed"
