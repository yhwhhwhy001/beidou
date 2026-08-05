"""
近线策略引擎入口 (Strategy Engine)。Clock Domain: NEARLINE。

启动顺序：
1. 加载 Model Registry 与 Champion 模型
2. 初始化 Market State Estimator（三维状态）
3. 初始化 Alpha SDK 组件图
4. 初始化 Cost Model 与组合优化器
5. 进入策略评估循环
"""

from __future__ import annotations

import signal
import sys


def main() -> None:
    print("[beidou-strategy] ========================================")
    print("[beidou-strategy] Starting Strategy Engine V2.0")
    print("[beidou-strategy] Clock Domain: NEARLINE")
    print("[beidou-strategy] ========================================")

    # 1. Model Registry
    from beidou_strategy.alpha.model_registry import ModelRegistry
    registry = ModelRegistry()
    print("[beidou-strategy] Model Registry: initialized")

    # 2. Market State Estimator
    from beidou_strategy.state.market_state import MarketStateEstimator
    from beidou_shared.types import VenueId, InstrumentId
    estimator = MarketStateEstimator()
    state = estimator.estimate(VenueId("BINANCE"), InstrumentId("BTCUSDT"), {})
    print(f"[beidou-strategy] Market State: direction={state.direction.regime} stress={state.stress.level} quality={state.quality.tier}")

    # 3. Alpha SDK
    from beidou_strategy.alpha import AlphaGraph
    from beidou_shared.types import StrategyId
    graph = AlphaGraph(strategy_id=StrategyId("default"))
    print(f"[beidou-strategy] Alpha Graph: strategy={graph.strategy_id}")

    # 4. Cost Model + Portfolio
    from beidou_strategy.state.cost_model import CostModel
    cost_model = CostModel()
    cost_model.set_fee_tier(VenueId("BINANCE"), "vip1", 2.0, 4.0)
    from beidou_strategy.portfolio.optimizer import PortfolioOptimizerImpl
    optimizer = PortfolioOptimizerImpl()
    print("[beidou-strategy] Cost Model + Portfolio Optimizer: ready")

    # 5. Signal Fusion
    from beidou_strategy.alpha.signal_fusion import SignalFuser
    fuser = SignalFuser()
    print("[beidou-strategy] Signal Fusion: ready")

    print("[beidou-strategy] ========================================")
    print("[beidou-strategy] All systems initialized. Evaluating...")
    print("[beidou-strategy] ========================================")

    def shutdown(signum: int, frame: object) -> None:
        print("\n[beidou-strategy] Received shutdown signal. Gracefully stopping...")
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        signal.pause()
    except AttributeError:
        import time
        while True:
            time.sleep(1)


if __name__ == "__main__":
    main()
