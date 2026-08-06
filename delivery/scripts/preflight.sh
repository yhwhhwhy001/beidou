#!/usr/bin/env bash
set -euo pipefail
printf '=== Beidou execution preflight ===
'
git status --short
test -z "$(git status --porcelain)" || { echo 'Working tree must be clean'; exit 1; }
printf 'branch: '; git branch --show-current
printf 'head: '; git rev-parse HEAD
python --version
python -m compileall -q beidou_* apps
python delivery/scripts/validate_package.py
printf 'Preflight PASS. Mainnet remains PROHIBITED.
'
