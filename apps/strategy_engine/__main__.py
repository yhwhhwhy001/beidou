"""近线策略引擎入口 (Strategy Engine)。Clock Domain: NEARLINE。

现在委托给统一的 beidou_core.engine.AutonomousEngine。
保留此入口用于向后兼容和独立策略层测试。
"""

from __future__ import annotations

import os
import sys


def main() -> None:
    proj_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, proj_root)
    os.chdir(proj_root)
    if "BEIDOU_ENV" not in os.environ:
        os.environ["BEIDOU_ENV"] = "testnet"

    import asyncio

    from beidou_core.engine import AutonomousEngine

    engine = AutonomousEngine(symbols=["BTCUSDT", "ETHUSDT"], mode="paper")
    asyncio.run(engine.run())


if __name__ == "__main__":
    main()
