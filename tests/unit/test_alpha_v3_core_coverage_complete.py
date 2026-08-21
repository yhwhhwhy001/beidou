"""Full-contract fixtures for the execution package's V3 core modules.

The tests in this module exercise both successful data flow and the explicit
UNKNOWN/rejection paths required by the V3 package.  They are intentionally
semantic: each boundary assertion checks the returned contract or exception,
not merely that a line executed.
"""

from __future__ import annotations

import asyncio
from dataclasses import fields, replace
from datetime import UTC, datetime

import pytest

from beidou_shared.types import (
    InstrumentId,
    ModelId,
    MonetaryValue,
    OrderSide,
    Quantity,
    SchemaVersion,
    StrategyId,
    VenueId,
)
from beidou_strategy.alpha import AlphaComponentType, AlphaSignal, SignalDirection
from beidou_strategy.alpha.contracts import (
    DEGRADE_MULTIPLIER,
    AlphaForecast,
    DataQualityTier,
    EnsembleComponent,
    EnsembleForecast,
    EntryProposal,
    FilterDecision,
    FilterResult,
    KLineEvent,
    MarketEvent,
    StrategyProposal,
    _forecast_value,
)
from beidou_strategy.alpha.extensions import (
    ContractExtension,
    DerivativeRiskContext,
    FundingRateExtension,
    FundingRateSignal,
    LiquidationCascadeExtension,
    LiquidationCascadeRisk,
)
from beidou_strategy.alpha.mean_reversion import MeanReversionAlpha, MeanReversionEngine, MultiPeriodMomentum
from beidou_strategy.alpha.model_registry import (
    CalibrationRegistry,
    DriftDetector,
    ModelRecord,
    ModelRegistry,
    ModelStatus,
)
from beidou_strategy.alpha.signal_fusion import SignalFuser
from beidou_strategy.alpha.typed_graph import (
    EntryNode,
    ExitNode,
    FeatureNode,
    FilterNode,
    ForecastNode,
    FusionNode,
    NodeFailurePolicy,
    NodeType,
    TypedAlphaGraph,
    TypedGraphNode,
    TypedNodeOutput,
)
from beidou_strategy.portfolio import PortfolioTarget, PositionOwnership
from beidou_strategy.portfolio import optimizer as optimizer_module
from beidou_strategy.portfolio.constraints import ConstraintOptimizer
from beidou_strategy.portfolio.contracts import PortfolioOptimizationInput
from beidou_strategy.portfolio.exposure_governor import ExposureTarget
from beidou_strategy.portfolio.optimizer import (
    ActiveOptimizationResult,
    ActivePortfolioOptimizer,
    ActivePortfolioPolicy,
    PortfolioOptimizerImpl,
    signed_exposure_math,
)
from beidou_strategy.state import market_state as market_state_module
from beidou_strategy.state.market_state import (
    MarketStateEstimator,
    MarketStatePolicy,
    QualityState,
    StressState,
)

TIMESTAMP = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def _forecast(
    alpha_id: str = "core-alpha",
    *,
    expected_return: float | None = 0.02,
    after_cost: float | None = 0.01965,
    volatility: float | None = 0.2,
) -> AlphaForecast:
    return AlphaForecast(
        alpha_id=alpha_id,
        strategy_id=StrategyId("core-v3"),
        instrument_id=InstrumentId("ETHUSDT"),
        venue_id=VenueId("BINANCE"),
        side=OrderSide.BUY,
        raw_score=0.6,
        expected_return=expected_return,
        expected_return_after_cost=after_cost,
        expected_volatility=volatility,
        horizon_seconds=3600,
        probability_positive=0.8,
        confidence=0.8,
        uncertainty=0.2,
        expected_fee_bps=2.0,
        expected_slippage_bps=1.0,
        expected_funding_bps=0.5,
        market_beta=0.2,
        regime_fit=0.9,
        capacity_score=0.8,
        model_version=SchemaVersion("core-model"),
        policy_version="core-policy",
        feature_hash="core-features",
        timestamp=TIMESTAMP,
        cost_source_hash="core-cost",
    )


def _ensemble(value: float = 0.02, *, after_cost: float | None = 0.0165) -> EnsembleForecast:
    return EnsembleForecast(
        instrument_id=InstrumentId("ETHUSDT"),
        venue_id=VenueId("BINANCE"),
        expected_return=value,
        expected_return_after_cost=after_cost,
        expected_volatility=0.2,
        uncertainty=0.2,
        conflict_score=0.0,
        components=(
            EnsembleComponent(
                alpha_id="core-alpha",
                weight=1.0,
                expected_return=value,
                contribution=after_cost,
                regime_fit=0.9,
                reliability=0.9,
            ),
        ),
        model_version="core-ensemble",
        timestamp=TIMESTAMP,
    )


def _exposure_target(**overrides: object) -> ExposureTarget:
    values: dict[str, object] = {
        "target_beta": 0.2,
        "beta_min": -0.8,
        "beta_max": 0.8,
        "target_gross": 0.6,
        "gross_min": 0.0,
        "gross_max": 0.8,
        "target_net": 0.2,
        "net_min": -0.8,
        "net_max": 0.8,
        "target_volatility": 0.2,
        "confidence": 0.8,
        "reason_codes": ("core",),
        "policy_version": "core-exposure-policy",
        "timestamp": TIMESTAMP,
    }
    values.update(overrides)
    return ExposureTarget.from_policy_values(**values)


def _market_features() -> dict[str, object]:
    return {
        "benchmark_returns": {5: 0.03, 20: 0.10, 50: 0.25},
        "trend_slopes": {5: 0.003, 20: 0.01, 50: 0.025},
        "breadth": 0.85,
        "breakout_breadth": 0.8,
        "volume_expansion": 1.3,
        "realized_volatility": 0.2,
        "dispersion": 0.1,
        "cross_sectional_correlation": 0.3,
        "liquidity_score": 0.9,
        "data_quality": "PASS",
    }


def test_alpha_forecast_and_ensemble_contracts_cover_all_validation_paths() -> None:
    forecast = _forecast()
    assert forecast.cost_verifiable
    assert forecast.total_cost_bps == pytest.approx(3.5)
    assert forecast.cost_breakdown["source_hash"] == "core-cost"
    assert not replace(forecast, cost_source_hash="").cost_verifiable
    assert not replace(forecast, expected_fee_bps=None).cost_verifiable
    assert not replace(forecast, expected_return_after_cost=0.0).cost_verifiable

    with pytest.raises(ValueError, match="non-empty"):
        replace(forecast, alpha_id="")
    with pytest.raises(ValueError, match="timezone"):
        replace(forecast, timestamp=datetime.fromisoformat("2026-01-01"))
    with pytest.raises(ValueError, match="positive"):
        replace(forecast, horizon_seconds=0)
    with pytest.raises(ValueError, match="finite"):
        replace(forecast, expected_return=float("nan"))
    with pytest.raises(ValueError, match=">="):
        replace(forecast, expected_fee_bps=-1.0)
    with pytest.raises(ValueError, match="within"):
        replace(forecast, confidence=2.0)
    assert replace(forecast, regime_fit=None).regime_fit is None
    with pytest.raises(ValueError, match="unsupported"):
        replace(forecast, side="INVALID")

    assert _forecast_value(OrderSide.BUY) == "BUY"
    assert _forecast_value([OrderSide.SELL, (1, 2)]) == ["SELL", [1, 2]]
    assert _forecast_value({"b": OrderSide.BUY, "a": 1})["b"] == "BUY"
    event = MarketEvent(VenueId("BINANCE"), InstrumentId("ETHUSDT"), TIMESTAMP)
    assert _forecast_value(event)["venue_id"] == "BINANCE"
    kline = KLineEvent(VenueId("BINANCE"), InstrumentId("ETHUSDT"), TIMESTAMP, close=101.0)
    assert _forecast_value(kline)["close"] == 101.0

    with pytest.raises(ValueError, match="alpha_id"):
        EnsembleComponent("", 0.1, 0.01, 0.01, 0.5, 0.5)
    with pytest.raises(ValueError, match="within"):
        EnsembleComponent("x", 0.1, 0.01, 0.01, 1.1, 0.5)
    ensemble = _ensemble()
    assert ensemble.forecast_hash
    with pytest.raises(ValueError, match="timezone"):
        replace(ensemble, timestamp=datetime.fromisoformat("2026-01-01"))
    with pytest.raises(ValueError, match="policy"):
        replace(ensemble, policy_version="")
    with pytest.raises(ValueError, match="within"):
        replace(ensemble, conflict_score=2.0)

    entry = EntryProposal(
        strategy_id=StrategyId("core"),
        instrument_id=InstrumentId("ETHUSDT"),
        venue_id=VenueId("BINANCE"),
        side=OrderSide.BUY,
        metadata={"enum": OrderSide.SELL, "nested": [1, {"side": OrderSide.BUY}]},
    )
    proposal = StrategyProposal(
        strategy_id=StrategyId("core"),
        instrument_id=InstrumentId("ETHUSDT"),
        venue_id=VenueId("BINANCE"),
        side=OrderSide.BUY,
        entry_proposals=[entry],
        filter_results=[FilterResult(FilterDecision.DEGRADE, DEGRADE_MULTIPLIER, DEGRADE_MULTIPLIER)],
    )
    assert proposal.hash()


def test_derivative_extensions_and_calibration_registry_cover_rejection_paths() -> None:
    with pytest.raises(ValueError, match="confidence"):
        FundingRateSignal(VenueId("B"), InstrumentId("E"), 0.1, 1.0, confidence=2.0)
    with pytest.raises(ValueError, match="finite"):
        FundingRateSignal(VenueId("B"), InstrumentId("E"), float("nan"), 1.0)
    with pytest.raises(ValueError, match="policy"):
        FundingRateSignal(VenueId("B"), InstrumentId("E"), 0.1, 1.0, policy_version="")
    with pytest.raises(ValueError, match="policy"):
        LiquidationCascadeRisk(VenueId("B"), InstrumentId("E"), policy_version="")
    with pytest.raises(ValueError, match="between"):
        LiquidationCascadeRisk(VenueId("B"), InstrumentId("E"), cascade_probability=2.0)
    with pytest.raises(ValueError, match="finite"):
        LiquidationCascadeRisk(VenueId("B"), InstrumentId("E"), long_liq_cluster_notional=float("inf"))

    funding = FundingRateSignal(VenueId("B"), InstrumentId("E"), 0.001, 10.0, source_hash="funding")
    liquidation = LiquidationCascadeRisk(VenueId("B"), InstrumentId("E"), cascade_probability=0.2, source_hash="liq")
    assert DerivativeRiskContext.from_signals(funding=funding, liquidation=None, position_sign=1.0).is_verifiable
    assert DerivativeRiskContext.from_signals(funding=None, liquidation=liquidation, position_sign=1.0).is_verifiable
    with pytest.raises(ValueError, match="share"):
        DerivativeRiskContext.from_signals(
            funding=funding,
            liquidation=LiquidationCascadeRisk(VenueId("OTHER"), InstrumentId("E"), source_hash="liq"),
            position_sign=1.0,
        )
    assert not DerivativeRiskContext.from_signals(
        funding=replace(funding, source_hash=""), liquidation=None, position_sign=1.0
    ).is_verifiable
    with pytest.raises(ValueError, match="position_sign"):
        funding.net_return_adjustment(2.0)
    with pytest.raises(ValueError, match="at least"):
        DerivativeRiskContext.from_signals(funding=None, liquidation=None, position_sign=0.0)
    assert asyncio.run(FundingRateExtension().analyze(InstrumentId("E"), VenueId("B")))
    assert asyncio.run(LiquidationCascadeExtension().analyze(InstrumentId("E"), VenueId("B")))

    class BareExtension(ContractExtension):
        async def analyze(self, instrument_id: InstrumentId, venue_id: VenueId) -> dict:
            return await super().analyze(instrument_id, venue_id)

    assert asyncio.run(BareExtension().analyze(InstrumentId("E"), VenueId("B"))) is None

    registry = CalibrationRegistry()
    artifact = __import__(
        "beidou_strategy.alpha.forecast", fromlist=["CalibrationArtifact"]
    ).CalibrationArtifact.create(
        artifact_id="core-cal",
        model_version="core-model",
        feature_schema_version="core-features",
        coefficients={"raw_score": 0.1},
        intercept=0.0,
        training_window="train",
        oos_window="oos",
        regime_scope="ALL",
        metrics={"mae": 0.01},
    )
    assert registry.publish(artifact, evidence_ids=["e1"], published_by="reviewer", alpha_id="core")
    changed = __import__("beidou_strategy.alpha.forecast", fromlist=["CalibrationArtifact"]).CalibrationArtifact.create(
        artifact_id="core-cal",
        model_version="changed-model",
        feature_schema_version="core-features",
        coefficients={"raw_score": 0.2},
        intercept=0.0,
        training_window="train",
        oos_window="oos",
        regime_scope="ALL",
        metrics={"mae": 0.01},
    )
    assert not registry.publish(changed, evidence_ids=["e1"], published_by="reviewer", alpha_id="core")
    assert registry.get_for_alpha("core") == artifact
    assert registry.get_for_alpha("missing") is None
    assert registry.registration("missing") is None

    models = ModelRegistry()
    sid = StrategyId("core")
    first = ModelRecord(ModelId("m1"), sid, ModelStatus.CHALLENGER, SchemaVersion("1"))
    second = ModelRecord(ModelId("m2"), sid, ModelStatus.CHALLENGER, SchemaVersion("1"))
    models.register_model(first)
    models.register(second)
    assert len(models.list_models(sid)) == 2
    assert not models.promote_to_champion(sid, ModelId("missing"))
    assert models.promote_to_champion(sid, ModelId("m1"))
    assert models.promote_to_champion(sid, ModelId("m1"))
    assert models.get_champion(sid) == first
    assert models.demote_champion(StrategyId("missing")) is False
    assert models.demote_champion(sid, "drift")
    assert models.get_champion(sid) is None
    models.register(ModelRecord(ModelId("m3"), sid, ModelStatus.CHALLENGER, SchemaVersion("1")))
    models._champions[sid] = ModelId("missing")
    assert models.get_champion(sid) is None
    retired = models.retire_strategy(sid)
    assert len(retired) == 3 and all(model.status is ModelStatus.RETIRED for model in retired)

    detector = DriftDetector()
    assert not detector.is_calibrated()
    assert detector.detect({"mae": 2.0}) == {}
    detector.set_baseline({"mae": 1.0, "zero": 0.0})
    assert detector.is_calibrated()
    assert detector.detect({"mae": 1.2, "zero": 5.0}) == {"mae": pytest.approx(0.2)}
    assert detector.should_retire({"mae": 0.2})


def _signal(direction: SignalDirection, strength: float = 0.8, confidence: float = 0.8) -> AlphaSignal:
    return AlphaSignal(
        strategy_id=StrategyId("core"),
        component_type=AlphaComponentType.ENTRY,
        direction=direction,
        strength=strength,
        confidence=confidence,
        instrument_id=InstrumentId("ETHUSDT"),
        venue_id=VenueId("BINANCE"),
        model_version=SchemaVersion("core-model"),
    )


def test_signal_fusion_legacy_boundary_and_v3_delegate_cover_all_outcomes() -> None:
    fuser = SignalFuser(min_agreement_ratio=0.75)
    assert fuser.detect_conflict([]) == (False, "")
    assert fuser.detect_conflict([_signal(SignalDirection.LONG)])[0] is False
    fused_empty = fuser.fuse([])
    assert fused_empty.direction is SignalDirection.NO_ACTION
    zero = fuser.fuse([_signal(SignalDirection.NO_ACTION)])
    assert zero.direction is SignalDirection.NO_ACTION
    low_agreement = fuser.fuse(
        [_signal(SignalDirection.LONG), _signal(SignalDirection.SHORT), _signal(SignalDirection.NO_ACTION)]
    )
    assert low_agreement.conflict_detected and low_agreement.direction is SignalDirection.NO_ACTION
    long_result = fuser.fuse([_signal(SignalDirection.LONG), _signal(SignalDirection.LONG)])
    short_result = fuser.fuse([_signal(SignalDirection.SHORT), _signal(SignalDirection.SHORT)])
    assert long_result.direction is SignalDirection.LONG
    assert short_result.direction is SignalDirection.SHORT
    assert fuser.fuse_forecasts([_forecast()]).forecast_hash


def test_market_state_policy_helpers_and_fail_closed_paths_cover_core_branches() -> None:
    with pytest.raises(ValueError, match="policy_version"):
        MarketStatePolicy(policy_version="")
    with pytest.raises(ValueError, match="horizons must"):
        MarketStatePolicy(horizons=(0, 1, 2), horizon_weights=(0.2, 0.3, 0.5))
    with pytest.raises(ValueError, match="align"):
        MarketStatePolicy(horizon_weights=(1.0,))
    with pytest.raises(ValueError, match="finite and non-negative"):
        MarketStatePolicy(horizon_weights=(float("nan"), 0.0, 1.0))
    with pytest.raises(ValueError, match="positive mass"):
        MarketStatePolicy(horizon_weights=(0.0, 0.0, 0.0))
    with pytest.raises(ValueError, match="finite and non-negative"):
        MarketStatePolicy(momentum_weight=-1.0)
    with pytest.raises(ValueError, match="scales"):
        MarketStatePolicy(momentum_scale=0.0)
    with pytest.raises(ValueError, match="thresholds"):
        MarketStatePolicy(trend_threshold=0.8, strong_trend_threshold=0.2)
    with pytest.raises(ValueError, match="volatility"):
        MarketStatePolicy(elevated_volatility=0.9, high_volatility=0.8)
    with pytest.raises(ValueError, match="annualization"):
        MarketStatePolicy(annualization_bars=float("inf"))

    assert market_state_module._finite(True) is None
    assert market_state_module._finite("bad") is None
    assert market_state_module._finite(1.0) == 1.0
    assert isinstance(market_state_module._canonical(object()), str)
    estimator = MarketStateEstimator()
    assert estimator._mapping_series({"x": []}, ("x",)) == ({}, True, True)
    assert estimator._mapping_series({"x": "bad"}, ("x",)) == ({}, True, True)
    assert estimator._mapping_series({"x": {"bad": 1.0}}, ("x",)) == ({}, True, True)
    assert estimator._mapping_series({"x": {1: float("nan")}}, ("x",)) == ({}, True, True)
    assert estimator._mapping_series({}, ("x",)) == ({}, False, False)
    assert estimator._prices({}) == ([], False, False)
    assert estimator._prices({"prices": "bad"}) == ([], True, True)
    assert estimator._prices({"prices": [100.0, 0.0]}) == ([], True, True)
    assert estimator._prices({"prices": [100.0]}) == ([100.0], True, True)
    assert estimator._prices({"prices": [100.0, 101.0]}) == ([100.0, 101.0], True, False)

    assert estimator._extract_returns({"trend_5_pct": "bad"})[1]
    direct_returns, direct_invalid = estimator._extract_returns({"trend_5_pct": 10.0})
    assert direct_returns == {5: 0.1} and not direct_invalid
    derived_returns, derived_invalid = estimator._extract_returns({"prices": [100.0 + index for index in range(60)]})
    assert derived_returns and not derived_invalid
    assert estimator._extract_returns({"prices": [100.0, 101.0]})[1]
    assert estimator._extract_returns({"prices": [100.0]})[1]
    assert estimator._extract_returns({})[1]
    assert estimator._extract_slopes({"trend_slope": "bad"}, {5: 0.1})[1]
    assert estimator._extract_slopes({"trend_slope": 0.01}, {5: 0.1}) == ({50: 0.01}, False)
    assert estimator._extract_slopes({}, {5: 0.1}) == ({5: 0.02}, False)
    assert estimator._extract_slopes({}, {})[1]
    assert estimator._extract_volatility({"realized_volatility": -1.0})[1]
    assert estimator._extract_volatility({"realized_vol_by_horizon": {1: 0.2}}) == (0.2, False)
    assert estimator._extract_volatility({"realized_vol_by_horizon": {1: float("nan")}})[1]
    assert estimator._extract_volatility({"prices": [100.0, 101.0]})[1]
    assert estimator._extract_volatility({"prices": [100.0, 101.0, 102.0, 103.0]})[0] is not None
    assert estimator._extract_volatility({})[1]
    assert estimator._optional_number({}, ("x",)) == (None, False)

    base = _market_features()
    assert estimator.estimate(
        VenueId("B"), InstrumentId("E"), base, timestamp=datetime.fromisoformat("2026-01-01")
    ).timestamp.tzinfo
    negative = dict(base)
    negative["benchmark_returns"] = {5: -0.03, 20: -0.1, 50: -0.25}
    negative["trend_slopes"] = {5: -0.003, 20: -0.01, 50: -0.025}
    assert (
        estimator.estimate(VenueId("B"), InstrumentId("E"), negative, timestamp=TIMESTAMP).direction.regime
        == "TRENDING_DOWN"
    )
    invalid_volatility = dict(base)
    invalid_volatility["realized_volatility"] = -1.0
    assert (
        estimator.estimate(VenueId("B"), InstrumentId("E"), invalid_volatility, timestamp=TIMESTAMP).quality.tier
        == "UNKNOWN"
    )
    volatile = dict(base)
    volatile["benchmark_returns"] = {5: 0.0, 20: 0.0, 50: 0.0}
    volatile["trend_slopes"] = {5: 0.0, 20: 0.0, 50: 0.0}
    volatile["realized_volatility"] = 0.9
    assert (
        estimator.estimate(VenueId("B"), InstrumentId("E"), volatile, timestamp=TIMESTAMP).direction.regime
        == "VOLATILE"
    )
    for automatic_volatility, expected_level in ((1.4, "CRISIS"), (0.4, "ELEVATED")):
        automatic = dict(base)
        automatic["realized_volatility"] = automatic_volatility
        assert (
            estimator.estimate(VenueId("B"), InstrumentId("E"), automatic, timestamp=TIMESTAMP).stress.level
            == expected_level
        )
    no_horizon = dict(base)
    no_horizon["benchmark_returns"] = {1: 0.1}
    no_horizon["trend_slopes"] = {1: 0.01}
    assert (
        estimator.estimate(VenueId("B"), InstrumentId("E"), no_horizon, timestamp=TIMESTAMP).quality.tier == "UNKNOWN"
    )

    for name, value in (
        ("breadth", 2.0),
        ("breakout_breadth", 2.0),
        ("cross_sectional_correlation", 2.0),
        ("liquidity_score", 2.0),
        ("dispersion", -1.0),
        ("volume_expansion", 0.0),
        ("data_quality", "INVALID"),
        ("data_gap_seconds", -1.0),
    ):
        invalid = dict(base)
        invalid[name] = value
        assert (
            estimator.estimate(VenueId("B"), InstrumentId("E"), invalid, timestamp=TIMESTAMP).quality.tier == "UNKNOWN"
        )
    unknown_stress = dict(base)
    unknown_stress["stress_level"] = "INVALID"
    assert (
        estimator.estimate(VenueId("B"), InstrumentId("E"), unknown_stress, timestamp=TIMESTAMP).quality.tier
        == "UNKNOWN"
    )
    for level, volatility in (("ELEVATED", 0.4), ("HIGH", 0.9), ("NORMAL", 0.2)):
        explicit = dict(base)
        explicit["stress_level"] = level
        explicit["realized_volatility"] = volatility
        assert estimator.estimate(VenueId("B"), InstrumentId("E"), explicit, timestamp=TIMESTAMP).stress.level == level
    degraded = dict(base)
    degraded["data_quality"] = "CONDITIONAL"
    assert estimator.estimate(VenueId("B"), InstrumentId("E"), degraded, timestamp=TIMESTAMP).quality.tier == "DEGRADED"
    explicit_unknown = dict(base)
    explicit_unknown["data_quality"] = "UNKNOWN"
    assert (
        estimator.estimate(VenueId("B"), InstrumentId("E"), explicit_unknown, timestamp=TIMESTAMP).quality.tier
        == "UNKNOWN"
    )
    missing_optional = {
        "benchmark_returns": {5: 0.01, 20: 0.02, 50: 0.03},
        "realized_volatility": 0.2,
        "data_quality": "PASS",
    }
    assert (
        estimator.estimate(VenueId("B"), InstrumentId("E"), missing_optional, timestamp=TIMESTAMP).quality.tier
        == "DEGRADED"
    )

    rollback_estimator = MarketStateEstimator()
    assert rollback_estimator.rollback_rate() == 0.0
    rollback_estimator.record_rollback()
    invalid_state = rollback_estimator.estimate(VenueId("B"), InstrumentId("E"), {}, timestamp=TIMESTAMP)
    assert invalid_state.rollback_rate_pct == 100.0
    custom = replace(
        invalid_state,
        stress=StressState("UNKNOWN", 0.0),
        quality=QualityState("GOOD"),
    )
    assert not custom.is_tradable()


def test_active_optimizer_and_constraints_cover_validation_and_projection_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="finite"):
        signed_exposure_math({"ETHUSDT": float("nan")})
    for kwargs in (
        {"policy_version": ""},
        {"lambda_risk": -1.0},
        {"max_symbol_weight": 0.0},
        {"numerical_epsilon": 0.0},
    ):
        with pytest.raises(ValueError):
            ActivePortfolioPolicy(**kwargs)

    result = ActiveOptimizationResult({}, {}, 0.0, 0.0, 0.0, None, "t", "o")
    assert not result.is_tradable
    assert ActiveOptimizationResult({"ETHUSDT": 0.1}, {}, 0.1, 0.1, 0.1, 1.0, "t", "o").is_tradable

    optimizer = ActivePortfolioOptimizer()
    base_input = _optimizer_input()
    accepted = optimizer.optimize(base_input)
    assert accepted.weights and accepted.is_tradable
    rejection_inputs = (
        {"ETHUSDT": object()},
        {"ETHUSDT": replace(_ensemble(), expected_return_after_cost=None)},
    )
    for forecasts in rejection_inputs:
        rejected = optimizer.optimize(replace(base_input, forecasts=forecasts))
        assert rejected.weights == {}
    for field, value in (
        ("min_notional", {"ETHUSDT": "bad"}),
        ("step_sizes", {"ETHUSDT": 0.0}),
        ("tick_sizes", {"ETHUSDT": float("nan")}),
        ("fee_costs", {"ETHUSDT": -1.0}),
        ("asset_betas", {}),
        ("liquidity_limits", {"ETHUSDT": 0.0}),
        ("capacity_limits", {"ETHUSDT": 0.0}),
        ("funding_costs", {}),
    ):
        rejected = optimizer.optimize(replace(base_input, **{field: value}))
        assert rejected.weights == {}
    zero_edge = optimizer.optimize(replace(base_input, forecasts={"ETHUSDT": _ensemble(0.0, after_cost=0.0)}))
    assert "NO_VERIFIABLE_EDGE" in zero_edge.rejected.values()
    fallback_vol = optimizer.optimize(
        replace(base_input, forecasts={"ETHUSDT": replace(_ensemble(), expected_volatility=None)})
    )
    assert fallback_vol.output_hash
    narrow_target = _exposure_target(target_gross=0.01, gross_max=0.01, target_net=0.0, net_min=-0.01, net_max=0.01)
    violations = optimizer.optimize(
        replace(base_input, exposure_target=narrow_target, current_weights={"ETHUSDT": 1.0})
    )
    assert violations.output_hash
    violating_target = _exposure_target(target_beta=0.0, beta_min=-0.1, beta_max=0.1, net_min=-0.5, net_max=0.5)
    with monkeypatch.context() as scoped:
        scoped.setattr(optimizer, "_project", lambda weights, inputs: {"ETHUSDT": 2.0})
        explicit_violations = optimizer.optimize(replace(base_input, exposure_target=violating_target))
    assert {
        "GROSS_TARGET_OUT_OF_BOUNDS",
        "NET_TARGET_OUT_OF_BOUNDS",
        "BETA_TARGET_OUT_OF_BOUNDS",
        "TURNOVER_LIMIT",
    }.issubset(set(explicit_violations.constraint_violations))
    assert optimizer._required_after_cost(_ensemble()) == pytest.approx(0.0165)
    with pytest.raises(ValueError, match="after-cost"):
        optimizer._required_after_cost(_ensemble(after_cost=None))

    class FlappingEnsemble(EnsembleForecast):
        __slots__ = ("_reads",)

        def __init__(self, base: EnsembleForecast) -> None:
            object.__setattr__(self, "_reads", -1)
            values = {field.name: getattr(base, field.name) for field in fields(EnsembleForecast)}
            super().__init__(**values)
            object.__setattr__(self, "_reads", 0)

        def __getattribute__(self, name: str) -> object:
            if name == "expected_return_after_cost":
                reads = object.__getattribute__(self, "_reads")
                object.__setattr__(self, "_reads", reads + 1)
                return 0.01965 if reads <= 1 else None
            return super().__getattribute__(name)

    with pytest.raises(ValueError, match="unexpectedly"):
        optimizer.optimize(replace(base_input, forecasts={"ETHUSDT": FlappingEnsemble(_ensemble())}))
    projected = optimizer._project({"ETHUSDT": 2.0}, replace(base_input, exposure_target=narrow_target))
    assert projected
    assert optimizer._project({}, base_input) == {}
    assert optimizer_module._finite("bad") is None
    no_capital_target = PortfolioTarget(
        strategy_id=StrategyId("a"),
        instrument_id=InstrumentId("ETHUSDT"),
        venue_id=VenueId("BINANCE"),
        target_quantity=Quantity(amount="1"),
        target_notional=MonetaryValue(amount="100"),
        capital_budget=MonetaryValue(amount="0"),
    )

    legacy = PortfolioOptimizerImpl(max_total_leverage=1.0)
    assert legacy.allocate_capital([], MonetaryValue(amount="100")) == {}
    allocation = legacy.allocate_capital([StrategyId("a"), StrategyId("b")], MonetaryValue(amount="100"))
    assert float(allocation[StrategyId("a")].amount) == pytest.approx(50.0)
    target = PortfolioTarget(
        strategy_id=StrategyId("a"),
        instrument_id=InstrumentId("ETHUSDT"),
        venue_id=VenueId("BINANCE"),
        target_quantity=Quantity(amount="2"),
        target_notional=MonetaryValue(amount="200"),
        capital_budget=MonetaryValue(amount="100"),
    )
    assert len(legacy._enforce_leverage_constraint([replace(target, target_notional=MonetaryValue(amount="50"))])) == 1
    assert legacy._enforce_leverage_constraint([no_capital_target]) == [no_capital_target]
    assert float(legacy._enforce_leverage_constraint([target])[0].target_notional.amount) < 200.0
    assert (
        legacy.allocate_capital([StrategyId("a")], MonetaryValue(amount="100"), weights={StrategyId("a"): 0.25})[
            StrategyId("a")
        ].amount
        == "25.0"
    )
    assert legacy.resolve_conflicts([target])[1] == 0
    shared_long = replace(target, ownership=PositionOwnership.SHARED, takeover_strategy=StrategyId("b"))
    transferred = legacy.exit_protection(StrategyId("a"), [shared_long, target])
    assert any(item.strategy_id == StrategyId("b") for item in transferred)
    assert legacy.exit_protection(StrategyId("a"), [replace(target, ownership=PositionOwnership.SHARED)]) == []
    assert legacy.exit_protection(StrategyId("missing"), [target]) == [target]

    constraints = ConstraintOptimizer(max_gross_leverage=0.01, max_net_leverage=0.1, max_per_symbol_pct=10.0)
    assert constraints._covariance_discount("ETHUSDT", set()) == 1.0
    constraints.covariance_matrix = {"ETHUSDT": {"BTCUSDT": 1.0}}
    assert constraints._covariance_discount("ETHUSDT", {"BTCUSDT"}) == pytest.approx(0.5)
    signals = {
        "ETHUSDT": {"direction": "LONG", "strength": 1.0, "price": 100.0},
        "BTCUSDT": {"direction": "SHORT", "strength": 1.0, "price": 100.0},
        "FLAT": {"direction": "NO_ACTION", "strength": 1.0, "price": 100.0},
        "BAD_PRICE": {"direction": "LONG", "strength": 1.0, "price": 0.0},
    }
    result = constraints.optimize(
        signals,
        account_equity=1000.0,
        min_notional={"ETHUSDT": 1.0, "BTCUSDT": 1.0},
        step_sizes={"ETHUSDT": 0.1, "BTCUSDT": 0.1},
    )
    assert result.rejected["BAD_PRICE"]
    assert result.constraint_violations
    assert result.is_tradable is False
    concentrated = ConstraintOptimizer(max_per_symbol_pct=0.5).optimize(
        {"ETHUSDT": {"direction": "LONG", "strength": 1.0, "price": 100.0}},
        account_equity=1000.0,
        min_notional={"ETHUSDT": 1.0},
        step_sizes={"ETHUSDT": 0.1},
    )
    assert concentrated.targets
    assert (
        ConstraintOptimizer()
        .optimize(
            {"ETHUSDT": {"direction": "LONG", "strength": 1.0, "price": 100.0}},
            account_equity=1000.0,
            min_notional={},
            step_sizes={},
        )
        .rejected["ETHUSDT"]
    )
    assert (
        ConstraintOptimizer()
        .optimize(
            {"ETHUSDT": {"direction": "LONG", "strength": 1.0, "price": 100.0}},
            account_equity=1000.0,
            min_notional={"ETHUSDT": 1.0},
            step_sizes={},
        )
        .rejected["ETHUSDT"]
    )
    assert (
        ConstraintOptimizer()
        .optimize(
            {"ETHUSDT": {"direction": "LONG", "strength": 0.001, "price": 100.0}},
            account_equity=1000.0,
            min_notional={"ETHUSDT": 100.0},
            step_sizes={"ETHUSDT": 0.1},
        )
        .rejected["ETHUSDT"]
    )
    assert (
        ConstraintOptimizer()
        .optimize(
            {"ETHUSDT": {"direction": "LONG", "strength": 0.001, "price": 100.0}},
            account_equity=1000.0,
            min_notional={"ETHUSDT": 0.001},
            step_sizes={"ETHUSDT": 1.0},
        )
        .rejected["ETHUSDT"]
    )
    assert (
        ConstraintOptimizer()
        .optimize(
            {"ETHUSDT": {"direction": "OTHER", "strength": 1.0, "price": 100.0}},
            account_equity=1000.0,
            min_notional={"ETHUSDT": 1.0},
            step_sizes={"ETHUSDT": 0.1},
        )
        .targets
    )
    assert (
        ConstraintOptimizer(max_net_leverage=0.001)
        .optimize(
            {"ETHUSDT": {"direction": "LONG", "strength": 1.0, "price": 100.0}},
            account_equity=1000.0,
            min_notional={"ETHUSDT": 1.0},
            step_sizes={"ETHUSDT": 0.1},
        )
        .constraint_violations
    )
    v3_result = constraints.optimize(
        {"ETHUSDT": {"target_weight": 0.2}},
        account_equity=1000.0,
        min_notional={"ETHUSDT": 1.0},
        step_sizes={"ETHUSDT": 0.1},
        mode="V3",
    )
    assert v3_result.targets
    assert constraints.optimize({}, account_equity=0.0).constraint_violations
    assert constraints.optimize_targets(
        {"ETHUSDT": float("nan")}, account_equity=1000.0, min_notional={}, step_sizes={}
    ).rejected
    assert constraints.optimize_targets({"ETHUSDT": 0.2}, account_equity=0.0, min_notional={}, step_sizes={}).rejected
    assert constraints.optimize_targets(
        {"ETHUSDT": 0.2}, account_equity=1000.0, min_notional={}, step_sizes={}
    ).rejected
    assert constraints.optimize_targets(
        {"ETHUSDT": 0.2}, account_equity=1000.0, min_notional={"ETHUSDT": 1.0}, step_sizes={"ETHUSDT": 0.0}
    ).rejected
    assert constraints.optimize_targets(
        {"ETHUSDT": 0.0001}, account_equity=1000.0, min_notional={"ETHUSDT": 1.0}, step_sizes={"ETHUSDT": 0.1}
    ).rejected
    with pytest.raises(ValueError, match="policy version"):
        replace(_optimizer_input(), policy_version="")
    with pytest.raises(ValueError, match="account_equity"):
        replace(_optimizer_input(), account_equity=0.0)


def test_mean_reversion_engine_covers_bands_direction_and_momentum_outcomes() -> None:
    engine = MeanReversionEngine(z_threshold=1.0)
    stable_prices = [100.0] * 20
    assert engine.compute_z_score(100.0, stable_prices) == 0.0
    assert engine.estimate_half_life(stable_prices[:19]) == 0.0

    varied_prices = [100.0 + (index % 4) * 0.1 for index in range(60)]
    long_result = engine.evaluate(98.0, varied_prices, 0.20, 1.0, "RANGING")
    assert long_result.signal_direction == "LONG"
    assert long_result.strength > 0.0
    assert long_result.confidence > 0.5

    decreasing = [100.0 * (0.99**index) for index in range(8)]
    momentum = MultiPeriodMomentum([2, 3, 4])
    assert momentum.evaluate(decreasing, 0.02).trend_direction == "DOWN"


def test_mean_reversion_adapter_covers_unknown_input_and_metadata_branches() -> None:
    adapter = MeanReversionAlpha()
    assert adapter.validate()
    adapter.alpha_id = ""
    assert not adapter.validate()
    adapter = MeanReversionAlpha()

    assert adapter._timestamp({}, {}).tzinfo is not None
    assert adapter._timestamp({"timestamp": datetime.fromisoformat("2026-08-21T12:00:00")}, {}).tzinfo is not None

    assert adapter._costs({"expected_fee_bps": "bad"}, {}) == (None, None, None, "")
    assert adapter._costs(
        {
            "expected_fee_bps": 2.0,
            "expected_slippage_bps": 1.0,
            "expected_funding_bps": 0.5,
            "source_hash": "cost-core",
        },
        {},
    ) == (2.0, 1.0, 0.5, "cost-core")

    invalid_prices = adapter.generate_forecast(
        {
            "features": {
                "prices": [100.0, "bad"],
                "close": 100.0,
                "realized_volatility": 0.2,
                "calibrated_expected_return": "bad",
                "feature_hash": "feature-invalid",
            }
        }
    )
    assert invalid_prices.expected_return is None
    assert invalid_prices.feature_hash == "feature-invalid"

    nonpositive_prices = adapter.generate_forecast(
        {
            "features": {
                "prices": [100.0, 0.0],
                "close": 100.0,
                "realized_volatility": 0.2,
                "calibrated_expected_return": float("nan"),
            }
        }
    )
    assert nonpositive_prices.expected_return is None
    assert nonpositive_prices.feature_hash

    invalid_features = adapter.generate_forecast({"features": "invalid"})
    assert invalid_features.expected_volatility is None
    non_sequence_prices = adapter.generate_forecast(
        {"features": {"prices": "invalid", "close": 100.0, "realized_volatility": 0.2}}
    )
    assert non_sequence_prices.expected_volatility == pytest.approx(0.2)
    invalid_numbers = adapter.generate_forecast({"features": {"close": "bad", "realized_volatility": "bad"}})
    assert invalid_numbers.expected_volatility is None

    string_timestamp = adapter.generate_forecast(
        {
            "timestamp": "2026-08-21T12:00:00+00:00",
            "features": {"prices": [100.0], "close": 100.0, "realized_volatility": 0.2},
        }
    )
    assert string_timestamp.timestamp.year == 2026
    assert adapter.generate({"features": {"prices": [], "close": 100.0, "realized_volatility": 0.2}}).alpha_id


def _optimizer_input() -> PortfolioOptimizationInput:
    return PortfolioOptimizationInput(
        forecasts={"ETHUSDT": _ensemble()},
        current_weights={"ETHUSDT": 0.0},
        covariance={"ETHUSDT": {"ETHUSDT": 0.04}},
        asset_betas={"ETHUSDT": 0.2},
        exposure_target=_exposure_target(),
        account_equity=100000.0,
        fee_costs={"ETHUSDT": 2.0},
        slippage_costs={"ETHUSDT": 1.0},
        funding_costs={"ETHUSDT": 0.5},
        liquidity_limits={"ETHUSDT": 0.8},
        capacity_limits={"ETHUSDT": 0.8},
        min_notional={"ETHUSDT": 10.0},
        step_sizes={"ETHUSDT": 0.001},
        tick_sizes={"ETHUSDT": 0.01},
        policy_version="core-portfolio-policy",
    )


def _entry_proposal(*, side: OrderSide | None = OrderSide.BUY, strength: float = 0.5) -> EntryProposal:
    return EntryProposal(
        strategy_id=StrategyId("core"),
        instrument_id=InstrumentId("ETHUSDT"),
        venue_id=VenueId("BINANCE"),
        side=side,
        strength=strength,
        confidence=0.8,
    )


@pytest.mark.asyncio
async def test_typed_graph_core_nodes_and_failure_policies_cover_all_paths() -> None:
    class BareNode(TypedGraphNode):
        async def execute(self, inputs: dict[str, TypedNodeOutput], context: dict) -> TypedNodeOutput:
            return await super().execute(inputs, context)

    bare = BareNode("bare", NodeType.FACTOR)
    assert bare.input_dependencies == []
    assert bare.validate()
    with pytest.raises(NotImplementedError):
        await bare.execute({}, {})
    with pytest.raises(NotImplementedError):
        await TypedGraphNode.execute(bare, {}, {})

    feature = FeatureNode("feature", ["close"])
    missing = await feature.execute({}, {"features": None})
    assert missing.dq_tier is DataQualityTier.BLOCK
    assert missing.metadata["missing_features"] == ["close"]
    boolean = await feature.execute({}, {"features": {"close": True}})
    text_value = await feature.execute({}, {"features": {"close": "bad"}})
    assert boolean.metadata["invalid_features"] == ["close"]
    assert text_value.metadata["invalid_features"] == ["close"]
    passed = await feature.execute({}, {"features": {"close": 100.0}})
    assert passed.dq_tier is DataQualityTier.PASS and passed.data == {"close": 100.0}

    async def broken_entry(_context: dict) -> EntryProposal:
        raise RuntimeError("entry failure")

    for policy, expected_hash in (
        (NodeFailurePolicy.FAIL_CLOSED, "error"),
        (NodeFailurePolicy.SKIP, "skipped"),
        (NodeFailurePolicy.DEGRADE, "degraded"),
    ):
        node = EntryNode(f"entry-{expected_hash}", broken_entry)
        node.failure_policy = policy
        output = await node.execute({}, {})
        assert output.output_hash == expected_hash
    invalid_entry_policy = EntryNode("entry-invalid-policy", broken_entry)
    invalid_entry_policy.failure_policy = "INVALID"  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="entry failure"):
        await invalid_entry_policy.execute({}, {})

    async def entry_fn(_context: dict) -> EntryProposal:
        return _entry_proposal()

    entry_output = await EntryNode("entry-ok", entry_fn, factor_version="f", model_version="m").execute({}, {})
    assert entry_output.dq_tier is DataQualityTier.PASS

    async def async_forecast(_context: dict) -> AlphaForecast:
        return _forecast("forecast-node")

    forecast_output = await ForecastNode("forecast", async_forecast).execute({}, {})
    assert forecast_output.dq_tier is DataQualityTier.PASS
    assert (
        await ForecastNode("forecast-bad", lambda _context: object()).execute({}, {})
    ).dq_tier is DataQualityTier.BLOCK

    async def raising_forecast(_context: dict) -> AlphaForecast:
        raise RuntimeError("forecast failure")

    assert (await ForecastNode("forecast-error", raising_forecast).execute({}, {})).output_hash == "forecast_error"

    async def filter_fn(_context: dict, _entry: EntryProposal | None) -> FilterResult:
        return FilterResult(FilterDecision.ACCEPT, component_id="filter")

    filter_node = FilterNode("filter", filter_fn)
    assert (await filter_node.execute({}, {})).data.decision is FilterDecision.ACCEPT
    non_entry_output = TypedNodeOutput("other", NodeType.FEATURE, "", {}, DataQualityTier.PASS)
    assert (await filter_node.execute({"other": non_entry_output}, {})).data.decision is FilterDecision.ACCEPT
    filter_with_entry = await filter_node.execute(
        {"entry": entry_output},
        {},
    )
    assert filter_with_entry.data.decision is FilterDecision.ACCEPT

    async def broken_filter(_context: dict, _entry: EntryProposal | None) -> FilterResult:
        raise RuntimeError("filter failure")

    for policy, expected_hash in (
        (NodeFailurePolicy.FAIL_CLOSED, "error_veto"),
        (NodeFailurePolicy.SKIP, "skipped"),
        (NodeFailurePolicy.DEGRADE, "degraded"),
    ):
        node = FilterNode(f"filter-{expected_hash}", broken_filter)
        node.failure_policy = policy
        output = await node.execute({}, {})
        assert output.output_hash == expected_hash
    invalid_filter_policy = FilterNode("filter-invalid-policy", broken_filter)
    invalid_filter_policy.failure_policy = "INVALID"  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="filter failure"):
        await invalid_filter_policy.execute({}, {})

    async def valid_exit(_context: dict) -> EntryProposal:
        return _entry_proposal(side=None)

    async def violating_exit_fn(_context: dict) -> EntryProposal:
        return _entry_proposal()

    assert (await ExitNode("exit-none").execute({}, {})).output_hash == "no_exit"
    assert (
        await ExitNode("exit-error", lambda _context: (_ for _ in ()).throw(RuntimeError("exit"))).execute({}, {})
    ).dq_tier is DataQualityTier.BLOCK
    violating_exit = await ExitNode("exit-violation", violating_exit_fn).execute({}, {})
    assert violating_exit.output_hash == "exit_violation"
    safe_exit = await ExitNode("exit-safe", valid_exit).execute({}, {})
    assert safe_exit.dq_tier is DataQualityTier.PASS and safe_exit.data is not None

    fusion = FusionNode("fusion")
    forecast_input = TypedNodeOutput("forecast", NodeType.ENTRY, "", _forecast("fusion-a"), DataQualityTier.PASS)
    blocked_input = replace(forecast_input, dq_tier=DataQualityTier.BLOCK)
    blocked_output = await fusion.execute({"forecast": blocked_input}, {})
    assert blocked_output.output_hash == "forecast_blocked"
    duplicate = await fusion.execute(
        {
            "a": forecast_input,
            "b": replace(forecast_input, node_id="forecast-b", data=_forecast("fusion-a")),
        },
        {},
    )
    assert duplicate.output_hash == "forecast_fusion_error"
    no_entry = await fusion.execute({}, {})
    assert no_entry.output_hash == "no_entry"
    no_side = TypedNodeOutput("no-side", NodeType.ENTRY, "", _entry_proposal(side=None), DataQualityTier.PASS)
    directional = TypedNodeOutput("directional", NodeType.ENTRY, "", _entry_proposal(), DataQualityTier.PASS)
    proposal_output = await fusion.execute({"no": no_side, "yes": directional}, {})
    assert isinstance(proposal_output.data, StrategyProposal)
    two_directional = await fusion.execute({"first": directional, "second": directional}, {})
    assert isinstance(two_directional.data, StrategyProposal)
    mixed_inputs = await fusion.execute(
        {"entry": directional, "other": non_entry_output},
        {},
    )
    assert isinstance(mixed_inputs.data, StrategyProposal)
    veto_output = await fusion.execute(
        {
            "entry": directional,
            "filter": TypedNodeOutput(
                "filter", NodeType.FILTER, "", FilterResult(FilterDecision.VETO), DataQualityTier.PASS
            ),
        },
        {},
    )
    assert veto_output.output_hash == "vetoed"
    low_strength = await fusion.execute(
        {"entry": TypedNodeOutput("entry", NodeType.ENTRY, "", _entry_proposal(strength=0.001), DataQualityTier.PASS)},
        {},
    )
    assert isinstance(low_strength.data, StrategyProposal) and low_strength.data.side is None


@pytest.mark.asyncio
async def test_typed_graph_execution_short_circuit_and_graph_validation_cover_remaining_paths() -> None:
    async def entry_fn(_context: dict) -> EntryProposal:
        return _entry_proposal()

    async def veto_fn(_context: dict, _entry: EntryProposal | None) -> FilterResult:
        return FilterResult(FilterDecision.VETO, component_id="mandatory")

    async def safe_exit(_context: dict) -> EntryProposal:
        return _entry_proposal(side=None)

    graph = TypedAlphaGraph(StrategyId("core"))
    graph.add_node(EntryNode("entry", entry_fn))
    graph.add_node(FilterNode("mandatory", veto_fn, is_mandatory=True))
    graph.add_node(ExitNode("exit", safe_exit))
    graph.add_node(FusionNode("fusion"))
    graph.connect("entry", "mandatory")
    graph.connect("mandatory", "exit")
    graph.connect("entry", "fusion")
    graph.connect("mandatory", "fusion")
    detailed = await graph._execute_detailed({})
    assert detailed["proposal"] is None
    assert "exit" in detailed["component_outputs"]

    veto_without_exit_data = TypedAlphaGraph(StrategyId("core"))
    veto_without_exit_data.add_node(EntryNode("entry", entry_fn))
    veto_without_exit_data.add_node(FilterNode("mandatory", veto_fn, is_mandatory=True))
    veto_without_exit_data.add_node(ExitNode("exit"))
    veto_without_exit_data.add_node(FusionNode("fusion"))
    veto_without_exit_data.connect("entry", "mandatory")
    veto_without_exit_data.connect("mandatory", "exit")
    veto_without_exit_data.connect("entry", "fusion")
    veto_without_exit_data.connect("mandatory", "fusion")
    empty_exit_detail = await veto_without_exit_data._execute_detailed({})
    assert empty_exit_detail["proposal"] is None

    async def accept_fn(_context: dict, _entry: EntryProposal | None) -> FilterResult:
        return FilterResult(FilterDecision.ACCEPT, component_id="accept")

    continuing = TypedAlphaGraph(StrategyId("core"))
    continuing.add_node(EntryNode("entry", entry_fn))
    continuing.add_node(FilterNode("mandatory", accept_fn, is_mandatory=True))
    continuing.add_node(FusionNode("fusion"))
    continuing.connect("entry", "mandatory")
    continuing.connect("entry", "fusion")
    continuing.connect("mandatory", "fusion")
    assert await continuing.execute({}) is not None

    empty_exit = TypedAlphaGraph(StrategyId("core"))
    empty_exit.add_node(ExitNode("exit"))
    empty_exit_detail = await empty_exit._execute_detailed({})
    assert empty_exit_detail["proposal"] is None

    duplicate = TypedAlphaGraph(StrategyId("core"))
    duplicate.add_node(FusionNode("node"))
    with pytest.raises(ValueError, match="already exists"):
        duplicate.add_node(FusionNode("node"))
    with pytest.raises(ValueError, match="Source"):
        duplicate.connect("missing", "node")
    with pytest.raises(ValueError, match="Target"):
        duplicate.connect("node", "missing")
    cycle = TypedAlphaGraph(StrategyId("core"))
    cycle.add_node(FusionNode("a"))
    cycle.add_node(FusionNode("b"))
    cycle.connect("a", "b")
    cycle.connect("b", "a")
    with pytest.raises(ValueError, match="unresolved"):
        cycle.topological_order()
