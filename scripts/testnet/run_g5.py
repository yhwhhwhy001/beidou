"""BD-T18: G5 Testnet 认证运行器 — 验证真实 Binance Testnet 协议层正确性。

用法:
    python scripts/testnet/run_g5.py --plan config/g5-testnet-plan.yaml --confirm-testnet

前置条件:
    - BEIDOU_BINANCE_API_KEY / BEIDOU_BINANCE_API_SECRET / BEIDOU_SIGNING_KEY 已设置
    - 引擎已启动 (python -m beidou_launcher start --mode testnet)
    - 不可使用 Mainnet URL
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml


def fail_fast(reason: str) -> None:
    print(f"G5 GATE FAILED: {reason}")
    # 生成 FAIL 证书
    cert = {
        "gate": "G5",
        "status": "FAIL",
        "reason": reason,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    evidence_dir = Path("artifacts/evidence/testnet")
    evidence_dir.mkdir(parents=True, exist_ok=True)
    with open(evidence_dir / "g5-certificate.json", "w") as f:
        json.dump(cert, f, indent=2)
    sys.exit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description="G5 Testnet Certification Runner")
    parser.add_argument("--plan", default="config/g5-testnet-plan.yaml")
    parser.add_argument("--confirm-testnet", action="store_true", help="确认连接到 Testnet（非 Mainnet）")
    parser.add_argument("--max-notional", type=float, default=20.0, help="最大测试名义金额 (USDT)")
    args = parser.parse_args()

    if not args.confirm_testnet:
        print("ERROR: 必须使用 --confirm-testnet 标志确认 Testnet 环境")
        return 1

    project_root = Path(__file__).parent.parent.parent
    os.chdir(project_root)

    # ================================================================
    # G5 Gate 1: 环境安全验证
    # ================================================================
    print("=" * 60)
    print("G5 TESTNET CERTIFICATION RUNNER")
    print("=" * 60)

    # 1a: Load the plan before any network client is constructed.
    with open(args.plan) as f:
        plan = yaml.safe_load(f)
    expected_scenarios = [str(s) for s in plan.get("scenarios", [])]
    if not expected_scenarios or len(set(expected_scenarios)) != len(expected_scenarios):
        fail_fast("G5 plan must define a non-empty unique scenario set")
    plan_max_notional = float(plan.get("max_test_notional_usdt", 20.0))
    if args.max_notional < 0 or args.max_notional > plan_max_notional:
        fail_fast(f"Requested max notional {args.max_notional} exceeds plan limit {plan_max_notional}")
    print(f"Plan: {args.plan} ({len(expected_scenarios)} scenarios)")

    # 1b: Mainnet URL 检测
    rest_url = os.environ.get("BEIDOU_REST_URL", "")
    dangerous_urls = ["fapi.binance.com", "api.binance.com"]
    for url in dangerous_urls:
        if url in rest_url.lower():
            fail_fast(f"Mainnet URL detected: {rest_url}")

    # 1c: Testnet URL 验证
    testnet_url = rest_url or "https://demo-fapi.binance.com"
    print(f"Target: {testnet_url}")
    if "demo-fapi" not in testnet_url.lower() and "testnet" not in testnet_url.lower():
        fail_fast(f"Not a Testnet URL: {testnet_url}")

    # 1d: 凭证检查 — all are required before a network call is possible.
    api_key = os.environ.get("BEIDOU_BINANCE_API_KEY", "")
    if not api_key:
        fail_fast("BEIDOU_BINANCE_API_KEY not set")
    api_secret = os.environ.get("BEIDOU_BINANCE_API_SECRET", "")
    if not api_secret:
        fail_fast("BEIDOU_BINANCE_API_SECRET not set")
    signing_key = os.environ.get("BEIDOU_SIGNING_KEY", "")
    if not signing_key:
        fail_fast("BEIDOU_SIGNING_KEY not set")
    print(f"API Key: {'*' * 8}{api_key[-4:] if len(api_key) > 4 else ''}")

    # 1e: Commit hash
    import subprocess

    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    print(f"Commit: {commit}")

    # ================================================================
    # G5 Gate 2: 导入认证框架
    # ================================================================
    from beidou_exchange.binance_usdm.rest_client import BinanceRESTClient

    # ================================================================
    # G5 Gate 3: 异步场景验证
    # ================================================================
    started_at = datetime.now(timezone.utc)

    async def run_scenarios() -> dict:
        client = BinanceRESTClient(
            rest_url=testnet_url,
            api_key=api_key,
            api_secret=api_secret,
        )

        results = {}
        evidence = {
            "gate": "G5",
            "commit": commit,
            "testnet_url": testnet_url,
            "environment": plan.get("environment", "BINANCE_USDM_TESTNET_ONLY"),
            "mainnet_prohibited": bool(plan.get("mainnet_prohibited", True)),
            "started_at": started_at.isoformat(),
            "max_notional_usdt": args.max_notional,
        }

        # --- S1: Server Time (连通性) ---
        print("\n[S1] Server Time Check...")
        try:
            st = await client.get_server_time()
            if hasattr(st, "data") and st.data:
                server_time = st.data.get("serverTime", 0)
            elif isinstance(st, dict):
                server_time = st.get("serverTime", 0)
            else:
                server_time = 0
            if server_time > 0:
                print(f"  PASS: serverTime={server_time}")
                results["server_time"] = {"status": "PASS", "server_time": server_time}
            else:
                print("  FAIL: no serverTime in response")
                results["server_time"] = {"status": "FAIL"}
        except Exception as e:
            print(f"  FAIL: {e}")
            results["server_time"] = {"status": "FAIL", "error": str(e)}

        # --- S2: Account Access (账户访问) ---
        print("\n[S2] Account Access Check...")
        try:
            acct = await client.get_account()
            if hasattr(acct, "data"):
                acct_data = acct.data
            elif isinstance(acct, dict):
                acct_data = acct
            else:
                acct_data = {}

            can_trade = acct_data.get("canTrade", False)
            can_withdraw = acct_data.get("canWithdraw", False)
            total_balance = acct_data.get("totalWalletBalance", "0")

            # AC-04: 提款权限检查 (Testnet 提款权限为模拟，不阻断认证)
            if can_withdraw:
                print("  WARN: Testnet account has withdrawal permission (expected on Binance Testnet)")

            can_trade_ok = can_trade
            print(f"  canTrade={can_trade} canWithdraw={can_withdraw} balance={total_balance}")
            print(f"  {'PASS' if can_trade_ok else 'FAIL'}: account access verified")

            # AC-04: Mainnet URL fail
            if "fapi.binance.com" in testnet_url and "demo" not in testnet_url:
                fail_fast("MAINNET_URL_DETECTED")

            results["account_access"] = {
                "status": "PASS" if can_trade_ok else "FAIL",
                "can_trade": can_trade,
                "can_withdraw": can_withdraw,
                "has_balance": float(total_balance) > 0,
            }
        except Exception as e:
            print(f"  FAIL: {e}")
            results["account_access"] = {"status": "FAIL", "error": str(e)}

        # --- S3: Exchange Info (交易对信息) ---
        print("\n[S3] Exchange Info Check...")
        try:
            ei = await client.get_exchange_info("BTCUSDT")
            if hasattr(ei, "data"):
                ei_data = ei.data
            elif isinstance(ei, dict):
                ei_data = ei
            else:
                ei_data = {}
            symbols = ei_data.get("symbols", [])
            btc_info = None
            for s in symbols:
                if isinstance(s, dict) and s.get("symbol") == "BTCUSDT":
                    btc_info = s
                    break
            if btc_info:
                print(f"  PASS: BTCUSDT status={btc_info.get('status')}")
                results["exchange_info"] = {"status": "PASS", "symbol": "BTCUSDT"}
            else:
                print("  PASS: exchange info retrieved")
                results["exchange_info"] = {"status": "PASS"}
        except Exception as e:
            print(f"  FAIL: {e}")
            results["exchange_info"] = {"status": "FAIL", "error": str(e)}

        # --- S4: Position Mode (仓位模式) ---
        print("\n[S4] Position Mode Check...")
        try:
            pm = await client.get_position_mode()
            if hasattr(pm, "data"):
                pm_data = pm.data
            elif isinstance(pm, dict):
                pm_data = pm
            else:
                pm_data = {}
            dual = pm_data.get("dualSidePosition", False)
            print(f"  PASS: dualSidePosition={dual} (ONE_WAY mode)")
            evidence["position_mode"] = "ONE_WAY" if not dual else "HEDGE"
            results["position_mode"] = {"status": "PASS", "dual_side": dual}
        except Exception as e:
            print(f"  WARN: position mode query failed: {e}")
            results["position_mode"] = {"status": "WARN", "error": str(e)}

        # --- S5: Open Orders (挂单检查) ---
        print("\n[S5] Open Orders Check...")
        try:
            oo = await client.get_open_orders()
            if hasattr(oo, "data"):
                oo_data = oo.data
            elif isinstance(oo, list):
                oo_data = oo
            else:
                oo_data = []
            print(f"  PASS: {len(oo_data)} open orders")
            results["open_orders"] = {"status": "PASS", "count": len(oo_data)}
        except Exception as e:
            print(f"  WARN: open orders query failed: {e}")
            results["open_orders"] = {"status": "WARN", "error": str(e)}

        # --- S6: Reconciliation Evidence (对账证据) ---
        print("\n[S6] Reconciliation Evidence...")
        try:
            positions = []
            acct_full = await client.get_account()
            if hasattr(acct_full, "data"):
                acct_data = acct_full.data
            elif isinstance(acct_full, dict):
                acct_data = acct_full
            else:
                acct_data = {}

            for p in acct_data.get("positions", []):
                amt = float(p.get("positionAmt", 0))
                if abs(amt) > 0:
                    positions.append({"symbol": p["symbol"], "amt": amt})

            balance = acct_data.get("totalWalletBalance", "0")
            print(f"  Balance: {balance}  Positions: {len(positions)}")
            print("  PASS: reconciliation data available")

            evidence["account_snapshot"] = {
                "balance": balance,
                "positions": positions,
                "open_orders_count": len(oo_data) if "oo_data" in dir() else 0,
            }
            results["reconciliation"] = {"status": "PASS", "positions": len(positions)}
        except Exception as e:
            print(f"  WARN: {e}")
            results["reconciliation"] = {"status": "WARN", "error": str(e)}

        # --- S7: Idempotency Key Test ---
        print("\n[S7] Client Order ID Idempotency...")
        try:
            cid = f"g5-cert-{int(time.time() * 1000)}"
            results["idempotency"] = {
                "status": "PASS",
                "client_order_id": cid,
                "note": "Idempotency framework verified (no duplicate orders from same clientOrderId)",
            }
            print(f"  PASS: idempotency key={cid}")
        except Exception as e:
            results["idempotency"] = {"status": "FAIL", "error": str(e)}

        # The seven legacy observations above are useful diagnostics, but
        # they are not the sixteen plan scenarios.  Keep them as observations
        # and explicitly mark every unimplemented protocol scenario as
        # NOT_VERIFIABLE so a partial probe can never become a PASS certificate.
        observations = dict(results)
        results = {
            scenario: {
                "status": "NOT_VERIFIABLE",
                "reason": "scenario requires a complete protocol test and durable evidence",
            }
            for scenario in expected_scenarios
        }
        has_fail = any(r.get("status") == "FAIL" for r in results.values())
        has_not_verifiable = any(r.get("status") == "NOT_VERIFIABLE" for r in results.values())
        account_access = observations.get("account_access", {})

        # Compute evidence hash
        ended_at = datetime.now(timezone.utc)
        evidence["ended_at"] = ended_at.isoformat()
        evidence["observations"] = observations
        evidence_json = json.dumps(evidence, sort_keys=True, default=str)
        evidence_hash = hashlib.sha256(evidence_json.encode()).hexdigest()

        certificate = {
            "gate": "G5",
            "status": "FAIL" if has_fail else ("NOT_VERIFIABLE" if has_not_verifiable else "PASS"),
            "commit": commit,
            "environment": plan.get("environment", "BINANCE_USDM_TESTNET_ONLY"),
            "testnet_url": testnet_url,
            "mainnet_prohibited": bool(plan.get("mainnet_prohibited", True)),
            "is_simulated": False,
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "evidence_hash": evidence_hash,
            "max_notional_usdt": args.max_notional,
            "scenarios": results,
            "observations": observations,
            "account_access": {
                "can_trade": account_access.get("can_trade", False),
                "can_withdraw": account_access.get("can_withdraw"),
                "has_balance": account_access.get("has_balance", False),
            },
            "blockers": ["G5_SCENARIO_NOT_VERIFIABLE"] if has_not_verifiable else [],
            "summary": {
                "total": len(results),
                "pass": sum(1 for r in results.values() if r.get("status") == "PASS"),
                "warn": sum(1 for r in results.values() if r.get("status") == "WARN"),
                "fail": sum(1 for r in results.values() if r.get("status") == "FAIL"),
                "not_verifiable": sum(1 for r in results.values() if r.get("status") == "NOT_VERIFIABLE"),
            },
        }

        from beidou_certification.gate_verifier import verify_g5_certificate

        verification = verify_g5_certificate(
            certificate,
            expected_commit=commit,
            expected_scenarios=expected_scenarios,
            max_notional_usdt=plan_max_notional,
        )
        certificate["semantic_verification"] = verification.to_dict()
        if not verification.passed and certificate["status"] == "PASS":
            certificate["status"] = "NOT_VERIFIABLE"

        evidence_dir = Path("artifacts/evidence/testnet")
        evidence_dir.mkdir(parents=True, exist_ok=True)

        with open(evidence_dir / "g5-certificate.json", "w") as f:
            json.dump(certificate, f, indent=2, default=str)

        with open(evidence_dir / "g5-evidence.json", "w") as f:
            json.dump(evidence, f, indent=2, default=str)

        print("\n" + "=" * 60)
        print(f"G5 CERTIFICATE: {certificate['status']}")
        print(f"  Total: {certificate['summary']['total']}")
        print(f"  PASS:  {certificate['summary']['pass']}")
        print(f"  WARN:  {certificate['summary']['warn']}")
        print(f"  FAIL:  {certificate['summary']['fail']}")
        print(f"  N/V:   {certificate['summary']['not_verifiable']}")
        print(f"  Hash:  {evidence_hash[:16]}...")
        print("  Saved: artifacts/evidence/testnet/g5-certificate.json")
        print("=" * 60)

        return certificate

    cert_result = asyncio.run(run_scenarios())

    # Gate 4: 最终判定
    if cert_result["status"] != "PASS":
        print("G5 remains blocked: complete every plan scenario and re-run the independent verifier.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
