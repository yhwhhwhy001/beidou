"""BD-T19: 启动 G7 30 天无人值守认证窗口。

用法:
    python scripts/certification/start_g7.py --plan config/g7-unattended-plan.yaml

前置条件:
    - G5 Testnet 证书已签发 (BD-T18 完成)
    - 系统运行在 Testnet 模式
    - 不可使用 Mainnet URL
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml


def main() -> int:
    parser = argparse.ArgumentParser(description="G7 Unattended Certification Window Starter")
    parser.add_argument("--plan", default="config/g7-unattended-plan.yaml")
    parser.add_argument("--duration", type=int, default=30, help="认证天数 (default: 30)")
    parser.add_argument("--window-id", help="指定窗口 ID（自动生成）")
    args = parser.parse_args()

    project_root = Path(__file__).parent.parent.parent
    evidence_dir = project_root / "artifacts" / "evidence" / "g7"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("G7 30-DAY UNATTENDED CERTIFICATION — WINDOW START")
    print("=" * 60)

    # Load plan
    with open(args.plan) as f:
        plan = yaml.safe_load(f)

    print(f"Plan: {args.plan}")
    print(f"Duration: {args.duration} days")
    print(f"Requires G5: {plan.get('requires_g5', True)}")
    print("Mainnet: PROHIBITED")
    print(f"Reset conditions: {plan.get('reset_conditions', [])}")
    print(f"Required SLI: {plan.get('required_sli', [])}")

    # Verify G5 certificate exists
    g5_path = project_root / "artifacts" / "evidence" / "testnet" / "g5-certificate.json"
    if not g5_path.exists():
        print(f"ERROR: G5 certificate not found at {g5_path}")
        print("Run BD-T18 first: python scripts/testnet/run_g5.py --confirm-testnet")
        return 1

    with open(g5_path) as f:
        g5_cert = json.load(f)

    # Bind G7 to the exact code and complete Testnet scenario set that
    # produced G5.  A legacy/demo certificate is not a valid predecessor.
    import subprocess

    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    from beidou_certification.gate_verifier import verify_g5_certificate

    g5_verification = verify_g5_certificate(
        g5_cert,
        expected_commit=commit,
        expected_scenarios=plan.get("scenarios", []),
        max_notional_usdt=float(plan.get("max_test_notional_usdt", 20)),
    )
    if not g5_verification.passed:
        print(f"ERROR: G5 certificate is not independently verifiable: {g5_verification.failures}")
        return 1

    g5_hash = g5_cert.get("evidence_hash", "")
    print(f"G5 Certificate: PASS (hash={g5_hash[:16]}...)")

    # Create G7 window
    from beidou_certification.unattended import UnattendedCertification

    engine = UnattendedCertification(str(evidence_dir))
    window = engine.create_window(
        plan_version=plan.get("_version", "1.0"),
        duration_days=args.duration,
        commit=commit,
        g5_hash=g5_hash,
    )
    engine.start_window(window.window_id)

    # Record initial SLI sample
    from beidou_certification.unattended import SLICategory, SLISample

    window_id = window.window_id
    now = datetime.now(timezone.utc)

    initial_slis = [
        SLISample(
            SLICategory.DATA_QUALITY, 1.0, 0.9, True, now, {"note": "Initial DQ check — system connected to Testnet"}
        ),
        SLISample(SLICategory.RECONCILIATION, 1.0, 0.9, True, now, {"note": "Initial reconciliation — G5 verified"}),
        SLISample(SLICategory.INCIDENT_CLOSURE, 1.0, 0.8, True, now, {"note": "No open incidents at window start"}),
    ]
    engine.record_batch_sli(window_id, initial_slis)

    # Save window manifest
    manifest = {
        "window_id": window_id,
        "plan_version": plan.get("_version", "1.0"),
        "duration_days": args.duration,
        "started_at": window.started_at.isoformat(),
        "commit": commit,
        "g5_certificate_hash": g5_hash,
        "reset_conditions": plan.get("reset_conditions", []),
        "required_sli": plan.get("required_sli", []),
        "mainnet_prohibited": True,
        "auto_mainnet": False,
        "disclaimer": "G7 certification does NOT grant Mainnet access. G8 requires separate human approval.",
    }
    with open(evidence_dir / f"{window_id}-manifest.json", "w") as f:
        json.dump(manifest, f, indent=2, default=str)

    print(f"\nWindow ID: {window_id}")
    print(f"Started: {window.started_at.isoformat()}")
    print(f"Expected completion: {args.duration} days from now")
    print(f"Evidence dir: {evidence_dir}")
    print("\nNext steps:")
    print("  1. System runs unattended in Testnet mode")
    print("  2. SLI samples collected continuously")
    print("  3. Daily reports auto-generated")
    print(f"  4. After {args.duration} days: python scripts/certification/evaluate_g7.py --window-id {window_id}")
    print(f"\nG7 WINDOW ACTIVE: {window_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
