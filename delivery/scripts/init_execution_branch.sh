#!/usr/bin/env bash
set -euo pipefail
test -z "$(git status --porcelain)" || { echo 'Working tree must be clean'; exit 1; }
HEAD=$(git rev-parse HEAD)
echo "Current HEAD: $HEAD"
if [[ "$HEAD" != "6b0d95dfa67465be421d9a4f5eaa5a406e7c3341" ]]; then
  echo "WARNING: baseline differs from package baseline 6b0d95dfa67465be421d9a4f5eaa5a406e7c3341. Create a baseline delta report before development."
fi
git switch -c refactor/full-system-convergence-v3
echo 'Branch created. Do not enable Mainnet.'
