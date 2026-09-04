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
notify() {
  echo "[$(stamp)] FAIL $1: $2"
  if [ -n "${BEIDOU_ALERTS_WEBHOOK_URL:-}" ]; then
    curl -fsS -X POST -H 'Content-Type: application/json' \
      --data "$(python3 -c 'import json,sys; print(json.dumps({"text": sys.argv[1]}))' "beidou check FAILED ($1): $2")" \
      "$BEIDOU_ALERTS_WEBHOOK_URL" >/dev/null 2>&1 || echo "[$(stamp)] webhook delivery failed"
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
exit "$failed"
