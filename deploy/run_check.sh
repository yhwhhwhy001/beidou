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
# `verify`'s tolerance is 1e-9, and the reproduction's own float-level disagreement measures about
# 2e-8 - twenty times larger - so M-011 can fail on nothing at all.  Setting the tolerance from that
# needs the distribution, and `verify-failures.jsonl` cannot supply it: it records only runs that
# EXCEEDED the tolerance, which is a sample censored at the very number in question.  The successful
# runs are the other half, and this is the cheapest place to keep them - one field on a line that is
# already written, into a log that is already append-only.  Raising a tolerance to quiet an alarm is
# the move that most deserves suspicion, so the number has to exist before anyone picks one.
reading() {
  [ "$1" = verify ] || return 0
  printf ' (max_contribution_diff %s)' "$(printf '%s' "$2" | "$REPO/.venv/bin/python" -c \
    'import json,sys; print(json.load(sys.stdin)["max_contribution_diff"])' 2>/dev/null || echo '?')"
}
failed=0
for check in status verify; do
  if output="$("$REPO/.venv/bin/beidou" live "$check" --check 2>&1)"; then
    echo "[$(stamp)] ok   $check$(reading "$check" "$output")"
  else
    failed=1
    notify "$check" "$(echo "$output" | tail -n 3 | tr '\n' ' ')"
  fi
done
# The plists launchd actually reads.  `deploy/com.beidou.paper-l3.plist` carried `--paper` inside an XML
# comment for a day: two dashes end a comment, so the file was not well-formed XML.  launchd took it
# anyway (CFPropertyList is more forgiving than expat) and the soak ran, so nothing said a word - but a
# file that loads only because of one parser's tolerance stops loading the day that changes, and the L3
# soak is a seven-day accumulator that dies silently.  The suite checks the repo copies; it may not
# resolve the real home (test_tests_never_touch_the_real_app_support.py forbids it, correctly), so the
# installed copies are checked here.  Gated, unlike the soak line below: this is a static file that is
# either valid or is not, the fix is one edit, and there is no window over which it is expected to fail.
if output="$("$REPO/.venv/bin/python" -c '
import plistlib, sys
from pathlib import Path
bad = []
for path in sorted((Path.home() / "Library" / "LaunchAgents").glob("com.beidou.*.plist")):
    try:
        payload = plistlib.loads(path.read_bytes())
    except Exception as error:
        bad.append(f"{path.name}: {error}")
        continue
    if not payload.get("Label") or not payload.get("ProgramArguments"):
        bad.append(f"{path.name}: parses but names no Label/ProgramArguments")
print("\n".join(bad))
sys.exit(1 if bad else 0)
' 2>&1)"; then
  echo "[$(stamp)] ok   plists"
else
  failed=1
  notify "plists" "$(echo "$output" | tail -n 3 | tr '\n' ' ')"
fi
# Which plists this repo ships that the machine is NOT running.  REPORTED, NOT GATED, and for the
# same reason the soak line below is: some of them are meant to be off (`shadow` and `paper-l3` are
# started and stopped by hand), so gating would page hourly for a deliberate choice.
#
# What made it a line.  2026-09-17 shipped `com.beidou.forward-board.plist` - the daily read of the
# candidate forward board - and nothing installed it.  The board had a candidate on it, the command
# worked, the tests were green, and the instrument was inert: a file in `deploy/` is not a job.  That
# is the third shape of the same error in one day (a fixture that tested a contract nothing produced,
# a shell line the command could not accept), so it gets a line that says it out loud once an hour.
# 空输出有两种来源：真的没有缺的，和这个检查根本没跑起来。按「输出为空 = ok」写，第二种会被报成
# 第一种——而那正是这一整条要防的错误本身。所以看退出码，不看输出是不是空的。
if missing_plists="$("$REPO/.venv/bin/python" -c '
import sys
from pathlib import Path
repo, home = Path(sys.argv[1]), Path.home() / "Library" / "LaunchAgents"
shipped = {p.name for p in (repo / "deploy").glob("com.beidou.*.plist")}
print(" ".join(sorted(shipped - {p.name for p in home.glob("com.beidou.*.plist")})))
' "$REPO" 2>&1)"; then
  if [ -n "$missing_plists" ]; then
    echo "[$(stamp)] not installed (deliberate for some; a file in deploy/ is not a job): $missing_plists"
  else
    echo "[$(stamp)] ok   plists installed"
  fi
else
  echo "[$(stamp)] plists: 装没装这条检查自己没跑起来（$(echo "$missing_plists" | tail -n 1)）"
fi

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
