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
import math
import sys
from pathlib import Path

import yaml


def main() -> int:
    parser = argparse.ArgumentParser(description="G7 Unattended Certification Window Starter")
    parser.add_argument("--plan", default="config/g7-unattended-plan.yaml")
    parser.add_argument("--g5-plan", default="config/g5-testnet-plan.yaml", help="用于验证前置 G5 的场景计划")
    parser.add_argument("--duration", type=int, default=None, help="认证天数（不得低于 G7 plan）")
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
    if not isinstance(plan, dict):
        print("ERROR: G7 plan must be a mapping")
        return 1
    plan_duration = plan.get("duration_days")
    if not isinstance(plan_duration, int) or plan_duration < 30:
        print("ERROR: G7 plan must explicitly define duration_days >= 30")
        return 1
    duration_days = plan_duration if args.duration is None else args.duration
    if duration_days < plan_duration:
        print(f"ERROR: requested duration {duration_days} is below plan duration {plan_duration}")
        return 1
    if plan.get("requires_g5") is not True or plan.get("mainnet_prohibited") is not True:
        print("ERROR: G7 plan must explicitly require G5 and prohibit Mainnet")
        return 1
    plan_version = plan.get("_version")
    if not isinstance(plan_version, str) or not plan_version.strip():
        print("ERROR: G7 plan must explicitly define _version")
        return 1
    reset_conditions = plan.get("reset_conditions")
    required_sli = plan.get("required_sli")
    if (
        not isinstance(reset_conditions, list)
        or not reset_conditions
        or not isinstance(required_sli, list)
        or not required_sli
    ):
        print("ERROR: G7 plan must define reset_conditions and required_sli")
        return 1

    print(f"Plan: {args.plan}")
    print(f"Duration: {duration_days} days")
    print(f"Requires G5: {plan['requires_g5']}")
    print("Mainnet: PROHIBITED")
    print(f"Reset conditions: {reset_conditions}")
    print(f"Required SLI: {required_sli}")

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
        g5_plan = yaml.safe_load(f)
    if not isinstance(g5_plan, dict):
        print(f"ERROR: G5 plan must be a mapping: {g5_plan_path}")
        return 1
    expected_g5_scenarios = [str(s) for s in g5_plan.get("scenarios", [])]
    if not expected_g5_scenarios:
        print(f"ERROR: G5 plan has no scenarios: {g5_plan_path}")
        return 1
    raw_max_notional = g5_plan.get("max_test_notional_usdt")
    if raw_max_notional in (None, ""):
        print(f"ERROR: G5 plan has no max_test_notional_usdt: {g5_plan_path}")
        return 1
    try:
        max_notional = float(raw_max_notional)
    except (TypeError, ValueError):
        print(f"ERROR: G5 plan max_test_notional_usdt is invalid: {g5_plan_path}")
        return 1
    if not math.isfinite(max_notional) or max_notional <= 0:
        print(f"ERROR: G5 plan max_test_notional_usdt must be positive: {g5_plan_path}")
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
        max_notional_usdt=max_notional,
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
        plan_version=plan_version,
        duration_days=duration_days,
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
        "plan_version": plan_version,
        "duration_days": duration_days,
        "started_at": window.started_at.isoformat(),
        "commit": commit,
        "g5_certificate_hash": g5_hash,
        "reset_conditions": reset_conditions,
        "required_sli": required_sli,
        "mainnet_prohibited": True,
        "auto_mainnet": False,
        "disclaimer": "G7 certification does NOT grant Mainnet access. G8 requires separate human approval.",
    }
    with open(evidence_dir / f"{window_id}-manifest.json", "w") as f:
        json.dump(manifest, f, indent=2, default=str)

    print(f"\nWindow ID: {window_id}")
    print(f"Started: {window.started_at.isoformat()}")
    print(f"Expected completion: {duration_days} days from now")
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
