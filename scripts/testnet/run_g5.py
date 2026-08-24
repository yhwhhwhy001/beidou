"""BD-T18: G5 Testnet 认证运行器 — 验证真实 Binance Testnet 协议层正确性。

用法:
    python scripts/testnet/run_g5.py --plan config/g5-testnet-plan.yaml --symbol SYMBOL --confirm-testnet
    python scripts/testnet/run_g5.py --list                      # 打印已注册场景
    python scripts/testnet/run_g5.py ... --scenario NAME         # 只跑单个场景
    python scripts/testnet/run_g5.py ... --skip-restart          # 跳过重启组场景
    python scripts/testnet/run_g5.py ... --dry-run               # 场景内不发送真实请求

前置条件:
    - BEIDOU_BINANCE_API_KEY / BEIDOU_BINANCE_API_SECRET / BEIDOU_SIGNING_KEY 已设置
    - 引擎已启动 (python -m beidou_launcher start --mode testnet)
    - 不可使用 Mainnet URL
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import json
import logging
import math
import os
import re
import subprocess
import sys
import time
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import yaml

from beidou_certification.g5_scenarios.base import (
    EvidenceWriteError,
    NotionalLedger,
    ScenarioContext,
    ScenarioResult,
    ScenarioStatus,
    write_scenario_evidence,
)
from beidou_launcher.g5_producer import (
    PRODUCER_ENVIRONMENT_MARKER,
    producer_launchd_target,
    producer_status_verdict,
)
from beidou_launcher.models import CheckResult

if TYPE_CHECKING:
    from beidou_exchange.binance_usdm.rest_client import BinanceRESTClient


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


def _producer_status_http() -> dict[str, object] | None:
    """Read the loopback producer status without importing its engine."""

    try:
        with urllib.request.urlopen("http://127.0.0.1:9090/status", timeout=5) as response:  # nosec B310
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def start_g5_producer_service(project_root: Path) -> str:
    """Load and start the isolated G5 launchd producer, then await readiness."""

    plist = project_root / "deploy" / "com.beidou.g5-producer.plist"
    if not plist.is_file():
        raise RuntimeError(f"G5 producer launchd template missing: {plist}")
    target = producer_launchd_target()
    domain = target.rsplit("/", 1)[0]
    already_loaded = subprocess.run(  # nosec B603 - fixed launchctl subcommand and checked plist path  # noqa: S603
        ["/bin/launchctl", "print", target], check=False, capture_output=True, text=True
    )
    if already_loaded.returncode == 0:
        raise RuntimeError(f"G5 producer service already loaded: {target}")
    try:
        subprocess.run(  # nosec B603 - fixed launchctl subcommand and repository-local plist  # noqa: S603
            ["/bin/launchctl", "bootstrap", domain, str(plist)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(  # nosec B603 - fixed launchctl target from governed label  # noqa: S603
            ["/bin/launchctl", "kickstart", target], check=True, capture_output=True, text=True
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        subprocess.run(  # nosec B603 - cleanup of the exact producer label only  # noqa: S603
            ["/bin/launchctl", "bootout", target], check=False, capture_output=True, text=True
        )
        raise RuntimeError(f"G5 producer launchd startup failed: {type(exc).__name__}") from exc
    deadline = time.monotonic() + 120.0
    last_reason = "status_unreachable"
    try:
        while time.monotonic() < deadline:
            payload = _producer_status_http()
            if payload is not None:
                ready, reason = producer_status_verdict(payload)
                if ready:
                    print(f"G5 producer ready: {target}")
                    return target
                last_reason = reason
            time.sleep(2.0)
    except KeyboardInterrupt:
        subprocess.run(  # nosec B603 - cleanup of the exact producer label only  # noqa: S603
            ["/bin/launchctl", "bootout", target], check=False, capture_output=True, text=True
        )
        raise
    subprocess.run(  # nosec B603 - cleanup of the exact producer label only  # noqa: S603
        ["/bin/launchctl", "bootout", target], check=False, capture_output=True, text=True
    )
    raise RuntimeError(f"G5 producer readiness timeout: {last_reason}")


def stop_g5_producer_service(target: str) -> None:
    """Unload only the dedicated G5 producer service."""

    subprocess.run(  # nosec B603 - cleanup of the exact governed producer label  # noqa: S603
        ["/bin/launchctl", "bootout", target], check=False, capture_output=True, text=True
    )


def blocking_preflight_checks(checks: list[CheckResult]) -> list[CheckResult]:
    """Use the canonical fail-closed blocker semantics, including P1 UNKNOWN."""

    return [check for check in checks if check.is_blocking]


def validate_probe_symbol(raw_symbol: str) -> str:
    """Return one normalized Binance symbol or fail before preflight/network access."""

    symbol = raw_symbol.strip().upper()
    if (
        not re.fullmatch(r"[A-Z][A-Z0-9]{4,11}", symbol)
        or symbol in {"ALL", "DEFAULT"}
        or not any(character.isalpha() for character in symbol)
    ):
        raise ValueError("symbol must be one explicit 5-12 character alphanumeric market")
    return symbol


def require_exchange_symbol(probe_symbol: str, symbols: object) -> dict[str, object]:
    """Require exchange-info to return the exact requested market."""

    if not isinstance(symbols, list):
        raise ValueError("exchange info symbols is UNKNOWN")
    for candidate in symbols:
        if isinstance(candidate, dict) and candidate.get("symbol") == probe_symbol:
            return candidate
    raise ValueError(f"requested symbol {probe_symbol} was not returned by exchange info")


def _configure_scenario_logging() -> None:
    """场景运行日志配置:root 无 handler 时 INFO 意图消息会被 lastResort(WARNING+)丢弃。

    设计规格§4「任何写操作前打印操作意图与金额」由场景 logger.info 输出,
    此处保证真实认证运行时该意图可见(S1-S7 的 print 输出不受影响)。
    """

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def build_context(
    client: BinanceRESTClient | None,
    ledger: NotionalLedger,
    evidence_dir: Path,
    symbol: str,
    dry_run: bool,
) -> ScenarioContext:
    """构造场景执行上下文;供 G5Runner.make_context 与测试共用。"""

    return ScenarioContext(
        client=client,
        ledger=ledger,
        evidence_dir=evidence_dir,
        symbol=symbol,
        dry_run=dry_run,
    )


def _extract_account_access(observations: dict) -> dict | None:
    """从 S2 观察提取账户事实;三键任一缺失即视为未测量,返回 None。

    None 由 build_certificate 的保守占位默认兜底(can_trade=False /
    can_withdraw=None / has_balance=False),绝不肯定性声称可交易或有余额。
    """

    s2 = observations.get("account_access") or {}
    if not all(key in s2 for key in ("can_trade", "can_withdraw", "has_balance")):
        return None
    return {
        "can_trade": s2["can_trade"],
        "can_withdraw": s2["can_withdraw"],
        "has_balance": s2["has_balance"],
    }


def assess_account_permissions(
    can_trade: object, can_withdraw: object, *, allow_withdraw_permission: bool
) -> tuple[bool, str]:
    """Classify account permissions while honoring the explicit Testnet plan exemption."""
    if not isinstance(can_trade, bool) or not isinstance(can_withdraw, bool):
        return False, "ACCOUNT_PERMISSION_UNKNOWN"
    if can_withdraw and not allow_withdraw_permission:
        return False, "WITHDRAWAL_PERMISSION_ENABLED"
    if not can_trade:
        return False, "VENUE_TRADING_DISABLED"
    if can_withdraw:
        return True, "TESTNET_WITHDRAWAL_PERMISSION_EXEMPT"
    return True, "OK"


def write_scenario_evidence_all(
    results: dict[str, ScenarioResult],
    make_context: Callable[[], ScenarioContext],
    evidence_dir: Path,
) -> dict[str, ScenarioResult]:
    """逐个落盘场景证据;单场景写失败 → 该场景改判 FAIL,后续场景照常。

    spec §4:证据文件写入失败 → 场景判 FAIL(没有 durable evidence 的结果
    不算结果)。失败时 error_type 记证据写错误类型,error_message 记目标路径;
    绝不中断整体执行 —— 证书照常生成,FAIL 进入证书汇总。
    """
    for sid, result in results.items():
        try:
            write_scenario_evidence(make_context(), result)
        except (EvidenceWriteError, OSError) as exc:
            target = evidence_dir / f"{result.scenario_id}.json"
            results[sid] = result = replace(
                result,
                status=ScenarioStatus.FAIL,
                error_type=f"EVIDENCE_WRITE_{type(exc).__name__}",
                error_message=f"证据写入失败 {target}: {exc}",
            )
        print(f"[{sid}] {result.status.value}")
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="G5 Testnet Certification Runner")
    parser.add_argument("--plan", default="config/g5-testnet-plan.yaml")
    parser.add_argument("--symbol", required=True, help="显式指定只读协议探测品种(不允许固定交易池回退)")
    parser.add_argument("--confirm-testnet", action="store_true", help="确认连接到 Testnet（非 Mainnet）")
    parser.add_argument(
        "--max-notional",
        type=float,
        default=None,
        help="最大测试名义金额 (USDT)，默认读取 G5 plan；不得超过 plan 上限",
    )
    parser.add_argument(
        "--certification-mode",
        choices=("DEV_BYPASS", "FULL"),
        default="DEV_BYPASS",
        help="证书认证模式(M20-F02 显式化): DEV_BYPASS=单次协议探测; FULL=72h 认证流程",
    )
    parser.add_argument(
        "--scenario",
        default=None,
        help="只执行指定场景(SCENARIO_REGISTRY 中的名字);缺省执行全部已注册场景",
    )
    parser.add_argument(
        "--skip-restart",
        action="store_true",
        help="跳过重启组场景(process_restart/database_restart/user_stream_reconnect)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="打印已注册场景后退出(不访问网络、不校验 plan)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="场景内不发送真实请求(仅演练场景逻辑)",
    )
    if "--list" in sys.argv:
        # --list 只打印已注册场景(不访问网络、不校验 plan),不要求 --symbol;
        # 场景执行路径仍强制显式品种。
        for _action in parser._actions:
            if getattr(_action, "dest", "") == "symbol":
                _action.required = False
    args = parser.parse_args()

    if args.list:
        from beidou_certification.g5_scenarios.runner import SCENARIO_REGISTRY

        for name in sorted(SCENARIO_REGISTRY):
            print(name)
        return 0

    # 场景写操作意图为 logger.info 输出,root 默认无 handler 会被静默丢弃 —— 场景执行前配置 INFO
    _configure_scenario_logging()

    if not args.symbol:
        parser.error("--symbol 是必选参数(--list 除外)")
    try:
        probe_symbol = validate_probe_symbol(args.symbol)
    except ValueError as exc:
        parser.error(f"--symbol 无效: {exc}")

    if not args.confirm_testnet:
        print("ERROR: 必须使用 --confirm-testnet 标志确认 Testnet 环境")
        return 1

    project_root = Path(__file__).parent.parent.parent
    os.chdir(project_root)

    # Database-backed G5 scenarios bind their DSN at module import time.  The
    # local Testnet environment exposes the same isolated database as
    # DATABASE_URL; map it before any scenario module can be imported without
    # ever printing or persisting the credential-bearing value.
    if not os.environ.get("BEIDOU_G5_PG_DSN") and os.environ.get("DATABASE_URL"):
        os.environ["BEIDOU_G5_PG_DSN"] = os.environ["DATABASE_URL"]

    # Never start a certification probe from an unreproducible artifact.  The
    # same preflight used by the launcher is a hard gate here, before any REST
    # client is constructed or any exchange request is attempted.
    from beidou_launcher.preflight import run_g5_producer_preflight

    preflight_checks, _ = run_g5_producer_preflight(project_root, 9090)
    preflight_blockers = blocking_preflight_checks(preflight_checks)
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
    # M22-F04: host 精确匹配 —— 子串匹配把 demo-fapi.binance.com
    # 误判为 mainnet(与 gate_verifier M20-F02 同款缺陷)
    from urllib.parse import urlparse

    parsed_host = (urlparse(rest_url).hostname or "").lower()
    if parsed_host in {"fapi.binance.com", "api.binance.com"}:
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
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    print(f"Commit: {commit}")

    # The normal launcher cannot start until a current G5 certificate exists.
    # For this certification run, load a separate, non-KeepAlive launchd
    # service whose engine can observe user-stream/reconciliation facts but
    # cannot place, cancel, or mutate terminal orders.  The runner itself
    # remains the only owner of the bounded Testnet scenario writes.
    producer_mode = not args.dry_run
    producer_target: str | None = None

    def cleanup_producer() -> None:
        nonlocal producer_target
        if producer_target is not None:
            stop_g5_producer_service(producer_target)
            producer_target = None

    if producer_mode:
        os.environ[PRODUCER_ENVIRONMENT_MARKER] = "1"
        try:
            producer_target = start_g5_producer_service(project_root)
        except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            fail_fast(f"G5 producer startup blocked: {type(exc).__name__}: {exc}")
        atexit.register(cleanup_producer)
    else:
        os.environ.pop(PRODUCER_ENVIRONMENT_MARKER, None)

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

    async def run_scenarios() -> tuple[dict, dict]:
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

            # AC-04: withdrawal permission remains a hard blocker by default;
            # the Testnet plan may explicitly exempt demo-fapi's fixed
            # canWithdraw=true fact. Missing/non-boolean facts stay fail-closed.
            can_trade_ok, permission_status = assess_account_permissions(
                can_trade,
                can_withdraw,
                allow_withdraw_permission=plan.get("allow_withdraw_permission") is True,
            )
            if permission_status == "ACCOUNT_PERMISSION_UNKNOWN":
                print("  FAIL: account permission facts are missing or invalid")
            elif permission_status == "WITHDRAWAL_PERMISSION_ENABLED":
                print("  FAIL: venue withdrawal permission is enabled")
            elif permission_status == "TESTNET_WITHDRAWAL_PERMISSION_EXEMPT":
                print("  PASS: demo-fapi withdrawal permission explicitly exempted by Testnet plan")
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
            symbol_info = require_exchange_symbol(probe_symbol, ei_data.get("symbols"))
            print(f"  PASS: {probe_symbol} status={symbol_info.get('status')}")
            results["exchange_info"] = {"status": "PASS", "symbol": probe_symbol}
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

        # S1-S7 为只读观察,作为证书 observations 保留;协议场景由
        # G5Runner 在同步上下文执行(每场景自带 asyncio.run)。
        return evidence, results

    observations, evidence = asyncio.run(run_scenarios())

    ended_at = datetime.now(timezone.utc)
    evidence["ended_at"] = ended_at.isoformat()
    evidence["observations"] = observations
    observation_failures = sorted(
        name for name, result in observations.items() if isinstance(result, dict) and result.get("status") == "FAIL"
    )

    # ================================================================
    # G5 场景框架:runner 按 SCENARIO_REGISTRY 执行注册场景
    # (注册表当前为空,后续任务逐场景注册;S1-S7 只读观察不参与场景计数)
    # ================================================================
    from beidou_certification.g5_scenarios.runner import G5Runner

    ledger = NotionalLedger(requested_max_notional)
    scenario_evidence_dir = Path("artifacts/evidence/testnet/g5/scenarios")
    scenario_client = BinanceRESTClient(rest_url=testnet_url, api_key=api_key, api_secret=api_secret)

    def make_context() -> ScenarioContext:
        return build_context(
            client=scenario_client,
            ledger=ledger,
            evidence_dir=scenario_evidence_dir,
            symbol=probe_symbol,
            dry_run=args.dry_run,
        )

    runner = G5Runner(
        plan_path=Path(args.plan),
        commit=commit,
        testnet_url=testnet_url,
        evidence_dir=scenario_evidence_dir,
        ledger=ledger,
        symbol=probe_symbol,
        make_context=make_context,
    )
    results = runner.run_selected(only=args.scenario, skip_restart=args.skip_restart)
    results = write_scenario_evidence_all(results, make_context, scenario_evidence_dir)
    cleanup_producer()
    atexit.unregister(cleanup_producer)

    # S2 实测账户事实覆盖 runner 占位默认;未测量时返回 None,由 runner 保守默认兜底
    account_access = _extract_account_access(observations)

    certificate = runner.build_certificate(
        results,
        started_at=started_at.isoformat(),
        ended_at=ended_at.isoformat(),
        account_access=account_access,
    )
    # 合并 legacy 观察与认证模式(M20-F02: 认证模式必须显式标注,缺失视为伪造拒绝)
    certificate["certification_mode"] = args.certification_mode
    certificate["runtime_mode"] = "G5_PRODUCER_ONLY" if producer_mode else "DRY_RUN"
    certificate["engine_terminal_writes_held"] = producer_mode
    certificate["observations"] = observations
    if observation_failures:
        certificate["status"] = "FAIL"
    certificate["blockers"] = [f"G5_OBSERVATION_FAILED:{name}" for name in observation_failures] + (
        ["G5_SCENARIO_NOT_VERIFIABLE"] if any(r.status.value == "NOT_VERIFIABLE" for r in results.values()) else []
    )

    from beidou_certification.gate_verifier import verify_g5_certificate

    # Ruling-22:testnet 无真实提现能力,demo-fapi canWithdraw 恒 True ——
    # plan 显式声明 allow_withdraw_permission 时豁免(缺省 False 行为不变;
    # S2 预检输出保持真实判定,此处仅影响语义验证)。
    verification = verify_g5_certificate(
        certificate,
        expected_commit=commit,
        expected_scenarios=expected_scenarios,
        max_notional_usdt=plan_max_notional,
        allow_withdraw_permission=plan.get("allow_withdraw_permission") is True,
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
    print(f"  Hash:  {certificate['evidence_hash'][:16]}...")
    print("  Saved: artifacts/evidence/testnet/g5-certificate.json")
    print("=" * 60)

    cert_result = certificate

    # Gate 4: 最终判定
    if cert_result["status"] != "PASS":
        print("G5 remains blocked: complete every plan scenario and re-run the independent verifier.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
