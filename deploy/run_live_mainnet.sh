#!/bin/bash
# Launch wrapper for the small mainnet calibration loop (used by com.beidou.live-mainnet.plist; 切换方案 M5).
#
# demo 的循环由 run_live.sh 起，这份不改它。两份并排：凭据变量名不同、状态目录不同、账户锁按 key 分开。
#
# Secrets: BEIDOU_MAINNET_API_KEY / BEIDOU_MAINNET_API_SECRET, and the capital cap BEIDOU_MAINNET_MAX_EQUITY_USDT.
# They are read the same way run_live.sh reads demo's: ~/Library/Application Support/beidou/env.sh if it exists,
# otherwise the literal `export BEIDOU_*` lines of ~/.zshrc.  Never commit the values, and do not create env.sh
# for this: the file existing stops ~/.zshrc being read for BOTH loops, so it must hold every BEIDOU_* variable
# first (CLAUDE.md).
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUPPORT="$HOME/Library/Application Support/beidou"
mkdir -p "$SUPPORT"
if [ -f "$SUPPORT/env.sh" ]; then
  # shellcheck disable=SC1091
  source "$SUPPORT/env.sh"
elif [ -f "$HOME/.zshrc" ]; then
  eval "$(grep -E '^export BEIDOU_[A-Z0-9_]+=' "$HOME/.zshrc" || true)"
fi
for name in BEIDOU_MAINNET_API_KEY BEIDOU_MAINNET_API_SECRET BEIDOU_MAINNET_MAX_EQUITY_USDT; do
  if [ -z "${!name:-}" ]; then
    echo "run_live_mainnet.sh: ${name} is not in the environment" >&2
    exit 78   # EX_CONFIG
  fi
done
cd "$REPO"
# The guard refuses mainnet without the sign-off file anyway (M1).  Checked here too so a missing file reads as
# what it is, a configuration state, instead of a stack trace relaunched every ThrottleInterval.
if [ ! -f governance/MAINNET_ENABLED ]; then
  echo "run_live_mainnet.sh: governance/MAINNET_ENABLED is absent - mainnet is not released" >&2
  exit 78
fi
# No D-041 bridge: mainnet starts only on evidence that clears its own gate.  `--mainnet` is the guard's second
# condition; `--armed` is what makes a real-order run explicit (DL-L1), as in run_live.sh.
exec "$REPO/.venv/bin/beidou" live run --profile config/live.mainnet.yaml --immediate --armed --mainnet "$@"
