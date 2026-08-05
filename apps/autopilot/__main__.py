"""北斗 V2.0 自主运行入口 — Autopilot。

统一入口，启动所有时钟域，运行 24/7 自主交易系统。

用法:
  python -m apps.autopilot --symbols BTCUSDT,ETHUSDT          # 默认 paper 模式
  python -m apps.autopilot --mode testnet                      # Testnet 模式
  python -m apps.autopilot --symbols BTCUSDT --mode full       # 全自动（需完整证书链）
  python -m apps.autopilot --mode safety_only                  # 仅安全监控

安全约束:
  - 默认 paper 模式，不会发送任何交易写请求
  - production 模式永久阻断（PIVOT 决策，直至包完成）
  - full 模式需要 G5 证书、有效凭据和 testnet URL
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys


def _get_git_commit() -> str:
    """获取当前 Git commit hash。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else "UNKNOWN"
    except Exception:
        return "UNKNOWN"


def main() -> None:
    parser = argparse.ArgumentParser(description="北斗 V2.0 Autopilot")
    parser.add_argument(
        "--symbols", type=str, default="BTCUSDT,ETHUSDT", help="交易品种，逗号分隔 (默认: BTCUSDT,ETHUSDT)"
    )
    parser.add_argument(
        "--mode",
        type=str,
        default="paper",
        choices=["full", "paper", "testnet", "safety_only"],
        help="运行模式: paper=纸上交易(默认), testnet=测试网, full=全自动(需证书), safety_only=仅安全监控",
    )
    parser.add_argument("--port", type=int, default=9090, help="健康检查端口 (默认: 9090)")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    # 确保项目根目录在 path 上
    proj_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, proj_root)
    os.chdir(proj_root)

    # 设置环境
    if "BEIDOU_ENV" not in os.environ:
        os.environ["BEIDOU_ENV"] = "testnet"

    # ================================================================
    # P0 Startup Gate — EnvironmentGuard
    # ================================================================
    from beidou_core.guard import EnvironmentGuard, EnvironmentMode, StartupGateStatus

    commit = _get_git_commit()

    # 根据 mode 参数确定环境模式
    if args.mode == "paper":
        env_mode = EnvironmentMode.PAPER
    elif args.mode == "testnet":
        env_mode = EnvironmentMode.TESTNET
    elif args.mode == "full":
        # full 模式需要完整证书 — 默认视为 testnet
        env_mode = EnvironmentMode.TESTNET
    elif args.mode == "safety_only":
        env_mode = EnvironmentMode.PAPER
    else:
        env_mode = EnvironmentMode.PAPER

    # 从配置加载 REST URL（用于 Mainnet 检测）
    try:
        import yaml

        config_path = os.path.join(proj_root, "config", f"env.{os.environ.get('BEIDOU_ENV', 'testnet')}.yaml")
        if os.path.exists(config_path):
            with open(config_path) as f:
                cfg = yaml.safe_load(f)
            rest_url = cfg.get("exchange", {}).get("binance_usdm", {}).get("rest_base_url", "")
            api_key = str(cfg.get("exchange", {}).get("binance_usdm", {}).get("api_key", "")).strip()
            api_secret = str(cfg.get("exchange", {}).get("binance_usdm", {}).get("api_secret", "")).strip()
        else:
            rest_url = ""
            api_key = os.environ.get("BEIDOU_BINANCE_API_KEY", "")
            api_secret = os.environ.get("BEIDOU_BINANCE_API_SECRET", "")
    except Exception:
        rest_url = ""
        api_key = os.environ.get("BEIDOU_BINANCE_API_KEY", "")
        api_secret = os.environ.get("BEIDOU_BINANCE_API_SECRET", "")

    # 运行启动门禁
    guard = EnvironmentGuard(
        mode=env_mode.value,
        rest_url=rest_url,
        api_key=api_key,
        api_secret=api_secret,
        commit=commit,
        config_path=config_path if "config_path" in dir() else "",
    )
    gate_result = guard.run_all_checks(cli_mode=args.mode)

    if gate_result.status == StartupGateStatus.FAIL:
        print("=" * 60)
        print("  ❌ STARTUP GATE FAILED")
        for failure in gate_result.failures:
            print(f"     - {failure}")
        print("=" * 60)
        # 写入审计事件到磁盘后退出
        sys.exit(1)

    # 写入启动审计事件
    audit_event = EnvironmentGuard.generate_startup_audit_event(
        environment=env_mode.value,
        commit=commit,
        config_hash=guard._compute_config_hash(),
        certificate_status="NOT_PRESENT",
        account_capability="UNKNOWN",
        control_state="NO_NEW_RISK",
    )
    os.makedirs("evidence/BD-00", exist_ok=True)
    audit_path = os.path.join("evidence/BD-00", "startup_audit.json")
    existing = []
    if os.path.exists(audit_path):
        try:
            with open(audit_path) as f:
                existing = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass  # 首次启动时审计文件不存在是正常情况
    existing.append(audit_event.to_dict())
    with open(audit_path, "w") as f:
        json.dump(existing, f, indent=2)

    print("=" * 60)
    print("  北斗 V2.0 Autopilot")
    print(f"  环境: {os.environ['BEIDOU_ENV']}")
    print(f"  品种: {symbols}")
    print(f"  模式: {args.mode}")
    print(f"  环境模式: {env_mode.value}")
    print(f"  健康端口: {args.port}")
    print(f"  Gate: {gate_result.status.value}")
    print("=" * 60)

    # 延迟导入，避免启动时的循环依赖
    from beidou_core.engine import AutonomousEngine

    engine = AutonomousEngine(symbols=symbols, mode=args.mode)

    # 信号处理
    loop = asyncio.new_event_loop()

    def shutdown():
        print("\n[autopilot] Received shutdown signal...")
        engine._running = False

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown)
        except NotImplementedError:
            signal.signal(sig, lambda s, f: shutdown())

    try:
        loop.run_until_complete(engine.run())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
        print("[autopilot] Goodbye.")


if __name__ == "__main__":
    main()
