"""BD-T19: 评估 G7 30 天无人值守认证窗口并签发证书。

用法:
    python scripts/certification/evaluate_g7.py --window-id <id>
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="G7 Unattended Certification Evaluator")
    parser.add_argument("--window-id", required=True, help="认证窗口 ID")
    parser.add_argument("--force", action="store_true", help="即使未满 30 天也尝试评估")
    parser.add_argument("--fast-forward", action="store_true", help="使用历史数据模拟 30 天窗口（框架验证用）")
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

    if args.fast_forward:
        # Fast-forward remains a framework-only exercise.  It may create a
        # clearly simulated artifact, but it can never produce a certifying
        # PASS or satisfy the production evaluator.
        from beidou_certification.unattended import UnattendedCertification

        engine = UnattendedCertification(str(evidence_dir))
        # Bind the simulation to the operator-supplied ID through the public
        # API.  Do not create an orphan random window and then mutate private
        # state: that can leave misleading state artifacts beside the target
        # evidence and bypass the path/duplicate checks in create_window().
        engine.create_window("v1.0", duration_days=30, window_id=args.window_id)
        print("\n⏩ FAST-FORWARD MODE: Simulating 30-day window with historical data...")
        result = engine.fast_forward(args.window_id, duration_days=30)
        if result.get("status") == "PASS":
            result["status"] = "NOT_VERIFIABLE"
        print(f"\nEvaluation: {result.get('status', 'NOT_VERIFIABLE')}")
        print("Reason: fast-forward evidence is simulation-only and cannot certify G7")
        return 1

    # Normal evaluation is intentionally read-only.  A new process must not
    # create an empty in-memory window and accidentally evaluate that instead
    # of the persisted run.  The producer must have persisted a certificate;
    # this verifier then checks its semantic binding independently.
    manifest_path = evidence_dir / f"{args.window_id}-manifest.json"
    certificate_path = evidence_dir / f"{args.window_id}-g7-certificate.json"
    if not manifest_path.exists() or not certificate_path.exists():
        print("\nEvaluation: NOT_VERIFIABLE")
        print("Reason: persisted G7 manifest and certificate are both required")
        return 1

    with open(manifest_path) as f:
        manifest = json.load(f)
    with open(certificate_path) as f:
        certificate = json.load(f)

    from beidou_certification.gate_verifier import verify_g7_certificate

    expected_commit = manifest.get("commit", "")
    expected_g5_hash = manifest.get("g5_certificate_hash", "")
    verification = verify_g7_certificate(
        certificate,
        expected_commit=expected_commit,
        expected_g5_hash=expected_g5_hash,
        now=datetime.now(timezone.utc),
        minimum_days=float(manifest.get("duration_days", 30)),
    )

    print(f"\nEvaluation: {verification.status}")
    if verification.failures:
        print(f"Failures: {', '.join(verification.failures)}")

    if verification.passed:
        print("\nG7 CERTIFICATE VERIFIED")
        print(f"  Evidence hash: {certificate.get('evidence_hash', 'N/A')}")
        print(f"  SLI pass rate: {certificate.get('summary', {}).get('sli_pass_rate', 'N/A')}")
        print("  Mainnet: PROHIBITED")
    else:
        print("\nG7 CERTIFICATION NOT_VERIFIABLE")
        print("  Persisted evidence must be repaired or the window restarted.")

    return 0 if verification.passed else 1


if __name__ == "__main__":
    sys.exit(main())
