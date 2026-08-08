"""BD-T19: 评估 G7 30 天无人值守认证窗口并签发证书。

用法:
    python scripts/certification/evaluate_g7.py --window-id <id>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="G7 Unattended Certification Evaluator")
    parser.add_argument("--window-id", required=True, help="认证窗口 ID")
    parser.add_argument("--force", action="store_true", help="即使未满 30 天也尝试评估")
    args = parser.parse_args()

    evidence_dir = Path("artifacts/evidence/g7")

    # Load window state
    state_path = evidence_dir / f"{args.window_id}-state.json"
    if not state_path.exists():
        print(f"ERROR: Window state not found: {state_path}")
        return 1

    with open(state_path) as f:
        state = json.load(f)

    print("=" * 60)
    print("G7 UNATTENDED CERTIFICATION EVALUATION")
    print("=" * 60)
    print(f"Window ID: {state['window_id']}")
    print(f"Status: {state['status']}")
    print(f"Elapsed: {state['elapsed_days']:.1f} days")
    print(f"Incidents: {state['incident_count']}")
    print(f"SLI samples: {state['sli_sample_count']}")
    print(f"Reports: {state['report_count']}")
    print(f"Resets: {state['reset_count']}")

    from beidou_certification.unattended import UnattendedCertification

    engine = UnattendedCertification(str(evidence_dir))

    # Load window into engine
    window = engine.create_window("v1.0")
    window.window_id = args.window_id
    window.duration_days = 30
    engine._windows[args.window_id] = window
    engine._active_window = window

    # Evaluate
    result = engine.evaluate(args.window_id)

    print(f"\nEvaluation: {result['status']}")
    if 'reason' in result:
        print(f"Reason: {result['reason']}")

    if result['status'] == 'PASS':
        print(f"\nG7 CERTIFICATE ISSUED")
        print(f"  Evidence hash: {result.get('evidence_hash', 'N/A')}")
        print(f"  SLI pass rate: {result.get('summary', {}).get('sli_pass_rate', 'N/A')}")
        print(f"  Total incidents: {result.get('summary', {}).get('total_incidents', 0)}")
        print(f"\n  DISCLAIMER: This certificate does NOT grant Mainnet access.")
        print(f"  G8 requires separate human approval and capital ladder plan.")
    elif result['status'] == 'FAIL':
        print(f"\nG7 CERTIFICATION FAILED")
        print(f"  Reason: {result['reason']}")
        print(f"  Window must be restarted after fixing root cause.")
    else:
        print(f"\nG7 CERTIFICATION NOT_VERIFIABLE")
        print(f"  Reason: {result['reason']}")
        print(f"  Continue running unattended until 30 days complete.")

    return 0 if result['status'] == 'PASS' else 1


if __name__ == "__main__":
    sys.exit(main())
