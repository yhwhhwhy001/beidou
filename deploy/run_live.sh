#!/bin/bash
# Launch wrapper for the unattended demo loop (used by com.beidou.live.plist).
#
# Secrets: BEIDOU_BINANCE_API_KEY / BEIDOU_BINANCE_API_SECRET must be in the environment.
# Preferred: ~/Library/Application Support/beidou/env.sh (chmod 600).  Otherwise the
# operator's existing ~/.zshrc exports are picked up below.  Never commit the values.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUPPORT="$HOME/Library/Application Support/beidou"
mkdir -p "$SUPPORT"
if [ -f "$SUPPORT/env.sh" ]; then
  # shellcheck disable=SC1091
  source "$SUPPORT/env.sh"
elif [ -f "$HOME/.zshrc" ]; then
  # Fall back to the operator's existing exports instead of copying the secret to a second file.
  # Only literal BEIDOU_* export lines are evaluated; aliases and interactive-only code are ignored.
  eval "$(grep -E '^export BEIDOU_[A-Z0-9_]+=' "$HOME/.zshrc" || true)"
fi
if [ -z "${BEIDOU_BINANCE_API_KEY:-}" ] || [ -z "${BEIDOU_BINANCE_API_SECRET:-}" ]; then
  echo "run_live.sh: demo credentials are not in the environment" >&2
  exit 78   # EX_CONFIG; launchd will not hot-loop on a configuration error
fi
cd "$REPO"
# Exit code 0 (clean stop, e.g. --cycles reached) is not relaunched; any failure is, after ThrottleInterval.
# --armed is what makes a real-order run explicit (DL-L1).  This launcher is the only caller that
# should carry it; a loop started by hand in a worktree has to type it, which is the point.
exec "$REPO/.venv/bin/beidou" live run --profile config/live.demo.yaml --immediate --armed "$@"
