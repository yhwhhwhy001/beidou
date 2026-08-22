"""因子挖掘单品种 worker。

Worker 必须位于可导入模块中。macOS 使用 ``spawn`` 启动进程池时，
``python -m apps.factor_miner`` 的 ``__main__`` 模块无法作为稳定的
pickle 导入路径；把 worker 独立出来可以让 CLI 与测试共享同一实现。
"""

from __future__ import annotations

import os


def run_symbol_worker(payload: dict) -> dict:
    """执行单个交易品种的完整离线挖掘流程。"""
    sym = payload["symbol"]

    from beidou_core.feed import MarketDataFeed
    from beidou_research.data.dataset_manifest import DatasetManifest
    from beidou_research.data.kline_store import KlineStore, frame_to_price_data
    from beidou_research.mining.runner import (
        MiningRunner,
        PipelineConfig,
        compute_feature_manifest_hash,
    )

    store = KlineStore(root=payload["data_root"])
    manifest_hash = ""
    if payload["from_store"] or store.has_data(sym, payload["interval"], min_rows=100):
        if not store.has_data(sym, payload["interval"], min_rows=100):
            raise RuntimeError(f"SYMBOL_LOCAL_DATA_MISSING:{sym}/{payload['interval']}")
        frame = store.load(sym, payload["interval"])
        price_data = frame_to_price_data(frame)
        manifest = DatasetManifest.compute(frame, sym, payload["interval"])
        manifest_hash = DatasetManifest.hash_of(manifest)
        source = "local"
    else:
        feed = MarketDataFeed()
        klines = feed.fetch_klines(sym, interval=payload["interval"], limit=payload["limit"])
        if len(klines) < 100:
            return {"symbol": sym, "skipped": True, "reason": "insufficient_klines"}
        price_data = [
            {
                "timestamp": k["open_time"],
                "close": k["close"],
                "open": k["open"],
                "high": k["high"],
                "low": k["low"],
                "volume": k["volume"],
                "is_closed": k.get("is_closed") is True,
            }
            for k in klines
        ]
        source = "api"

    pipeline_config = PipelineConfig.from_yaml(payload["policy"])
    pipeline_config.evidence_dir = payload["output_dir"]
    pipeline_config.dataset_manifest_hash = manifest_hash
    # GAP-1: 特征 schema manifest 必须独立于 dataset payload 绑定。
    # 本地数据与 API 拉取两条路径共用此处配置构造，均注入。
    pipeline_config.feature_manifest_hash = compute_feature_manifest_hash()
    runner = MiningRunner(pipeline_config)

    result = runner.run(
        price_data=price_data,
        venue="BINANCE",
        symbol=sym,
        timeframe=payload["interval"],
    )
    return {
        "symbol": sym,
        "result": result,
        "manifest_hash": manifest_hash,
        "source": source,
        "rows": len(price_data),
        "pid": os.getpid(),
    }
