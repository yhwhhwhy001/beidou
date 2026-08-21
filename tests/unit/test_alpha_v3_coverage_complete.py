"""Adversarial branch fixtures for the Alpha V3 coverage gate.

These cases deliberately exercise invalid, stale, incomplete and cost-unknown
inputs as well as the happy paths.  They are semantic safety fixtures, not
coverage-only no-ops: every assertion checks the fail-closed contract or an
observable deterministic result.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import replace
from datetime import UTC, datetime
from enum import Enum
from types import SimpleNamespace
from typing import Any, cast

import pytest

from beidou_reporting.engine import EvidenceTier, ReportReference
from beidou_reporting.pnl_attribution import AttributionRecord, DecisionTrace
from beidou_research.backtest.alpha_v3_challenger import (
    _apply_costs,
    _compound,
    _finite_series,
    _max_drawdown,
    _metrics,
    _paper_dict,
    _ratio,
    _sample_std,
    _series_hash,
    run_alpha_v3_challenger,
)
from beidou_shared.types import CorrelationId, InstrumentId, SchemaVersion, StrategyId, VenueId
from beidou_strategy.alpha._forecast_utils import (
    clip,
    context_features,
    feature_payload,
    finite,
    horizon_values,
    parse_timestamp,
    point_in_time_universe,
    quality_factor,
    resolve_costs,
    resolve_timestamp,
    side_for_score,
    stable_hash,
)
from beidou_strategy.alpha.breakout import BreakoutAlpha
from beidou_strategy.alpha.contracts import AlphaForecast, EnsembleForecast
from beidou_strategy.alpha.extensions import (
    DerivativeRiskContext,
    FundingRateExtension,
    FundingRateSignal,
    LiquidationCascadeExtension,
    LiquidationCascadeRisk,
)
from beidou_strategy.alpha.forecast import (
    CalibrationArtifact,
    DeterministicForecastCalibrator,
    DynamicEnsemblePolicy,
    EnsembleFuser,
)
from beidou_strategy.alpha.mean_reversion_math import estimate_half_life, robust_zscore
from beidou_strategy.alpha.model_registry import CalibrationRegistry
from beidou_strategy.alpha.pipeline import AlphaV3ShadowEngine
from beidou_strategy.alpha.relative_strength import RelativeStrengthAlpha, RelativeStrengthPolicy
from beidou_strategy.alpha.residual_momentum import ResidualMomentumAlpha, ResidualMomentumArtifact
from beidou_strategy.alpha.trend import TrendAlpha, TrendAlphaPolicy, resolve_forecast_costs
from beidou_strategy.portfolio.contracts import ExposureTarget
from beidou_strategy.portfolio.exposure_governor import ExposureGovernor, ExposurePolicy
from beidou_strategy.state.benchmark import (
    BenchmarkDefinition,
    BenchmarkSnapshot,
    BenchmarkSnapshotBuilder,
    BenchmarkType,
)

TIMESTAMP = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


class _Direction(Enum):
    UP = "UP"


def _costs() -> dict[str, object]:
    return {
        "expected_fee_bps": 2.0,
        "expected_slippage_bps": 1.0,
        "expected_funding_bps": 0.5,
        "source_hash": "cost-complete-v1",
    }


def _forecast(
    alpha_id: str = "alpha-v3",
    *,
    expected_return: float | None = 0.02,
    after_cost: float | None = 0.015,
    volatility: float | None = 0.2,
    timestamp: datetime = TIMESTAMP,
    instrument: str = "ETHUSDT",
    venue: str = "BINANCE",
    confidence: float = 0.8,
    regime_fit: float | None = 0.9,
    capacity: float | None = 0.8,
    cost_hash: str = "cost-complete-v1",
) -> AlphaForecast:
    return AlphaForecast(
        alpha_id=alpha_id,
        strategy_id=StrategyId("coverage-v3"),
        instrument_id=InstrumentId(instrument),
        venue_id=VenueId(venue),
        side="BUY" if (after_cost or 0.0) >= 0 else "SELL",
        raw_score=0.6,
        expected_return=expected_return,
        expected_return_after_cost=after_cost,
        expected_volatility=volatility,
        horizon_seconds=3600,
        probability_positive=0.8,
        confidence=confidence,
        uncertainty=1.0 - confidence,
        expected_fee_bps=2.0 if cost_hash else None,
        expected_slippage_bps=1.0 if cost_hash else None,
        expected_funding_bps=0.5 if cost_hash else None,
        market_beta=0.2,
        regime_fit=regime_fit,
        capacity_score=capacity,
        model_version=SchemaVersion("v3-model"),
        policy_version="coverage-policy-v1",
        feature_hash="coverage-features-v1",
        timestamp=timestamp,
        cost_source_hash=cost_hash,
    )


def _alpha_context(*, features: dict[str, object] | None = None, costs: object = None) -> dict[str, object]:
    base: dict[str, object] = {
        "instrument_id": "ETHUSDT",
        "venue_id": "BINANCE",
        "strategy_id": "coverage-v3",
        "timestamp": "2026-08-21T12:00:00+00:00",
        "features": {
            "benchmark_returns": {5: 0.03, 20: 0.10, 50: 0.25},
            "trend_slopes": {5: 0.003, 20: 0.01, 50: 0.025},
            "breadth": 0.85,
            "breakout_breadth": 0.80,
            "volume_expansion": 1.3,
            "realized_volatility": 0.2,
            "liquidity_score": 0.9,
            "market_beta": 0.2,
            "data_quality": "PASS",
            "feature_hash": "coverage-features-v1",
        },
    }
    if features is not None:
        base["features"] = features
    if costs is not None:
        base["costs"] = costs
    return base


def test_forecast_helpers_cover_invalid_and_canonical_inputs() -> None:
    assert finite(True) is None
    assert finite("bad") is None
    assert finite(float("inf")) is None
    assert finite("2.5") == 2.5
    assert clip(2.0, -1.0, 1.0) == 1.0
    assert parse_timestamp("not-a-time") is None
    assert parse_timestamp(1) is None
    assert parse_timestamp(datetime.fromisoformat("2026-01-01")).tzinfo is UTC
    assert parse_timestamp("2026-01-01T00:00:00+08:00").hour == 16
    assert resolve_timestamp({}, {}) == datetime(1970, 1, 1, tzinfo=UTC)
    assert context_features({"features": []}) == {}
    assert context_features({"features": {"x": 1}}) == {"x": 1}
    assert side_for_score(0.5, 0.2) == "BUY"
    assert side_for_score(-0.5, 0.2) == "SELL"
    assert side_for_score(0.0, 0.2) == "NO_ACTION"
    assert feature_payload({"a": 1, "b": 2}, {"b", "c"}) == {"b": 2}
    assert quality_factor({"data_quality": "PASS"}) == 1.0
    assert quality_factor({"quality": "GOOD"}) == 1.0
    assert quality_factor({"quality": "RELIABLE"}) == 1.0
    assert quality_factor({"quality": "CONDITIONAL"}) == 0.7
    assert quality_factor({"quality": "DEGRADED"}) == 0.4
    assert quality_factor({"quality": "UNKNOWN"}) == 0.0
    assert stable_hash({"enum": _Direction.UP, "set": {2, 1}, "nan": float("nan")})
    assert stable_hash({"datetime": TIMESTAMP})

    assert horizon_values({}, ("missing",)) == ({}, False)
    assert horizon_values({"values": {}}, ("values",)) == ({}, True)
    assert horizon_values({"values": []}, ("values",)) == ({}, True)
    assert horizon_values({"values": {"bad": 1}}, ("values",)) == ({}, True)
    assert horizon_values({"values": {0: 1}}, ("values",)) == ({}, True)
    assert horizon_values({"values": {1: float("nan")}}, ("values",)) == ({}, True)
    assert horizon_values({"values": {"1": 0.2}}, ("values",)) == ({1: 0.2}, False)

    assert resolve_costs({}, {"costs": _costs()}) == (2.0, 1.0, 0.5, "cost-complete-v1")
    assert resolve_costs({"expected_fee_bps": -1.0}, {}) == (None, None, None, "")
    assert resolve_costs({"expected_fee_bps": 1.0, "expected_slippage_bps": 1.0, "expected_funding_bps": 1.0}, {}) == (
        None,
        None,
        None,
        "",
    )
    assert point_in_time_universe(
        {"timestamp": "2026-08-21T12:00:00+00:00"},
        {"universe_snapshot_timestamp": "2026-08-21T11:00:00+00:00", "universe_source_hash": "u1"},
    )
    assert not point_in_time_universe(
        {"timestamp": "2026-08-21T12:00:00+00:00"},
        {"universe_snapshot_timestamp": "2026-08-21T13:00:00+00:00", "universe_source_hash": "u1"},
    )
    assert not point_in_time_universe({}, {"universe_snapshot_timestamp": "bad", "universe_source_hash": ""})


def _benchmark_definition(
    *, kind: BenchmarkType = BenchmarkType.UNIVERSE_INDEX, weighting: str = "EQUAL_WEIGHT"
) -> BenchmarkDefinition:
    components = (
        (InstrumentId("ETHUSDT"),)
        if kind is BenchmarkType.SINGLE_ASSET
        else (
            InstrumentId("ETHUSDT"),
            InstrumentId("SOLUSDT"),
        )
    )
    return BenchmarkDefinition(
        benchmark_id="coverage-benchmark",
        benchmark_type=kind,
        components=components,
        weighting_method=weighting,
        rebalance_rule="STATIC",
        policy_version="benchmark-policy-v3",
    )


def test_benchmark_contracts_cover_validation_and_weighting_branches() -> None:
    with pytest.raises(ValueError, match="benchmark_id"):
        BenchmarkDefinition("", BenchmarkType.SINGLE_ASSET, (InstrumentId("ETHUSDT"),), "DIRECT", "STATIC", "v1")
    with pytest.raises(ValueError, match="components"):
        BenchmarkDefinition("x", BenchmarkType.UNIVERSE_INDEX, (), "EQUAL_WEIGHT", "STATIC", "v1")
    with pytest.raises(ValueError, match="unique"):
        BenchmarkDefinition(
            "x",
            BenchmarkType.UNIVERSE_INDEX,
            (InstrumentId("ETHUSDT"), InstrumentId("ETHUSDT")),
            "EQUAL_WEIGHT",
            "STATIC",
            "v1",
        )
    with pytest.raises(ValueError, match="exactly one"):
        BenchmarkDefinition(
            "x",
            BenchmarkType.SINGLE_ASSET,
            (InstrumentId("ETHUSDT"), InstrumentId("SOLUSDT")),
            "DIRECT",
            "STATIC",
            "v1",
        )
    with pytest.raises(ValueError, match="weighting_method"):
        BenchmarkDefinition("x", BenchmarkType.SINGLE_ASSET, (InstrumentId("ETHUSDT"),), "", "STATIC", "v1")
    with pytest.raises(ValueError, match="rebalance_rule"):
        BenchmarkDefinition("x", BenchmarkType.SINGLE_ASSET, (InstrumentId("ETHUSDT"),), "DIRECT", "", "v1")
    with pytest.raises(ValueError, match="policy_version"):
        BenchmarkDefinition("x", BenchmarkType.SINGLE_ASSET, (InstrumentId("ETHUSDT"),), "DIRECT", "STATIC", "")

    definition = _benchmark_definition()
    assert definition.definition_hash == definition.definition_hash
    with pytest.raises(ValueError, match="unsupported universe weighting"):
        BenchmarkSnapshotBuilder(_benchmark_definition(weighting="BAD"))._resolve_weights(None)
    with pytest.raises(ValueError, match="does not accept"):
        BenchmarkSnapshotBuilder(definition)._resolve_weights({"ETHUSDT": 1.0, "SOLUSDT": 1.0})
    custom = BenchmarkSnapshotBuilder(_benchmark_definition(weighting="CUSTOM"))
    assert custom._resolve_weights({"ETHUSDT": 2.0, "SOLUSDT": 1.0})["ETHUSDT"] == pytest.approx(2 / 3)
    with pytest.raises(ValueError, match="match"):
        custom._resolve_weights({"ETHUSDT": 1.0})
    with pytest.raises(ValueError, match="positive total"):
        custom._resolve_weights({"ETHUSDT": 0.0, "SOLUSDT": 0.0})
    with pytest.raises(ValueError, match="negative"):
        custom._resolve_weights({"ETHUSDT": -1.0, "SOLUSDT": 1.0})
    assert BenchmarkSnapshotBuilder(
        _benchmark_definition(kind=BenchmarkType.SINGLE_ASSET, weighting="DIRECT")
    )._resolve_weights(None) == {"ETHUSDT": 1.0}
    with pytest.raises(ValueError, match="positive"):
        BenchmarkSnapshotBuilder(definition)._normalize_horizons((0,))
    with pytest.raises(ValueError, match="negative"):
        BenchmarkSnapshotBuilder(definition)._normalize_weights({"ETHUSDT": -1.0})
    with pytest.raises(ValueError, match="symbol"):
        BenchmarkSnapshotBuilder(definition)._normalize_history({"": (1.0,)})
    with pytest.raises(ValueError, match="positive"):
        BenchmarkSnapshotBuilder(definition)._normalize_history({"ETHUSDT": (1.0, 0.0)})
    with pytest.raises(ValueError, match="duplicate"):
        BenchmarkSnapshotBuilder(definition)._normalize_history({"ETHUSDT": (1.0,), " ETHUSDT ": (1.0,)})

    snapshot = BenchmarkSnapshot(
        benchmark_id="b",
        timestamp=TIMESTAMP,
        returns_by_horizon={1: 0.01},
        realized_vol_by_horizon={1: 0.2},
        breadth=0.5,
        breakout_breadth=0.5,
        dispersion=0.1,
        data_quality="PASS",
        source_hash="source",
    )
    assert snapshot.is_verifiable
    assert not replace(snapshot, data_quality="DEGRADED").is_verifiable
    assert not replace(snapshot, returns_by_horizon={}).is_verifiable
    with pytest.raises(ValueError, match="benchmark_id"):
        replace(snapshot, benchmark_id="")
    with pytest.raises(ValueError, match="timezone"):
        replace(snapshot, timestamp=datetime.fromisoformat("2026-01-01"))
    with pytest.raises(ValueError, match="unsupported"):
        replace(snapshot, data_quality="BAD")
    with pytest.raises(ValueError, match="source_hash"):
        replace(snapshot, source_hash="")
    with pytest.raises(ValueError, match="positive"):
        replace(snapshot, returns_by_horizon={0: 1.0})
    with pytest.raises(ValueError, match="volatility horizons"):
        replace(snapshot, realized_vol_by_horizon={0: 0.1})
    with pytest.raises(ValueError, match="volatility"):
        replace(snapshot, realized_vol_by_horizon={1: -1.0})
    with pytest.raises(ValueError, match="[0, 1]"):
        replace(snapshot, breadth=2.0)
    with pytest.raises(ValueError, match="dispersion"):
        replace(snapshot, dispersion=-1.0)


def test_benchmark_builder_covers_missing_and_custom_snapshot_paths() -> None:
    builder = BenchmarkSnapshotBuilder(_benchmark_definition(weighting="CUSTOM"))
    missing = builder.build(timestamp=TIMESTAMP, price_history={}, horizons=(1,))
    assert missing.data_quality == "NOT_VERIFIABLE"
    with pytest.raises(ValueError, match="horizons"):
        builder.build(timestamp=TIMESTAMP, price_history={}, horizons=())
    with pytest.raises(ValueError, match="unsupported"):
        builder.build(timestamp=TIMESTAMP, price_history={}, horizons=(1,), data_quality="BAD")
    valid = builder.build(
        timestamp=TIMESTAMP,
        price_history={"ETHUSDT": (100.0, 102.0, 104.0), "SOLUSDT": (50.0, 51.0, 53.0)},
        horizons=(1, 2),
        weights={"ETHUSDT": 3.0, "SOLUSDT": 1.0},
        breadth=0.8,
        breakout_breadth=0.7,
        dispersion=0.2,
    )
    assert valid.is_verifiable
    assert valid.returns_by_horizon[1] > 0


def test_mean_reversion_math_and_derivative_extensions_cover_fail_closed_paths() -> None:
    assert not estimate_half_life([1.0] * 10).valid
    assert not estimate_half_life([1.0] * 30).valid
    assert not estimate_half_life([1.0] * 29 + [float("nan")]).valid
    assert estimate_half_life([100.0 + math.sin(index / 3) for index in range(60)]).sample_count == 59
    assert robust_zscore([1.0] * 25)[-1] == 0.0
    assert robust_zscore([float(index) for index in range(25)])[-1] != 0.0
    with pytest.raises(ValueError, match="window"):
        robust_zscore([1.0], 0)

    funding = FundingRateSignal(VenueId("BINANCE"), InstrumentId("ETHUSDT"), 0.001, 20.0, source_hash="f")
    liquidation = LiquidationCascadeRisk(
        VenueId("BINANCE"), InstrumentId("ETHUSDT"), cascade_probability=0.2, source_hash="l"
    )
    assert funding.net_return_adjustment(1.0) == -0.001
    assert funding.net_return_adjustment(0.0) == 0.0
    assert liquidation.stress_scalar == 0.8
    context = DerivativeRiskContext.from_signals(funding=funding, liquidation=liquidation, position_sign=-1.0)
    assert context.is_verifiable
    assert context.funding_return_adjustment == 0.001
    assert not DerivativeRiskContext.from_signals(
        funding=FundingRateSignal(VenueId("BINANCE"), InstrumentId("ETHUSDT"), 0.001, 20.0),
        liquidation=None,
        position_sign=1.0,
    ).is_verifiable
    with pytest.raises(ValueError, match="at least one"):
        DerivativeRiskContext.from_signals(funding=None, liquidation=None, position_sign=0.0)
    with pytest.raises(ValueError, match="position_sign"):
        funding.net_return_adjustment(2.0)
    with pytest.raises(ValueError, match="finite"):
        FundingRateSignal(VenueId("B"), InstrumentId("E"), float("nan"), 1.0)
    with pytest.raises(ValueError, match="notionals"):
        LiquidationCascadeRisk(VenueId("B"), InstrumentId("E"), long_liq_cluster_notional=-1.0)
    with pytest.raises(ValueError, match="between"):
        LiquidationCascadeRisk(VenueId("B"), InstrumentId("E"), cascade_probability=2.0)
    with pytest.raises(ValueError, match="share"):
        DerivativeRiskContext.from_signals(
            funding=funding,
            liquidation=LiquidationCascadeRisk(VenueId("B"), InstrumentId("E"), source_hash="l"),
            position_sign=1.0,
        )
    assert asyncio.run(FundingRateExtension().analyze(InstrumentId("E"), VenueId("B")))["type"] == "funding_rate"
    assert (
        asyncio.run(LiquidationCascadeExtension().analyze(InstrumentId("E"), VenueId("B")))["type"]
        == "liquidation_cascade"
    )


def test_trend_policy_helpers_and_all_fail_closed_branches() -> None:
    with pytest.raises(ValueError):
        TrendAlphaPolicy(policy_version="")
    with pytest.raises(ValueError):
        TrendAlphaPolicy(horizons=(1,), horizon_weights=(0.2, 0.3))
    with pytest.raises(ValueError):
        TrendAlphaPolicy(horizons=(0, 1, 2), horizon_weights=(0.2, 0.3, 0.5))
    with pytest.raises(ValueError):
        TrendAlphaPolicy(horizon_weights=(-1.0, 0.0, 1.0))
    with pytest.raises(ValueError):
        TrendAlphaPolicy(horizon_weights=(0.0, 0.0, 0.0))
    with pytest.raises(ValueError):
        TrendAlphaPolicy(return_weight=-1.0)
    with pytest.raises(ValueError):
        TrendAlphaPolicy(return_scale=0.0)
    with pytest.raises(ValueError):
        TrendAlphaPolicy(entry_threshold=0.0)
    with pytest.raises(ValueError):
        TrendAlphaPolicy(expected_return_scale=-1.0)
    with pytest.raises(ValueError):
        TrendAlphaPolicy(horizon_seconds=0)

    assert resolve_forecast_costs(
        {
            "costs": {
                "expected_fee_bps": 1.0,
                "expected_slippage_bps": 1.0,
                "expected_funding_bps": 1.0,
                "source_hash": "s",
            }
        },
        {},
    ) == (1.0, 1.0, 1.0, "s")
    assert resolve_forecast_costs({}, {"expected_fee_bps": "bad"}) == (None, None, None, "")
    alpha = TrendAlpha()
    invalid_contexts = (
        _alpha_context(features={"benchmark_returns": {}, "realized_volatility": 0.2}),
        _alpha_context(
            features={"benchmark_returns": {5: 0.1}, "trend_slopes": {"bad": 1.0}, "realized_volatility": 0.2}
        ),
        _alpha_context(
            features={"benchmark_returns": {5: 0.1}, "trend_slopes": {5: float("nan")}, "realized_volatility": 0.2}
        ),
        _alpha_context(features={"benchmark_returns": {1: 0.1}, "trend_slopes": {1: 0.1}, "realized_volatility": 0.2}),
        _alpha_context(features={"benchmark_returns": {5: 0.1}, "trend_slopes": {5: 0.1}, "realized_volatility": -1.0}),
    )
    for context in invalid_contexts:
        assert alpha.generate_forecast(context).expected_return is None
    assert (
        alpha.generate_forecast(
            _alpha_context(
                features={
                    "benchmark_returns": {5: 0.1},
                    "trend_slopes": {5: 0.1},
                    "realized_volatility": 0.2,
                    "breadth": "bad",
                }
            )
        ).expected_return
        is not None
    )
    assert (
        alpha.generate_forecast(
            _alpha_context(
                features={
                    "benchmark_returns": {5: 0.1},
                    "trend_slopes": {5: 0.1},
                    "realized_volatility": 0.2,
                    "breadth": True,
                }
            )
        ).expected_return
        is not None
    )
    assert alpha.generate_forecast({"features": [], "timestamp": TIMESTAMP}).expected_return is None
    assert (
        alpha.generate_forecast(
            _alpha_context(features={"benchmark_returns": {5: 0.1}, "realized_volatility": 0.2})
        ).expected_return
        is not None
    )
    assert alpha._feature_hash({"benchmark_returns": [0.1]})
    assert alpha._feature_hash({"benchmark_returns": float("nan")})
    assert alpha.generate_forecast({"features": {}}).timestamp.tzinfo is UTC
    assert (
        alpha.generate_forecast({"features": {}, "timestamp": datetime.fromisoformat("2026-01-01")}).timestamp.tzinfo
        is UTC
    )

    base = _alpha_context()
    base_features = dict(base["features"])
    base_features.pop("breadth")
    base_features.pop("breakout_breadth")
    base_features.pop("volume_expansion")
    base["features"] = base_features
    no_optional = alpha.generate_forecast(base)
    assert no_optional.expected_return is not None
    sell_features = dict(base_features)
    sell_features.update(
        {"benchmark_returns": {5: -0.3, 20: -0.3, 50: -0.3}, "trend_slopes": {5: -0.03, 20: -0.03, 50: -0.03}}
    )
    sell_context = _alpha_context(features=sell_features)
    sell_context["state"] = {"direction": "TRENDING_DOWN"}
    assert alpha.generate_forecast(sell_context).side == "SELL"
    mismatch = _alpha_context()
    mismatch["state"] = {"direction": "TRENDING_DOWN"}
    assert alpha.generate_forecast(mismatch).regime_fit < 0.5
    neutral = _alpha_context(
        features={
            "benchmark_returns": {5: 0.0, 20: 0.0, 50: 0.0},
            "trend_slopes": {5: 0.0, 20: 0.0, 50: 0.0},
            "realized_volatility": 0.2,
        }
    )
    assert alpha.generate_forecast(neutral).side == "NO_ACTION"
    assert alpha.generate({}).expected_return is None
    assert TrendAlpha().validate()


def test_breakout_and_relative_strength_cover_negative_and_unknown_paths() -> None:
    for kwargs in (
        {"policy_version": ""},
        {"entry_threshold": 0.0},
        {"expected_return_scale": -1.0},
        {"horizon_seconds": 0},
    ):
        with pytest.raises(ValueError):
            BreakoutAlpha(**kwargs)
    alpha = BreakoutAlpha()
    assert alpha.validate()
    for features in (
        {},
        {"close": 0.0, "atr": 1.0, "high_history": [1, 2], "low_history": [1, 2]},
        {"close": 2.0, "atr": 0.0, "high_history": [1, 2], "low_history": [1, 2]},
        {"close": 2.0, "atr": 1.0, "high_history": [float("nan"), 2], "low_history": [1, 2]},
        {"close": 2.0, "atr": 1.0, "high_history": [1, 2], "low_history": [1, 2], "volume_ratio": 1.0, "breadth": 2.0},
    ):
        result = alpha.generate_forecast(_alpha_context(features=features))
        assert result.expected_return is None
    down = {
        "close": 80.0,
        "high_history": [100.0, 102.0],
        "low_history": [95.0, 96.0],
        "atr": 5.0,
        "volume_ratio": 1.5,
        "breadth": 0.8,
        "breakout_breadth": 0.8,
        "realized_volatility": 0.2,
        "data_quality": "CONDITIONAL",
    }
    assert alpha.generate_forecast(_alpha_context(features=down)).side == "SELL"
    assert alpha.generate(_alpha_context(features=down)).side == "SELL"
    no_cost = dict(down)
    assert alpha.generate_forecast(_alpha_context(features=no_cost)).expected_return_after_cost is None

    for kwargs in (
        {"horizons": (1,), "horizon_weights": (0.2, 0.3)},
        {"horizons": (0, 1), "horizon_weights": (0.4, 0.6)},
        {"horizons": (1,), "horizon_weights": (0.0,)},
        {"horizon_weights": (-1.0, 2.0)},
        {"score_scale": 0.0},
    ):
        with pytest.raises(ValueError):
            RelativeStrengthPolicy(**kwargs)
    relative = RelativeStrengthAlpha()
    good = {
        "asset_returns": {5: 0.1, 20: 0.2},
        "benchmark_returns": {5: 0.02, 20: 0.05},
        "universe_returns": {"ETHUSDT": {5: 0.1, 20: 0.2}, "SOLUSDT": {5: 0.0, 20: 0.0}},
        "universe_snapshot_timestamp": "2026-08-21T11:00:00+00:00",
        "universe_source_hash": "u1",
        "realized_volatility": 0.2,
        "data_quality": "PASS",
    }
    assert relative.generate_forecast(_alpha_context(features=good)).expected_return is not None
    for bad in (
        {**good, "universe_returns": []},
        {**good, "universe_returns": {"ETHUSDT": []}},
        {**good, "universe_returns": {"ETHUSDT": {5: 0.1}}},
        {**good, "universe_returns": {"ETHUSDT": {"bad": 1.0}, "SOLUSDT": {5: 0.1, 20: 0.2}}},
        {**good, "universe_returns": {"SOLUSDT": {5: 0.1, 20: 0.2}}},
        {**good, "asset_returns": {1: 0.1}, "benchmark_returns": {1: 0.1}},
    ):
        assert relative.generate_forecast(_alpha_context(features=bad)).expected_return is None
    no_available = {**good, "asset_returns": {1: 0.1}, "benchmark_returns": {20: 0.05}}
    assert relative.generate_forecast(_alpha_context(features=no_available)).expected_return is None
    nonstring = dict(good)
    nonstring.pop("universe_returns")
    nonstring["universe"] = {"ETHUSDT": {5: 0.1, 20: 0.2}}
    context = _alpha_context(features=nonstring)
    context["instrument_id"] = 123
    assert relative.generate_forecast(context).expected_return is None
    single = {**good, "universe_returns": {"ETHUSDT": {5: 0.1, 20: 0.2}}}
    assert relative.generate_forecast(_alpha_context(features=single)).expected_return is not None
    assert relative.generate(_alpha_context(features=good)).forecast_hash
    assert relative.validate()


def _residual_artifact() -> ResidualMomentumArtifact:
    return ResidualMomentumArtifact.create(
        artifact_id="residual-coverage",
        model_version="residual-model-coverage",
        feature_schema_version="features-v3",
        beta=0.8,
        residual_scale=0.02,
        training_window="train",
        oos_window="oos",
        regime_scope="ALL",
    )


def test_residual_artifact_and_forecast_cover_mapping_and_edge_paths() -> None:
    artifact = _residual_artifact()
    with pytest.raises(ValueError, match="invalid residual momentum policy"):
        ResidualMomentumAlpha(policy_version="")
    assert (
        ResidualMomentumArtifact.from_mapping(
            {
                "artifact_id": artifact.artifact_id,
                "model_version": artifact.model_version,
                "feature_schema_version": artifact.feature_schema_version,
                "beta": artifact.beta,
                "residual_scale": artifact.residual_scale,
                "training_window": artifact.training_window,
                "oos_window": artifact.oos_window,
                "regime_scope": artifact.regime_scope,
                "checksum": artifact.checksum,
            }
        )
        == artifact
    )
    assert ResidualMomentumArtifact.from_mapping({}) is None
    assert ResidualMomentumArtifact.from_mapping({**artifact.payload(), "checksum": "bad"}) is None
    for kwargs in (
        {
            "artifact_id": "",
            "model_version": "m",
            "feature_schema_version": "f",
            "beta": 0.0,
            "residual_scale": 1.0,
            "training_window": "t",
            "oos_window": "o",
            "regime_scope": "r",
        },
        {
            "artifact_id": "a",
            "model_version": "m",
            "feature_schema_version": "f",
            "beta": float("nan"),
            "residual_scale": 1.0,
            "training_window": "t",
            "oos_window": "o",
            "regime_scope": "r",
        },
        {
            "artifact_id": "a",
            "model_version": "m",
            "feature_schema_version": "f",
            "beta": 0.0,
            "residual_scale": 0.0,
            "training_window": "t",
            "oos_window": "o",
            "regime_scope": "r",
        },
    ):
        with pytest.raises(ValueError):
            ResidualMomentumArtifact(**kwargs)

    base = _alpha_context(
        features={
            "asset_returns": {5: 0.10, 20: 0.18},
            "benchmark_returns": {5: 0.08, 20: 0.10},
            "realized_volatility": 0.2,
            "data_quality": "PASS",
            "liquidity_score": 0.9,
        },
        costs=_costs(),
    )
    base["residual_artifact"] = artifact
    alpha = ResidualMomentumAlpha()
    assert alpha.generate_forecast(base).expected_return_after_cost is not None
    mapped = dict(base)
    mapped["residual_artifact"] = artifact.payload() | {"checksum": artifact.checksum}
    assert alpha.generate(mapped).model_version == artifact.model_version
    explicit = dict(base)
    explicit_features = dict(explicit["features"])
    explicit_features["residual_returns"] = {5: -0.2, 20: -0.1}
    explicit["features"] = explicit_features
    assert alpha.generate_forecast(explicit).side == "SELL"
    for bad in (
        {**base, "residual_artifact": 1},
        {
            **base,
            "residual_artifact": None,
            "features": {
                "asset_returns": {5: 0.1},
                "benchmark_returns": {5: 0.1},
                "residual_artifact": artifact,
            },
        },
    ):
        assert alpha.generate_forecast(bad).expected_return is None
    invalid_residual = dict(base)
    invalid_features = dict(invalid_residual["features"])
    invalid_features["residual_returns"] = {"bad": 1.0}
    invalid_residual["features"] = invalid_features
    assert alpha.generate_forecast(invalid_residual).expected_return is None
    no_overlap = dict(base)
    no_overlap_features = dict(no_overlap["features"])
    no_overlap_features["asset_returns"] = {5: 0.1}
    no_overlap_features["benchmark_returns"] = {20: 0.1}
    no_overlap["features"] = no_overlap_features
    assert alpha.generate_forecast(no_overlap).expected_return is None
    empty_asset = dict(base)
    empty_asset_features = dict(empty_asset["features"])
    empty_asset_features["asset_returns"] = {}
    empty_asset["features"] = empty_asset_features
    assert alpha.generate_forecast(empty_asset).expected_return is None
    unknown_costs = dict(base)
    unknown_costs.pop("costs")
    assert alpha.generate_forecast(unknown_costs).expected_return_after_cost is None
    assert alpha.validate()


def _artifact() -> CalibrationArtifact:
    return CalibrationArtifact.create(
        artifact_id="cal-coverage",
        model_version="cal-model-coverage",
        feature_schema_version="alpha-features-v3",
        coefficients={"raw_score": 0.02, "confidence": 0.01},
        intercept=0.001,
        training_window="train",
        oos_window="oos",
        regime_scope="ALL",
        metrics={"mae": 0.001},
        normalization={"raw_score": (0.0, 1.0)},
    )


def test_calibration_registry_and_fuser_cover_rejection_and_conflict_paths() -> None:
    artifact = _artifact()
    assert artifact.payload()["normalization"]
    assert artifact.verify_checksum()
    with pytest.raises(ValueError, match="identity and windows"):
        CalibrationArtifact.create(
            artifact_id="",
            model_version="m",
            feature_schema_version="f",
            coefficients={},
            intercept=0.0,
            training_window="t",
            oos_window="o",
            regime_scope="r",
            metrics={},
        )
    with pytest.raises(ValueError):
        CalibrationArtifact.create(
            artifact_id="a",
            model_version="m",
            feature_schema_version="f",
            coefficients={"x": float("nan")},
            intercept=0.0,
            training_window="t",
            oos_window="o",
            regime_scope="r",
            metrics={},
        )
    with pytest.raises(ValueError):
        CalibrationArtifact.create(
            artifact_id="a",
            model_version="m",
            feature_schema_version="f",
            coefficients={},
            intercept=float("inf"),
            training_window="t",
            oos_window="o",
            regime_scope="r",
            metrics={},
        )
    with pytest.raises(ValueError):
        CalibrationArtifact.create(
            artifact_id="a",
            model_version="m",
            feature_schema_version="f",
            coefficients={},
            intercept=0.0,
            training_window="t",
            oos_window="o",
            regime_scope="r",
            metrics={},
            normalization={"x": (0.0, 0.0)},
        )
    calibrator = DeterministicForecastCalibrator(artifact)
    assert calibrator.predict({"raw_score": 0.5, "confidence": 0.8}) is not None
    assert calibrator.predict({"raw_score": "bad", "confidence": 0.8}) is None
    assert calibrator.apply(_forecast(), {"raw_score": float("nan")}).expected_return is None
    assert calibrator.apply(_forecast(), {}).expected_return is not None
    assert calibrator.apply(_forecast(cost_hash=""), {}).expected_return_after_cost is None
    with pytest.raises(ValueError):
        DynamicEnsemblePolicy(policy_version="")
    with pytest.raises(ValueError):
        DynamicEnsemblePolicy(correlation_penalty=2.0)
    with pytest.raises(ValueError):
        DynamicEnsemblePolicy(min_component_quality=2.0)

    registry = CalibrationRegistry()
    assert registry.get_for_alpha("alpha") is None
    assert not registry.publish(artifact, evidence_ids=[], published_by="reviewer", alpha_id="alpha")
    assert registry.publish(artifact, evidence_ids=["e1"], published_by="reviewer", alpha_id="alpha")
    assert registry.get_for_alpha("alpha") == artifact
    assert registry.is_published(artifact)

    fuser = EnsembleFuser()
    with pytest.raises(ValueError, match="at least"):
        fuser.fuse([])
    with pytest.raises(ValueError, match="unique"):
        fuser.fuse([_forecast("same"), _forecast("same")])
    with pytest.raises(ValueError, match="share"):
        fuser.fuse([_forecast("a"), _forecast("b", instrument="BTCUSDT")])
    unknown = _forecast("unknown", expected_return=None, after_cost=None)
    result = fuser.fuse([unknown], reliability={"unknown": float("nan")})
    assert result.expected_return_after_cost is None
    assert result.conflict_score == 0.0
    positive = _forecast("positive")
    negative = replace(_forecast("negative", expected_return=-0.02, after_cost=-0.015), raw_score=-0.6, side="SELL")
    fused = fuser.fuse(
        [negative, positive],
        reliability={"positive": 0.8, "negative": 0.8},
        correlations={("positive", "negative"): 0.8},
        timestamp=datetime.fromisoformat("2026-08-21"),
    )
    assert fused.conflict_score > 0.0
    assert sum(component.weight for component in fused.components) == pytest.approx(1.0)
    missing_vol = replace(positive, expected_volatility=None)
    assert fuser.fuse([missing_vol]).expected_volatility is None
    missing_after = replace(positive, expected_return_after_cost=None)
    assert fuser.fuse([missing_after]).expected_return_after_cost is None
    assert fuser.fuse([positive], reliability={"positive": float("nan")}).components[0].reliability == 0.0
    strict_fuser = EnsembleFuser(DynamicEnsemblePolicy(min_component_quality=1.0))
    assert strict_fuser.fuse([positive]).expected_return_after_cost is None

    class FlappingForecast:
        alpha_id = "flapping"
        instrument_id = positive.instrument_id
        venue_id = positive.venue_id
        expected_return = 0.02
        confidence = 0.8
        regime_fit = 0.9
        capacity_score = 0.8
        expected_volatility = 0.2
        uncertainty = 0.2
        timestamp = TIMESTAMP
        model_version = SchemaVersion("flapping-model")

        def __init__(self) -> None:
            self._after_cost_reads = 0

        @property
        def expected_return_after_cost(self) -> float | None:
            self._after_cost_reads += 1
            return 0.015 if self._after_cost_reads == 1 else None

    flapping = cast(AlphaForecast, FlappingForecast())
    flapping_result = fuser.fuse([flapping])
    assert flapping_result.expected_return_after_cost is None
    assert flapping_result.components[0].weight > 0


def _target(**overrides: object) -> ExposureTarget:
    values: dict[str, object] = {
        "target_beta": 0.2,
        "beta_min": -0.7,
        "beta_max": 0.7,
        "target_gross": 0.5,
        "gross_min": 0.0,
        "gross_max": 1.0,
        "target_net": 0.2,
        "net_min": -1.0,
        "net_max": 1.0,
        "target_volatility": 0.2,
        "confidence": 0.8,
        "reason_codes": ("coverage",),
        "policy_version": "exposure-v3",
        "timestamp": TIMESTAMP,
    }
    values.update(overrides)
    return ExposureTarget.from_policy_values(**values)


def _market_state(*, quality: str = "PASS", stress: str = "NORMAL", tradable: bool = True) -> Any:
    direction = SimpleNamespace(trend_probability=0.8)
    stress_value = SimpleNamespace(stress_score=0.1, level=stress)
    quality_value = SimpleNamespace(tier=quality)
    return SimpleNamespace(
        direction=direction,
        stress=stress_value,
        quality=quality_value,
        is_tradable=(lambda: tradable),
    )


def test_exposure_policy_target_and_governor_cover_all_gates() -> None:
    for kwargs in (
        {"policy_version": ""},
        {"beta_min": 1.0, "beta_max": 0.0},
        {"gross_min": 1.0, "gross_max": 0.0},
        {"net_min": 1.0, "net_max": 0.0},
        {"gross_min": -1.0},
        {"base_gross": -1.0},
        {"base_net": -1.0},
        {"edge_scale": 0.0},
        {"target_volatility": -1.0},
        {"base_beta": float("nan")},
    ):
        with pytest.raises(ValueError):
            ExposurePolicy(**kwargs)
    for kwargs in (
        {"target_beta": float("nan")},
        {"beta_min": 1.0, "beta_max": 0.0},
        {"target_gross": -1.0},
        {"target_gross": 2.0},
        {"target_beta": 2.0},
        {"target_net": 2.0},
        {"target_net": 0.8, "target_gross": 0.1},
        {"confidence": 2.0},
        {"policy_version": ""},
        {"timestamp": datetime.fromisoformat("2026-01-01")},
    ):
        with pytest.raises(ValueError):
            _target(**kwargs)
    governor = ExposureGovernor()
    ensemble = EnsembleForecast(
        instrument_id=InstrumentId("ETHUSDT"),
        venue_id=VenueId("BINANCE"),
        expected_return=0.02,
        expected_return_after_cost=0.015,
        expected_volatility=0.2,
        uncertainty=0.2,
        conflict_score=0.0,
        components=(),
        model_version="m",
        timestamp=TIMESTAMP,
    )
    assert governor.compute(_market_state(), ensemble, account_facts={"equity": 100.0}).target_gross > 0
    assert governor.compute(_market_state(), ensemble, account_facts={}).reason_codes == ("ACCOUNT_EQUITY_UNKNOWN",)
    dict_state = {
        "direction": {"trend_probability": 0.8},
        "stress": {"stress_score": 0.1, "level": "NORMAL"},
        "quality": {"tier": "PASS"},
    }
    assert governor.compute(dict_state, ensemble, account_facts={"equity": 1.0}).target_gross > 0
    no_stress_state = {
        **dict_state,
        "stress": {"stress_score": 0.0, "level": "NORMAL"},
    }
    assert (
        "STRESS_SCALAR_APPLIED"
        not in governor.compute(no_stress_state, ensemble, account_facts={"equity": 1.0}).reason_codes
    )
    naive_target = governor.compute(
        _market_state(), ensemble, account_facts={"equity": 1.0}, timestamp=datetime.fromisoformat("2026-01-01")
    )
    assert naive_target.timestamp.tzinfo is UTC
    assert governor.compute(
        _market_state(), replace(ensemble, expected_return=None), account_facts={"equity": 1.0}
    ).reason_codes == ("FORECAST_NOT_VERIFIABLE",)
    assert governor.compute(
        _market_state(), replace(ensemble, expected_return_after_cost=0.0), account_facts={"equity": 1.0}
    ).reason_codes == ("NO_POSITIVE_EDGE_AFTER_COST",)
    assert governor.compute(_market_state(tradable=False), ensemble, account_facts={"equity": 1.0}).reason_codes == (
        "STATE_NOT_TRADABLE",
    )
    assert governor.compute(_market_state(quality="UNKNOWN"), ensemble, account_facts={"equity": 1.0}).reason_codes == (
        "QUALITY_NOT_VERIFIABLE",
    )
    assert governor.compute(_market_state(stress="CRISIS"), ensemble, account_facts={"equity": 1.0}).target_gross == 0.0
    assert (
        "STRESS_SCALAR_APPLIED"
        in governor.compute(_market_state(stress="ELEVATED"), ensemble, account_facts={"equity": 1.0}).reason_codes
    )
    assert (
        "QUALITY_SCALAR_APPLIED"
        in governor.compute(_market_state(quality="DEGRADED"), ensemble, account_facts={"equity": 1.0}).reason_codes
    )
    assert governor.compute(
        _market_state(), ensemble, risk_budget={"risk_scale": -1.0}, account_facts={"equity": 1.0}
    ).reason_codes == ("RISK_BUDGET_UNKNOWN",)
    assert governor.compute(
        _market_state(), ensemble, risk_budget={"gross_max": -1.0}, account_facts={"equity": 1.0}
    ).reason_codes == ("RISK_BUDGET_UNKNOWN",)
    zero_opportunity = governor.compute(
        _market_state(), replace(ensemble, expected_return_after_cost=1e-13), account_facts={"equity": 1.0}
    )
    assert zero_opportunity.target_gross == 0.0


def _full_attribution() -> AttributionRecord:
    return AttributionRecord(
        decision_id="coverage-decision",
        benchmark_id="benchmark",
        benchmark_return=0.02,
        portfolio_return=0.01,
        realized_beta_contribution=0.002,
        gross_exposure=0.5,
        net_exposure=0.2,
        portfolio_beta=0.2,
        target_beta=0.2,
        target_gross=0.5,
        target_net=0.2,
        target_volatility=0.2,
        alpha_contributions={"trend": 0.004},
        alpha_selection_contribution=0.004,
        timing_contribution=0.001,
        no_action_drag=0.0,
        veto_drag=0.0,
        degrade_drag=0.0,
        cash_drag=0.0,
        gross_constraint_drag=0.0,
        beta_constraint_drag=0.0,
        turnover_drag=0.0,
        liquidity_drag=0.0,
        fee_drag=-0.0002,
        slippage_drag=-0.0001,
        funding_drag=-0.0001,
        protection_contribution=0.0034,
        benchmark_snapshot_hash="b",
        alpha_forecast_hash="a",
        ensemble_forecast_hash="e",
        exposure_target_hash="x",
        portfolio_target_hash="p",
        source_hash="s",
        policy_version="p-v3",
        correlation_id=CorrelationId("corr"),
        timestamp=TIMESTAMP,
        evidence_references=(ReportReference("unit", "ref", SchemaVersion("3.0.0"), TIMESTAMP),),
    )


def test_attribution_trace_record_and_finalization_cover_unknown_paths() -> None:
    trace = DecisionTrace.from_probe(
        {"symbol": "ETHUSDT", "features": {"x": 1}, "state": {"state_hash": "s"}, "timestamp": "2026-08-21T12:00:00"}
    )
    assert trace.timestamp.tzinfo is not None
    assert trace.market_data_hash and trace.state_hash == "s"
    trace2 = DecisionTrace.from_probe(
        {"timestamp": TIMESTAMP, "market_data_hash": "m", "state": {}, "correlation_id": CorrelationId("c")}
    )
    assert trace2.market_data_hash == "m"
    trace3 = DecisionTrace.from_probe({"timestamp": TIMESTAMP, "state": None})
    assert trace3.state_hash
    linked = trace.link_attribution(_full_attribution().finalize(residual_tolerance=1.0))
    assert linked.attribution_record_id == "coverage-decision"
    assert linked.to_dict()["status"] == EvidenceTier.VERIFIED.value
    record = _full_attribution()
    assert record.is_complete
    assert record.is_full_attribution_complete
    assert record.calculate_residual() == pytest.approx(0.0)
    assert record.field_values()["decision_id"] == "coverage-decision"
    assert record.finalize(residual_tolerance=1e-12, require_full=True).evidence_tier is EvidenceTier.VERIFIED
    with pytest.raises(ValueError, match="residual_tolerance"):
        record.finalize(residual_tolerance=-1.0)
    with pytest.raises(ValueError, match="finite"):
        replace(record, portfolio_return=float("nan"))
    with pytest.raises(ValueError, match="decision_id"):
        replace(record, decision_id="")
    with pytest.raises(ValueError, match="timezone"):
        replace(record, timestamp=datetime.fromisoformat("2026-01-01"))
    with pytest.raises(ValueError, match="gross_exposure"):
        replace(record, gross_exposure=-1.0)
    with pytest.raises(ValueError, match="target_gross"):
        replace(record, target_gross=-1.0)
    with pytest.raises(ValueError, match="target_volatility"):
        replace(record, target_volatility=-1.0)
    with pytest.raises(ValueError, match="alpha_contributions"):
        replace(record, alpha_contributions={"": 1.0})
    incomplete = replace(record, portfolio_return=None, alpha_contributions=())
    assert not incomplete.is_complete
    assert incomplete.calculate_residual() is None
    assert incomplete.finalize(residual_tolerance=1.0).evidence_tier is EvidenceTier.NOT_VERIFIABLE
    divergent = replace(record, portfolio_return=0.5)
    assert divergent.finalize(residual_tolerance=1e-12, require_full=True).evidence_tier is EvidenceTier.NOT_VERIFIABLE


def _published_registry() -> CalibrationRegistry:
    registry = CalibrationRegistry()
    for alpha_id in ("trend_v3", "breakout-v3", "relative-strength-v3", "residual-momentum-v3", "mean_reversion_v3"):
        artifact = CalibrationArtifact.create(
            artifact_id=f"cal-{alpha_id}-coverage",
            model_version=f"model-{alpha_id}-coverage",
            feature_schema_version="alpha-features-v3",
            coefficients={"raw_score": 0.02},
            intercept=0.005,
            training_window="train",
            oos_window="oos",
            regime_scope="ALL",
            metrics={"mae": 0.001},
        )
        assert registry.publish(artifact, evidence_ids=[f"e-{alpha_id}"], published_by="coverage", alpha_id=alpha_id)
    return registry


def _pipeline_context(*, with_rules: bool = True) -> dict[str, Any]:
    features: dict[str, Any] = {
        "benchmark_returns": {5: 0.03, 20: 0.10, 50: 0.25},
        "trend_slopes": {5: 0.003, 20: 0.01, 50: 0.025},
        "breadth": 0.85,
        "breakout_breadth": 0.80,
        "volume_expansion": 1.3,
        "realized_volatility": 0.2,
        "dispersion": 0.1,
        "cross_sectional_correlation": 0.3,
        "liquidity_score": 0.9,
        "market_beta": 1.0,
        "data_quality": "PASS",
        "close": 120.0,
        "high_history": [100.0, 102.0, 105.0, 108.0, 110.0],
        "low_history": [95.0, 96.0, 98.0, 99.0, 100.0],
        "atr": 5.0,
        "volume_ratio": 2.0,
        "asset_returns": {5: 0.10, 20: 0.20},
        "prices": [100.0 + index for index in range(30)],
        "market_regime": "RANGING",
        "residual_artifact": _residual_artifact(),
        "universe_returns": {"ETHUSDT": {5: 0.10, 20: 0.20}, "BTCUSDT": {5: 0.02, 20: 0.05}},
        "universe_snapshot_timestamp": "2026-08-21T11:59:00+00:00",
        "universe_source_hash": "universe-v1",
    }
    if with_rules:
        features["exchange_rules"] = {"min_notional": 10.0, "step_size": 0.001, "tick_size": 0.01}
    return {
        "timestamp": "2026-08-21T12:00:00+00:00",
        "instrument_id": "ETHUSDT",
        "venue_id": "BINANCE",
        "features": features,
        "costs": _costs(),
        "account_facts": {"equity": 100000.0},
    }


def test_pipeline_covers_derivative_calibration_and_blocked_runtime_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = AlphaV3ShadowEngine(_published_registry())
    result = engine.evaluate(_pipeline_context())
    assert result.to_dict()["forecast_count"] == 5
    assert result.trace_hash
    injection_engine = AlphaV3ShadowEngine(_published_registry())
    injected_snapshot = BenchmarkSnapshot(
        benchmark_id="injected-benchmark",
        timestamp=TIMESTAMP,
        returns_by_horizon={5: 0.01, 20: 0.02, 50: 0.03},
        realized_vol_by_horizon={5: 0.2, 20: 0.2, 50: 0.2},
        breadth=0.5,
        breakout_breadth=0.5,
        dispersion=0.1,
        data_quality="PASS",
        source_hash="injected-source",
    )
    monkeypatch.setattr(injection_engine, "_benchmark_snapshot", lambda *args, **kwargs: injected_snapshot)
    injected_context = _pipeline_context()
    injected_features = dict(injected_context["features"])
    injected_features.pop("benchmark_returns")
    injected_features.pop("realized_volatility")
    injected_context["features"] = injected_features
    injected = injection_engine.evaluate(injected_context)
    assert injected.benchmark_snapshot_hash
    assert injected.to_dict()["forecast_count"] == 5
    account_type_context = _pipeline_context()
    account_type_context["account_facts"] = []
    account_type_result = engine.evaluate(account_type_context)
    assert "ACCOUNT_EQUITY_UNKNOWN" in account_type_result.exposure_target.reason_codes
    invalid_risk = DerivativeRiskContext(VenueId("BINANCE"), InstrumentId("ETHUSDT"), None, 0.5, "", "", TIMESTAMP)
    forecasts = tuple(_forecast(f"a-{index}") for index in range(2))
    adjusted, reasons = engine._apply_derivative_risk(forecasts, {}, {})
    assert adjusted == forecasts and reasons == ()
    adjusted, reasons = engine._apply_derivative_risk(forecasts, {"derivative_risk_context": invalid_risk}, {})
    assert reasons == ("DERIVATIVE_RISK_NOT_VERIFIABLE",)
    valid_risk = DerivativeRiskContext(
        VenueId("BINANCE"), InstrumentId("ETHUSDT"), 0.001, 0.5, "risk", "policy", TIMESTAMP
    )
    adjusted, reasons = engine._apply_derivative_risk(
        (replace(_forecast(), capacity_score=None, regime_fit=None),), {"derivative_risk_context": valid_risk}, {}
    )
    assert not reasons and adjusted[0].expected_return_after_cost is not None
    no_funding_risk = DerivativeRiskContext(
        VenueId("BINANCE"), InstrumentId("ETHUSDT"), None, 0.8, "risk", "policy", TIMESTAMP
    )
    adjusted, reasons = engine._apply_derivative_risk(forecasts, {"derivative_risk_context": no_funding_risk}, {})
    assert not reasons and adjusted[0].expected_return_after_cost is not None
    missing_fee = replace(_forecast(), expected_fee_bps=None)
    adjusted, reasons = engine._apply_derivative_risk((missing_fee,), {"derivative_risk_context": valid_risk}, {})
    assert not reasons and adjusted[0].expected_return_after_cost == missing_fee.expected_return_after_cost
    assert engine._apply_published_calibration((_forecast(expected_return=None),), {})[1]
    bad_context = _pipeline_context()
    bad_features = dict(bad_context["features"])
    bad_features["benchmark_returns"] = {"bad": 1.0}
    bad_context["features"] = bad_features
    snapshot = engine._benchmark_snapshot(bad_context, bad_features, InstrumentId("ETHUSDT"), TIMESTAMP)
    assert snapshot.returns_by_horizon == {}
    bad_features["realized_volatility"] = "bad"
    assert (
        engine._benchmark_snapshot(
            bad_context,
            {"benchmark_returns": {5: 0.1}, "realized_volatility": "bad"},
            InstrumentId("ETHUSDT"),
            TIMESTAMP,
        ).realized_vol_by_horizon
        == {}
    )
    bad_features["data_quality"] = "bad"
    assert (
        engine._benchmark_snapshot(bad_context, bad_features, InstrumentId("ETHUSDT"), TIMESTAMP).data_quality
        == "UNKNOWN"
    )
    target = _target()
    ensemble = EnsembleForecast(
        InstrumentId("ETHUSDT"), VenueId("BINANCE"), 0.02, 0.01, 0.2, 0.2, 0.0, (), "m", TIMESTAMP
    )
    optimizer_input = engine._optimizer_input(
        {"costs": [], "account_facts": {"equity": 1.0}},
        {"exchange_rules": [], "liquidity": "bad", "market_beta": "bad"},
        ensemble,
        target,
        InstrumentId("ETHUSDT"),
    )
    assert optimizer_input.account_equity == 1.0
    monkeypatch.setattr(
        engine, "_optimizer_input", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("bad input"))
    )
    blocked = engine.evaluate(_pipeline_context())
    assert blocked.portfolio_result.weights == {}
    assert any(reason.startswith("PORTFOLIO_INPUT_UNKNOWN") for reason in blocked.failure_reasons)

    def raise_calibration(*args: object, **kwargs: object) -> AlphaForecast:
        raise ValueError("invalid calibration application")

    monkeypatch.setattr(DeterministicForecastCalibrator, "apply", raise_calibration)
    calibrated, failures = engine._apply_published_calibration((_forecast("trend_v3"),), {})
    assert calibrated[0].expected_return is None
    assert failures == ("CALIBRATION_ARTIFACT_INVALID:trend_v3",)


def _challenger_series() -> dict[str, object]:
    return {
        "benchmark_returns": (0.01, -0.01, 0.02, -0.02, 0.01, -0.01, 0.02, -0.02),
        "v2_gross_returns": (0.008, -0.008, 0.015, -0.015, 0.008, -0.008, 0.015, -0.015),
        "v3_gross_returns": (0.010, -0.009, 0.018, -0.014, 0.010, -0.009, 0.018, -0.014),
        "v2_positions": (1.0,) * 8,
        "v3_positions": (1.0,) * 8,
        "cost_bps": 5.0,
        "data_snapshot_hash_v2": "data",
        "data_snapshot_hash_v3": "data",
        "cost_model_hash_v2": "cost",
        "cost_model_hash_v3": "cost",
        "train_bars": 2,
        "oos_bars": 2,
        "regimes": ("BULL", "BULL", "RANGE", "RANGE", "BEAR", "BEAR", "RANGE", "RANGE"),
        "v2_beta_exposure": (1.0,) * 8,
        "v3_beta_exposure": (1.0,) * 8,
        "point_in_time_validated": True,
    }


def test_challenger_helpers_and_rejection_inputs_cover_metrics_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _finite_series([1, 2], "x") == (1.0, 2.0)
    with pytest.raises(ValueError):
        _finite_series([], "x")
    with pytest.raises(ValueError):
        _finite_series([float("nan")], "x")
    assert _compound([-1.0]) == -1.0
    assert _compound([0.1, -0.2]) < 0
    assert _max_drawdown([0.1, -0.2]) < 0
    assert _sample_std([1.0]) == 0.0
    assert _sample_std([1.0, 2.0]) > 0
    assert _ratio(1.0, 0.0) is None
    assert _ratio(1.0, 2.0) == 0.5
    assert _series_hash([1.0])
    net, costs = _apply_costs([0.01, -0.01], [1.0, -1.0], 5.0)
    assert net[0] < 0.01 and sum(costs) > 0
    metrics = _metrics([0.01, -0.01], [0.02, -0.02], [1.0, -1.0], None, 5.0)
    assert metrics.avg_beta_exposure is None
    assert _paper_dict(None) is None

    values = _challenger_series()
    from beidou_research.backtest.replay import PaperReplayResult

    values["paper_evidence"] = PaperReplayResult(window_bars=20, n_trades=2, evidence_source="paper")
    report = run_alpha_v3_challenger(**values)
    assert report.status == "PASS"
    assert report.to_dict()["paper_evidence"]["window_bars"] == 20
    no_beta = run_alpha_v3_challenger(
        **{**values, "v2_beta_exposure": None, "v3_beta_exposure": None, "paper_evidence": None}
    )
    assert no_beta.v2.avg_beta_exposure is None
    assert no_beta.v3.avg_beta_exposure is None
    no_evidence = run_alpha_v3_challenger(
        **{**values, "paper_evidence": None, "regimes": None, "point_in_time_validated": False}
    )
    assert no_evidence.status == "NOT_VERIFIABLE"
    with pytest.raises(ValueError):
        run_alpha_v3_challenger(**{**values, "v3_positions": (1.0,)})
    with pytest.raises(ValueError):
        run_alpha_v3_challenger(**{**values, "train_bars": 0})
    with pytest.raises(ValueError):
        run_alpha_v3_challenger(**{**values, "cost_bps": -1.0})
    with pytest.raises(ValueError):
        run_alpha_v3_challenger(**{**values, "v3_beta_exposure": (1.0,)})
    with pytest.raises(ValueError):
        run_alpha_v3_challenger(**{**values, "regimes": ("BULL",)})
    missing_hash = run_alpha_v3_challenger(**{**values, "data_snapshot_hash_v2": None})
    assert not missing_hash.checks["same_data"]
    missing_cost = run_alpha_v3_challenger(**{**values, "cost_model_hash_v2": None})
    assert not missing_cost.checks["same_cost_model"]
    empty_paper = PaperReplayResult(window_bars=0, n_trades=0, evidence_source="")
    assert not run_alpha_v3_challenger(**{**values, "paper_evidence": empty_paper}).checks["paper_shadow"]

    # Missing exposure evidence is a distinct fail-closed state, not a
    # leverage claim.  Exercise the defensive branch with a typed report.
    import beidou_research.backtest.alpha_v3_challenger as challenger

    original_metrics = challenger._metrics
    base_metrics = original_metrics([0.01], [0.01], [1.0], None, 0.0)
    unavailable = replace(base_metrics, avg_gross_exposure=None)
    monkeypatch.setattr(challenger, "_metrics", lambda *args, **kwargs: unavailable)
    missing_exposure = run_alpha_v3_challenger(**{**values, "paper_evidence": None})
    assert missing_exposure.leverage_only_improvement is None
