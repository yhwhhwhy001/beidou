"""实时安全与执行层入口 (Safety Executor)。Clock Domain: REALTIME。

现在委托给统一的 beidou_core.engine.AutonomousEngine。
保留此入口用于向后兼容和独立安全层测试。
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

    from beidou_core.engine import DEFAULT_UNIVERSE, AutonomousEngine

    engine = AutonomousEngine(symbols=list(DEFAULT_UNIVERSE)[:2], mode="safety_only")
    asyncio.run(engine.run())


if __name__ == "__main__":
    main()
