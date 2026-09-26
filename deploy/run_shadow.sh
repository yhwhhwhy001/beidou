#!/bin/bash
# DL-G5: soak a CANDIDATE registry beside the armed loop, writing nothing the armed loop reads.
#
# The canary answers one question - would this registry, deployed, behave like a working deployment? -
# and deliberately not whether the candidate has any edge (KILL-AR-04).  Reading it as an alpha filter
# is how a good sleeve gets blocked by a venue hiccup and the block gets recorded against the sleeve.
#
# Three isolations, each of which has to hold on its own:
#   --dry-run     no venue writes, and `--state-dir` refuses to run without it or --paper;
#   --state-dir   a separate state.json, cycles.jsonl and heartbeat, so the armed loop's record is
#                 untouched even if this process crashes mid-write;
#   --registry    a candidate file, so the armed loop's own registry is never opened for writing here.
#
# It does NOT restart, promote or write the real registry.  `beidou governance apply` does that, and
# only while the autonomy switch is on.
#
# It stops at the end of a soak because it asks the record first - not because of KeepAlive.  `live run`
# exits 1 when any of its cycles failed, launchd relaunches every non-zero exit, and on 2026-09-23 that
# silently began a second 168 in the same `cycles.jsonl` (and a clean exit would still meet RunAtLoad at
# the next login).  So before every start: a finished round exits 0, which launchd leaves down; an
# unfinished one (a crash, a reboot) runs only the cycles it still owes, so one record stays one soak.
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

CANDIDATE="${1:-config/alpha_registry.candidate.yaml}"
STATE_DIR="${2:-.beidou/live-shadow}"

if [ ! -f "$CANDIDATE" ]; then
  echo "shadow: no candidate registry at $CANDIDATE" >&2
  exit 64
fi
# The one check worth making before spending 168 hours: refuse to soak the file the armed loop reads.
if [ "$(cd "$(dirname "$CANDIDATE")" && pwd)/$(basename "$CANDIDATE")" = "$REPO/config/alpha_registry.yaml" ]; then
  echo "shadow: refusing to soak the live registry; copy it and edit the copy" >&2
  exit 64
fi

# The soak's length is the canary's (`SOAK_CYCLES`), read through the same cut it scores with; a second
# number here is how a launcher and its reader end up disagreeing about where a soak ends.
if ! CYCLES="$(.venv/bin/beidou governance canary --remaining --shadow-dir "$STATE_DIR")"; then
  echo "shadow: could not read the soak record for $STATE_DIR; not starting" >&2
  exit 70
fi
case "$CYCLES" in
  '' | *[!0-9]*)
    echo "shadow: expected a cycle count from the canary, got '$CYCLES'; not starting" >&2
    exit 70
    ;;
esac
if [ "$CYCLES" -eq 0 ]; then
  echo "shadow: the latest soak in $STATE_DIR is finished; not starting another."
  echo "shadow: score it with 'beidou governance canary'; to soak again, move the record away (docs/RUNBOOK.md)."
  exit 0
fi
echo "shadow: soaking $CANDIDATE for $CYCLES cycles into $STATE_DIR"
exec .venv/bin/beidou live run \
  --profile config/live.demo.yaml \
  --registry "$CANDIDATE" \
  --dry-run \
  --state-dir "$STATE_DIR" \
  --cycles "$CYCLES"
