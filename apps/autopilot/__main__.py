"""北斗 V2.0 自主运行入口 — Autopilot。

统一入口，启动所有时钟域，运行 24/7 自主交易系统。

用法:
  python -m apps.autopilot --symbols BTCUSDT,ETHUSDT          # 默认 paper 模式
  python -m apps.autopilot --mode research                     # 因子研究模式 (零写)
  python -m apps.autopilot --mode shadow                       # 影子交易模式 (零写)
  python -m apps.autopilot --mode testnet                      # Testnet 模式 (可写)
  python -m apps.autopilot --mode safety_only                  # 仅安全监控 (零写)

安全约束:
  - RESEARCH / PAPER / SHADOW / SAFETY_ONLY 在类型层禁止交易写请求
  - CANARY / LIVE 模式永久阻断（PIVOT 决策，直至包完成）
  - 不存在自动 RESUME — 需持久化 Startup Gate 证书
  - UNKNOWN 模式 → fail-closed 为 SAFETY_ONLY
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
        choices=["research", "paper", "shadow", "testnet", "safety_only"],
        help="运行模式: research=因子研究, paper=纸上交易(默认), shadow=影子交易, testnet=测试网, safety_only=仅安全监控",
    )
    parser.add_argument("--port", type=int, default=9090, help="健康检查端口 (默认: 9090)")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    # 特殊关键字：ALL/DEFAULT 展开为完整交易池
    if symbols == ["ALL"] or symbols == ["DEFAULT"]:
        from beidou_core.engine import DEFAULT_UNIVERSE

        symbols = list(DEFAULT_UNIVERSE)
        print(f"[autopilot] 展开 DEFAULT_UNIVERSE → {len(symbols)} 个标的")

    # 确保项目根目录在 path 上
    proj_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, proj_root)
    os.chdir(proj_root)

    # ================================================================
    # P0 Startup Gate — EnvironmentGuard
    # ================================================================
    from beidou_core.guard import EnvironmentGuard, EnvironmentMode, StartupGateStatus
    from beidou_shared.config import ConfigProvider

    commit = _get_git_commit()

    # 根据 mode 参数确定环境模式 — 显式映射，禁止回退
    _MODE_MAP: dict[str, EnvironmentMode] = {
        "research": EnvironmentMode.RESEARCH,
        "paper": EnvironmentMode.PAPER,
        "shadow": EnvironmentMode.SHADOW,
        "testnet": EnvironmentMode.TESTNET,
        "safety_only": EnvironmentMode.SAFETY_ONLY,
    }
    if args.mode in _MODE_MAP:
        env_mode = _MODE_MAP[args.mode]
    else:
        # UNKNOWN mode → fail-closed
        print(f"[autopilot] FATAL: Unknown mode '{args.mode}' — fail-closed")
        env_mode = EnvironmentMode.SAFETY_ONLY

    # 使用 ConfigProvider 统一加载配置 — 不再硬编码 BEIDOU_ENV=testnet，
    # 也不再直接读取 YAML 文件。ConfigProvider 是唯一配置入口，
    # 环境缺失/未知时回退 SAFETY_ONLY（写交易能力为 false）。
    os.environ["BEIDOU_ENV"] = env_mode.value
    settings = ConfigProvider().load(environment=env_mode.value)
    rest_url = settings.exchange.rest_base_url
    # 优先环境变量，其次配置文件 api_key_ref/api_secret_ref 字段
    api_key = os.environ.get("BEIDOU_BINANCE_API_KEY", "") or settings.exchange.api_key_ref
    api_secret = os.environ.get("BEIDOU_BINANCE_API_SECRET", "") or settings.exchange.api_secret_ref
    config_path = ""
    if settings.source.startswith("env-file:"):
        config_path = os.path.join(proj_root, "config", f"env.{settings.source.split(':', 1)[1]}.yaml")

    # 运行启动门禁
    guard = EnvironmentGuard(
        mode=env_mode.value,
        rest_url=rest_url,
        api_key=api_key,
        api_secret=api_secret,
        commit=commit,
        config_path=config_path,
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
    rejection_reason = ""
    if env_mode.is_write_blocked:
        if env_mode == EnvironmentMode.SAFETY_ONLY:
            rejection_reason = "SAFETY_ONLY mode — trading writes permanently disabled"
        elif env_mode in (EnvironmentMode.CANARY, EnvironmentMode.LIVE):
            rejection_reason = f"{env_mode.value} mode — blocked by PIVOT decision"
        else:
            rejection_reason = f"{env_mode.value} mode — trading writes disabled at type layer"

    audit_event = EnvironmentGuard.generate_startup_audit_event(
        environment=env_mode.value,
        commit=commit,
        config_hash=guard._compute_config_hash(),
        certificate_status="NOT_PRESENT",
        account_capability="UNKNOWN",
        control_state="NO_NEW_RISK",
        extra={
            "cli_mode": args.mode,
            "write_enabled": env_mode.can_write_trades,
            "write_blocked": env_mode.is_write_blocked,
            "rejection_reason": rejection_reason,
        },
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
    print(f"  CLI 模式: {args.mode}")
    print(f"  环境模式: {env_mode.value}")
    print(f"  写交易: {'ENABLED' if env_mode.can_write_trades else 'BLOCKED'}")
    print(f"  健康端口: {args.port}")
    print(f"  Gate: {gate_result.status.value}")
    if rejection_reason:
        print(f"  拒绝原因: {rejection_reason}")
    print("=" * 60)

    # 延迟导入，避免启动时的循环依赖
    from beidou_core.engine import AutonomousEngine

    engine = AutonomousEngine(symbols=symbols, mode=args.mode)

    # 信号处理
    loop = asyncio.new_event_loop()

    def shutdown() -> None:
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
