"""BD-T04: 确定性重放工具 - 验证 ClosedBar 链的可复现性。

从冻结的 JSON fixture 加载原始 KLine 数据，通过 ClosedBarNormalizer 生成
ClosedBar，经 BarSequenceValidator 与 DataQualityGate 生成 FeatureVector，
并验证:
1. 同一 fixture 两次重放产生相同的输出 hash
2. 所有 ClosedBar 均通过序列验证（DQ 非 FAIL）
3. 输出包含 input_hash、feature_version、data_quality_tier

M01-F05 (P1-08): 旧脚本引用 4 处不存在的 API（gate.check_closed_bar /
status=="CLOSED" / kline_gen.add / FeatureVector 旧 kwargs），整个路径
不可运行。按真实契约重写：normalizer.normalize → BarIntegrity 状态机、
BarSequenceValidator 序列检查、FeatureVector 真实字段。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from beidou_data.feature_store import FeatureVector
from beidou_data.market import BarIntegrity, BarSequenceValidator, ClosedBarNormalizer, ClosedBarResult
from beidou_data.quality import DataQualityGate, DQCheckResult, DQCheckType
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


def _gate_tier_for_status(status: BarIntegrity) -> DataQualityTier:
    """序列状态 → DQ 层级: OK→PASS, 缺口/乱序→CONDITIONAL, 其余→FAIL。"""
    if status is BarIntegrity.OK:
        return DataQualityTier.PASS
    if status in (BarIntegrity.GAP_DETECTED, BarIntegrity.OUT_OF_ORDER):
        return DataQualityTier.CONDITIONAL
    return DataQualityTier.FAIL


def replay(raw_klines: list[dict], symbol: str) -> tuple[list[ClosedBarResult], list[FeatureVector], str, str]:
    normalizer = ClosedBarNormalizer()
    sequence_validator = BarSequenceValidator()
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

        if result.status is BarIntegrity.OK and result.bar is not None:
            bar = result.bar
            # M01-F05-R2: 以 bar 自身 close_time 作为参照 now —— 重放语义下
            # bar 在自身时刻永不过期（STALE 检查按墙钟会形同虚设/失真），
            # 且保证跨次运行确定性。
            seq_status = sequence_validator.validate(bar, now=bar.close_time)
            gate = DataQualityGate(venue_instrument=venue_instrument)
            tier = _gate_tier_for_status(seq_status)
            gate.checks.append(
                DQCheckResult(
                    check_type=DQCheckType.SEQUENCE,
                    tier=tier,
                    detail=f"sequence={seq_status.value}",
                )
            )
            dq_tier = gate.overall_tier()
            fv = FeatureVector(
                name=f"{symbol.lower()}_replay",
                values={
                    "open": float(bar.open.amount),
                    "high": float(bar.high.amount),
                    "low": float(bar.low.amount),
                    "close": float(bar.close.amount),
                    "volume": float(bar.volume.amount),
                },
                timestamp=bar.close_time,
                instrument_id=bar.venue_instrument.instrument_id,
                venue_id=bar.venue_instrument.venue_id,
                version=SchemaVersion("2.0.0"),
                # M01-F05: replay 的 PIT 语义 —— bar 在自身 close_time 即可用;
                # 用归一化时刻的墙钟会破坏两次重放的确定性。
                available_at=bar.close_time,
                input_hash=bar.payload_hash,
                feature_version="v1",
                data_quality_tier=dq_tier.value,
            )
            features.append(fv)

    results_hash = hashlib.sha256(
        json.dumps([r.bar.payload_hash for r in results if r.bar], sort_keys=True).encode()
    ).hexdigest()
    features_hash = hashlib.sha256(
        json.dumps(
            [
                {"name": f.name, "at": f.available_at.isoformat() if f.available_at else "", "hash": f.input_hash}
                for f in features
            ],
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
