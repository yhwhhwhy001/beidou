#!/bin/bash
# Launch wrapper for the unattended demo loop (used by com.beidou.live.plist).
#
# Secrets: put `export BEIDOU_DEMO_API_KEY=...` / `export BEIDOU_DEMO_API_SECRET=...`
# in ~/Library/Application Support/beidou/env.sh (chmod 600).  Never commit them.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUPPORT="$HOME/Library/Application Support/beidou"
mkdir -p "$SUPPORT"
if [ -f "$SUPPORT/env.sh" ]; then
  # shellcheck disable=SC1091
  source "$SUPPORT/env.sh"
fi
cd "$REPO"
# Exit code 0 (clean stop, e.g. --cycles reached) is not relaunched; any failure is, after ThrottleInterval.
exec "$REPO/.venv/bin/beidou" live run --profile config/live.demo.yaml --immediate "$@"
