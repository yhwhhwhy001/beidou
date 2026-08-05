"""北斗 V2.0 自主运行入口 — Autopilot。

统一入口，替代分散的三个 apps/*/__main__.py。
启动所有时钟域，运行 24/7 自主交易系统。

用法:
  python -m apps.autopilot --symbols BTCUSDT,ETHUSDT
  python -m apps.autopilot --no-trade            # 仅监控，不下单
  python -m apps.autopilot --mode paper          # 纸上交易模式
  python -m apps.autopilot --symbols BTCUSDT --mode full  # 全自动实盘
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import time


def main() -> None:
    parser = argparse.ArgumentParser(description="北斗 V2.0 Autopilot")
    parser.add_argument("--symbols", type=str, default="BTCUSDT,ETHUSDT",
                        help="交易品种，逗号分隔 (默认: BTCUSDT,ETHUSDT)")
    parser.add_argument("--mode", type=str, default="full",
                        choices=["full", "paper", "safety_only"],
                        help="运行模式: full=全自动, paper=纸上交易, safety_only=仅安全监控")
    parser.add_argument("--port", type=int, default=9090,
                        help="健康检查端口 (默认: 9090)")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    # 确保项目根目录在 path 上
    proj_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, proj_root)
    os.chdir(proj_root)

    # 设置环境
    if "BEIDOU_ENV" not in os.environ:
        os.environ["BEIDOU_ENV"] = "testnet"

    print("=" * 60)
    print("  北斗 V2.0 Autopilot")
    print(f"  环境: {os.environ['BEIDOU_ENV']}")
    print(f"  品种: {symbols}")
    print(f"  模式: {args.mode}")
    print(f"  健康端口: {args.port}")
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
