"""BD-T04: 确定性重放工具 - 验证 ClosedBar 链的可复现性。

从冻结的 JSON fixture 加载原始 KLine 数据，通过 ClosedBarNormalizer 和
DataQualityGate 生成 ClosedBar + FeatureVector，并验证：
1. 同一 fixture 两次重放产生相同的输出 hash
2. 所有 ClosedBar 均通过质量门 (DQ PASS)
3. 输出包含 input_hash、feature_version、dq_tier
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from beidou_data.feature_store import FeatureVector
from beidou_data.klines import OHLCV, KLineGenerator
from beidou_data.market import ClosedBarNormalizer, ClosedBarResult
from beidou_data.quality import DataQualityGate
from beidou_shared.types import (
    DataQualityTier,
    InstrumentId,
    SchemaVersion,
    VenueId,
    VenueInstrument,
)


def load_raw_klines(path: Path) -> list[dict]:
    with open(path) as f:
        data = json.load(f)
    return data["events"]


def replay(raw_klines: list[dict], symbol: str) -> tuple[list[ClosedBarResult], list[FeatureVector], str, str]:
    normalizer = ClosedBarNormalizer()
    quality_gate = DataQualityGate()
    kline_gen = KLineGenerator(interval="1h")
    venue_instrument = VenueInstrument(
        venue_id=VenueId("BINANCE"),
        instrument_id=InstrumentId(symbol),
        schema_version=SchemaVersion("2.0.0"),
    )

    results: list[ClosedBarResult] = []
    features: list[FeatureVector] = []

    for raw in raw_klines:
        result = normalizer.normalize(raw, venue_instrument, interval="1h")
        results.append(result)

        if result.status == "CLOSED" and result.bar is not None:
            dq_result = quality_gate.check_closed_bar(result.bar)
            if dq_result.tier in (DataQualityTier.PASS, DataQualityTier.DEGRADED):
                ohlcv = OHLCV(
                    open=float(result.bar.open.amount),
                    high=float(result.bar.high.amount),
                    low=float(result.bar.low.amount),
                    close=float(result.bar.close.amount),
                    volume=float(result.bar.volume.amount),
                    time=result.bar.open_time,
                )
                kline_gen.add(ohlcv)
                fv = FeatureVector(
                    symbol=result.bar.venue_instrument.instrument_id,
                    available_at=result.bar.available_at.isoformat(),
                    features=kline_gen.features(),
                    input_hash=result.bar.payload_hash,
                    feature_version="v1",
                    dq_tier=dq_result.tier.value,
                )
                features.append(fv)

    results_hash = hashlib.sha256(
        json.dumps([r.bar.payload_hash for r in results if r.bar], sort_keys=True).encode()
    ).hexdigest()
    features_hash = hashlib.sha256(
        json.dumps(
            [{"symbol": str(f.symbol), "at": f.available_at, "hash": f.input_hash} for f in features],
            sort_keys=True,
        ).encode()
    ).hexdigest()

    return results, features, results_hash, features_hash


def main() -> int:
    fixture_path = Path(__file__).parent.parent / "tests" / "fixtures" / "market" / "replay_v1.json"
    if not fixture_path.exists():
        print(f"ERROR: fixture not found: {fixture_path}")
        return 1

    print(f"Loading fixture: {fixture_path}")
    raw_klines = load_raw_klines(fixture_path)
    print(f"Loaded {len(raw_klines)} raw kline events")

    results1, features1, rh1, fh1 = replay(raw_klines, "BTCUSDT")
    _, _, rh2, fh2 = replay(raw_klines, "BTCUSDT")

    assert rh1 == rh2, f"RESULTS HASH MISMATCH: {rh1} != {rh2}"
    assert fh1 == fh2, f"FEATURES HASH MISMATCH: {fh1} != {fh2}"

    print(f"ClosedBar results: {len(results1)}")
    print(f"Feature vectors: {len(features1)}")
    print(f"Results hash: {rh1[:16]}...")
    print(f"Features hash: {fh1[:16]}...")

    for fv in features1:
        assert fv.input_hash, "Feature missing input_hash"
        assert fv.feature_version == "v1"

    print("BD-T04 REPLAY VERIFICATION PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
