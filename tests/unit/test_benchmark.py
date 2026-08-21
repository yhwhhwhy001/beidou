"""Alpha V3 A0-002 benchmark contracts and deterministic snapshots."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from beidou_shared.types import InstrumentId
from beidou_strategy.state.benchmark import (
    BenchmarkDefinition,
    BenchmarkSnapshotBuilder,
    BenchmarkType,
)

TIMESTAMP = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


def test_single_asset_benchmark_has_returns_volatility_and_lineage() -> None:
    definition = BenchmarkDefinition(
        benchmark_id="ETH_SINGLE",
        benchmark_type=BenchmarkType.SINGLE_ASSET,
        components=(InstrumentId("ETHUSDT"),),
        weighting_method="DIRECT",
        rebalance_rule="STATIC",
        policy_version="benchmark-policy-v1",
    )

    snapshot = BenchmarkSnapshotBuilder(definition).build(
        timestamp=TIMESTAMP,
        price_history={InstrumentId("ETHUSDT"): (100.0, 102.0, 101.0, 104.0, 108.0)},
        horizons=(1, 2, 4),
    )

    assert snapshot.returns_by_horizon[1] == pytest.approx(108.0 / 104.0 - 1.0)
    assert snapshot.realized_vol_by_horizon[1] >= 0.0
    assert snapshot.data_quality == "PASS"
    assert snapshot.source_hash
    assert snapshot.policy_version == "benchmark-policy-v1"


def test_universe_benchmark_is_order_invariant_and_not_btc_specific() -> None:
    definition = BenchmarkDefinition(
        benchmark_id="ALT_UNIVERSE",
        benchmark_type=BenchmarkType.UNIVERSE_INDEX,
        components=(InstrumentId("SOLUSDT"), InstrumentId("ADAUSDT")),
        weighting_method="EQUAL_WEIGHT",
        rebalance_rule="STATIC",
        policy_version="benchmark-policy-v1",
    )
    history = {
        InstrumentId("SOLUSDT"): (100.0, 110.0, 121.0),
        InstrumentId("ADAUSDT"): (50.0, 51.0, 52.02),
    }
    reversed_history = dict(reversed(tuple(history.items())))

    builder = BenchmarkSnapshotBuilder(definition)
    first = builder.build(timestamp=TIMESTAMP, price_history=history, horizons=(1, 2))
    second = builder.build(timestamp=TIMESTAMP, price_history=reversed_history, horizons=(1, 2))

    assert first.returns_by_horizon == second.returns_by_horizon
    assert first.source_hash == second.source_hash
    assert first.returns_by_horizon[2] == pytest.approx((0.21 + 0.0404) / 2.0)


def test_missing_history_is_explicitly_not_verifiable() -> None:
    definition = BenchmarkDefinition(
        benchmark_id="SOL_SINGLE",
        benchmark_type=BenchmarkType.SINGLE_ASSET,
        components=(InstrumentId("SOLUSDT"),),
        weighting_method="DIRECT",
        rebalance_rule="STATIC",
        policy_version="benchmark-policy-v1",
    )

    snapshot = BenchmarkSnapshotBuilder(definition).build(
        timestamp=TIMESTAMP,
        price_history={InstrumentId("SOLUSDT"): (100.0, 101.0)},
        horizons=(1, 5),
    )

    assert snapshot.data_quality == "NOT_VERIFIABLE"
    assert snapshot.returns_by_horizon == {}
    assert snapshot.realized_vol_by_horizon == {}


def test_non_finite_prices_fail_closed() -> None:
    definition = BenchmarkDefinition(
        benchmark_id="SOL_SINGLE",
        benchmark_type=BenchmarkType.SINGLE_ASSET,
        components=(InstrumentId("SOLUSDT"),),
        weighting_method="DIRECT",
        rebalance_rule="STATIC",
        policy_version="benchmark-policy-v1",
    )

    with pytest.raises(ValueError, match="finite"):
        BenchmarkSnapshotBuilder(definition).build(
            timestamp=TIMESTAMP,
            price_history={InstrumentId("SOLUSDT"): (100.0, float("nan"), 101.0)},
            horizons=(1,),
        )
