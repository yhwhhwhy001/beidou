"""
离线研究实验入口 (Research Lab)。Clock Domain: OFFLINE。

启动顺序：
1. 初始化数据集管理器（版本化 + Lineage）
2. 初始化回测/Replay 引擎（反作弊检查）
3. 初始化因子研究工具
4. 进入交互式研究环境
"""

from __future__ import annotations

import signal
import sys


def main() -> None:
    print("[beidou-research] ========================================")
    print("[beidou-research] Starting Research Lab V2.0")
    print("[beidou-research] Clock Domain: OFFLINE")
    print("[beidou-research] ========================================")

    # 1. Dataset Manager
    from beidou_data.datasets import DatasetManager
    dm = DatasetManager()
    from beidou_shared.types import InstrumentId
    v = dm.create_version("research_dataset_v1", None, instrument_ids=frozenset({InstrumentId("BTCUSDT")}), sample_count=0)
    print(f"[beidou-research] Dataset Manager: version={v.version}")

    # 2. Replay Validator
    from beidou_research.backtest.replay import ReplayValidator, CheatDetection
    validator = ReplayValidator()
    print("[beidou-research] Replay Validator: anti-cheat checks loaded")
    for check in CheatDetection:
        print(f"[beidou-research]   - {check.value}")

    # 3. Factor Research tools
    from beidou_data.feature_store import FeatureStore
    fs = FeatureStore()
    print("[beidou-research] Feature Store: ready")

    # 4. Model Drift Detection
    from beidou_strategy.alpha.model_registry import DriftDetector
    dd = DriftDetector(threshold=0.1)
    print("[beidou-research] Drift Detector: ready")

    print("[beidou-research] ========================================")
    print("[beidou-research] Research environment ready.")
    print("[beidou-research] ========================================")

    def shutdown(signum: int, frame: object) -> None:
        print("\n[beidou-research] Shutting down research lab...")
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
