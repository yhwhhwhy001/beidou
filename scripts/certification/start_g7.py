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
from pathlib import Path

import yaml


def main() -> int:
    parser = argparse.ArgumentParser(description="G7 Unattended Certification Window Starter")
    parser.add_argument("--plan", default="config/g7-unattended-plan.yaml")
    parser.add_argument("--g5-plan", default="config/g5-testnet-plan.yaml", help="用于验证前置 G5 的场景计划")
    parser.add_argument("--duration", type=int, default=30, help="认证天数 (default: 30)")
    parser.add_argument("--window-id", help="指定窗口 ID（自动生成）")
    parser.add_argument(
        "--preflight-port",
        type=int,
        default=0,
        help="仅用于 preflight 端口检查；默认 0 使用临时端口，不触碰运行中的健康端口",
    )
    args = parser.parse_args()

    project_root = Path(__file__).parent.parent.parent

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

    # G7 must not create a durable window from an artifact that could not
    # start the same guarded Testnet runtime.  Port 0 keeps this read-only
    # preflight independent from an already-running health server.
    from beidou_launcher.preflight import run_preflight

    preflight_checks, _settings = run_preflight(project_root, "testnet", args.preflight_port)
    preflight_blockers = [check for check in preflight_checks if check.is_blocking]
    if preflight_blockers:
        details = "; ".join(f"{check.check_id}: {check.message}" for check in preflight_blockers)
        print(f"ERROR: Testnet preflight blocked G7 window creation: {details}")
        return 1

    # Verify G5 certificate exists
    g5_path = project_root / "artifacts" / "evidence" / "testnet" / "g5-certificate.json"
    if not g5_path.exists():
        print(f"ERROR: G5 certificate not found at {g5_path}")
        print("Run BD-T18 first: python scripts/testnet/run_g5.py --confirm-testnet")
        return 1

    with open(g5_path) as f:
        g5_cert = json.load(f)

    g5_plan_path = project_root / args.g5_plan
    if not g5_plan_path.exists():
        print(f"ERROR: G5 plan not found at {g5_plan_path}")
        return 1
    with open(g5_plan_path) as f:
        g5_plan = yaml.safe_load(f) or {}
    expected_g5_scenarios = [str(s) for s in g5_plan.get("scenarios", [])]
    if not expected_g5_scenarios:
        print(f"ERROR: G5 plan has no scenarios: {g5_plan_path}")
        return 1

    # Bind G7 to the exact code and complete Testnet scenario set that
    # produced G5.  A legacy/demo certificate is not a valid predecessor.
    import subprocess

    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    from beidou_certification.gate_verifier import verify_g5_certificate

    g5_verification = verify_g5_certificate(
        g5_cert,
        expected_commit=commit,
        expected_scenarios=expected_g5_scenarios,
        max_notional_usdt=float(g5_plan.get("max_test_notional_usdt", 20)),
    )
    if not g5_verification.passed:
        print(f"ERROR: G5 certificate is not independently verifiable: {g5_verification.failures}")
        return 1

    g5_hash = g5_cert.get("evidence_hash", "")
    print(f"G5 Certificate: PASS (hash={g5_hash[:16]}...)")

    # Create G7 window
    from beidou_certification.unattended import UnattendedCertification

    evidence_dir = project_root / "artifacts" / "evidence" / "g7"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    engine = UnattendedCertification(str(evidence_dir))
    window = engine.create_window(
        plan_version=plan.get("_version", "1.0"),
        duration_days=args.duration,
        commit=commit,
        g5_hash=g5_hash,
        window_id=args.window_id,
    )
    engine.start_window(window.window_id)

    window_id = window.window_id
    # Do not seed PASS samples: G7 evidence must come from actual Supervisor
    # monitoring cycles.  G5 and a clean preflight are prerequisites, not
    # observations of the new unattended window.
    print("No synthetic initial SLI samples written; awaiting real Supervisor cycles.")

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
