#!/usr/bin/env bash
set -euo pipefail
TASK=BD-T00
run(){ python delivery/scripts/collect_evidence.py --task "$TASK" -- "$@"; }
run python -m py_compile beidou_core/guard.py
run python -m compileall -q beidou_* apps
run ruff check beidou_* apps tests scripts
run ruff format --check beidou_* apps tests scripts
run mypy beidou_* apps --no-error-summary
run pytest --collect-only -q
run pytest tests/unit tests/integration tests/architecture -q
