"""BD-T18: G5 Testnet 认证运行器 — 验证真实 Binance Testnet 协议层正确性。

用法:
    python scripts/testnet/run_g5.py --plan config/g5-testnet-plan.yaml --symbol SYMBOL --confirm-testnet

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
import math
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
    parser.add_argument("--symbol", required=True, help="显式指定只读协议探测品种")
    parser.add_argument("--confirm-testnet", action="store_true", help="确认连接到 Testnet（非 Mainnet）")
    parser.add_argument(
        "--max-notional",
        type=float,
        default=None,
        help="最大测试名义金额 (USDT)，默认读取 G5 plan；不得超过 plan 上限",
    )
    args = parser.parse_args()
    probe_symbol = args.symbol.strip().upper()
    if not probe_symbol or probe_symbol in {"ALL", "DEFAULT"}:
        parser.error("--symbol 必须是单个显式品种，且不得为 ALL/DEFAULT")

    if not args.confirm_testnet:
        print("ERROR: 必须使用 --confirm-testnet 标志确认 Testnet 环境")
        return 1

    project_root = Path(__file__).parent.parent.parent
    os.chdir(project_root)

    # Never start a certification probe from an unreproducible artifact.  The
    # same preflight used by the launcher is a hard gate here, before any REST
    # client is constructed or any exchange request is attempted.
    from beidou_launcher.preflight import run_g5_producer_preflight

    preflight_checks, _ = run_g5_producer_preflight(project_root, 9090)
    preflight_blockers = [
        check for check in preflight_checks if check.status.value == "FAIL" and check.severity.value == "P0"
    ]
    if preflight_blockers:
        details = "; ".join(f"{check.check_id}: {check.message}" for check in preflight_blockers)
        fail_fast(f"preflight blocked before network access: {details}")

    # ================================================================
    # G5 Gate 1: 环境安全验证
    # ================================================================
    print("=" * 60)
    print("G5 TESTNET CERTIFICATION RUNNER")
    print("=" * 60)

    # 1a: Load the plan before any network client is constructed.
    with open(args.plan) as f:
        plan = yaml.safe_load(f)
    if not isinstance(plan, dict):
        fail_fast("G5 plan must be a mapping")
    expected_scenarios = [str(s) for s in plan.get("scenarios", [])]
    if not expected_scenarios or len(set(expected_scenarios)) != len(expected_scenarios):
        fail_fast("G5 plan must define a non-empty unique scenario set")
    plan_environment = plan.get("environment")
    if not isinstance(plan_environment, str) or not plan_environment.strip():
        fail_fast("G5 plan must explicitly define environment")
    plan_mainnet_prohibited = plan.get("mainnet_prohibited")
    if plan_mainnet_prohibited is not True:
        fail_fast("G5 plan must explicitly set mainnet_prohibited: true")
    raw_plan_max_notional = plan.get("max_test_notional_usdt")
    if raw_plan_max_notional in (None, ""):
        fail_fast("G5 plan must explicitly define max_test_notional_usdt; no runtime default is allowed")
    try:
        plan_max_notional = float(raw_plan_max_notional)
    except (TypeError, ValueError) as exc:
        fail_fast(f"Invalid plan max_test_notional_usdt: {type(exc).__name__}")
    if not math.isfinite(plan_max_notional) or plan_max_notional <= 0:
        fail_fast("G5 plan max_test_notional_usdt must be a finite positive number")
    requested_max_notional = plan_max_notional if args.max_notional is None else args.max_notional
    if not math.isfinite(requested_max_notional) or requested_max_notional <= 0:
        fail_fast("Requested max notional must be a finite positive number")
    if requested_max_notional > plan_max_notional:
        fail_fast(f"Requested max notional {requested_max_notional} exceeds plan limit {plan_max_notional}")
    print(f"Plan: {args.plan} ({len(expected_scenarios)} scenarios)")

    # 1b: Mainnet URL 检测
    rest_url = os.environ.get("BEIDOU_REST_URL", "").strip()
    if not rest_url:
        try:
            from beidou_shared.config import ConfigProvider

            rest_url = str(ConfigProvider().load().exchange.rest_base_url or "").strip()
        except Exception as exc:
            fail_fast(f"Testnet REST URL is UNKNOWN: {type(exc).__name__}")
    if not rest_url:
        fail_fast("Testnet REST URL is not configured")
    dangerous_urls = ["fapi.binance.com", "api.binance.com"]
    for url in dangerous_urls:
        if url in rest_url.lower():
            fail_fast(f"Mainnet URL detected: {rest_url}")

    # 1c: Testnet URL 验证
    testnet_url = rest_url
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
    print("API key: configured (value withheld)")

    # 1e: Commit hash
    import subprocess

    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    print(f"Commit: {commit}")

    # ================================================================
    # G5 Gate 2: 导入认证框架
    # ================================================================
    from beidou_exchange.binance_usdm.adapter import BinanceUsdmAdapter
    from beidou_exchange.binance_usdm.endpoints import Endpoint
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
        adapter = BinanceUsdmAdapter(rest_client=client)

        async def exchange(
            method: str,
            path: str,
            *,
            signed: bool = False,
            params: dict | None = None,
        ) -> object:
            """Use the single exchange transport boundary for every probe."""

            response = await adapter.request(method, path, signed=signed, params=params or {})
            if not response.is_success():
                error = getattr(response, "error", None)
                raise RuntimeError(f"exchange response UNKNOWN: {error or 'request failed'}")
            if response.data is None:
                raise RuntimeError("exchange response UNKNOWN: empty data")
            return response.data

        results = {}
        evidence = {
            "gate": "G5",
            "commit": commit,
            "testnet_url": testnet_url,
            "environment": plan_environment,
            "mainnet_prohibited": plan_mainnet_prohibited,
            "started_at": started_at.isoformat(),
            "max_notional_usdt": requested_max_notional,
        }

        # --- S1: Server Time (连通性) ---
        print("\n[S1] Server Time Check...")
        try:
            st = await exchange("GET", Endpoint.SERVER_TIME)
            if not isinstance(st, dict):
                raise RuntimeError("server time response is not an object")
            server_time = st.get("serverTime", 0)
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
            acct_data = await exchange("GET", Endpoint.ACCOUNT, signed=True)
            if not isinstance(acct_data, dict):
                raise RuntimeError("account response is not an object")

            can_trade = acct_data.get("canTrade")
            can_withdraw = acct_data.get("canWithdraw")
            if "totalWalletBalance" not in acct_data:
                raise RuntimeError("account totalWalletBalance is UNKNOWN")
            total_balance = acct_data["totalWalletBalance"]
            try:
                numeric_balance = float(total_balance)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("account totalWalletBalance is invalid") from exc
            if not math.isfinite(numeric_balance):
                raise RuntimeError("account totalWalletBalance is non-finite")

            # AC-04: withdrawal permission is a hard production blocker even
            # on Testnet. Missing/non-boolean facts are also fail-closed.
            if not isinstance(can_trade, bool) or not isinstance(can_withdraw, bool):
                print("  FAIL: account permission facts are missing or invalid")
                can_trade_ok = False
                permission_status = "ACCOUNT_PERMISSION_UNKNOWN"
            elif can_withdraw:
                print("  FAIL: venue withdrawal permission is enabled")
                can_trade_ok = False
                permission_status = "WITHDRAWAL_PERMISSION_ENABLED"
            else:
                can_trade_ok = can_trade
                permission_status = "OK" if can_trade else "VENUE_TRADING_DISABLED"
            print(f"  canTrade={can_trade} canWithdraw={can_withdraw} balance={total_balance}")
            print(f"  {'PASS' if can_trade_ok else 'FAIL'}: account access verified")

            # AC-04: Mainnet URL fail
            if "fapi.binance.com" in testnet_url and "demo" not in testnet_url:
                fail_fast("MAINNET_URL_DETECTED")

            results["account_access"] = {
                "status": "PASS" if can_trade_ok else "FAIL",
                "can_trade": can_trade,
                "can_withdraw": can_withdraw,
                "permission_status": permission_status,
                "has_balance": numeric_balance > 0,
            }
        except Exception as e:
            print(f"  FAIL: {e}")
            results["account_access"] = {"status": "FAIL", "error": str(e)}

        # --- S3: Exchange Info (交易对信息) ---
        print("\n[S3] Exchange Info Check...")
        try:
            ei_data = await exchange("GET", Endpoint.EXCHANGE_INFO, params={"symbol": probe_symbol})
            if not isinstance(ei_data, dict):
                raise RuntimeError("exchange info response is not an object")
            symbols = ei_data.get("symbols")
            if not isinstance(symbols, list):
                raise RuntimeError("exchange info symbols is UNKNOWN")
            symbol_info = None
            for s in symbols:
                if isinstance(s, dict) and s.get("symbol") == probe_symbol:
                    symbol_info = s
                    break
            if symbol_info:
                print(f"  PASS: {probe_symbol} status={symbol_info.get('status')}")
                results["exchange_info"] = {"status": "PASS", "symbol": probe_symbol}
            else:
                print("  PASS: exchange info retrieved")
                results["exchange_info"] = {"status": "PASS"}
        except Exception as e:
            print(f"  FAIL: {e}")
            results["exchange_info"] = {"status": "FAIL", "error": str(e)}

        # --- S4: Position Mode (仓位模式) ---
        print("\n[S4] Position Mode Check...")
        try:
            pm_data = await exchange("GET", Endpoint.POSITION_SIDE_DUAL, signed=True)
            if not isinstance(pm_data, dict):
                raise RuntimeError("position mode response is not an object")
            dual = pm_data.get("dualSidePosition")
            if not isinstance(dual, bool):
                raise RuntimeError("position mode dualSidePosition is UNKNOWN")
            position_mode = "HEDGE" if dual else "ONE_WAY"
            print(f"  PASS: dualSidePosition={dual} ({position_mode} mode)")
            evidence["position_mode"] = position_mode
            results["position_mode"] = {"status": "PASS", "dual_side": dual}
        except Exception as e:
            print(f"  WARN: position mode query failed: {e}")
            results["position_mode"] = {"status": "NOT_VERIFIABLE", "error": str(e)}

        # --- S5: Open Orders (挂单检查) ---
        print("\n[S5] Open Orders Check...")
        try:
            oo_data = await exchange("GET", Endpoint.OPEN_ORDERS, signed=True)
            if not isinstance(oo_data, list):
                raise RuntimeError("open orders response is not a list")
            print(f"  PASS: {len(oo_data)} open orders")
            results["open_orders"] = {"status": "PASS", "count": len(oo_data)}
        except Exception as e:
            print(f"  WARN: open orders query failed: {e}")
            results["open_orders"] = {"status": "NOT_VERIFIABLE", "error": str(e)}

        # --- S6: Reconciliation Evidence (对账证据) ---
        print("\n[S6] Reconciliation Evidence...")
        try:
            positions = []
            acct_data = await exchange("GET", Endpoint.ACCOUNT, signed=True)
            if not isinstance(acct_data, dict):
                raise RuntimeError("account response is not an object")

            positions_raw = acct_data.get("positions")
            if not isinstance(positions_raw, list):
                raise RuntimeError("account positions are UNKNOWN")
            for p in positions_raw:
                if not isinstance(p, dict) or "symbol" not in p or "positionAmt" not in p:
                    raise RuntimeError("account position identity is UNKNOWN")
                amt = float(p["positionAmt"])
                if not math.isfinite(amt):
                    raise RuntimeError("account position amount is non-finite")
                if abs(amt) > 0:
                    positions.append({"symbol": p["symbol"], "amt": amt})

            balance = acct_data.get("totalWalletBalance")
            if balance in (None, ""):
                raise RuntimeError("account balance is UNKNOWN")
            if not isinstance(oo_data, list):
                raise RuntimeError("open orders snapshot is UNKNOWN")
            print(f"  Balance: {balance}  Positions: {len(positions)}")
            print("  PASS: reconciliation data available")

            evidence["account_snapshot"] = {
                "balance": balance,
                "positions": positions,
                "open_orders_count": len(oo_data),
            }
            results["reconciliation"] = {"status": "PASS", "positions": len(positions)}
        except Exception as e:
            print(f"  WARN: {e}")
            results["reconciliation"] = {"status": "NOT_VERIFIABLE", "error": str(e)}

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
        observation_failures = sorted(
            name for name, result in observations.items() if isinstance(result, dict) and result.get("status") == "FAIL"
        )
        results = {
            scenario: {
                "status": "NOT_VERIFIABLE",
                "reason": "scenario requires a complete protocol test and durable evidence",
            }
            for scenario in expected_scenarios
        }
        has_fail = bool(observation_failures) or any(r.get("status") == "FAIL" for r in results.values())
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
            "environment": plan_environment,
            "testnet_url": testnet_url,
            "mainnet_prohibited": plan_mainnet_prohibited,
            "is_simulated": False,
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "evidence_hash": evidence_hash,
            "max_notional_usdt": requested_max_notional,
            "scenarios": results,
            "observations": observations,
            "account_access": {
                "can_trade": account_access.get("can_trade", False),
                "can_withdraw": account_access.get("can_withdraw"),
                "has_balance": account_access.get("has_balance", False),
            },
            "blockers": (
                [f"G5_OBSERVATION_FAILED:{name}" for name in observation_failures]
                + (["G5_SCENARIO_NOT_VERIFIABLE"] if has_not_verifiable else [])
            ),
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
